"""OpenAI-compatible entry: tag -> evaluate -> forward. The pipeline and the
backend map are in-memory snapshots (design 4.4); routing never opens the
database — only the async request logger (and the breaker's low-rate sync)
touches it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router.api.upstream import resolve_backend
from router.pipeline.engine import evaluate
from router.pipeline.scope import compute_signal_scope
from router.reasoning import ReasoningError, apply_reasoning
from router.signals.tagger import TagRequest

router = APIRouter()

logger = logging.getLogger(__name__)

BLOCK_MESSAGE = {"zh": "请求被安全策略拦截", "en": "Blocked by safety policy"}


#: Upper bound for a request body (10 MiB). The router reads the body into
#: memory; a bigger one is rejected instead of being buffered.
MAX_BODY_BYTES = 10 * 1024 * 1024


def _last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):      # OpenAI content parts
                return " ".join(p.get("text", "") for p in content
                                if isinstance(p, dict))
    return ""


@router.post("/chat/completions")
async def chat_completions(request: Request):
    started = time.monotonic()
    # Entry defense: a malformed body is the client's mistake (400), not a
    # server error (500).
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413,
                            content={"error": {"message": "request_too_large"}})
    # A chunked transfer declares no content-length, so the gate above passes
    # for it by design (the value "None" is unknown until read): count the
    # bytes while streaming them off the wire and stop at the cap. A body is
    # never buffered past MAX_BODY_BYTES regardless of what the headers claim.
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413,
                                content={"error": {"message":
                                                   "request_too_large"}})
    try:
        body = json.loads(bytes(buf))        # same contract as request.json()
    except Exception:                     # noqa: BLE001 - any decode error
        return JSONResponse(status_code=400,
                            content={"error": {"message": "invalid_json"}})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": {"message": "body_must_be_object"}})
    messages = body.get("messages")
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        return JSONResponse(status_code=400,
                            content={"error": {"message": "messages_must_be_list"}})
    text = _last_user_text(messages)
    tagger = request.app.state.tagger
    # One snapshot for both the trimming hint and the decision: a concurrent
    # PUT /admin/pipeline swaps the whole list atomically, and reading it twice
    # could score the old tree but evaluate the new one.
    stations = request.app.state.pipeline
    # Pipeline-aware trimming (docs/pipeline-aware-trimming.md): score only the
    # JEV questions this tree can read. The tagger ignores the hint when it does
    # not implement trimming (heuristic/checkpoint/prompt modes).
    scope = (compute_signal_scope(stations)
             if request.app.state.settings.jev_trim else None)
    result = await tagger.tag(TagRequest(
        text=text, headers=dict(request.headers), n_messages=len(messages),
        scope=scope))
    # Every request is judged on its own: tag -> pipeline decision. There is
    # no session state (2026-10-21: session stickiness removed — vanilla
    # clients carry no reliable conversation identity; see
    # docs/superpowers/specs/2026-10-21-remove-session-stickiness.md).
    decision, trace = evaluate(stations, result.signals)

    # ---- upstream body + reasoning injection (§6) -------------------------
    # decision.reasoning is None -> FULL passthrough: only `model` is replaced,
    # client reasoning fields are neither injected nor stripped. A configured
    # level that the current capability snapshot cannot honour (concurrent
    # config edit) degrades to passthrough + a warning header instead of
    # failing the user's request.
    backend = None
    upstream_body = None
    reasoning_level = decision.reasoning
    reasoning_warning = None
    if decision.kind == "route" and decision.model:
        try:
            backend = resolve_backend(request.app.state.backends, decision.model)
        except KeyError:
            backend = None
        if backend is not None:
            base_body = {**body, "model": backend.upstream_name}
            if reasoning_level is None:
                upstream_body = base_body
            else:
                try:
                    upstream_body = apply_reasoning(
                        base_body, backend.reasoning, reasoning_level)
                except ReasoningError as exc:
                    reasoning_warning = "unsupported"
                    upstream_body = base_body
                    logger.warning(
                        "reasoning fallback model=%s level=%s: %s",
                        decision.model, reasoning_level, exc)

    # Input window actually scored by the model signals (JEV head-truncates the
    # request text; heuristic/checkpoint read the full text and report {}).
    window = result.input or {}
    request.app.state.requestlog.enqueue({
        "id": uuid.uuid4().hex,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text, "decision": decision.kind, "model": decision.model,
        "station_id": decision.station_id, "leaf_id": decision.node_id,
        "score": decision.score,
        "reasoning": reasoning_level,
        "reasoning_warning": reasoning_warning,
        # Truncation must be visible downstream (PII report P0): the model only
        # reads the first N tokens of the request text.
        "input_tokens": window.get("input_tokens"),
        "input_visible_tokens": window.get("input_visible_tokens"),
        "input_truncated": bool(window.get("input_truncated", False)),
        "latency_ms": (time.monotonic() - started) * 1000,
        "trace": trace, "signals": result.signals})

    if decision.kind == "block":
        return JSONResponse(status_code=403, content={
            "error": {"message": BLOCK_MESSAGE, "code": "blocked_by_policy"}})
    if decision.kind != "route" or not decision.model:
        return JSONResponse(status_code=502,
                            content={"error": {"message": "no_route"}})
    if backend is None:
        return JSONResponse(status_code=502,
                            content={"error": {"message": "unknown_model"}})

    breaker = request.app.state.breaker
    if breaker.is_open(decision.model):
        return JSONResponse(status_code=503, content={
            "error": {"message": "model_circuit_open", "model": decision.model}})

    client: httpx.AsyncClient = request.app.state.http
    headers = {"x-sr-model": decision.model}
    if reasoning_warning:
        headers["x-sr-reasoning-warning"] = reasoning_warning
    elif reasoning_level is not None:
        headers["x-sr-reasoning"] = reasoning_level
    try:
        if body.get("stream"):
            return await _stream(client, backend, upstream_body, headers,
                                 breaker, decision.model)
        resp = await client.post(backend.url, json=upstream_body,
                                 headers=backend.headers)
    except httpx.HTTPError:
        breaker.failure(decision.model)
        return JSONResponse(status_code=502,
                            content={"error": {"message": "upstream_error"}},
                            headers=headers)
    if resp.status_code >= 500:
        breaker.failure(decision.model)
        return JSONResponse(status_code=502,
                            content={"error": {"message": "upstream_error"}},
                            headers=headers)
    if resp.status_code < 400:
        # Same contract as the streaming path (_stream): a 4xx is
        # request-specific — neither a model failure nor proof of health — so
        # it must not clear the failure counter (an already open circuit must
        # not be closed by an error response; a half-open probe needs a real
        # 2xx to recover).
        breaker.success(decision.model)
    try:
        content = resp.json()
    except json.JSONDecodeError:
        content = {"raw": resp.text}
    return JSONResponse(status_code=resp.status_code, content=content,
                        headers=headers)


async def _stream(client, backend, body, headers, breaker, model):
    # Streaming has its own timeout policy: the global 60s read timeout would
    # cut a slow-but-alive stream (a thinking model can pause for a minute
    # before its first token). Connect/write stay bounded; the read timeout is
    # unlimited.
    req = client.build_request("POST", backend.url, json=body,
                               headers=backend.headers,
                               timeout=httpx.Timeout(60.0, read=None))
    resp = await client.send(req, stream=True)
    if resp.status_code >= 400:
        # An upstream rejection is not a stream: read the error body and answer
        # with its status code, exactly like the non-streaming path. Reporting
        # it as a 200 SSE would hand the client an empty event stream instead
        # of the real error (e.g. 400 "model not found").
        raw = await resp.aread()
        await resp.aclose()
        if resp.status_code >= 500:
            breaker.failure(model)
            return JSONResponse(status_code=502,
                                content={"error": {"message": "upstream_error"}},
                                headers=headers)
        # 4xx: the upstream rejected the request. It is neither a model
        # failure (no breaker.failure) nor proof of health (no breaker.success:
        # the counter must survive so an open circuit stays open).
        try:
            content = json.loads(raw)
        except (ValueError, TypeError):
            content = {"raw": raw.decode("utf-8", "replace")}
        return JSONResponse(status_code=resp.status_code, content=content,
                            headers=headers)
    breaker.success(model)

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers=headers)
