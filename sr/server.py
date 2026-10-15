"""sr/server.py —— /v1 双协议路由主流程 + admin 读/写端点 + /metrics
（spec §7.1 端点 / §7.2 十二步 / §7.5 流式红线 / §9.1-9.2 / §10 / §11.1）。

模块职责（集成 Task 1-7 全部模块，spec §7.2 十二步逐项对齐，对照表见任务报告）：
- :func:`create_app` —— 工厂：``POST /v1/chat/completions``（OpenAI）、
  ``POST /v1/messages``（Anthropic）、``GET /v1/models``（auto + 全部别名，§7.1）；
  Task 9 起加 admin 读端点（§9.1 引导序列七 GET + ``/admin/traffic/{id}``）、
  ``GET /metrics``（§11.1 手工 OpenMetrics）；Task 10 起加 admin 写端点
  （tree 整树替换/新增/删除/审计/试算/预估/模板、models 接入/修改/探测、
  settings/bands、export/import，§9 表 + §9.4 探测序列）；控制台托管
  ``GET /`` 归 Task 12。
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
import os
import time
import uuid
from datetime import datetime, timedelta

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

import sr.db as db
from sr.adapters import UpstreamError
from sr.adapters import openai as oai
from sr.adapters import anthropic as anth
from sr.adapters.anthropic import StreamState
from sr.engine import (Branch, Node, RouteDecision, TEMPLATES, evaluate,
                       evaluate_trace, extract_session, resolve_session,
                       rows_to_tree)
from sr.health import run_checkup
from sr.i18n import MESSAGES, detect_lang
from sr.models import is_circuit_open, record_failure, record_success, resolve_backend
from sr.tags import DEFAULT_CONF_THRESHOLDS, TAGS, conf_band, tag_all

# 两协议适配器（§7.1 按路径判定 proto → 同构七件套）
ADAPTERS = {"openai": oai, "anthropic": anth}

# 客户端 anthropic 的 ping 心跳间隔（§7.4「15s ping」逐字）
PING_INTERVAL_S = 15.0
# anthropic 客户端 max_tokens 缺失时的出站兜底（裁定 9）
DEFAULT_MAX_TOKENS = 1024
# 反馈解锁的显式头取值（§4.4 反馈来源 1：x-sr-feedback）
FEEDBACK_VALUES = {"satisfied", "need_clarification", "wrong_answer", "want_different"}

# §9.4 流式首包断言预算（秒）：「大量故障只在流式暴露」，首包 3s 内必须到。
STREAM_FIRST_BYTE_S = 3.0

# 影响预估（POST /admin/tree/estimate）的均摊 token 假设：重放只有标签包、没有
# 真实 usage，成本按 价目 × 以下常量 折算（§9.2 cost_delta 口径）；复盘回填
# estimates.actual 时按 requests.cost 实算，不经此常量（§11.4）。
AVG_IN = 800        # 平均输入 tokens / 请求
AVG_OUT = 600       # 平均输出 tokens / 请求

# admin 写端点的 changes.actor（§3.1 changes 表；M1 无登录体系，固定操作者）。
_WRITE_ACTOR = "admin"

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
                          primary: str | None = None, final: str | None = None,
                          decision: RouteDecision | None = None) -> None:
    """一次 /v1 请求的请求级计数（在 _log_request 的每个落账点调用）。

    - route 标签取诊断头同源值（树命中 = "N.M" 路径，否则 direct/sticky/
      unlocked/fallback，取不到记 "none"）；
    - model 标签 = 实际去向（block / 模型 id；error 行 routed_to 为 NULL → ""）；
    - primary≠final 且非试水记 sr_fallback_total（Task 10 折入项：旧实现用诊断头
      字符串 ``diag.get("x-sr-route") != "trial"`` 排除试水，而 source=trial 时该头
      是 "N.M"——恒真死代码；改按 decision.source 判定，不再依赖诊断头字符串）。
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
    if final and primary and final != primary and \
            (decision is None or decision.source != "trial"):
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
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision)
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
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision)
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
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision)
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
                              primary=decision.model, final=final_model,
                              decision=decision)
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
# 9) admin 写端点辅助：树 JSON 解析 / 重编号 / 落库 / 体检参数（Task 10，§9-§10）
# ─────────────────────────────────────────────────────────────────────────────


def _models_set(conn) -> dict[str, dict]:
    """models 表 → ``{model_id: {}}``（run_checkup 按 id 集合语义消费，简报测试
    传 ``{"qwen3-8b": {}, …}`` 即此口径）。"""
    return {r["id"]: {} for r in conn.execute("SELECT id FROM models")}


def _hits_30d(conn) -> dict[str, int] | None:
    """近 30 天 hit_branch → 命中数（check_no_hits 数据源，"N.M" = 节点 idx +
    分支位置序号，Task 5 裁定）；窗口内无任何流量 → None（体检跳过该检查：
    零流量报「全部无命中」是噪声不是结论）。"""
    cutoff = (datetime.now().astimezone()
              - timedelta(days=30)).isoformat(timespec="seconds")
    total = conn.execute("SELECT COUNT(*) c FROM requests WHERE ts >= ?",
                         (cutoff,)).fetchone()["c"]
    if not total:
        return None
    return {r["hit_branch"]: r["c"] for r in conn.execute(
        "SELECT hit_branch, COUNT(*) c FROM requests "
        "WHERE ts >= ? AND hit_branch IS NOT NULL GROUP BY hit_branch", (cutoff,))}


