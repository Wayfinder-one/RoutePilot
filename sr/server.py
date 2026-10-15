"""sr/server.py —— /v1 双协议路由主流程 + admin 读端点 + /metrics
（spec §7.1 端点 / §7.2 十二步 / §7.5 流式红线 / §9.1-9.2 / §11.1）。

模块职责（集成 Task 1-7 全部模块，spec §7.2 十二步逐项对齐，对照表见任务报告）：
- :func:`create_app` —— 工厂：``POST /v1/chat/completions``（OpenAI）、
  ``POST /v1/messages``（Anthropic）、``GET /v1/models``（auto + 全部别名，§7.1）；
  Task 9 起加 admin 读端点（§9.1 引导序列七 GET + ``/admin/traffic/{id}``）、
  ``GET /metrics``（§11.1 手工 OpenMetrics）；控制台托管 ``GET /`` 与 admin
  写端点归 Task 10/12。
- :func:`_handle` —— 十二步主流程：协议解析 → IR → 主文本 → model 判定
  （auto 走树 / 具体名或别名直通 R2 / 其他 400）→ 会话沿用 → 打标 → 走树 →
  供应商解析 → 上游调用 → 入站名回写 → requests+tags 入库 → 诊断响应头。
- :func:`_call_nonstream` / :func:`_call_stream` —— 候选链（主模型 → fallbacks 依序）
  + 每候选 max_retries 次重试 + 熔断跳过（§8.3）；流式首包（上游首字节）即锁定，
  此后不重试、不换模型、不回退（§7.5 红线，裁定 5）。
- :func:`_relay_stream` —— 流式管道：上游 SSE 帧 → chunk_to_ir → IRChunk →
  按客户端协议重发（openai.ir_to_chunk 逐帧 + [DONE] / anthropic StreamState +
  ir_to_events + 15s ping 心跳），跨协议 §7.6 以 IRChunk 为中转；空帧过滤、
  上游切断忠实透传（不补终止符——§7.5 红线的可观测口径）。
- :class:`Metrics` —— §11.1 指标的内存计数器（依赖白名单无 prometheus_client，
  纯文本手拼 OpenMetrics，裁定 6）；create_app 时从 requests 表 backfill
  ``sr_requests_total``，/metrics 渲染成 classic text 格式。
- admin 读端点 —— §9.2 形状逐字：tree（rows_to_tree 逆组装，cond/action 原样
  透出规范键）、providers/models/settings/bands、traffic（f 五种快捷筛选各一条
  WHERE + 分页 50）、traffic/{id}（详情 + 全量标签）、i18n/{lang}（sr.i18n.MESSAGES）。

约定：每请求自开 sqlite 连接（db.get，WAL），事务由本模块 commit 后关闭；
树缓存为模块级 dict（裁定 4），每请求开头 SELECT tree_version，变了才重载。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

import sr.db as db
from sr.adapters import UpstreamError
from sr.adapters import openai as oai
from sr.adapters import anthropic as anth
from sr.adapters.anthropic import StreamState
from sr.engine import (RouteDecision, evaluate, extract_session, resolve_session,
                       rows_to_tree)
from sr.i18n import MESSAGES, detect_lang
from sr.models import is_circuit_open, record_failure, record_success, resolve_backend
from sr.tags import DEFAULT_CONF_THRESHOLDS, tag_all

# 两协议适配器（§7.1 按路径判定 proto → 同构七件套）
ADAPTERS = {"openai": oai, "anthropic": anth}

# 客户端 anthropic 的 ping 心跳间隔（§7.4「15s ping」逐字）
PING_INTERVAL_S = 15.0
# anthropic 客户端 max_tokens 缺失时的出站兜底（裁定 9）
DEFAULT_MAX_TOKENS = 1024
# 反馈解锁的显式头取值（§4.4 反馈来源 1：x-sr-feedback）
FEEDBACK_VALUES = {"satisfied", "need_clarification", "wrong_answer", "want_different"}

# 跨协议非流式 shim 的终止原因互查（与 sr/adapters/anthropic._STOP_REASON 互为逆表；
# 协议层未导出该私有表，server 层按 §7.3/§7.4 枚举自持，多出值映射 None 不猜语义）
_STOP_TO_FINISH = {"end_turn": "stop", "max_tokens": "length",
                   "tool_use": "tool_calls", "stop_sequence": "stop"}
_FINISH_TO_STOP = {"stop": "end_turn", "length": "max_tokens",
                   "tool_calls": "tool_use", "function_call": "tool_use"}

# 树缓存（裁定 4：模块级 {"version": int, "nodes": list}；每请求开头 SELECT
# tree_version，变了重载）。多一枚 "path" 键：同进程先后挂多个库（pytest tmp_path）
# 时 version 都从 1 起步，不辨库会串树——按库路径隔离，出入记任务报告。
_TREE_CACHE: dict = {"path": None, "version": None, "nodes": []}


# ─────────────────────────────────────────────────────────────────────────────
# 1) 小工具：设置加载 / 树缓存 / 模型查找（detect_lang 自 Task 9 起在 sr/i18n.py）
# ─────────────────────────────────────────────────────────────────────────────


def _load_settings(conn) -> dict:
    """settings 表 → ``{key: value}``（值一律字符串，§3.1）。"""
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def _conf_cuts(conn) -> list[float]:
    """conf_settings 单行切点 → list（缺行回默认 [0.30, 0.60, 0.85]，§3.1）。"""
    row = conn.execute("SELECT thresholds FROM conf_settings WHERE id = 1").fetchone()
    return json.loads(row["thresholds"]) if row else list(DEFAULT_CONF_THRESHOLDS)


def _load_tree(conn, db_path: str) -> list:
    """树缓存读取（裁定 4 + §12 worker 口径）：开头一条 SELECT tree_version；
    没变用内存树，变了（或换了库）从 SQLite 重新载入并原子替换。"""
    version = db.get_tree_version(conn)
    if _TREE_CACHE["path"] != db_path or _TREE_CACHE["version"] != version:
        rows = conn.execute("SELECT * FROM tree").fetchall()
        _TREE_CACHE["path"] = db_path
        _TREE_CACHE["version"] = version
        _TREE_CACHE["nodes"] = rows_to_tree(rows)
    return _TREE_CACHE["nodes"]


def _lookup_model(conn, name: str):
    """请求名 → models 行：先按 id，再扫 aliases JSON 数组（别名视同该模型，R2）。
    查无返回 None（调用方回 400）。"""
    row = conn.execute("SELECT * FROM models WHERE id = ?", (name,)).fetchone()
    if row is not None:
        return row
    for r in conn.execute("SELECT * FROM models WHERE enabled = 1 ORDER BY id"):
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except ValueError:
            aliases = []
        if name in aliases:
            return r
    return None


def _last_msg(conn, session_id: str | None) -> str | None:
    """sessions.last_msg（§4.6 similarity/reask 的比对基准）；无会话回 None。"""
    if not session_id:
        return None
    row = conn.execute("SELECT last_msg FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    return row["last_msg"] if row is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# 2) 错误信封 / 诊断头 / 入库
# ─────────────────────────────────────────────────────────────────────────────


def _error(proto: str, status: int, message: str, etype: str,
           code: str | None = None, headers: dict | None = None) -> JSONResponse:
    """按客户端协议回错误信封（§7.3 / §7.4 各自形状）。"""
    if proto == "anthropic":
        payload: dict = {"type": "error", "error": {"type": etype, "message": message}}
    else:
        err: dict = {"message": message, "type": etype}
        if code:
            err["code"] = code
        payload = {"error": err}
    return JSONResponse(payload, status_code=status, headers=headers or {})


def _store_tags(conn, request_id: str, signals: dict | None) -> None:
    """tag_all 产物 → tags 表 17 行（裁定 3：全键入库）。

    value 过 JSON 序列化或标量原样（int 档位 → "1"、list → JSON 数组、None →
    "null"），confidence 规则标签为 NULL，is_rule 按 tag_all 的标记（§3.1 列口径）。
    """
    for tag, sig in (signals or {}).items():
        v = sig["value"]
        conn.execute(
            "INSERT OR REPLACE INTO tags (request_id, tag, value, confidence, is_rule) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, tag,
             v if isinstance(v, str) else json.dumps(v, ensure_ascii=False),
             sig.get("confidence"), 1 if sig.get("is_rule") else 0))


def _log_request(conn, *, proto, asked, hit_branch, routed_to, session_id,
                 session_tag, outcome, status, answer_ms, upstream_ms, cost) -> str:
    """requests 表一行（§3.1 列口径）；返回行 id 供 tags 外键。"""
    rid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO requests (id, ts, proto, model_asked, hit_branch, routed_to, "
        "session_id, session_tag, outcome, status, answer_ms, upstream_ms, cost) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, db._now(), proto, asked, hit_branch, routed_to, session_id,
         session_tag, outcome, status, answer_ms, upstream_ms, cost))
    return rid


def _upsert_session(conn, session_id: str, model: str, text: str) -> None:
    """树命中成功后写会话（§4.4：current_model 供下一轮沿用，last_msg 供相似度）。"""
    conn.execute(
        "INSERT INTO sessions (id, current_model, last_active, turns, unlocks, last_msg) "
        "VALUES (?, ?, datetime('now'), 1, 0, ?) "
        "ON CONFLICT(id) DO UPDATE SET current_model = excluded.current_model, "
        "last_active = datetime('now'), turns = turns + 1, last_msg = excluded.last_msg",
        (session_id, model, text))


def _cost_of(conn, model_id: str, usage: dict | None) -> float | None:
    """按 models 价目 × usage 估成本（§3.1 cost 列）；无价目/无 usage → NULL。"""
    if not usage:
        return None
    row = conn.execute("SELECT price_in, price_out FROM models WHERE id = ?",
                       (model_id,)).fetchone()
    if row is None or row["price_in"] is None or row["price_out"] is None:
        return None
    pin = usage.get("prompt_tokens", usage.get("input_tokens") or 0) or 0
    pout = usage.get("completion_tokens", usage.get("output_tokens") or 0) or 0
    return (pin * row["price_in"] + pout * row["price_out"]) / 1_000_000


# ─────────────────────────────────────────────────────────────────────────────
# 3) 上游调用：候选链 + 重试 + 熔断 + §7.5 首包锁定（裁定 5）
# ─────────────────────────────────────────────────────────────────────────────


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


async def _call_nonstream(conn, ir, decision: RouteDecision, settings: dict,
                    metrics: Metrics | None = None):
    """非流式上游调用。返回 (backend, model_id, resp_json, upstream_ms)；
    全链失败抛最后一个 UpstreamError。每失败记 record_failure，成功记
    record_success（裁定 5）；is_circuit_open 的候选直接跳过。
    metrics 可选：每次尝试失败记 sr_retry_total{model,status}；失败后刚好
    越过熔断阈值（本候选此后会被跳过，不会再进来）记 sr_circuit_open_total。"""
    max_retries = int(settings.get("max_retries") or 0)
    last: UpstreamError | None = None
    for cand in _chain(decision):
        if is_circuit_open(cand):
            continue
        try:
            backend = resolve_backend(conn, cand)
        except Exception as exc:          # KeyError / env 缺失：配置面问题，换下一候选
            last = UpstreamError(502, f"resolve {cand}: {exc}", "config_error")
            continue
        payload = _outbound_payload(ir, backend, decision.think)
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
                                {"model": cand, "status": str(exc.status or 0)})
                    if is_circuit_open(cand):
                        metrics.inc("sr_circuit_open_total", {"model": cand})
            except httpx.HTTPError as exc:
                record_failure(conn, cand)
                last = UpstreamError(502, f"upstream unreachable: {exc}", "api_error")
                if metrics is not None:
                    metrics.inc("sr_retry_total", {"model": cand, "status": "0"})
                    if is_circuit_open(cand):
                        metrics.inc("sr_circuit_open_total", {"model": cand})
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
            metrics.inc("sr_retry_total", {"model": cand, "status": str(status)})
            if is_circuit_open(cand):
                metrics.inc("sr_circuit_open_total", {"model": cand})

    for cand in _chain(decision):
        if is_circuit_open(cand):
            continue
        try:
            backend = resolve_backend(conn, cand)
        except Exception as exc:
            last = UpstreamError(502, f"resolve {cand}: {exc}", "config_error")
            continue
        payload = _outbound_payload(ir, backend, decision.think)
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


# ─────────────────────────────────────────────────────────────────────────────
# 4) 流式管道：上游 SSE 帧 → IRChunk → 客户端协议（§7.6 + §7.4 ping + §7.5 忠实透传）
# ─────────────────────────────────────────────────────────────────────────────


async def _relay_stream(client_proto: str, up_proto: str, model_asked: str | None,
                        resp: httpx.Response, client: httpx.AsyncClient,
                        aiter, first_frame: bytes):
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
        except Exception:
            frames.put_nowait(None)            # 读错误按收束处理：忠实截断（§7.5）

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
            ic = ADAPTERS[up_proto].chunk_to_ir(chunk)
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


# ─────────────────────────────────────────────────────────────────────────────
# 5) 跨协议非流式 shim（§7.6 只写了流式；非流式 M1 最小实现：纯文本面互转）
# ─────────────────────────────────────────────────────────────────────────────


def _from_upstream(client_proto: str, up_proto: str, resp: dict,
                   model_asked: str | None) -> dict:
    """上游响应 → 客户端响应：同协议走 from_upstream（§7.2-10 入站名回写）；
    跨协议按纯文本面最小互转（tool_calls 等复杂面 M2），model 一律回写。"""
    if client_proto == up_proto:
        return ADAPTERS[client_proto].from_upstream(resp, model_asked)
    if up_proto == "openai":                      # openai 上游 → anthropic 客户端
        choice = (resp.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        usage = resp.get("usage") or {}
        return {"id": resp.get("id") or "msg_smart-router", "type": "message",
                "role": "assistant", "model": model_asked,
                "content": [{"type": "text",
                             "text": (choice.get("message") or {}).get("content") or ""}],
                "stop_reason": _FINISH_TO_STOP.get(finish), "stop_sequence": None,
                "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                          "output_tokens": usage.get("completion_tokens", 0)}}
    # anthropic 上游 → openai 客户端
    text = "".join(blk.get("text", "") for blk in resp.get("content") or []
                   if isinstance(blk, dict) and blk.get("type") == "text")
    usage = resp.get("usage") or {}
    return {"id": resp.get("id") or "chatcmpl-smart-router",
            "object": "chat.completion", "created": int(time.time()),
            "model": model_asked,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": _STOP_TO_FINISH.get(resp.get("stop_reason"))}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("input_tokens", 0)
                      + usage.get("output_tokens", 0)}}


# ─────────────────────────────────────────────────────────────────────────────
# 6) §11.1 指标：内存计数器 + 手工 OpenMetrics 文本（依赖白名单无
#    prometheus_client，禁新增依赖——裁定 6）
# ─────────────────────────────────────────────────────────────────────────────


# §11.1 指标清单（family 名, 类型, HELP；顺序 = 渲染顺序 = §11.1 逐字）。
# sr_eval_route_match_rate / sr_estimate_error 两 gauge 归 Task 11 评测闭环，
# 此处先声明 family（无样本时只出 HELP/TYPE 行，名称在 /metrics 恒可见）。
_METRIC_FAMILIES: list[tuple[str, str, str]] = [
    ("sr_requests_total", "counter",
     "Total /v1 requests by proto/route/model/outcome"),
    ("sr_route_hits_total", "counter", "Tree branch hits by route path"),
    ("sr_answer_latency_seconds", "summary", "Tagging latency (answer_ms)"),
    ("sr_upstream_latency_seconds", "summary",
     "Upstream first-byte latency by model"),
    ("sr_no_match_total", "counter",
     "Requests matched no branch (defensive fallback)"),
    ("sr_fallback_total", "counter", "Fallback chain promotions"),
    ("sr_retry_total", "counter", "Upstream attempt failures by model/status"),
    ("sr_session_sticky_total", "counter", "Session sticky hits"),
    ("sr_session_unlock_total", "counter", "Feedback unlocks"),
    ("sr_budget_triggered_total", "counter", "Budget protection triggers"),
    ("sr_circuit_open_total", "counter", "Circuit breaker openings by model"),
    ("sr_eval_route_match_rate", "gauge", "Latest eval route match rate"),
    ("sr_estimate_error", "gauge", "Latest estimate review error"),
]


def _label_key(labels: dict | None) -> tuple:
    """标签 dict → 可哈希键（排序保证同标签集归并）。"""
    return tuple(sorted((labels or {}).items()))


def _label_str(labels: tuple) -> str:
    """标签键 → Prometheus 文本 ``{k="v",...}``（值转义 \\ \" 与换行）。"""
    if not labels:
        return ""

    def esc(v) -> str:
        s = str(v)
        s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return s

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in labels) + "}"


