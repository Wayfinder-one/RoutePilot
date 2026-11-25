"""Peripheral data-plane entry: validate, ask the kernel, execute the model call.

The peripheral owns the Provider Key and the SQLite execution catalog; the
kernel owns graph selection and JEV.  For Auto requests this module:

1. validates the request (size / JSON / ``messages`` / session / trusted
   department) exactly as before,
2. posts the original body to the kernel decision endpoint
   (``app.state.core.decide``) — the user ``Authorization`` is never forwarded,
3. resolves the decided logical model against the peripheral execution catalog
   (``backend_for_decision``); a catalog that disagrees with the kernel decision
   is a 503 ``execution_catalog_mismatch`` and no upstream is called,
4. rewrites the request body to the kernel's real model plus the peripheral
   Provider Key / reasoning support and hands it to ``UpstreamProxy`` (JSON or
   SSE).  Execution results are discarded (``DiscardOutcome``): the peripheral
   keeps no request log and the kernel already emitted the decision event.

Fixed-model requests (admin Test chat only) stay peripheral-local: they resolve
the requested model directly from the SQLite catalog (``load_backends``) and
never call the kernel.

Presentation, routing and event construction live in the kernel; transport and
lease ownership live in ``router.upstream.proxy``.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote_to_bytes

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router.api.deps import get_conn
from router.core_client import CoreDecisionError
from router.request_outcome import DiscardOutcome
from router.upstream.catalog import load_backends
from router.upstream.execution import ExecutionCatalogMismatch, backend_for_decision
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


def _kernel_error(exc: CoreDecisionError) -> JSONResponse:
    """Map a kernel decision failure to the same HTTP status + detail.

    The kernel answers with the same flat ``{"error": {"message": ...}}`` body the
    peripheral used to produce locally (400/413/422 validation, 503
    ``no_active_tree``/``no_default_tree``/``no_available_backend``/...), so the
    standalone data plane keeps its existing error contract byte for byte.
    """
    payload = exc.payload
    if isinstance(payload, dict) and payload:
        return JSONResponse(status_code=exc.status_code, content=payload)
    return _error("routing_failed", exc.status_code)


def _auto_headers(decision: dict) -> dict:
    """Response ``x-sr-*`` facts, all read from the kernel decision payload."""
    headers = {
        "x-sr-model": decision["model"],
        "x-sr-selection": decision["selection_reason"],
    }
    if decision.get("tree_id") is not None:
        headers["x-sr-tree-id"] = decision["tree_id"]
    if decision.get("graph_selection") is not None:
        headers["x-sr-graph-selection"] = decision["graph_selection"]
    if decision.get("reasoning_warning"):
        headers["x-sr-reasoning-warning"] = decision["reasoning_warning"]
    elif decision.get("reasoning") is not None:
        headers["x-sr-reasoning"] = decision["reasoning"]
    return headers


async def _execute(request: Request, body: dict, *, backend, reasoning,
                   headers: dict, breaker_key: str):
    """Prepare the upstream body and run one JSON/SSE exchange (no outcome log)."""
    upstream_body, reasoning_warning = prepare_upstream_request(body, backend, reasoning)
    if reasoning_warning:
        headers["x-sr-reasoning-warning"] = reasoning_warning
    elif reasoning is not None:
        headers["x-sr-reasoning"] = reasoning
    proxy = UpstreamProxy(request.app.state.http, request.app.state.breaker)
    outcome = DiscardOutcome()
    if body.get("stream"):
        streamed = await proxy.stream(
            backend=backend, body=upstream_body, headers=headers,
            model=backend.upstream_name, breaker_key=breaker_key, outcome=outcome,
            is_disconnected=request.is_disconnected,
            release=lambda: None,
        )
        if isinstance(streamed, ProxyStream):
            # ``streamed.body`` owns the upstream response; the server closes it.
            return StreamingResponse(
                streamed.body, status_code=streamed.status_code,
                media_type=streamed.media_type, headers=streamed.headers,
            )
        return _json_response(streamed)
    return _json_response(await proxy.send(
        backend=backend, body=upstream_body, headers=headers,
        model=backend.upstream_name, breaker_key=breaker_key, outcome=outcome,
        release=lambda: None,
    ))


def _fixed_backend(request: Request, conn, fixed_model: str):
    """Peripheral-local fixed-model resolution; ``(backend, error_response)``."""
    backend = load_backends(conn).get(fixed_model)
    if backend is None:
        return None, _error("unknown_model", 404)
    breaker_key = runtime_model_key("", fixed_model)
    if request.app.state.breaker.is_open(breaker_key):
        return None, _error("no_available_backend", 503)
    return (backend, breaker_key), None


async def run_chat_completion(request: Request, *, use_requested_model: bool = False,
                              conn=None, allow_department_header: bool = False):
    """Shared data-plane executor (``/v1`` and the admin Test chat page).

    ``use_requested_model`` is the only model-selection difference between the
    public data plane and the admin test endpoint: when false the request
    ``model`` stays ignored and the kernel decides; when true ``"auto"`` still
    calls the kernel, any other model is a fixed direct selection that resolves
    through the peripheral SQLite catalog and never calls the kernel.

    ``conn`` is the peripheral catalog connection (both endpoints pass one); the
    fixed-model test uses it directly, while Auto uses it only for
    ``backend_for_decision``.

    ``allow_department_header`` gates the trusted ``x-sr-department`` header: the
    admin test chat sets it so an operator can probe department selection, while
    the public data plane rejects the header outright
    (``400 department_header_not_allowed``).
    """
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

    if fixed_model is not None:
        resolved, error = _fixed_backend(request, conn, fixed_model)
        if error is not None:
            return error
        backend, breaker_key = resolved
        # 固定模型直连是管理目录测试：不参与选图，不输出 x-sr-graph-selection。
        return await _execute(
            request, body, backend=backend, reasoning=None,
            headers={"x-sr-model": fixed_model, "x-sr-selection": "direct"},
            breaker_key=breaker_key,
        )

    try:
        decision = await request.app.state.core.decide(
            body, department=department, session_id=session_id,
            request_id=request.headers.get("x-request-id", ""),
        )
    except CoreDecisionError as exc:
        return _kernel_error(exc)
    try:
        backend = backend_for_decision(decision, conn)
    except ExecutionCatalogMismatch:
        # 目录与内核决策不一致：返回 503，不调用上游，也不拿其它 Provider 凭据顶替。
        logger.warning(
            "execution catalog mismatch logical_model=%r provider=%r",
            decision.get("logical_model"), (decision.get("provider") or {}).get("name"))
        return _error("execution_catalog_mismatch", 503)
    breaker_key = runtime_model_key(
        tree_scope(decision["tree_id"]), decision["model"])
    if request.app.state.breaker.is_open(breaker_key):
        return _error("no_available_backend", 503)
    return await _execute(
        request, body, backend=backend, reasoning=decision.get("reasoning"),
        headers=_auto_headers(decision), breaker_key=breaker_key,
    )


@router.post("/chat/completions")
async def chat_completions(
    request: Request, conn=Depends(get_conn),
):
    return await run_chat_completion(request, conn=conn)