def _take_idx(idx, require_idx: bool, seen: set) -> int:
    """idx 校验：整数、（require_idx 时）必填、全树唯一（tree.idx 是全表主键，
    节点/分支共用一个编号空间，§3.1）。非法抛 ValueError（端点转 400）。"""
    if not isinstance(idx, int) or isinstance(idx, bool):
        if require_idx:
            raise ValueError(f"idx must be an integer: {idx!r}")
        return -1                                  # 候选节点/estimate 占位
    if idx in seen:
        raise ValueError(f"duplicate idx: {idx}")
    seen.add(idx)
    return idx


def _branch_from_json(obj, *, require_idx: bool, seen: set) -> Branch:
    """§9.2 分支 JSON → Branch（cond/action 原样收，零剥键——GET→PUT 回环无损）。"""
    if not isinstance(obj, dict):
        raise ValueError("branch must be an object")
    idx = _take_idx(obj.get("idx"), require_idx, seen)
    cond = obj.get("cond")
    action = obj.get("action")
    if not isinstance(cond, list):
        raise ValueError(f"branch {idx}: cond must be a list")
    if not isinstance(action, dict):
        raise ValueError(f"branch {idx}: action must be an object")
    kids = [_branch_from_json(k, require_idx=require_idx, seen=seen)
            for k in (obj.get("kids") or [])]
    if len(kids) > 9:
        raise ValueError(f"branch {idx}: more than 9 kids (idx convention, §9.2)")
    trial = obj.get("trial")
    if trial is not None and not isinstance(trial, dict):
        raise ValueError(f"branch {idx}: trial must be an object or null")
    return Branch(idx=idx, cond=cond, action=action, trial=trial,
                  enabled=bool(obj.get("enabled", True)), kids=kids)


def _node_from_json(obj, *, require_idx: bool, seen: set) -> Node:
    """§9.2 节点 JSON → Node。``kind`` 不入参（读侧由 rows_to_tree 按内容推导，
    Task 9 口径）；候选节点（POST nodes / estimate）可无 idx（require_idx=False）。"""
    if not isinstance(obj, dict):
        raise ValueError("node must be an object")
    idx = _take_idx(obj.get("idx"), require_idx, seen)
    branches = [_branch_from_json(b, require_idx=require_idx, seen=seen)
                for b in (obj.get("branches") or [])]
    if len(branches) > 9:
        raise ValueError(f"node {idx}: more than 9 branches (idx convention, §9.2)")
    return Node(idx=idx, kind=str(obj.get("kind") or "normal"),
                name=str(obj.get("name") or ""), desc=obj.get("desc"),
                enabled=bool(obj.get("enabled", True)), branches=branches)


def _tree_from_json(body) -> list[Node]:
    """PUT /admin/tree（或 import 的 tree 节）请求体 → Node 列表。

    形状非法抛 ValueError（端点统一转 400）：body 是对象、nodes 非空列表、
    全行 idx 整数且唯一、每节点分支数/每分支子分支数 ≤ 9（「父 idx*10 + 段内
    序号」的段内上限，§9.2）。GET 附带的 version/kind 等键忽略。"""
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), list) \
            or not body["nodes"]:
        raise ValueError("body must be {nodes: [...]} with at least one node")
    seen: set = set()
    return [_node_from_json(n, require_idx=True, seen=seen) for n in body["nodes"]]


def _renumber(nodes: list[Node]) -> None:
    """位置序 → idx（§9.2 约定：分支 = 父 idx*10 + 段内序号；节点 0 的 0 段与
    顶层节点号冲突，特例落 100+序号——seed 同款）。全树 ≤ 10 节点、每段 ≤ 9 子
    （调用方校验）时编号空间互不重叠。"""
    for i, n in enumerate(nodes):
        n.idx = i
        for j, b in enumerate(n.branches):
            b.idx = (100 + j) if i == 0 else (i * 10 + j)
            for k, kid in enumerate(b.kids):
                kid.idx = b.idx * 10 + k


def _write_branch_rows(conn, parent_idx: int, b: Branch) -> None:
    """分支行 INSERT（kids 递归；分支行 name 存空串——显示名由控制台按 cond
    渲染，§3.1；updated_at/by 记审计面）。"""
    conn.execute(
        "INSERT INTO tree (idx, parent, name, desc, cond, action, trial, enabled, "
        "updated_at, updated_by) VALUES (?, ?, '', NULL, ?, ?, ?, ?, ?, ?)",
        (b.idx, parent_idx, db._dump(b.cond), db._dump(b.action),
         None if b.trial is None else db._dump(b.trial),
         1 if b.enabled else 0, db._now(), _WRITE_ACTOR))
    for kid in b.kids:
        _write_branch_rows(conn, b.idx, kid)


def _write_tree_rows(conn, nodes: list[Node]) -> None:
    """整树落库（事务由调用方 commit）：先 DELETE 后按 §3.1 行结构 INSERT。
    节点行 cond/action 为 NOT NULL 占位（'[]' / 'null'，init_db._seed 同款）。"""
    conn.execute("DELETE FROM tree")
    for n in nodes:
        conn.execute(
            "INSERT INTO tree (idx, parent, name, desc, cond, action, trial, "
            "enabled, updated_at, updated_by) "
            "VALUES (?, NULL, ?, ?, '[]', 'null', NULL, ?, ?, ?)",
            (n.idx, n.name, n.desc, 1 if n.enabled else 0, db._now(), _WRITE_ACTOR))
        for b in n.branches:
            _write_branch_rows(conn, n.idx, b)


def _checkup_or_422(conn, nodes: list[Node]):
    """保存前体检（§10 强制行为）：有 hard → 422 ``{"findings": […]}``（硬错误
    优先排序，控制台首屏直指拦截原因；全量 findings 由 /admin/tree/audit 提供）；
    通过 → None。"""
    findings = run_checkup(nodes, _models_set(conn), TAGS, hits=_hits_30d(conn))
    if any(f.level == "hard" for f in findings):
        ordered = sorted(findings, key=lambda f: f.level != "hard")
        return JSONResponse({"findings": [f.to_json() for f in ordered]},
                            status_code=422)
    return None