class Metrics:
    """§11.1 计数器（内存 dict；每 app 实例一份，app.state.metrics 隔离）。

    - counter：``inc(name, labels, n)``；
    - summary（时延）：``observe(name, labels, seconds)`` 渲染为 ``_count/_sum``；
    - gauge：``set(name, value)``（评测闭环接入前无样本）。
    """

    def __init__(self):
        self.counters: dict[tuple, float] = {}
        self.summaries: dict[tuple, list] = {}     # key → [count, sum_seconds]
        self.gauges: dict[tuple, float] = {}

    def inc(self, name: str, labels: dict | None = None, n: float = 1) -> None:
        key = (name, _label_key(labels))
        self.counters[key] = self.counters.get(key, 0) + n

    def observe(self, name: str, labels: dict | None, seconds: float) -> None:
        s = self.summaries.setdefault((name, _label_key(labels)), [0, 0.0])
        s[0] += 1
        s[1] += seconds

    def set(self, name: str, value: float, labels: dict | None = None) -> None:
        self.gauges[(name, _label_key(labels))] = value

    def backfill_requests(self, conn) -> None:
        """启动 backfill：requests 表按 (proto, hit_branch, routed_to, outcome)
        聚合灌入 sr_requests_total（裁定 6 只 backfill 此一族；hit_branch 为
        NULL 的非树请求 route 记 "none"——运行期 live 路径用诊断头值，重启后
        两口径对非树行不逐一对齐，记任务报告）。"""
        rows = conn.execute(
            "SELECT proto, hit_branch, routed_to, outcome, COUNT(*) n "
            "FROM requests GROUP BY proto, hit_branch, routed_to, outcome")
        for r in rows:
            self.inc("sr_requests_total",
                     {"proto": r["proto"], "route": r["hit_branch"] or "none",
                      "model": r["routed_to"] or "", "outcome": r["outcome"] or ""},
                     r["n"])

    def render(self) -> str:
        """§11.1 清单 → classic OpenMetrics 文本（HELP/TYPE 恒出，样本有则出）。"""
        lines: list[str] = []
        for name, typ, help_text in _METRIC_FAMILIES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            if typ == "counter":
                for (n, labels), v in sorted(self.counters.items()):
                    if n == name:
                        lines.append(f"{name}{_label_str(labels)} {v:g}")
            elif typ == "summary":
                for (n, labels), (c, s) in sorted(self.summaries.items()):
                    if n == name:
                        lines.append(f"{name}_count{_label_str(labels)} {c}")
                        lines.append(f"{name}_sum{_label_str(labels)} {s:.6f}")
            else:
                for (n, labels), v in sorted(self.gauges.items()):
                    if n == name:
                        lines.append(f"{name}{_label_str(labels)} {v:g}")
        return "\n".join(lines) + "\n"


