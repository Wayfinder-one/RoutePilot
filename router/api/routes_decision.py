"""Decision-only API: the kernel's single data-plane endpoint.

``POST /internal/v1/route-decision`` runs exactly one routing decision against
the graph selected for the trusted department and returns the non-secret
decision contract both callers consume (APISIX rewrites the request ``model``;
the peripheral service resolves the provider credential itself).

Boundary rules enforced here:

* no model upstream call: ``prepare_upstream_request()`` is pure in-memory
  compatibility work, its rewritten body is thrown away;
* no user credential: ``Authorization`` is never read, so it can neither be
  forwarded nor enter the decision event;
* one self-contained event per selected graph: success events and
  ``status=routing_error`` events both carry the current graph; requests
  rejected before graph selection produce no replayable event;
* every return path releases the scheduling lease (``finally``).
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from router.api.routes_v1 import (
    MAX_BODY_BYTES,
    _decision_record,
    _last_user_text,
    _routing_failure_record,
    decode_department,
)
from router.decision_events import build_decision_event
from router.routing.types import (
    NoActiveTreeError,
    NoAvailableBackend,
    NoDefaultTreeError,
    RoutingMiss,
    RoutingRequest,
)
from router.upstream.proxy import prepare_upstream_request

router = APIRouter()
logger = logging.getLogger(__name__)


class DecisionRequestError(Exception):
    """A decision request rejected before any graph is selected."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def provider_payload(runtime, provider_name: str) -> dict:
    """Non-secret provider facts the caller needs to resolve execution.

    Only ``name``/``base_url``/``reasoning_format`` may cross the kernel
    boundary: provider keys and ``key_env`` stay on the execution side.
    """
    provider = next(
        item for item in runtime.backends.providers if item["name"] == provider_name)
    return {
        "name": provider["name"], "base_url": provider["base_url"],
        "reasoning_format": provider["reasoning_format"],
    }


async def read_decision_request(request: Request):
    """Validate one decision request and return ``(body, text, department, session_id)``.

    Keeps the data-plane validation the internal chat endpoint always had:
    10 MiB body, JSON object, ``messages`` list, strictly decoded trusted
    department and the 256-char session id.  ``Authorization`` is deliberately
    not read: the kernel receives no user key.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise DecisionRequestError("request_too_large", 413)
    try:
        department = decode_department(request.headers.get("x-sr-department"))
    except ValueError as exc:
        raise DecisionRequestError("invalid_department", 400) from exc
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > MAX_BODY_BYTES:
            raise DecisionRequestError("request_too_large", 413)
    try:
        body = json.loads(bytes(buffer))
    except Exception as exc:
        raise DecisionRequestError("invalid_json", 400) from exc
    if not isinstance(body, dict):
        raise DecisionRequestError("body_must_be_object", 400)
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise DecisionRequestError("messages_must_be_list", 400)
    session_id = request.headers.get("x-sr-session-id", "").strip()
    if len(session_id) > 256:
        raise DecisionRequestError("invalid_session_id", 400)
    return body, _last_user_text(messages), department, session_id


@router.post("/route-decision")
async def route_decision(request: Request):
    try:
        body, text, department, session_id = await read_decision_request(request)
    except DecisionRequestError as exc:
        return _error(exc.message, exc.status_code)

    route_set = request.app.state.snapshot_set
    if route_set is None:
        return _error("no_valid_snapshot", 503)
    try:
        selection = route_set.select(department)
    except NoActiveTreeError:
        return _error("no_active_tree", 503)
    except NoDefaultTreeError:
        return _error("no_default_tree", 503)
    snapshot = selection.snapshot
    # 内核不按请求模型选路；它只是事件里的事实字段。非字符串值（例如恶意
    # ``"model": {...}`` 或 JSON ``NaN``）无法进入校验后的事件，归一为空串，
    # 决策本身照常进行。
    requested_model = body.get("model", "auto")
    if not isinstance(requested_model, str):
        requested_model = ""
    trace: list[dict] = []
    started = time.monotonic()
    decision = None

    def fail(error_class: str, status_code: int, stage: str, message: str):
        """Emit the ``routing_error`` event for the already-selected graph."""
        trace.append({
            "event": "routing_error",
            "stage": stage,
            "error_class": error_class,
            "message": message,
        })
        total_ms = (time.monotonic() - started) * 1000.0
        record = _routing_failure_record(
            snapshot, text, session_id, trace,
            error_class=error_class, routing_ms=total_ms, total_ms=total_ms,
        )
        record["graph_selection"] = selection.reason
        record["upstream_model"] = None
        request.app.state.decision_sink.emit(build_decision_event(
            record, snapshot, department=department,
            requested_model=requested_model))
        return _error(error_class, status_code)

    try:
        try:
            decision = await request.app.state.routing_service.route(
                snapshot, RoutingRequest(text=text, session_id=session_id),
                trace_sink=trace)
        except NoActiveTreeError as exc:
            return fail("no_active_tree", 503, "graph", str(exc))
        except NoAvailableBackend as exc:
            return fail("no_available_backend", 503, "schedule", str(exc))
        except RoutingMiss as exc:
            return fail("routing_invariant_violation", 503, "graph", str(exc))
        except Exception:
            logger.exception("kernel routing failed")
            return fail("routing_failed", 503, "graph", "routing failed")
        backend = snapshot.backends.get(decision.model)
        if backend is None:
            # 防御性不变式：正常引擎不可达，decision.model 必来自同一快照。
            return fail("no_available_backend", 503, "schedule",
                        "decision model is not registered in the snapshot")
        # 纯内存推理兼容性计算：改写结果不发送给任何上游，只保留 warning。
        _unused_upstream_body, reasoning_warning = prepare_upstream_request(
            body, backend, decision.reasoning)
        record = _decision_record(
            decision, snapshot, text, reasoning_warning,
            (time.monotonic() - started) * 1000.0, started)
        record["graph_selection"] = selection.reason
        record["upstream_model"] = backend.upstream_name
        result = {
            "decision_id": record["id"], "model": backend.upstream_name,
            "logical_model": decision.model,
            "provider": provider_payload(snapshot.tree.runtime, backend.provider),
            "reasoning": decision.reasoning,
            "reasoning_warning": reasoning_warning,
            "tree_id": snapshot.tree_id, "tree_revision": snapshot.tree_revision,
            "target_id": decision.target_id,
            "selection_reason": decision.selection_reason,
            "graph_selection": selection.reason,
        }
        request.app.state.decision_sink.emit(build_decision_event(
            record, snapshot, department=department,
            requested_model=requested_model))
        return result
    finally:
        if decision is not None:
            decision.release()
