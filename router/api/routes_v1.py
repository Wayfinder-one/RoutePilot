"""OpenAI-compatible entry: tag -> evaluate -> forward. The pipeline and the
backend map are in-memory snapshots (design 4.4); routing never opens the
database — only the async request logger touches it.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router.api.sessions import read_last_vec, session_id_from
from router.api.upstream import resolve_backend
from router.pipeline.engine import evaluate
from router.signals.tagger import TagRequest

router = APIRouter()

BLOCK_MESSAGE = {"zh": "请求被安全策略拦截", "en": "Blocked by safety policy"}


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
    body = await request.json()
    messages = body.get("messages") or []
    text = _last_user_text(messages)
    tagger = request.app.state.tagger
    sid = session_id_from(request)
    result = await tagger.tag(TagRequest(
        text=text, headers=dict(request.headers), n_messages=len(messages),
        last_vec=read_last_vec(request, sid)))
    decision, _ = evaluate(request.app.state.pipeline, result.signals)

    request.app.state.requestlog.enqueue({
        "id": uuid.uuid4().hex,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text, "decision": decision.kind, "model": decision.model,
        "node_id": decision.node_id, "branch_id": decision.branch_id,
        "score": decision.score,
        "latency_ms": (time.monotonic() - started) * 1000,
        "signals": result.signals,
        "session_id": sid, "vec": result.vec})

    if decision.kind == "block":
        return JSONResponse(status_code=403, content={
            "error": {"message": BLOCK_MESSAGE, "code": "blocked_by_policy"}})
    if decision.kind != "route" or not decision.model:
        return JSONResponse(status_code=502,
                            content={"error": {"message": "no_route"}})

    try:
        backend = resolve_backend(request.app.state.backends, decision.model)
    except KeyError:
        return JSONResponse(status_code=502,
                            content={"error": {"message": "unknown_model"}})

    breaker = request.app.state.breaker
    if breaker.is_open(decision.model):
        return JSONResponse(status_code=503, content={
            "error": {"message": "model_circuit_open", "model": decision.model}})

    client: httpx.AsyncClient = request.app.state.http
    upstream_body = {**body, "model": backend.upstream_name}
    headers = {"x-sr-model": decision.model}
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
    breaker.success(decision.model)
    try:
        content = resp.json()
    except json.JSONDecodeError:
        content = {"raw": resp.text}
    return JSONResponse(status_code=resp.status_code, content=content,
                        headers=headers)


async def _stream(client, backend, body, headers, breaker, model):
    req = client.build_request("POST", backend.url, json=body,
                               headers=backend.headers)
    resp = await client.send(req, stream=True)
    if resp.status_code >= 500:
        await resp.aclose()
        breaker.failure(model)
        return JSONResponse(status_code=502,
                            content={"error": {"message": "upstream_error"}},
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