def _bump_request_metrics(metrics: Metrics, *, proto: str, diag: dict,
                          routed_to: str | None, outcome: str,
                          session_tag: str, hit_branch: str | None,
                          primary: str | None = None,
                          final: str | None = None) -> None:
    """一次 /v1 请求的请求级计数（在 _log_request 的每个落账点调用）。

    - route 标签取诊断头同源值（树命中 = "N.M" 路径，否则 direct/sticky/
      unlocked/fallback，取不到记 "none"）；
    - model 标签 = 实际去向（block / 模型 id；error 行 routed_to 为 NULL → ""）；
    - primary≠final 且非试水（trial 改道是灰度不是回退）记 sr_fallback_total。
    """
    route = diag.get("x-sr-route", "none")
    metrics.inc("sr_requests_total", {"proto": proto, "route": route,
                                      "model": routed_to or "", "outcome": outcome})
    if hit_branch:
        metrics.inc("sr_route_hits_total", {"route": hit_branch})
    if session_tag == "sticky":
        metrics.inc("sr_session_sticky_total")
    elif session_tag == "unlocked":
        metrics.inc("sr_session_unlock_total")
    if final and primary and final != primary and diag.get("x-sr-route") != "trial":
        metrics.inc("sr_fallback_total", {"from": primary, "to": final})


