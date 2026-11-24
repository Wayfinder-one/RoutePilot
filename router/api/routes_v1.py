"""Data-plane entry point: validate, route, hand the decision to the proxy.

This module keeps only the HTTP-facing request work:

1. request size / JSON / ``messages`` / session validation,
2. one ``RoutingService.route()`` call and its exception mapping,
3. initial request-record construction (machine fields),
4. one ``UpstreamProxy`` call for the upstream exchange, and the framework
   response around the proxy's neutral result.

Presentation lives in ``request_presenter``/``trace_presenter``; transport and
lease ownership live in ``router.upstream.proxy``.  The proxy never builds a
framework response: it returns ``ProxyResult``/``ProxyStream`` and this module
renders them.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import unquote_to_bytes

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router.api.request_presenter import (
    OUTCOME_INCOMPLETE,
    OUTCOME_ROUTING_ERROR,
    OutcomeRecorder,
    node_labels,
    present_record,
)
from router.api.trace_presenter import present_trace
from router.jev.client import ChoiceAnswer
from router.jev.serialization import answer_to_dict
from router.routing.types import (
    NoActiveTreeError,
    NoAvailableBackend,
    NoDefaultTreeError,
    RoutingMiss,
    RoutingRequest,
    session_digest,
)
from router.upstream.catalog import load_backends
from router.upstream.proxy import (
    ProxyResult,
    ProxyStream,
    UpstreamProxy,
    prepare_upstream_request,
)
from router.upstream.runtime_keys import runtime_model_key, tree_scope

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024

# 部门 Header 的严格解码只信任 ``%XX`` 形式：裸 ``%`` 或截断的百分号转义都拒绝。
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def decode_department(raw: str | None) -> str:
    """严格解码 ``x-sr-department``（管理端可信 Header，公开入口禁用）。

    ``None``/空串表示「未声明部门」，返回 ``''``。任何畸形输入（非法百分号
    转义、非 UTF-8 字节、解码后超过 64 字符、控制字符）都抛
    ``ValueError('invalid_department')``，由调用方映射成 400。
    """
    if raw is None or raw == '':
        return ''
    if len(raw) > 768 or _BAD_PERCENT.search(raw):
        raise ValueError('invalid_department')
    try:
        value = unquote_to_bytes(raw).decode('utf-8').strip()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('invalid_department') from exc
    if len(value) > 64 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError('invalid_department')
    return value


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def _json_response(result: ProxyResult) -> JSONResponse:
    """Render a neutral proxy result; the JSON bytes stay Starlette's canonical ones."""
    return JSONResponse(status_code=result.status_code, content=result.content,
                        headers=result.headers)


def _last_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _first_wave_choice(decision):
    """第一波按 node_id 排序后的首个 ChoiceAnswer（日志兼容列，spec §3.8）。"""
    first_wave = decision.waves[0] if decision.waves else None
    if first_wave is None:
        return None
    for node_id in sorted(first_wave.questions):
        answer = decision.answers.get(node_id)
        if isinstance(answer, ChoiceAnswer):
            return answer
    return None


