"""上游调用：候选链 + 重试 + 熔断 + §7.5 首包锁定 + 流式管道（spec §7.2-8/9、§7.4-§7.6）。

A1（audit §3）：自 sr/server.py 拆出（原「3) 上游调用」+「4) 流式管道」两段）。
A3（audit §3）：:data:`ADAPTERS` 协议注册表亦落在此（server 与 upstream 共用同一
对象）；错误信封/探测序列的注册化见 ``sr/adapters`` 各适配器的 ``error_envelope``
与 :func:`sr.adapters.cross.from_upstream`。

依赖方向：本模块 → sr.adapters（openai/anthropic/UpstreamError）、sr.engine
（RouteDecision）、sr.metrics（Metrics）、sr.models（熔断/后端解析）。不 import
sr.server。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from sr.adapters import UpstreamError
from sr.adapters import anthropic as anth
from sr.adapters import openai as oai
from sr.adapters.anthropic import StreamState
from sr.adapters.registry import ADAPTERS
from sr.engine import RouteDecision
from sr.metrics import Metrics
from sr.models import (is_circuit_open, record_failure, record_success,
                       resolve_backend)

# 协议注册表在 sr/adapters/registry.py（A3：server/upstream/cross 共用同一对象）；
# 此处仅作模块内短名使用。
__all__ = ["ADAPTERS", "PING_INTERVAL_S", "_chain", "_outbound_payload",
           "_outbound_payload_or_none", "_call_nonstream", "_call_stream",
           "_relay_stream"]

# 客户端 anthropic 的 ping 心跳间隔（§7.4「15s ping」逐字）
PING_INTERVAL_S = 15.0

# 内部异常只进日志，不回吐客户端（M-c）
logger = logging.getLogger("sr.upstream")


def _chain(decision: RouteDecision) -> list[str]:
    """候选链：主模型 → decision.fallbacks 依序，去重去空（裁定 5）。"""
    out: list[str] = []
    for m in [decision.model, *decision.fallbacks]:
        if m and m not in out:
            out.append(m)
    return out


def _outbound_payload(ir, backend, think: str | None) -> dict:
    """§7.2-9 出站体：按上游协议序列化 + 出站名 + think 映射。

    上游 openai 且流式一律补 ``stream_options.include_usage``（裁定 7：Task 7
    修复的契约——终止段以 usage 包为终点）。
    """
    payload = ADAPTERS[backend.proto].to_upstream(ir, backend.upstream_name, think)
    if ir.stream and backend.proto == "openai":
        so = dict(payload.get("stream_options") or {})
        so["include_usage"] = True
        payload["stream_options"] = so
    return payload


def _outbound_payload_or_none(ir, cand, decision: RouteDecision, conn):
    """候选 → (backend, payload)；配置面不可用时回 (None, UpstreamError)。

    两处失败同族（都是「这个候选没法出站」，换下一候选）：
    - ``resolve_backend`` 抛异常（KeyError / 环境变量缺失）：配置面问题；
    - ``_outbound_payload`` 抛异常——M3/M4（audit Major）：值域外/非字符串 think
      让 to_upstream 抛 ValueError/TypeError，坏 action 形状抛 KeyError。旧实现
      只捕 UpstreamError/httpx.HTTPError → 逃逸成兜底 500，**每条**请求持久 500。

    两种都按 §7 错误信封收口为 502 upstream_error（config_error 在 _handle 里
    归一到 api_error），文案通用、不回吐 Python 异常原文（M-c）；原始异常只进
    服务端日志。
    """
    try:
        backend = resolve_backend(conn, cand)
    except Exception as exc:              # KeyError / env 缺失：配置面问题
        logger.warning("resolve_backend failed for %s: %r", cand, exc)
        return None, UpstreamError(502, "upstream resolve failed", "config_error")
    try:
        return backend, _outbound_payload(ir, backend, decision.think)
    except Exception as exc:              # M3/M4：出站体构造失败（值域外 think 等）
        logger.warning("outbound payload build failed for %s: %r", cand, exc)
        return None, UpstreamError(502, "failed to build upstream request body",
                                   "config_error")


async def _call_nonstream(conn, ir, decision: RouteDecision, settings: dict,
                          metrics: Metrics | None = None):
    """非流式上游调用。返回 (backend, model_id, resp_json, upstream_ms)；
    全链失败抛最后一个 UpstreamError。每失败记 record_failure，成功记
    record_success（裁定 5）；is_circuit_open 的候选直接跳过。
    metrics 可选：每次尝试失败记 sr_retry_total{model,status}；失败后刚好
    越过熔断阈值（本候选此后会被跳过，不会再进来）记 sr_circuit_open_total。

    候选级配置失败（解析后端 / 构造出站体，含 M3/M4 的坏 action 形状）经
    :func:`_outbound_payload_or_none` 统一换下一候选，不裸 500。
    """
    max_retries = int(settings.get("max_retries") or 0)
    last: UpstreamError | None = None
    for cand in _chain(decision):
        if is_circuit_open(cand, conn):
            continue
        backend, payload = _outbound_payload_or_none(ir, cand, decision, conn)
        if backend is None:
            last = payload
            continue
        for _ in range(max_retries + 1):  # 每候选最多重试 max_retries 次（裁定 5）
            t0 = time.monotonic()
            client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
            try:
                resp = await client.post(backend.url, json=payload,
                                         headers=backend.headers)
                ms = (time.monotonic() - t0) * 1000
                if resp.status_code != 200:
                    raise ADAPTERS[backend.proto].parse_error(resp.status_code,
                                                              resp.content)
                try:
                    data = resp.json()
                except ValueError:
                    raise UpstreamError(502, "upstream returned invalid JSON",
                                        "api_error")
                record_success(conn, cand)
                return backend, cand, data, ms
            except UpstreamError as exc:
                record_failure(conn, cand)
                last = exc
                if metrics is not None:
                    metrics.inc("sr_retry_total",
                                {"model": cand, "status": str(exc.status or 0)},
                                conn=conn)
                    if is_circuit_open(cand, conn):
                        metrics.inc("sr_circuit_open_total", {"model": cand}, conn=conn)
            except httpx.HTTPError as exc:
                record_failure(conn, cand)
                last = UpstreamError(502, f"upstream unreachable: {exc}", "api_error")
                if metrics is not None:
                    metrics.inc("sr_retry_total", {"model": cand, "status": "0"}, conn=conn)
                    if is_circuit_open(cand, conn):
                        metrics.inc("sr_circuit_open_total", {"model": cand}, conn=conn)
            finally:
                await client.aclose()
    raise last or UpstreamError(502, "no upstream candidate available", "api_error")


async def _call_stream(conn, ir, decision: RouteDecision, settings: dict,
                       metrics: Metrics | None = None):
    """流式上游调用：首包（上游首字节）未发出前允许重试与回退（§7.5）。

    返回 (backend, model_id, resp, client, aiter, first_frame, upstream_ms)——aiter
    是已启动的上游字节迭代器（httpx 响应流只许消费一次，必须把同一迭代器交给
    _relay_stream 续传，不能重开 aiter_bytes），first_frame 是已收到的首个字节块。
    锁定即 record_success；首包前的每一失败记 record_failure（裁定 5）+ 可选
    metrics 计 sr_retry_total / sr_circuit_open_total（口径同 _call_nonstream）。"""
    max_retries = int(settings.get("max_retries") or 0)
    last: UpstreamError | None = None

    def _fail(cand: str, status) -> None:
        if metrics is not None:
            metrics.inc("sr_retry_total", {"model": cand, "status": str(status)}, conn=conn)
            if is_circuit_open(cand, conn):
                metrics.inc("sr_circuit_open_total", {"model": cand}, conn=conn)

    for cand in _chain(decision):
        if is_circuit_open(cand, conn):
            continue
        backend, payload = _outbound_payload_or_none(ir, cand, decision, conn)
        if backend is None:                 # M3/M4 同 _call_nonstream：换下一候选
            last = payload
            continue
        for _ in range(max_retries + 1):
            t0 = time.monotonic()
            client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))
            try:
                req = client.build_request("POST", backend.url, json=payload,
                                           headers=backend.headers)
                resp = await client.send(req, stream=True)
            except httpx.HTTPError as exc:
                record_failure(conn, cand)
                last = UpstreamError(502, f"upstream unreachable: {exc}", "api_error")
                _fail(cand, 0)
                await client.aclose()
                continue
            if resp.status_code != 200:            # 首包未发：仍可重试 / 回退
                raw = await resp.aread()
                await resp.aclose()
                await client.aclose()
                record_failure(conn, cand)
                last = ADAPTERS[backend.proto].parse_error(resp.status_code, raw)
                _fail(cand, last.status)
                continue
            try:
                aiter = resp.aiter_bytes()         # 流只消费一次：迭代器从此移交
                first = await aiter.__anext__()    # 等上游首字节
            except (StopAsyncIteration, httpx.HTTPError) as exc:
                record_failure(conn, cand)
                last = UpstreamError(502, f"empty/broken upstream stream: {exc!r}",
                                     "api_error")
                _fail(cand, 0)
                await resp.aclose()
                await client.aclose()
                continue
            # ── 锁定：首包已到手，此后不重试不换模型不回退（§7.5 红线）──
            record_success(conn, cand)
            return (backend, cand, resp, client, aiter, first,
                    (time.monotonic() - t0) * 1000)
    raise last or UpstreamError(502, "no upstream candidate available", "api_error")


async def _relay_stream(client_proto: str, up_proto: str, model_asked: str | None,
                        resp: httpx.Response, client: httpx.AsyncClient,
                        aiter, first_frame: bytes, usage_sink: dict | None = None):
    """续传上游流并按客户端协议重发（裁定 8）。

    ``aiter`` 是 _call_stream 移交的已启动字节迭代器（httpx 流只许消费一次）。

    - 帧解析：按 SSE 空行切帧、收集 data: 行（openai 无 event 行；anthropic 的
      event 行仅凭 data.type 即可分派，忽略不拆）。
    - openai 客户端：逐 IRChunk 经 ir_to_chunk 重发（过滤空帧）+ 终止后 ``[DONE]``；
    - anthropic 客户端：StreamState/ir_to_events 重发 §7.4 事件序列 + 每 15s 注释行
      ping 心跳（wait_for 预算制，上游静默也照发）；
    - 上游 ``data: [DONE]`` 不透传——我方 [DONE] 只在见过终止包（finish/usage）后
      补发；上游半途切断（fail=midstream，§7.5 验收器具）则客户端同样收不到终止符，
      忠实透传，绝不补写、绝不换模型。
    """
    buf = first_frame
    data_lines: list[str] = []
    state = StreamState() if client_proto == "anthropic" else None
    saw_terminator = False
    last_ping = time.monotonic()

    async def next_frame() -> str | None:
        """下一完整 SSE 帧；上游流收束回 None（PEP 479：async generator 体内
        不得让 StopAsyncIteration 逃逸，否则 RuntimeError）。"""
        nonlocal buf, data_lines
        while True:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", "replace").rstrip("\r")
                if not s:
                    if data_lines:
                        out = "\n".join(data_lines)
                        data_lines.clear()
                        return out
                elif s.startswith("data:"):
                    data_lines.append(s[5:].strip())
            try:
                buf += await aiter.__anext__()
            except StopAsyncIteration:
                if data_lines:                 # 残余无终止空行的末帧，如实吐出
                    out = "\n".join(data_lines)
                    data_lines.clear()
                    return out
                return None

    # 上游读取放独立 task：ping 心跳的 wait_for 只取消 queue.get（可安全取消），
    # 绝不取消 httpx 的上游读——响应流被取消一次即坏，后续帧全丢（实测踩坑）。
    frames: asyncio.Queue = asyncio.Queue()    # 无界：读端只受上游流速约束（M1 取舍）

    async def reader():
        try:
            while True:
                frame = await next_frame()
                frames.put_nowait(frame)
                if frame is None:
                    return
        except Exception as exc:
            # 读错误按收束处理：忠实截断（§7.5）。旧实现静默吞掉，客户端只看到
            # 「流突然结束」，排障无线索——错误进日志，行为不变。
            logger.warning("upstream stream reader failed (truncating): %r", exc)
            frames.put_nowait(None)

    task = asyncio.create_task(reader())
    try:
        while True:
            if client_proto == "anthropic":        # §7.4 15s ping 心跳（裁定 8）
                budget = PING_INTERVAL_S - (time.monotonic() - last_ping)
                if budget <= 0:
                    yield b": ping\n\n"
                    last_ping = time.monotonic()
                    continue
                try:
                    frame = await asyncio.wait_for(frames.get(), timeout=budget)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    last_ping = time.monotonic()
                    continue
            else:
                frame = await frames.get()         # None → 上游流收束
            if frame is None:
                break
            last_ping = time.monotonic()
            payload = frame.strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            try:
                ic = ADAPTERS[up_proto].chunk_to_ir(chunk)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
                # backlog 修：上游畸形帧（choices 非数组 / delta 非对象 / 顶层非对象等）
                # 旧实现让 KeyError/AttributeError 逃出生成器（未捕获异常）；改为
                # **忠实截断**（§7.5：不补写终止符、不换模型），错误进日志。
                logger.warning("malformed upstream chunk (truncating stream): %r", exc)
                break
            if usage_sink is not None:
                # M3：把末包 usage 交给调用方（流式首包落账时拿不到，续传后回填 cost）。
                # B3（audit）：必须**合并**而非覆盖——Anthropic 的 input_tokens 只在
                # message_start 出现（`chunk_to_ir` 存进 `IRChunk.input_tokens`），
                # message_delta 的 usage 只有 output_tokens；旧实现既只取 `ic.usage`
                # 又是覆盖式赋值 → 输入侧被抹成 0，计价系统性低估。
                # OpenAI 流式的 usage 自带 prompt_tokens，合并语义同样正确。
                if ic.usage is not None:
                    usage_sink.setdefault("usage", {}).update(ic.usage)
                if ic.input_tokens is not None:
                    usage_sink.setdefault("usage", {}) \
                              .setdefault("input_tokens", ic.input_tokens)
            if client_proto == "openai":
                if ic.text is None and ic.finish is None and ic.usage is None:
                    continue                       # 过滤空帧（裁定 8）
                out = oai.ir_to_chunk(ic, model_asked)
                yield f"data: {json.dumps(out, ensure_ascii=False)}\n\n".encode()
                if ic.finish is not None or ic.usage is not None:
                    saw_terminator = True
            else:
                for evt in anth.ir_to_events(ic, state, model_asked):
                    yield evt.encode()
                if ic.usage is not None:
                    saw_terminator = True
        if client_proto == "openai" and saw_terminator:
            yield b"data: [DONE]\n\n"
    finally:
        task.cancel()                              # 处理端提前退出时回收读任务
        try:
            await task
        except BaseException:
            pass
        await resp.aclose()
        await client.aclose()