# ─────────────────────────────────────────────────────────────────────────────
# 7) admin 读端点辅助：树逆组装 / traffic 筛选（§9.2 形状，Task 9）
# ─────────────────────────────────────────────────────────────────────────────


def _branch_json(b) -> dict:
    """Branch → §9.2 分支形状；cond/action 原样透出（含 conf_min/fallback/
    block_msg 等全部规范键，不剥任何键——Task 10 GET→PUT 回环依赖此保证）。"""
    return {"idx": b.idx, "cond": b.cond, "action": b.action, "trial": b.trial,
            "enabled": b.enabled, "kids": [_branch_json(k) for k in b.kids]}


def _tree_json(nodes: list, version: int) -> dict:
    """rows_to_tree 的逆组装 → §9.2 首块形状（与原型 TREE 结构一一对应）。"""
    return {"version": version,
            "nodes": [{"idx": n.idx, "kind": n.kind, "name": n.name,
                       "desc": n.desc, "enabled": n.enabled,
                       "branches": [_branch_json(b) for b in n.branches]}
                      for n in nodes]}


def _confmin_tags(clauses: list) -> list[str]:
    """cond 子句列表（含 or/not 嵌套）里带 conf_min 的标签键。"""
    out: list[str] = []
    for c in clauses:
        if not isinstance(c, dict):
            continue
        if "or" in c:
            out += _confmin_tags(c["or"])
        elif "not" in c:
            out += _confmin_tags([c["not"]])
        elif "conf_min" in c:
            out.append(c["tag"])
    return out