def _decision_record(decision, snapshot, text: str, reasoning_warning,
                     routing_ms: float, started: float) -> dict:
    """Initial record for a completed routing decision (machine fields only)."""
    first_wave = decision.waves[0] if decision.waves else None
    answer = _first_wave_choice(decision)
    return {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
        "decision": "route",
        "model": decision.model,
        "target_id": decision.target_id,
        "tree_id": snapshot.tree_id,
        "tree_revision": snapshot.tree_revision,
        "snapshot_revision": snapshot.snapshot_revision,
        "policy_path": decision.policy_path,
        "selection_reason": decision.selection_reason,
        "fallback_reason": decision.fallback_reason,
        "sticky_hit": 1 if decision.sticky_hit else 0,
        "session_hash": decision.session_hash,
        "jev_model": snapshot.jev.model if first_wave is not None and snapshot.jev else None,
        "jev_choice": answer.selected_key if answer else None,
        "jev_top": answer.p_top if answer else None,
        "jev_margin": answer.margin if answer else None,
        "jev_ms": first_wave.duration_ms if first_wave is not None else None,
        "jev_probabilities": dict(answer.probabilities) if answer else None,
        "waves": len(decision.waves),
        "answers": {node_id: answer_to_dict(value) for node_id, value in decision.answers.items()},
        "reasoning": decision.reasoning,
        "reasoning_warning": reasoning_warning,
        "routing_ms": routing_ms,
        "latency_ms": (time.monotonic() - started) * 1000.0,
        "trace": list(decision.trace),
        "node_labels": node_labels(snapshot),
        "upstream_model": None,
        "upstream_status": None,
        "upstream_ms": None,
        "ttft_ms": None,
        "total_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "error_class": None,
        "outcome_state": OUTCOME_INCOMPLETE,
    }


def _routing_failure_record(snapshot, text, session_id, trace, *, error_class,
                            routing_ms, total_ms):
    # 调度失败 ≠ 图没有决策：恢复 trace 中最后一个 final 的目标元数据；
    # 无 final（图前失败）时保持 None，不从 sticky/schedule 推断成功结论。
    final = next(
        (
            event for event in reversed(trace)
            if isinstance(event, dict) and event.get("event") == "final"
        ),
        None,
    )
    target_id = final.get("target_id") if final is not None else None
    final_kind = final.get("kind") if final is not None else None
    if final_kind == "target":
        selection_reason = "target"
        fallback_reason = None
    elif final_kind in {"otherwise", "error_fallback"}:
        selection_reason = "error_fallback"
        fallback_reason = "error_fallback"
    else:
        selection_reason = None
        fallback_reason = None
    return {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
        "decision": "route",
        "model": None,
        "target_id": target_id,
        "tree_id": getattr(snapshot, "tree_id", None),
        "tree_revision": getattr(snapshot, "tree_revision", None),
        "snapshot_revision": getattr(snapshot, "snapshot_revision", None),
        "policy_path": target_id,
        "selection_reason": selection_reason,
        "fallback_reason": fallback_reason,
        "sticky_hit": 0,
        "session_hash": session_digest(session_id),
        "jev_model": snapshot.jev.model if snapshot is not None and snapshot.jev else None,
        "waves": sum(event.get("event") == "wave" for event in trace),
        "answers": {},
        "routing_ms": routing_ms,
        "latency_ms": total_ms,
        "total_ms": total_ms,
        "trace": trace,
        "node_labels": node_labels(snapshot),
        "outcome_state": OUTCOME_ROUTING_ERROR,
        "error_class": error_class,
    }


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await run_chat_completion(request)


