"""Data-plane entry point: validate, route, hand the decision to the proxy.

This module keeps only the HTTP-facing request work:

1. request size / JSON / ``messages`` / session validation,
2. one ``RoutingService.route()`` call and its exception mapping,
3. initial request-record construction (machine fields),
4. one ``UpstreamProxy`` call for the upstream exchange.

Presentation lives in ``request_presenter``/``trace_presenter``; transport and
lease ownership live in ``router.upstream.proxy``.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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
    RoutingMiss,
    RoutingRequest,
    session_digest,
)
from router.upstream.proxy import UpstreamProxy, prepare_upstream_request

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def _has_time_period(snapshot) -> bool:
    return bool(getattr(snapshot, "time_periods", ()))


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
    started = time.monotonic()
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return _error("request_too_large", 413)
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

    text = _last_user_text(messages)
    requestlog = request.app.state.requestlog
    routing_started = time.monotonic()
    trace: list[dict] = []

    def fail(error_class, status_code, stage, message):
        routing_ms = (time.monotonic() - routing_started) * 1000.0
        total_ms = (time.monotonic() - started) * 1000.0
        trace.append({
            "event": "routing_error",
            "stage": stage,
            "error_class": error_class,
            "message": message,
        })
        trace[:] = present_trace(trace, has_time_period=_has_time_period(snapshot))
        record = present_record(_routing_failure_record(
            snapshot, text, session_id, trace,
            error_class=error_class, routing_ms=routing_ms, total_ms=total_ms,
        ))
        requestlog.enqueue(record)
        return _error(error_class, status_code)

    snapshot = request.app.state.snapshot
    if snapshot is None:
        return fail("no_valid_snapshot", 503, "snapshot", "no valid snapshot")
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
    routing_ms = (time.monotonic() - routing_started) * 1000.0
    backend = snapshot.backends.get(decision.model)
    # 防御性不变式：正常引擎不可达，decision.model 必来自同一快照的 backends；真到达时无请求记录。
    if backend is None:
        decision.release()
        return _error("no_available_backend", 503)

    upstream_body, reasoning_warning = prepare_upstream_request(
        body, backend, decision.reasoning)
    trace[:] = present_trace(trace, has_time_period=_has_time_period(snapshot))
    record = present_record(_decision_record(
        decision, snapshot, text, reasoning_warning, routing_ms, started))
    requestlog.enqueue(record)
    outcome = OutcomeRecorder(requestlog, record, started)
    headers = {"x-sr-model": decision.model, "x-sr-selection": decision.selection_reason}
    if reasoning_warning:
        headers["x-sr-reasoning-warning"] = reasoning_warning
    elif decision.reasoning is not None:
        headers["x-sr-reasoning"] = decision.reasoning
    proxy = UpstreamProxy(request.app.state.http, request.app.state.breaker)
    if body.get("stream"):
        return await proxy.stream(
            backend=backend, body=upstream_body, headers=headers,
            model=decision.model, outcome=outcome, request=request,
            release=decision.release,
        )
    return await proxy.send(
        backend=backend, body=upstream_body, headers=headers,
        model=decision.model, outcome=outcome, release=decision.release,
    )