def _traffic_filter(conn, f: str, db_path: str) -> tuple[str, list]:
    """§9.2 ``f`` 五种快捷筛选 → 一条 WHERE（裁定 4）。

    - blocked / fallback / unlocked：outcome / session_tag 单等值；
    - small：「去了小模型」（设计方案 §4.3）→ 去向 = 阶梯最低档（settings.ladder
      首个）；
    - low_conf：命中分支条件含 conf_min 且该标签 conf_band < 2——分支按
      hit_branch "节点.位置" 定位（Task 5 裁定 hit_path=[节点idx, 分支序号]），
      conf_band 由 tags.confidence 对 conf_settings 切点计数（达到切点个数，
      与 sr.tags.conf_band 同语义）在 SQL 内联计算；confidence NULL（规则标签）
      比较为 NULL → EXISTS 不成立，天然排除。
    """
    simple = {"blocked": "outcome = 'block'",
              "fallback": "outcome = 'fallback'",
              "unlocked": "session_tag = 'unlocked'"}
    if f in simple:
        return simple[f], []
    if f == "small":
        ladder0 = (_load_settings(conn).get("ladder") or "").split(",")[0].strip()
        return "routed_to = ?", [ladder0]
    if f == "low_conf":
        gated: dict[str, list[str]] = {}
        for node in _load_tree(conn, db_path):
            for i, b in enumerate(node.branches):
                tags = sorted(set(_confmin_tags(b.cond)))
                if tags:
                    gated[f"{node.idx}.{i}"] = tags
        if not gated:                       # 树上没有任何把握度门槛 → 恒空集
            return "1 = 0", []
        cuts = _conf_cuts(conn)
        # 每个比较单独加括号：SQL 里 + 优先级高于 >=，裸写会变成 confidence 与
        # 「切点+布尔」之和比较（实测踩坑：0.90 的行也被算进 band<2）
        band_sql = " + ".join(["(t.confidence >= ?)"] * len(cuts))
        groups, gparams = [], []
        for hb, tags in sorted(gated.items()):
            ph = ",".join("?" * len(tags))
            groups.append(
                f"(requests.hit_branch = ? AND EXISTS (SELECT 1 FROM tags t "
                f"WHERE t.request_id = requests.id AND t.tag IN ({ph}) "
                f"AND ({band_sql}) < 2))")
            gparams += [hb, *tags, *cuts]
        return "(" + " OR ".join(groups) + ")", gparams
    raise HTTPException(status_code=400, detail=f"unknown filter: {f}")


def _tag_value(raw: str):
    """tags.value 存储串 → 渲染值：JSON 数组/对象反序列化（pii 多标签），其余原样
    （§3.1：分类存枚举键、档位存索引串，显示层翻译，不入库显示词）。"""
    if raw[:1] in ("[", "{"):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _tags_brief(conn, ids: list[str]) -> dict[str, list]:
    """每行 tags_brief：入库序（rowid）前 3 条 {tag, value}（§9.2 示例形状）。"""
    out: dict[str, list] = {}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    for r in conn.execute(
            f"SELECT request_id, tag, value FROM tags WHERE request_id IN ({ph}) "
            "ORDER BY rowid", ids):
        brief = out.setdefault(r["request_id"], [])
        if len(brief) < 3:
            brief.append({"tag": r["tag"], "value": _tag_value(r["value"])})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 8) 十二步主流程（spec §7.2 逐项；对照表见任务报告）
# ─────────────────────────────────────────────────────────────────────────────