async def run_chat_completion(request: Request, *, use_requested_model: bool = False,
                              conn=None, allow_department_header: bool = False):
    """Shared data-plane executor (``/v1`` and the admin Test chat page).

    ``use_requested_model`` is the only model-selection difference between the
    public data plane and the admin test endpoint: when false the request
    ``model`` stays ignored and routing decides; when true ``"auto"`` still
    routes, any other model is a fixed direct selection that skips
    graph/JEV/sticky.

    ``conn`` is the admin catalog connection: a fixed model resolves through the
    SQLite catalog (``load_backends``) instead of the published route set, because
    the fixed-model test is a catalog check independent of any canvas.  ``/v1``
    passes no connection, so the public hot path stays snapshot-only.

    ``allow_department_header`` gates the trusted ``x-sr-department`` header:
    the admin test chat sets it to let an operator probe department selection,
    while the public data plane rejects the header outright
    (``400 department_header_not_allowed``).  Auto requests always select a
    graph from the snapshot set (department match, else the default graph);
    fixed-model direct requests never need a route set or a default graph and
    emit no ``x-sr-graph-selection``.
    """
    started = time.monotonic()
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return _error("request_too_large", 413)
    raw_department = request.headers.get("x-sr-department")
    department = ""
    if raw_department is not None:
        if not allow_department_header:
            return _error("department_header_not_allowed", 400)
        try:
            department = decode_department(raw_department)
        except ValueError:
            return _error("invalid_department", 400)
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > MAX_BODY_BYTES:
            return _error("request_too_large", 413)
    try:
        body = json.loads(bytes(buffer))
    except Exception:
        return _error("invalid_json", 400)
    if not isinstance(body, dict):
        return _error("body_must_be_object", 400)
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return _error("messages_must_be_list", 400)
    session_id = request.headers.get("x-sr-session-id", "").strip()
    if len(session_id) > 256:
        return _error("invalid_session_id", 400)

    fixed_model = None
    if use_requested_model:
        requested_model = body.get("model", "auto")
        if not isinstance(requested_model, str) or not requested_model.strip():
            return _error("invalid_model", 422)
        requested_model = requested_model.strip()
        fixed_model = None if requested_model == "auto" else requested_model

    text = _last_user_text(messages)
    requestlog = request.app.state.requestlog
    routing_started = time.monotonic()
    trace: list[dict] = []
    # 选图失败时账本仍要落一条 snapshot=None 的失败记录：fail() 读取调用时
    # 的 snapshot，默认值必须先存在。
    snapshot = None

    def fail(error_class, status_code, stage, message):
        routing_ms = (time.monotonic() - routing_started) * 1000.0
        total_ms = (time.monotonic() - started) * 1000.0
        trace.append({
            "event": "routing_error",
            "stage": stage,
            "error_class": error_class,
            "message": message,
        })
        trace[:] = present_trace(trace)
        record = present_record(_routing_failure_record(
            snapshot, text, session_id, trace,
            error_class=error_class, routing_ms=routing_ms, total_ms=total_ms,
        ))
        requestlog.enqueue(record, snapshot=snapshot)
        return _error(error_class, status_code)

    # 公开数据面严格使用路由集默认图；管理端测试对话凭可信 Header 按部门选图。
    route_set = request.app.state.snapshot_set
    selection = None
    if fixed_model is None:
        if route_set is None:
            return fail("no_valid_snapshot", 503, "snapshot", "no valid snapshot set")
        try:
            selection = route_set.select(department)
            snapshot = selection.snapshot
        except NoActiveTreeError as exc:
            return fail("no_active_tree", 503, "snapshot", str(exc))
        except NoDefaultTreeError as exc:
            return fail("no_default_tree", 503, "snapshot", str(exc))
        try:
            decision = await request.app.state.routing_service.route(
                snapshot,
                RoutingRequest(text=text, session_id=session_id),
                trace_sink=trace,
            )
        except NoActiveTreeError as exc:
            return fail("no_active_tree", 503, "graph", str(exc))
        except NoAvailableBackend as exc:
            return fail("no_available_backend", 503, "schedule", str(exc))
        except RoutingMiss as exc:
            return fail("routing_invariant_violation", 503, "graph", str(exc))
        except Exception:
            logger.exception("routing failed")
            return fail("routing_failed", 503, "graph", "routing failed")
        backend = snapshot.backends.get(decision.model)
        # 防御性不变式：正常引擎不可达，decision.model 必来自同一快照的 backends；真到达时无请求记录。
        if backend is None:
            decision.release()
            return _error("no_available_backend", 503)
    else:
        # 固定模型直连是管理目录测试：只复用默认快照做日志上下文，不要求
        # 路由集/默认图存在；有连接时只查 SQLite 目录。
        snapshot = route_set.default_snapshot if route_set is not None else None
        direct_backends = load_backends(conn) if conn is not None else None
        if direct_backends is not None:
            backend = direct_backends.get(fixed_model)
        else:
            backend = snapshot.backends.get(fixed_model) if snapshot is not None else None
        if backend is None:
            return _error("unknown_model", 404)
        try:
            lease = request.app.state.balancer.acquire([fixed_model])
        except NoAvailableBackend:
            return _error("no_available_backend", 503)
        decision = SimpleNamespace(
            model=fixed_model,
            reasoning=None,
            selection_reason="direct",
            release=lease.release,
        )
    routing_ms = (time.monotonic() - routing_started) * 1000.0

    upstream_body, reasoning_warning = prepare_upstream_request(
        body, backend, decision.reasoning)
    trace[:] = present_trace(trace)
    if fixed_model is None:
        record = present_record(_decision_record(
            decision, snapshot, text, reasoning_warning, routing_ms, started))
    else:
        record = present_record({
            "id": uuid.uuid4().hex,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "text": text,
            "decision": "test",
            "model": fixed_model,
            "target_id": None,
            "tree_id": getattr(snapshot, "tree_id", None),
            "tree_revision": getattr(snapshot, "tree_revision", None),
            "snapshot_revision": getattr(snapshot, "snapshot_revision", None),
            "policy_path": None,
            "selection_reason": "direct",
            "fallback_reason": None,
            "sticky_hit": 0,
            "session_hash": session_digest(session_id),
            "jev_model": None,
            "jev_choice": None,
            "jev_top": None,
            "jev_margin": None,
            "jev_ms": None,
            "jev_probabilities": None,
            "waves": 0,
            "answers": {},
            "reasoning": None,
            "reasoning_warning": None,
            "routing_ms": routing_ms,
            "latency_ms": (time.monotonic() - started) * 1000.0,
            "trace": [],
            "node_labels": node_labels(snapshot),
            "upstream_model": None,
            "upstream_status": None,
            "upstream_ms": None,
            "ttft_ms": None,
            "total_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "error_class": None,
            "outcome_state": OUTCOME_INCOMPLETE,
        })
    requestlog.enqueue(record, snapshot=snapshot)
    outcome = OutcomeRecorder(requestlog, record, started)
    headers = {"x-sr-model": decision.model, "x-sr-selection": decision.selection_reason}
    if fixed_model is None:
        # 选图证据只属于 Auto 路由：固定模型直连是目录测试，不输出 x-sr-graph-selection。
        headers["x-sr-tree-id"] = snapshot.tree_id
        headers["x-sr-graph-selection"] = selection.reason
    if reasoning_warning:
        headers["x-sr-reasoning-warning"] = reasoning_warning
    elif decision.reasoning is not None:
        headers["x-sr-reasoning"] = decision.reasoning
    # 熔断/inflight 的内部键按图作用域隔离：固定模型直连保持裸模型键，Auto
    # 必须与 ModelScheduler 的 tree_scope(snapshot.tree.id) 完全一致；
    # snapshot.tree_id 就是 tree.id（跨副本稳定），固定路径可能没有路由集。
    scope = '' if fixed_model is not None else tree_scope(snapshot.tree_id)
    breaker_key = runtime_model_key(scope, decision.model)
    proxy = UpstreamProxy(request.app.state.http, request.app.state.breaker)
    if body.get("stream"):
        streamed = await proxy.stream(
            backend=backend, body=upstream_body, headers=headers,
            model=decision.model, breaker_key=breaker_key, outcome=outcome,
            is_disconnected=request.is_disconnected,
            release=decision.release,
        )
        if isinstance(streamed, ProxyStream):
            # ``streamed.body`` owns the upstream response and the lease; the
            # server closes that iterator, so the route releases nothing.
            return StreamingResponse(
                streamed.body, status_code=streamed.status_code,
                media_type=streamed.media_type, headers=streamed.headers,
            )
        return _json_response(streamed)
    return _json_response(await proxy.send(
        backend=backend, body=upstream_body, headers=headers,
        model=decision.model, breaker_key=breaker_key, outcome=outcome,
        release=decision.release,
    ))