def _signal_from_row(tag: str, raw: str, confidence, is_rule) -> dict:
    """tags 行 → 引擎可求值的信号包（tags 表重放的唯一入口：try 的
    request_id 路径与 estimate 重放共用）。

    值形态按标签 kind 复原（§3.1 存储约定 × engine 求值需求）：ordered 标签
    存档位索引的数字串（_store_tags 对非字符串 json.dumps → "1"），_tag_value
    只反序列化 [/{ 开头的串——裸数字串须按 kind 转 int，否则 engine 的
    is/gte/lte 整数比较（§5.1）恒 False；multi（pii）的 JSON 数组由 _tag_value
    解析；choice/noul/free 原样字符串。未知标签保守原样（不过求值会 miss）。"""
    value = _tag_value(raw)
    spec = TAGS.get(tag)
    if spec is not None and spec.kind == "ordered" and isinstance(value, str) \
            and value.lstrip("-").isdigit():
        value = int(value)
    return {"value": value, "confidence": confidence, "is_rule": is_rule}


def _est_cost(model: str | None, prices: dict) -> tuple[float | None, bool]:
    """重放样本的单次成本估算（AVG_IN/AVG_OUT 假设见模块常量注释）。

    返回 (成本, 价格缺失)。model=None（防御性 fallback，本就没服务）按 0 计；
    模型不在价目表或价格 NULL → 价格缺失（调用方把 cost_delta 置 null，§9.2）。"""
    if model is None:
        return 0.0, False
    if model not in prices:
        return None, True
    pin, pout = prices[model]
    if pin is None or pout is None:
        return None, True
    return (pin * AVG_IN + pout * AVG_OUT) / 1_000_000, False