async def _handle(proto: str, request: Request, db_path: str, tagger: str,
                  metrics: Metrics):
    """``POST /v1/chat/completions`` / ``POST /v1/messages`` 共用主流程。"""
    conn = None
    try:
        # ── 1–2 按路径判定协议 + 解析请求体 → IR ──
        try:
            body = await request.json()
        except Exception:
            return _error(proto, 400, "request body is not valid JSON",
                          "invalid_request_error", "invalid_json",
                          {"x-sr-tagger": tagger})
        if not isinstance(body, dict):
            return _error(proto, 400, "request body must be a JSON object",
                          "invalid_request_error", "invalid_json",
                          {"x-sr-tagger": tagger})
        ir = ADAPTERS[proto].parse(body)
        if proto == "anthropic" and ir.params.get("max_tokens") is None:
            # anthropic 客户端 max_tokens 必填，缺失兜底 1024（裁定 9，声明记任务报告）
            ir.params["max_tokens"] = DEFAULT_MAX_TOKENS

        # ── 3 提取主文本（最后一条 user 消息） ──
        text = ADAPTERS[proto].main_text(ir)
        asked = ir.model_asked

        conn = db.get(db_path)
        settings = _load_settings(conn)
        nodes = _load_tree(conn, db_path)
        cuts = _conf_cuts(conn)
        req_headers = dict(request.headers)
        accept_lang = request.headers.get("accept-language")
        session_id: str | None = None
        session_tag = "new"
        answer_ms = 0.0
        signals: dict | None = None
        hit_branch: str | None = None

        # ── 4 model 判定：auto → 树；具体名/别名 → 直通 R2；其他 → 400 ──
        if asked == "auto":
            mode = "tree"
        else:
            row = _lookup_model(conn, str(asked))
            if row is None or not row["enabled"]:
                return _error(proto, 400, f"unknown model: {asked}",
                              "invalid_request_error", "model_not_found",
                              {"x-sr-tagger": tagger})
            mode = "direct"

        if mode == "direct":
            # R2：显式模型一律直通——不参与沿用、不走树，但仍打标入库（§4.4 末段）
            decision = RouteDecision(kind="route", model=row["id"], source="direct")
            session_id = extract_session(
                proto, body, req_headers,
                request.client.host if request.client else "",
                sticky_enabled=(settings.get("session_sticky") == "1"))
            t1 = time.monotonic()
            signals = tag_all(text, req_headers, len(ir.messages),
                              datetime.now().astimezone(), _last_msg(conn, session_id))
            answer_ms = (time.monotonic() - t1) * 1000
            metrics.observe("sr_answer_latency_seconds", {}, answer_ms / 1000.0)
        else:
            # ── 5 查会话沿用（§4.4）──
            session_id = extract_session(
                proto, body, req_headers,
                request.client.host if request.client else "",
                sticky_enabled=(settings.get("session_sticky") == "1"))
            feedback = request.headers.get("x-sr-feedback")       # §4.4 反馈来源 1
            if feedback not in FEEDBACK_VALUES:
                feedback = None
            pre_signals: dict | None = None
            if feedback is None and settings.get("sticky_detect_feedback") == "1":
                # §4.4 反馈来源 2：开启后沿用路径仍做一次前向，只为读 user_feedback
                pre_signals = tag_all(text, req_headers, len(ir.messages),
                                      datetime.now().astimezone(),
                                      _last_msg(conn, session_id))
                feedback = pre_signals["user_feedback"]["value"]
            sess = resolve_session(conn, session_id, pre_signals, settings,
                                   feedback=feedback)
            if sess is not None:                  # sticky / unlocked：跳过打标（§14.2）
                decision = sess
                session_tag = sess.source
                signals = pre_signals             # 仅反馈探测模式有产物
            else:
                session_tag = "new"
                # ── 6 打标签（一次前向） ──
                t1 = time.monotonic()
                signals = pre_signals or tag_all(text, req_headers, len(ir.messages),
                                                 datetime.now().astimezone(),
                                                 _last_msg(conn, session_id))
                answer_ms = (time.monotonic() - t1) * 1000
                metrics.observe("sr_answer_latency_seconds", {}, answer_ms / 1000.0)
                # ── 7 走分流树 → RouteDecision ──
                decision = evaluate(nodes, signals, cuts)

        # ── 12(预备) 诊断头取值（裁定 1）──
        diag: dict = {"x-sr-tagger": tagger, "x-sr-session": session_tag}
        if decision.source == "direct":
            diag["x-sr-route"] = "direct"
        elif decision.source in ("sticky", "unlocked"):
            diag["x-sr-route"] = decision.source
        elif decision.source == "fallback":       # 坏树全 miss 的防御决定
            diag["x-sr-route"] = "fallback"
        elif decision.hit_path:
            diag["x-sr-route"] = ".".join(map(str, decision.hit_path))
            hit_branch = diag["x-sr-route"]       # 裁定 2：hit_branch 存 "N.M" 串

        # block → 403 + 双语 block_msg 按 accept-language（裁定 6）
        if decision.kind == "block":
            msg_map = decision.block_msg or {}
            message = msg_map.get(detect_lang(accept_lang)) or msg_map.get("en") \
                or "blocked by safety policy"
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to="block", session_id=session_id,
                               session_tag=session_tag, outcome="block", status=403,
                               answer_ms=answer_ms, upstream_ms=None, cost=None)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to="block", outcome="block",
                                  session_tag=session_tag, hit_branch=hit_branch)
            conn.commit()
            return _error(proto, 403, message, "permission_error", "content_blocked",
                          diag)

        if not decision.model:
            # 防御性 fallback（无兜底的坏树，正常不可达）：x-sr-route=fallback + 502
            metrics.inc("sr_no_match_total")
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to=None, session_id=session_id,
                               session_tag=session_tag, outcome="error", status=502,
                               answer_ms=answer_ms, upstream_ms=None, cost=None)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to=None, outcome="error",
                                  session_tag=session_tag, hit_branch=hit_branch)
            conn.commit()
            return _error(proto, 502, "routing produced no model",
                          "api_error", "no_route", diag)

        # ── 8–9 供应商解析 + 上游调用（候选链 + 重试 + 熔断 + 首包锁定） ──
        try:
            if ir.stream:
                backend, final_model, resp, client, aiter, first, upstream_ms = \
                    await _call_stream(conn, ir, decision, settings, metrics)
            else:
                backend, final_model, data, upstream_ms = \
                    await _call_nonstream(conn, ir, decision, settings, metrics)
        except UpstreamError as exc:
            # 全链失败 → 502 错误信封（outcome=error，裁定 2 三值枚举）
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to=None, session_id=session_id,
                               session_tag=session_tag, outcome="error",
                               status=exc.status, answer_ms=answer_ms,
                               upstream_ms=None, cost=None)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to=None, outcome="error",
                                  session_tag=session_tag, hit_branch=hit_branch)
            conn.commit()
            return _error(proto, exc.status or 502, exc.message,
                          exc.type if exc.type != "config_error" else "api_error",
                          "upstream_error", diag)

        # ── 11 requests + tags 入库（流式在首包锁定后、续传前落账；裁定 2/3） ──
        diag["x-sr-model"] = final_model
        diag["x-sr-upstream-model"] = backend.upstream_name
        rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                           routed_to=final_model, session_id=session_id,
                           session_tag=session_tag, outcome="upstream", status=200,
                           answer_ms=answer_ms, upstream_ms=upstream_ms,
                           cost=_cost_of(conn, final_model, None))
        _store_tags(conn, rid, signals)
        metrics.observe("sr_upstream_latency_seconds", {"model": final_model},
                        (upstream_ms or 0) / 1000.0)
        _bump_request_metrics(metrics, proto=proto, diag=diag,
                              routed_to=final_model, outcome="upstream",
                              session_tag=session_tag, hit_branch=hit_branch,
                              primary=decision.model, final=final_model)
        if mode == "tree" and session_id:
            # 树命中成功才写会话（直通 R2 不参与沿用；沿用轮 resolve_session 已记账）
            if session_tag == "new":
                _upsert_session(conn, session_id, final_model, text)
            else:
                conn.execute("UPDATE sessions SET last_msg = ? WHERE id = ?",
                             (text, session_id))
        conn.commit()
        # conn 交由外层 finally 统一关闭（在返回前，流式续传不占库连接）
        conn = None

        # ── 9(续)/10 响应体按客户端协议转换 + 入站名回写 + 诊断头 ──
        if ir.stream:
            return StreamingResponse(
                _relay_stream(proto, backend.proto, asked, resp, client, aiter, first),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", **diag})
        return JSONResponse(_from_upstream(proto, backend.proto, data, asked),
                            headers=diag)
    except Exception as exc:                      # 兜底：错误信封按客户端协议
        if conn is not None:
            conn.rollback()
        return _error(proto, 500, f"internal error: {exc}", "api_error",
                      "internal", {"x-sr-tagger": tagger})
    finally:
        if conn is not None:                      # 400/block/502 等提前返回路径也关连接
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 9) create_app —— 端点装配（§7.1 /v1 + §9.1-9.2 admin 读端点 + /metrics；
#    控制台托管 GET / 与 admin 写端点归 Task 10/12）
# ─────────────────────────────────────────────────────────────────────────────


def create_app(db_path: str, tagger: str = "heuristic") -> FastAPI:
    """构造路由应用（简报 Produces 接口逐字）。

    :param db_path: SQLite 路径（调用方先 init_db；本层每请求自开连接）。
    :param tagger: ``checkpoint`` | ``heuristic``（x-sr-tagger 回显，裁定 1）。

    构造时从 requests 表 backfill sr_requests_total（裁定 6），计数器挂
    ``app.state.metrics``（每 app 实例独立，测试互不串扰）。
    """
    app = FastAPI(title="smart-router", docs_url=None, redoc_url=None)
    app.state.db_path = db_path
    app.state.tagger = tagger
    metrics = Metrics()
    conn = db.get(db_path)
    try:
        metrics.backfill_requests(conn)
    finally:
        conn.close()
    app.state.metrics = metrics

    # ── /v1 双协议主流程（§7.1）──
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _handle("openai", request, db_path, tagger, metrics)

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await _handle("anthropic", request, db_path, tagger, metrics)

    # ── admin 读端点（§9.1 引导序列 + §9.2 形状；Task 9）──
    @app.get("/admin/providers")
    async def admin_providers():
        """供应商列表（providers 表全行；extra JSON 反序列化，ok 转布尔）。"""
        conn = db.get(db_path)
        try:
            rows = conn.execute("SELECT * FROM providers ORDER BY name").fetchall()
        finally:
            conn.close()
        return JSONResponse([{"name": r["name"], "proto": r["proto"],
                              "base_url": r["base_url"], "key_env": r["key_env"],
                              "extra": json.loads(r["extra"] or "{}"),
                              "ok": bool(r["ok"]),
                              "break_reason": r["break_reason"]} for r in rows])

    @app.get("/admin/models")
    async def admin_models():
        """模型列表（含阶梯、熔断状态，§8 表）：caps/aliases 反序列化、
        ok/enabled 转布尔、on_ladder = 是否在 settings.ladder 阶梯上。"""
        conn = db.get(db_path)
        try:
            ladder = [s.strip() for s in
                      (_load_settings(conn).get("ladder") or "").split(",")
                      if s.strip()]
            rows = conn.execute("SELECT * FROM models ORDER BY id").fetchall()
        finally:
            conn.close()
        return JSONResponse([{"id": r["id"], "provider": r["provider"],
                              "proto": r["proto"], "upstream": r["upstream"],
                              "caps": json.loads(r["caps"] or "[]"),
                              "aliases": json.loads(r["aliases"] or "[]"),
                              "step": r["step"], "price_in": r["price_in"],
                              "price_out": r["price_out"], "ok": bool(r["ok"]),
                              "break_reason": r["break_reason"],
                              "enabled": bool(r["enabled"]),
                              "on_ladder": r["id"] in ladder} for r in rows])

    @app.get("/admin/tree")
    async def admin_tree():
        """分流树（§9.2 首块形状；rows_to_tree 逆组装，cond/action 原样透出）。"""
        conn = db.get(db_path)
        try:
            body = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
        finally:
            conn.close()
        return JSONResponse(body)

    @app.get("/admin/settings")
    async def admin_settings():
        """全局设置（§3.1 settings 表全键值，值一律字符串）。"""
        conn = db.get(db_path)
        try:
            body = _load_settings(conn)
        finally:
            conn.close()
        return JSONResponse(body)

    @app.get("/admin/bands")
    async def admin_bands():
        """标签分档 + 把握度分档（设置页；conf_settings 切点 + tag_settings 各行）。"""
        conn = db.get(db_path)
        try:
            body = {"conf_thresholds": _conf_cuts(conn),
                    "tag_thresholds": [
                        {"tag": r["tag"], "thresholds": json.loads(r["thresholds"])}
                        for r in conn.execute("SELECT tag, thresholds FROM tag_settings "
                                              "ORDER BY tag")]}
        finally:
            conn.close()
        return JSONResponse(body)

    @app.get("/admin/traffic")
    async def admin_traffic(f: str | None = None, proto: str | None = None,
                            hours: int | None = None, page: int = 1):
        """流量列表（§9.2 形状）：``?f=&proto=&hours=&page=``，每页 50。

        f 五种快捷筛选各一条 WHERE（裁定 4，见 _traffic_filter）；outcome 用库中
        实际值（upstream/block/error）；tags_brief 每行入库序前 3 条。
        """
        conn = db.get(db_path)
        try:
            where: list[str] = []
            params: list = []
            if proto:
                where.append("proto = ?")
                params.append(proto)
            if hours:
                cutoff = (datetime.now().astimezone()
                          - timedelta(hours=hours)).isoformat(timespec="seconds")
                where.append("ts >= ?")
                params.append(cutoff)
            if f:
                clause, fparams = _traffic_filter(conn, f, db_path)
                where.append(clause)
                params += fparams
            wsql = (" WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(f"SELECT COUNT(*) c FROM requests{wsql}",
                                 params).fetchone()["c"]
            page = max(1, page)
            rows = conn.execute(
                f"SELECT * FROM requests{wsql} ORDER BY ts DESC, rowid DESC "
                "LIMIT 50 OFFSET ?", [*params, (page - 1) * 50]).fetchall()
            brief = _tags_brief(conn, [r["id"] for r in rows])
        finally:
            conn.close()
        return JSONResponse({"total": total, "rows": [
            {"id": r["id"], "ts": r["ts"], "proto": r["proto"],
             "model_asked": r["model_asked"], "tags_brief": brief.get(r["id"], []),
             "hit_branch": r["hit_branch"], "routed_to": r["routed_to"],
             "session_tag": r["session_tag"], "outcome": r["outcome"],
             "status": r["status"], "answer_ms": r["answer_ms"],
             "upstream_ms": r["upstream_ms"]} for r in rows]})

    @app.get("/admin/traffic/{rid}")
    async def admin_traffic_detail(rid: str):
        """请求详情（标签 + 路径）：requests 全列 + tags 全行（入库序）。"""
        conn = db.get(db_path)
        try:
            r = conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
            if r is None:
                return JSONResponse({"detail": "request not found"}, status_code=404)
            tags = [{"tag": t["tag"], "value": _tag_value(t["value"]),
                     "confidence": t["confidence"], "is_rule": bool(t["is_rule"])}
                    for t in conn.execute("SELECT tag, value, confidence, is_rule "
                                          "FROM tags WHERE request_id = ? "
                                          "ORDER BY rowid", (rid,))]
        finally:
            conn.close()
        return JSONResponse({"id": r["id"], "ts": r["ts"], "proto": r["proto"],
                             "model_asked": r["model_asked"],
                             "hit_branch": r["hit_branch"],
                             "routed_to": r["routed_to"],
                             "session_id": r["session_id"],
                             "session_tag": r["session_tag"],
                             "outcome": r["outcome"], "status": r["status"],
                             "answer_ms": r["answer_ms"],
                             "upstream_ms": r["upstream_ms"], "cost": r["cost"],
                             "tags": tags})

    @app.get("/admin/i18n/{lang}")
    async def admin_i18n(lang: str):
        """指定语言的翻译表（前端启动时加载，§9.1）。lang ∈ zh|en；其他回落
        zh（裁定 5 二选一取回落：§6.1「默认 zh」，控制台引导永不断链）。"""
        return JSONResponse(MESSAGES.get(lang) or MESSAGES["zh"])

    # ── /metrics（§11.1 手工 OpenMetrics，裁定 6）──
    @app.get("/metrics")
    async def metrics_endpoint():
        return PlainTextResponse(
            metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/v1/models")
    async def list_models():
        """§7.1 目录：auto + 全部别名（enabled 模型的 aliases 并集，去重保序）。"""
        conn = db.get(db_path)
        try:
            ids = ["auto"]
            for r in conn.execute(
                    "SELECT aliases FROM models WHERE enabled = 1 ORDER BY id"):
                try:
                    aliases = json.loads(r["aliases"] or "[]")
                except ValueError:
                    aliases = []
                for a in aliases:
                    if a not in ids:
                        ids.append(a)
        finally:
            conn.close()
        return JSONResponse({"object": "list",
                             "data": [{"id": i, "object": "model",
                                       "owned_by": "smart-router"} for i in ids]},
                            headers={"x-sr-tagger": tagger})

    return app