# ─────────────────────────────────────────────────────────────────────────────
# 10) create_app —— 端点装配（§7.1 /v1 + §9.1-9.2 admin 读端点 + Task 10 写端点
#     + /metrics；控制台托管 GET / 归 Task 12）
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

    # ── admin 写端点（§9 表「写」各行 + §10 保存前体检 + §9.4 探测；Task 10）──
    @app.put("/admin/tree")
    async def admin_put_tree(request: Request):
        """整树替换（§9/§10）：请求体 = GET /admin/tree 同形状 → 体检（hard
        422 拦截，findings 硬错误优先）→ 过则事务内 DELETE+INSERT 整树 +
        ``tree_version+1`` + changes 审计。写侧与 Task 9 读侧同样零剥键，
        GET→PUT 回环无损（含 kids 子分支行）。"""
        try:
            body = await request.json()
            nodes = _tree_from_json(body)
        except Exception as exc:                   # JSON 或形状非法 → 400
            return JSONResponse({"detail": f"invalid tree payload: {exc}"},
                                status_code=400)
        conn = db.get(db_path)
        try:
            blocked = _checkup_or_422(conn, nodes)
            if blocked is not None:
                return blocked
            old = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
            _write_tree_rows(conn, nodes)
            db.bump_tree_version(conn)
            db.log_change(conn, _WRITE_ACTOR, "tree", "*",
                          before={"version": old["version"], "nodes": old["nodes"]},
                          after={"nodes": body["nodes"]})
            conn.commit()
            version = db.get_tree_version(conn)
        finally:
            conn.close()
        return JSONResponse(_tree_json(nodes, version))

    @app.post("/admin/tree/audit")
    async def admin_tree_audit():
        """体检当前树、不写库（§9）：findings 按 §10 CHECKS 顺序全量返回。"""
        conn = db.get(db_path)
        try:
            findings = run_checkup(_load_tree(conn, db_path), _models_set(conn),
                                   TAGS, hits=_hits_30d(conn))
        finally:
            conn.close()
        return JSONResponse({"findings": [f.to_json() for f in findings]})

    @app.post("/admin/tree/try")
    async def admin_tree_try(request: Request):
        """试算（§9.2）：``{text}`` 现打标或 ``{request_id}`` 取入库信号包重放；
        复用 tag_all + evaluate_trace（§5.2 试算形状），不调上游。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "request body must be a JSON object"},
                                status_code=400)
        conn = db.get(db_path)
        try:
            cuts = _conf_cuts(conn)
            rid = body.get("request_id")
            if rid is not None:
                rows = conn.execute(
                    "SELECT tag, value, confidence, is_rule FROM tags "
                    "WHERE request_id = ? ORDER BY rowid", (rid,)).fetchall()
                if not rows:
                    return JSONResponse({"detail": "request not found"},
                                        status_code=404)
                signals = {r["tag"]: _signal_from_row(r["tag"], r["value"],
                                                      r["confidence"],
                                                      r["is_rule"])
                           for r in rows}
                answer_ms = 0.0                  # 重放无打标耗时（§9.2 answer_ms）
            else:
                text = body.get("text")
                if not isinstance(text, str):
                    return JSONResponse(
                        {"detail": "body requires \"text\" or \"request_id\""},
                        status_code=400)
                t1 = time.monotonic()
                signals = tag_all(text, dict(request.headers), 1,
                                  datetime.now().astimezone(), None)
                answer_ms = (time.monotonic() - t1) * 1000
            trace = evaluate_trace(_load_tree(conn, db_path), signals, cuts)
        finally:
            conn.close()
        d = trace["decision"]
        return JSONResponse({
            "tagger": tagger,
            "tags": [{"tag": t, "value": s["value"],
                      "confidence": s["confidence"],
                      "conf_band": None if s["confidence"] is None
                      else conf_band(s["confidence"], cuts),
                      "is_rule": bool(s.get("is_rule"))}
                     for t, s in signals.items()],
            "path": trace["path"], "also_match": trace["also_match"],
            "decision": {"kind": d.kind, "model": d.model, "think": d.think,
                         "fallback": d.fallbacks, "source": d.source,
                         "hit_path": d.hit_path},
            "answer_ms": round(answer_ms, 1)})

    @app.post("/admin/tree/estimate")
    async def admin_tree_estimate(request: Request):
        """影响预估（§9.2）：近 N 天入库标签重放「现树 vs 现树在 pos 插候选」。

        - 样本 = 窗口内 requests×tags 信号包（沿用/直通行无全量标签，天然缺席）；
        - 改道 = 候选节点成为新树命中点（hit_path 首位 == 候选）；
        - from_fallback = 改道样本的原去向是兜底（命中兜底节点或防御性
          fallback），from_other = 其余改道；
        - cost_delta = Σ(新成本 − 原成本)，成本按 models.price_in/out ×
          AVG_IN=800 / AVG_OUT=600 折算（均摊假设，常量注释见模块头）；
          任一涉及模型价格缺失 → null（§9.2）。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "request body must be a JSON object"},
                                status_code=400)
        try:
            cand = _node_from_json(body.get("node"), require_idx=False, seen=set())
        except ValueError as exc:
            return JSONResponse({"detail": f"invalid candidate node: {exc}"},
                                status_code=400)
        pos, days = body.get("pos"), body.get("days", 7)
        if not isinstance(pos, int) or isinstance(pos, bool) \
                or not isinstance(days, int) or isinstance(days, bool) or days < 1:
            return JSONResponse(
                {"detail": "pos must be an integer and days a positive integer"},
                status_code=400)
        conn = db.get(db_path)
        try:
            nodes = _load_tree(conn, db_path)
            if not 1 <= pos <= len(nodes):
                return JSONResponse(
                    {"detail": f"pos out of range 1..{len(nodes)}"}, status_code=400)
            cand.idx = 10 ** 6 + pos            # 临时 idx：不与现树冲突、仅定位用
            cand_nodes = list(nodes[:pos]) + [cand] + list(nodes[pos:])
            cuts = _conf_cuts(conn)
            prices = {r["id"]: (r["price_in"], r["price_out"])
                      for r in conn.execute(
                          "SELECT id, price_in, price_out FROM models")}
            cutoff = (datetime.now().astimezone()
                      - timedelta(days=days)).isoformat(timespec="seconds")
            packs: dict[str, dict] = {}
            for r in conn.execute(
                    "SELECT t.request_id rid, t.tag, t.value, t.confidence, "
                    "t.is_rule FROM tags t JOIN requests r ON r.id = t.request_id "
                    "WHERE r.ts >= ? ORDER BY t.rowid", (cutoff,)):
                packs.setdefault(r["rid"], {})[r["tag"]] = _signal_from_row(
                    r["tag"], r["value"], r["confidence"], r["is_rule"])
            fb_idx = {n.idx for n in nodes if n.kind == "fallback"}
            total = diverted = from_fb = 0
            cost_delta = 0.0
            price_missing = False
            for signals in packs.values():
                total += 1
                base = evaluate(nodes, signals, cuts)
                new = evaluate(cand_nodes, signals, cuts)
                if not new.hit_path or new.hit_path[0] != cand.idx:
                    continue                    # 候选节点没抢到流量
                diverted += 1
                if base.source == "fallback" or \
                        (base.hit_path and base.hit_path[0] in fb_idx):
                    from_fb += 1                # 原去向是兜底（节点或防御决定）
                c0, miss0 = _est_cost(base.model, prices)
                c1, miss1 = _est_cost(new.model, prices)
                price_missing = price_missing or miss0 or miss1
                if c0 is not None and c1 is not None:
                    cost_delta += c1 - c0
        finally:
            conn.close()
        return JSONResponse({"sample_days": days,
                             "daily": round(total / days, 2),
                             "ratio": round(diverted / total, 4) if total else 0.0,
                             "from_fallback": from_fb,
                             "from_other": diverted - from_fb,
                             "cost_delta": None if price_missing
                             else round(cost_delta, 6)})

    @app.get("/admin/tree/estimates")
    async def admin_tree_estimates():
        """预估列表（§11.4 复盘）：estimates 全行，actual NULL = 未复盘。"""
        conn = db.get(db_path)
        try:
            rows = conn.execute("SELECT * FROM estimates "
                                "ORDER BY ts DESC, rowid DESC").fetchall()
        finally:
            conn.close()
        return JSONResponse([{"id": r["id"], "ts": r["ts"], "node": r["node"],
                              "params": json.loads(r["params"]),
                              "est": json.loads(r["est"]),
                              "actual": json.loads(r["actual"]) if r["actual"]
                              else None} for r in rows])

    @app.post("/admin/tree/nodes")
    async def admin_tree_add_node(request: Request):
        """新增节点（§9）：候选插到求值位置 pos。树表以 idx 为序（§3.1），要使
        「插在 pos」对求值顺序真实生效必须重编后继节点 idx——否则预估口径与实际
        求值顺序脱节；代价是 requests.hit_branch 历史行失配（M1 取舍，记任务
        报告）。sticky 节点必须保持 idx 0（rows_to_tree 按 idx==0 推导），故
        pos ≥ 1。带 estimate 时写 estimates 表快照（§9.2 末段 / §11.4）。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "request body must be a JSON object"},
                                status_code=400)
        try:
            cand = _node_from_json(body.get("node"), require_idx=False, seen=set())
        except ValueError as exc:
            return JSONResponse({"detail": f"invalid node: {exc}"}, status_code=400)
        pos = body.get("pos")
        if not isinstance(pos, int) or isinstance(pos, bool):
            return JSONResponse({"detail": "pos must be an integer"}, status_code=400)
        conn = db.get(db_path)
        try:
            nodes = sorted(_load_tree(conn, db_path), key=lambda n: n.idx)
            if not 1 <= pos <= len(nodes):
                return JSONResponse(
                    {"detail": f"pos out of range 1..{len(nodes)}"}, status_code=400)
            if len(nodes) >= 10:
                return JSONResponse(
                    {"detail": "tree exceeds the M1 idx convention (10 nodes)"},
                    status_code=400)
            new_nodes = nodes[:pos] + [cand] + nodes[pos:]
            _renumber(new_nodes)
            blocked = _checkup_or_422(conn, new_nodes)
            if blocked is not None:
                return blocked
            old = _tree_json(nodes, db.get_tree_version(conn))
            _write_tree_rows(conn, new_nodes)
            db.bump_tree_version(conn)
            if "estimate" in body:
                if not isinstance(body["estimate"], dict):
                    return JSONResponse(
                        {"detail": "estimate must be the object returned by "
                                   "POST /admin/tree/estimate"}, status_code=400)
                conn.execute(
                    "INSERT INTO estimates (id, ts, node, params, est, actual) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (uuid.uuid4().hex, db._now(), str(cand.idx),
                     db._dump(body["node"]), db._dump(body["estimate"])))
            db.log_change(conn, _WRITE_ACTOR, "tree", str(cand.idx),
                          before={"version": old["version"], "nodes": old["nodes"]},
                          after={"pos": pos, "node": body.get("node")})
            conn.commit()
            version = db.get_tree_version(conn)
        finally:
            conn.close()
        return JSONResponse(_tree_json(new_nodes, version))

    @app.delete("/admin/tree/nodes/{idx}")
    async def admin_tree_delete_node(idx: int):
        """删除节点（§9）：连带其分支行；删后体检，hard 拦截（如删兜底 → 422）。"""
        conn = db.get(db_path)
        try:
            nodes = sorted(_load_tree(conn, db_path), key=lambda n: n.idx)
            victim = next((n for n in nodes if n.idx == idx), None)
            if victim is None:
                return JSONResponse({"detail": f"node {idx} not found"},
                                    status_code=404)
            rest = [n for n in nodes if n is not victim]
            blocked = _checkup_or_422(conn, rest)
            if blocked is not None:
                return blocked
            old = _tree_json(nodes, db.get_tree_version(conn))
            _write_tree_rows(conn, rest)
            db.bump_tree_version(conn)
            db.log_change(conn, _WRITE_ACTOR, "tree", str(idx),
                          before={"version": old["version"], "nodes": old["nodes"]},
                          after=None)
            conn.commit()
            version = db.get_tree_version(conn)
        finally:
            conn.close()
        return JSONResponse(_tree_json(rest, version))

    @app.post("/admin/tree/template")
    async def admin_tree_template(request: Request):
        """切换模板（§5.3）：TEMPLATES[name] 整树替换 + version+1 + 审计；
        未知模板名 400。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        name = body.get("name") if isinstance(body, dict) else None
        if name not in TEMPLATES:
            return JSONResponse(
                {"detail": f"unknown template: {name!r} (known: {sorted(TEMPLATES)})"},
                status_code=400)
        conn = db.get(db_path)
        try:
            old = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
            conn.execute("DELETE FROM tree")
            for node in TEMPLATES[name]:
                conn.execute(
                    "INSERT INTO tree (idx, parent, name, desc, cond, action, "
                    "trial, enabled) VALUES (?, NULL, ?, ?, '[]', 'null', NULL, 1)",
                    (node["idx"], node["name"], node.get("desc")))
                for branch in node.get("branches", []):
                    db._seed_branch(conn, node["idx"], branch)   # 与 init_db 同一条落库路径
            db.bump_tree_version(conn)
            db.log_change(conn, _WRITE_ACTOR, "tree", "template",
                          before={"version": old["version"], "nodes": old["nodes"]},
                          after={"template": name})
            conn.commit()
            version = db.get_tree_version(conn)
        finally:
            conn.close()
        return JSONResponse({"version": version, "template": name})

    @app.post("/admin/models")
    async def admin_add_model(request: Request):
        """接入新模型（§9）：proto 跟随 provider（§8.1）；provider 不存在 400、
        id 已存在 409；写 changes 审计。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "request body must be a JSON object"},
                                status_code=400)
        mid, provider, upstream = body.get("id"), body.get("provider"), \
            body.get("upstream")
        if not (isinstance(mid, str) and mid and isinstance(provider, str)
                and isinstance(upstream, str) and upstream):
            return JSONResponse(
                {"detail": "id/provider/upstream are required strings"},
                status_code=400)
        caps, aliases = body.get("caps", []), body.get("aliases", [])
        if not isinstance(caps, list) or not isinstance(aliases, list):
            return JSONResponse({"detail": "caps/aliases must be lists"},
                                status_code=400)
        step = body.get("step")
        if step is not None and (not isinstance(step, int) or isinstance(step, bool)):
            return JSONResponse({"detail": "step must be an integer or null"},
                                status_code=400)
        price_in, price_out = body.get("price_in"), body.get("price_out")
        for p in (price_in, price_out):
            if (p is not None and not isinstance(p, (int, float))) \
                    or isinstance(p, bool):
                return JSONResponse(
                    {"detail": "price_in/price_out must be numbers or null"},
                    status_code=400)
        enabled = bool(body.get("enabled", True))
        conn = db.get(db_path)
        try:
            prov = conn.execute("SELECT proto FROM providers WHERE name = ?",
                                (provider,)).fetchone()
            if prov is None:
                return JSONResponse({"detail": f"unknown provider: {provider!r}"},
                                    status_code=400)
            if conn.execute("SELECT 1 FROM models WHERE id = ?", (mid,)).fetchone():
                return JSONResponse({"detail": f"model exists: {mid}"},
                                    status_code=409)
            conn.execute(
                "INSERT INTO models (id, provider, proto, upstream, caps, aliases, "
                "step, price_in, price_out, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, provider, prov["proto"], upstream, db._dump(caps),
                 db._dump(aliases), step, price_in, price_out,
                 1 if enabled else 0))
            db.log_change(conn, _WRITE_ACTOR, "models", mid, before=None,
                          after={"id": mid, "provider": provider,
                                 "upstream": upstream, "caps": caps,
                                 "aliases": aliases, "step": step,
                                 "price_in": price_in, "price_out": price_out,
                                 "enabled": enabled})
            ladder = [s.strip() for s in
                      (_load_settings(conn).get("ladder") or "").split(",")
                      if s.strip()]
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"id": mid, "provider": provider,
                             "proto": prov["proto"], "upstream": upstream,
                             "caps": caps, "aliases": aliases, "step": step,
                             "price_in": price_in, "price_out": price_out,
                             "ok": True, "break_reason": None,
                             "enabled": enabled, "on_ladder": mid in ladder})

    @app.put("/admin/models/{mid}")
    async def admin_update_model(mid: str, request: Request):
        """修改模型（§9：阶梯/启用/停用等）：白名单字段部分更新 + 审计；
        未知字段 400、未知模型 404。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        allowed = {"upstream", "caps", "aliases", "step",
                   "price_in", "price_out", "enabled"}
        if not isinstance(body, dict) or not body or set(body) - allowed:
            return JSONResponse(
                {"detail": f"updatable fields: {sorted(allowed)}"}, status_code=400)
        if not isinstance(body.get("caps", []), list) \
                or not isinstance(body.get("aliases", []), list):
            return JSONResponse({"detail": "caps/aliases must be lists"},
                                status_code=400)
        if "step" in body and (not isinstance(body["step"], int)
                               or isinstance(body["step"], bool)) \
                and body["step"] is not None:
            return JSONResponse({"detail": "step must be an integer or null"},
                                status_code=400)
        for k in ("price_in", "price_out"):
            if k in body and ((not isinstance(body[k], (int, float))
                               and body[k] is not None)
                              or isinstance(body[k], bool)):
                return JSONResponse(
                    {"detail": f"{k} must be a number or null"}, status_code=400)
        conn = db.get(db_path)
        try:
            row = conn.execute("SELECT * FROM models WHERE id = ?", (mid,)).fetchone()
            if row is None:
                return JSONResponse({"detail": f"model not found: {mid}"},
                                    status_code=404)
            before = {k: row[k] for k in row.keys()}
            sets, params = [], []
            for k, v in body.items():
                if k in ("caps", "aliases"):
                    v = db._dump(v)
                elif k == "enabled":
                    v = 1 if v else 0
                sets.append(f"{k} = ?")
                params.append(v)
            conn.execute(f"UPDATE models SET {', '.join(sets)} WHERE id = ?",
                         [*params, mid])
            db.log_change(conn, _WRITE_ACTOR, "models", mid, before=before,
                          after={**before, **body})
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"ok": True})

    @app.post("/admin/models/{mid}/test")
    async def admin_model_test(mid: str):
        """接入测试（§9.4 两步探测，设计方案 B5）：

        ① ``connect``——非流式连通：openai → ``GET {base}/models``；
           anthropic → 最小 ``POST /v1/messages``；
        ② ``stream_first_byte``——发 ``stream: true`` 最小请求，断言
           :data:`STREAM_FIRST_BYTE_S` 秒内收到第一个数据帧。

        逐步返回 ``{name, ok, ms, detail}``；上游不可达/超时 → 该步 ok=false
        （整体 ``ok`` 为 false，HTTP 仍 200——探测失败是结论不是故障）。key_env
        未设置时不带鉴权头（对真上游会 401 → ok=false，也是有效探测结论）。
        """
        conn = db.get(db_path)
        try:
            model = conn.execute("SELECT * FROM models WHERE id = ?",
                                 (mid,)).fetchone()
            if model is None:
                return JSONResponse({"detail": f"model not found: {mid}"},
                                    status_code=404)
            prov = conn.execute("SELECT * FROM providers WHERE name = ?",
                                (model["provider"],)).fetchone()
        finally:
            conn.close()
        if prov is None:
            return JSONResponse(
                {"detail": f"provider missing: {model['provider']}"}, status_code=400)
        key = os.environ.get(prov["key_env"] or "")
        proto = prov["proto"]
        headers: dict = {}
        if key:
            headers = ({"Authorization": f"Bearer {key}"} if proto == "openai"
                       else {"x-api-key": key,
                             "anthropic-version": "2023-06-01"})
        base = prov["base_url"].rstrip("/")
        steps: list[dict] = []
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            # ── step1 非流式连通（§9.4-1）──
            t0 = time.monotonic()
            try:
                if proto == "openai":
                    url = f"{base}/models"
                    resp = await client.get(url, headers=headers)
                else:
                    url = f"{base}/v1/messages"
                    resp = await client.post(url, headers=headers, json={
                        "model": model["upstream"], "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}]})
                steps.append({"name": "connect",
                              "ok": 200 <= resp.status_code < 300,
                              "ms": round((time.monotonic() - t0) * 1000, 1),
                              "detail": f"HTTP {resp.status_code} {url}"})
            except httpx.HTTPError as exc:
                steps.append({"name": "connect", "ok": False,
                              "ms": round((time.monotonic() - t0) * 1000, 1),
                              "detail": f"unreachable: {exc}"})
            # ── step2 流式首包（§9.4-2：3s 内第一个数据帧）──
            t0 = time.monotonic()
            url, payload = \
                (f"{base}/chat/completions", {"model": model["upstream"],
                                              "stream": True, "max_tokens": 1,
                                              "messages": [{"role": "user",
                                                            "content": "ping"}]}) \
                if proto == "openai" else \
                (f"{base}/v1/messages", {"model": model["upstream"],
                                         "stream": True, "max_tokens": 1,
                                         "messages": [{"role": "user",
                                                       "content": "ping"}]})
            resp = None
            try:
                req = client.build_request("POST", url, json=payload,
                                           headers=headers)
                resp = await client.send(req, stream=True)
                if resp.status_code != 200:
                    steps.append({"name": "stream_first_byte", "ok": False,
                                  "ms": round((time.monotonic() - t0) * 1000, 1),
                                  "detail": f"HTTP {resp.status_code} {url}"})
                else:
                    first = await asyncio.wait_for(
                        resp.aiter_bytes().__anext__(),
                        timeout=STREAM_FIRST_BYTE_S)
                    steps.append({"name": "stream_first_byte", "ok": bool(first),
                                  "ms": round((time.monotonic() - t0) * 1000, 1),
                                  "detail": f"first frame {len(first)} bytes"})
            except asyncio.TimeoutError:
                steps.append({"name": "stream_first_byte", "ok": False,
                              "ms": round((time.monotonic() - t0) * 1000, 1),
                              "detail": f"no first frame within "
                                        f"{STREAM_FIRST_BYTE_S}s"})
            except (httpx.HTTPError, StopAsyncIteration) as exc:
                steps.append({"name": "stream_first_byte", "ok": False,
                              "ms": round((time.monotonic() - t0) * 1000, 1),
                              "detail": f"unreachable/empty: {exc}"})
            finally:
                if resp is not None:
                    await resp.aclose()
        return JSONResponse({"ok": all(s["ok"] for s in steps), "steps": steps})

    @app.put("/admin/settings")
    async def admin_put_settings(request: Request):
        """修改设置（§9）：键集必须与 DEFAULT_SETTINGS 完全一致（多/缺都 400，
        防拼写漂移）；值一律转字符串（§3.1）。``tree_version`` 系统管理（只随
        树写操作递增）：校验要求在场（GET 原样回传），写库时跳过。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "request body must be a JSON object"},
                                status_code=400)
        want, got = set(db.DEFAULT_SETTINGS), set(body)
        if got != want:
            return JSONResponse(
                {"detail": "settings key set must equal DEFAULT_SETTINGS; "
                           f"unknown: {sorted(got - want)}, "
                           f"missing: {sorted(want - got)}"},
                status_code=400)
        conn = db.get(db_path)
        try:
            before = _load_settings(conn)
            for k, v in body.items():
                if k == "tree_version":
                    continue                    # 系统管理键：不收写入口
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v)))
            db.log_change(conn, _WRITE_ACTOR, "settings", "*", before=before,
                          after=body)
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"ok": True})

    @app.put("/admin/bands")
    async def admin_put_bands(request: Request):
        """修改分档（§9）：与 GET /admin/bands 同形状回写（conf 切点 + 各标签
        档位表，只更新 thresholds、不动 keywords）；结构非法 400。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "request body is not valid JSON"},
                                status_code=400)
        if not isinstance(body, dict) \
                or not isinstance(body.get("conf_thresholds"), list) \
                or not isinstance(body.get("tag_thresholds"), list):
            return JSONResponse(
                {"detail": "body must be {conf_thresholds: [...], "
                           "tag_thresholds: [{tag, thresholds}]}"},
                status_code=400)
        cuts = body["conf_thresholds"]
        if not cuts or len(set(cuts)) != len(cuts) or sorted(cuts) != cuts \
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           and 0 < c < 1 for c in cuts):
            return JSONResponse(
                {"detail": "conf_thresholds must be distinct ascending "
                           "numbers in (0,1)"}, status_code=400)
        tag_rows = []
        for t in body["tag_thresholds"]:
            if not isinstance(t, dict) or not isinstance(t.get("tag"), str) \
                    or not isinstance(t.get("thresholds"), list) \
                    or not all(isinstance(x, (int, float))
                               and not isinstance(x, bool)
                               for x in t["thresholds"]):
                return JSONResponse(
                    {"detail": f"invalid tag_thresholds item: {t!r}"},
                    status_code=400)
            tag_rows.append((t["tag"], db._dump(t["thresholds"])))
        conn = db.get(db_path)
        try:
            before = {"conf_thresholds": _conf_cuts(conn),
                      "tag_thresholds": [
                          {"tag": r["tag"], "thresholds": json.loads(r["thresholds"])}
                          for r in conn.execute(
                              "SELECT tag, thresholds FROM tag_settings ORDER BY tag")]}
            conn.execute(
                "INSERT INTO conf_settings (id, thresholds) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET thresholds = excluded.thresholds",
                (db._dump(cuts),))
            for tag_, th in tag_rows:
                conn.execute(
                    "INSERT INTO tag_settings (tag, thresholds) VALUES (?, ?) "
                    "ON CONFLICT(tag) DO UPDATE SET thresholds = excluded.thresholds",
                    (tag_, th))
            db.log_change(conn, _WRITE_ACTOR, "bands", "*", before=before,
                          after=body)
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"ok": True})

    @app.get("/admin/export")
    async def admin_export():
        """导出 YAML（§9.2：providers/models/tree/settings 四节；pyyaml
        safe_dump，依赖白名单 §13 明列）。providers/models 只导可编辑配置——
        ok/break_reason 是熔断运行态，不进配置包。"""
        conn = db.get(db_path)
        try:
            providers = [{"name": r["name"], "proto": r["proto"],
                          "base_url": r["base_url"], "key_env": r["key_env"],
                          "extra": json.loads(r["extra"] or "{}")}
                         for r in conn.execute(
                             "SELECT * FROM providers ORDER BY name")]
            models = [{"id": r["id"], "provider": r["provider"],
                       "upstream": r["upstream"],
                       "caps": json.loads(r["caps"] or "[]"),
                       "aliases": json.loads(r["aliases"] or "[]"),
                       "step": r["step"], "price_in": r["price_in"],
                       "price_out": r["price_out"], "enabled": bool(r["enabled"])}
                      for r in conn.execute("SELECT * FROM models ORDER BY id")]
            tree = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
            settings = _load_settings(conn)
        finally:
            conn.close()
        text = yaml.safe_dump({"providers": providers, "models": models,
                               "tree": tree, "settings": settings},
                              allow_unicode=True, sort_keys=False)
        return PlainTextResponse(text, media_type="application/x-yaml")

    @app.post("/admin/import")
    async def admin_import(request: Request):
        """导入 YAML（§9）：normalize 全量校验（四节结构 + 字段/值域/引用）+
        run_checkup，hard 整包拒绝（422，不部分写入）；过则单事务写入
        providers → models → settings → tree（UPSERT，不删既有行）。"""
        raw = await request.body()
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return JSONResponse({"detail": f"invalid YAML: {exc}"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse(
                {"detail": "YAML package must be a mapping with "
                           "providers/models/tree/settings"}, status_code=400)
        missing = [s for s in ("providers", "models", "tree", "settings")
                   if s not in data]
        if missing:
            return JSONResponse({"detail": f"missing sections: {missing}"},
                                status_code=400)
        provs = []
        for p in data["providers"]:
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) \
                    or p.get("proto") not in ("openai", "anthropic") \
                    or not isinstance(p.get("base_url"), str):
                return JSONResponse(
                    {"detail": f"invalid provider row: {p!r}"}, status_code=400)
            extra = p.get("extra")
            if not isinstance(extra, dict):
                extra = {}
            provs.append((p["name"], p["proto"], p["base_url"],
                          p.get("key_env"), db._dump(extra)))
        prov_names = {p[0] for p in provs}
        model_rows = []
        for m in data["models"]:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str) \
                    or not isinstance(m.get("provider"), str) \
                    or not isinstance(m.get("upstream"), str):
                return JSONResponse({"detail": f"invalid model row: {m!r}"},
                                    status_code=400)
            if m["provider"] not in prov_names:
                return JSONResponse(
                    {"detail": f"model {m['id']}: unknown provider "
                               f"{m['provider']!r}"}, status_code=400)
            caps, aliases = m.get("caps", []), m.get("aliases", [])
            if not isinstance(caps, list) or not isinstance(aliases, list):
                return JSONResponse(
                    {"detail": f"model {m['id']}: caps/aliases must be lists"},
                    status_code=400)
            step = m.get("step")
            if step is not None and (not isinstance(step, int)
                                     or isinstance(step, bool)):
                return JSONResponse(
                    {"detail": f"model {m['id']}: step must be int or null"},
                    status_code=400)
            model_rows.append((m["id"], m["provider"], m["upstream"],
                               db._dump(caps), db._dump(aliases), step,
                               m.get("price_in"), m.get("price_out"),
                               1 if m.get("enabled", True) else 0))
        settings = data["settings"]
        if not isinstance(settings, dict) \
                or set(settings) - set(db.DEFAULT_SETTINGS):
            return JSONResponse(
                {"detail": "settings keys must be within DEFAULT_SETTINGS"},
                status_code=400)
        try:
            nodes = _tree_from_json(data["tree"])
        except ValueError as exc:
            return JSONResponse({"detail": f"invalid tree: {exc}"}, status_code=400)
        conn = db.get(db_path)
        try:
            model_ids = set(_models_set(conn)) | {r[0] for r in model_rows}
            findings = run_checkup(nodes, {m: {} for m in model_ids}, TAGS)
            if any(f.level == "hard" for f in findings):
                ordered = sorted(findings, key=lambda f: f.level != "hard")
                return JSONResponse(
                    {"findings": [f.to_json() for f in ordered]}, status_code=422)
            for name, proto, base_url, key_env, extra in provs:
                conn.execute(
                    "INSERT INTO providers (name, proto, base_url, key_env, extra) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET proto = excluded.proto, "
                    "base_url = excluded.base_url, key_env = excluded.key_env, "
                    "extra = excluded.extra",
                    (name, proto, base_url, key_env, extra))
            for mid_, provider, upstream, caps, aliases, step, pin, pout, en \
                    in model_rows:
                conn.execute(
                    "INSERT INTO models (id, provider, proto, upstream, caps, "
                    "aliases, step, price_in, price_out, enabled) "
                    "VALUES (?, ?, (SELECT proto FROM providers WHERE name = ?), "
                    "?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, "
                    "proto = excluded.proto, upstream = excluded.upstream, "
                    "caps = excluded.caps, aliases = excluded.aliases, "
                    "step = excluded.step, price_in = excluded.price_in, "
                    "price_out = excluded.price_out, enabled = excluded.enabled",
                    (mid_, provider, provider, upstream, caps, aliases, step,
                     pin, pout, en))
            for k, v in settings.items():
                if k != "tree_version":         # 系统管理键：不收写入口
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (k, str(v)))
            old = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
            _write_tree_rows(conn, nodes)
            db.bump_tree_version(conn)
            db.log_change(conn, _WRITE_ACTOR, "import", "*",
                          before={"version": old["version"], "nodes": old["nodes"]},
                          after={"sections": sorted(data)})
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"ok": True})

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
