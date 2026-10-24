"""OpenAI-compatible entry: tag -> evaluate -> forward. The pipeline and the
backend map are in-memory snapshots (design 4.4); routing never opens the
database — only the async request logger (and the breaker's low-rate sync)
touches it.

Every request that reaches a routing decision also lands one ``request_ledger``
row (JEV plan §3.1): the record enqueued before the upstream call is the half
row (action only); after the response is known its outcome fields are filled in
place and a second flush event overwrites the row by id. That is how 200 vs
upstream 500 become distinguishable, and how the ledger records the real
upstream status (the client still sees the mapped 502), the upstream model,
token usage, routing/upstream/TTFT/total durations, the error class, and the
pipeline/tagger versions. No request text, signals or PII reach the ledger.
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

#: Give up sniffing one SSE line once the unparsed remainder is this large: the
#: usage chunk is a short JSON line; a pathological stream must not grow the
#: buffer without bound while we merely look for optional usage data.
MAX_SSE_LINE_BYTES = 1024 * 1024


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


def _tagger_info(tagger) -> dict:
    """``tagger.info()`` (JEV plan §1.3) read per request, guarded.

    Taggers without the optional interface (heuristic/checkpoint/prompt) yield
    ``{}``: the ledger's ``jev_*``/``questions_sha256`` columns stay NULL. The
    call is cheap by contract (a dict of versions, not model work) and is NOT
    cached on purpose: a hot question-config edit must change
    ``questions_sha256`` on the next request. Reproducibility metadata must
    never break a request, so any failure degrades to ``{}``.
    """
    info = getattr(tagger, "info", None)
    if not callable(info):
        return {}
    try:
        meta = info()
    except Exception:                      # noqa: BLE001 - metadata only
        logger.warning("tagger.info() failed; ledger version fields are null",
                       exc_info=True)
        return {}
    return meta if isinstance(meta, dict) else {}


def _version_fields(request) -> dict:
    """The ledger's reproduction columns (pipeline + JEV question/model versions).

    ``pipeline_version`` is the settings value cached by the ``RequestLogger``
    (read at startup, hot-synced by the admin pipeline write; the hot path must
    not query the DB); the rest comes from the tagger's optional ``info()``.
    ``jev_window`` is the input window actually scored (JEV plan §2.1), so
    window A/B traffic can be split by ledger rows.
    """
    info = _tagger_info(request.app.state.tagger)
    return {
        "pipeline_version": request.app.state.requestlog.pipeline_version,
        "jev_engine": info.get("engine"),
        "jev_model": info.get("model_name") or info.get("model_dir"),
        "jev_window": info.get("window"),
        "jev_temperature": info.get("temperature"),
        "questions_sha256": info.get("questions_sha256"),
    }


def _usage_fields(usage) -> dict:
    """Token columns from an OpenAI ``usage`` object (all NULL when absent)."""
    fields: dict = {"prompt_tokens": None, "completion_tokens": None,
                    "total_tokens": None}
    if isinstance(usage, dict):
        for key in fields:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                fields[key] = value
    return fields


def _record_outcome(requestlog, record: dict, started: float, *,
                    error_class: str | None,
                    upstream_model: str | None = None,
                    upstream_status: int | None = None,
                    upstream_ms: float | None = None,
                    ttft_ms: float | None = None,
                    usage: dict | None = None) -> None:
    """Fill the outcome into the ring record and queue the ledger update.

    Called exactly once on every exit path (block / no route / unknown model /
    circuit open / upstream error / success / stream end). The ring holds this
    same dict, so the console log sees the outcome; the flush replaces the half
    row on the ``id`` primary key.
    """
    record.update({
        "upstream_model": upstream_model,
        "upstream_status": upstream_status,
        "upstream_ms": upstream_ms,
        "ttft_ms": ttft_ms,
        "total_ms": (time.monotonic() - started) * 1000.0,
        "error_class": error_class,
        **_usage_fields(usage),
    })
    requestlog.enqueue_result(record)


class _SseUsage:
    """Passive SSE scanner that remembers the last ``usage`` object it sees.

    The router never injects ``stream_options.include_usage`` (§3.1: the
    upstream request semantics are unchanged): when the *client* asks for usage
    the upstream's final ``data: {...}`` chunk carries it and the ledger records
    the tokens; otherwise the token columns stay NULL. Bytes are relayed
    verbatim — this class only reads them.
    """

    __slots__ = ("_buf", "usage")

    def __init__(self):
        self._buf = b""
        self.usage: dict | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            self._scan(line)
        if len(self._buf) > MAX_SSE_LINE_BYTES:
            self._buf = b""                    # never need a >1MiB "line"

    def finish(self) -> None:
        """Scan a trailing line that arrived without its newline (defensive)."""
        if self._buf:
            line, self._buf = self._buf, b""
            self._scan(line)

    def _scan(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]


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
    # Ledger `routing_ms`: from the fully read body to the finished decision.
    routing_started = time.monotonic()
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
    routing_ms = (time.monotonic() - routing_started) * 1000.0

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
    requestlog = request.app.state.requestlog
    # The record is enqueued BEFORE the upstream call (the requests half row is
    # written as soon as the decision is known) and mutated in place once the
    # outcome is known — see _record_outcome. Detail (text/signals/trace) stays
    # memory-only: the ledger flush selects only action/outcome/version columns.
    record = {
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
        "trace": trace, "signals": result.signals,
        # Ledger metadata known already at decision time (design §3.1).
        "routing_ms": routing_ms,
        **_version_fields(request),
        # Outcome placeholders (filled in place by _record_outcome once the
        # response is known). Keeping the keys from the start gives the console
        # a stable record shape even while a stream is still running.
        "upstream_model": None, "upstream_status": None,
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "upstream_ms": None, "ttft_ms": None, "total_ms": None,
        "error_class": None,
    }
    requestlog.enqueue(record)

    if decision.kind == "block":
        _record_outcome(requestlog, record, started, error_class=None)
        return JSONResponse(status_code=403, content={
            "error": {"message": BLOCK_MESSAGE, "code": "blocked_by_policy"}})
    if decision.kind != "route" or not decision.model:
        _record_outcome(requestlog, record, started, error_class="no_route")
        return JSONResponse(status_code=502,
                            content={"error": {"message": "no_route"}})
    if backend is None:
        _record_outcome(requestlog, record, started,
                        error_class="unknown_model")
        return JSONResponse(status_code=502,
                            content={"error": {"message": "unknown_model"}})

    breaker = request.app.state.breaker
    if breaker.is_open(decision.model):
        _record_outcome(requestlog, record, started,
                        error_class="circuit_open",
                        upstream_model=backend.upstream_name)
        return JSONResponse(status_code=503, content={
            "error": {"message": "model_circuit_open", "model": decision.model}})

    client: httpx.AsyncClient = request.app.state.http
    headers = {"x-sr-model": decision.model}
    if reasoning_warning:
        headers["x-sr-reasoning-warning"] = reasoning_warning
    elif reasoning_level is not None:
        headers["x-sr-reasoning"] = reasoning_level
    upstream_started = time.monotonic()
    try:
        if body.get("stream"):
            return await _stream(client, backend, upstream_body, headers,
                                 breaker, decision.model, record=record,
                                 requestlog=requestlog, started=started)
        resp = await client.post(backend.url, json=upstream_body,
                                 headers=backend.headers)
    except httpx.HTTPError:
        breaker.failure(decision.model)
        _record_outcome(requestlog, record, started,
                        error_class="upstream_error",
                        upstream_model=backend.upstream_name,
                        upstream_ms=(time.monotonic() - upstream_started) * 1000.0)
        return JSONResponse(status_code=502,
                            content={"error": {"message": "upstream_error"}},
                            headers=headers)
    upstream_ms = (time.monotonic() - upstream_started) * 1000.0
    if resp.status_code >= 500:
        breaker.failure(decision.model)
        # The ledger keeps the REAL upstream status; the client keeps seeing the
        # mapped 502 (external contract unchanged).
        _record_outcome(requestlog, record, started,
                        error_class="upstream_error",
                        upstream_model=backend.upstream_name,
                        upstream_status=resp.status_code,
                        upstream_ms=upstream_ms)
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
    _record_outcome(requestlog, record, started,
                    error_class=None if resp.status_code < 400 else "client_4xx",
                    upstream_model=backend.upstream_name,
                    upstream_status=resp.status_code,
                    upstream_ms=upstream_ms,
                    usage=content.get("usage") if isinstance(content, dict) else None)
    return JSONResponse(status_code=resp.status_code, content=content,
                        headers=headers)


async def _stream(client, backend, body, headers, breaker, model, *,
                  record, requestlog, started):
    # Streaming has its own timeout policy: the global 60s read timeout would
    # cut a slow-but-alive stream (a thinking model can pause for a minute
    # before its first token). Connect/write stay bounded; the read timeout is
    # unlimited.
    upstream_started = time.monotonic()
    req = client.build_request("POST", backend.url, json=body,
                               headers=backend.headers,
                               timeout=httpx.Timeout(60.0, read=None))
    resp = await client.send(req, stream=True)
    # "upstream_ms" for a stream = request start -> response headers received
    # (the rest of the body keeps flowing; total_ms covers the whole stream).
    upstream_ms = (time.monotonic() - upstream_started) * 1000.0
    if resp.status_code >= 400:
        # An upstream rejection is not a stream: read the error body and answer
        # with its status code, exactly like the non-streaming path. Reporting
        # it as a 200 SSE would hand the client an empty event stream instead
        # of the real error (e.g. 400 "model not found").
        raw = await resp.aread()
        await resp.aclose()
        if resp.status_code >= 500:
            breaker.failure(model)
            _record_outcome(requestlog, record, started,
                            error_class="upstream_error",
                            upstream_model=backend.upstream_name,
                            upstream_status=resp.status_code,
                            upstream_ms=upstream_ms)
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
        _record_outcome(requestlog, record, started,
                        error_class="client_4xx",
                        upstream_model=backend.upstream_name,
                        upstream_status=resp.status_code,
                        upstream_ms=upstream_ms)
        return JSONResponse(status_code=resp.status_code, content=content,
                            headers=headers)
    breaker.success(model)

    async def relay():
        usage_sniffer = _SseUsage()
        first_chunk_at: float | None = None
        error_class: str | None = None
        try:
            async for chunk in resp.aiter_raw():
                if first_chunk_at is None:       # TTFT: first byte of the body
                    first_chunk_at = time.monotonic()
                usage_sniffer.feed(chunk)
                yield chunk
        except Exception:                        # noqa: BLE001 - mid-stream break
            error_class = "upstream_error"
            raise
        finally:
            try:
                await resp.aclose()
            except Exception:                    # noqa: BLE001 - close is best effort
                logger.warning("upstream stream close failed", exc_info=True)
            ttft_ms = (None if first_chunk_at is None
                       else (first_chunk_at - upstream_started) * 1000.0)
            usage_sniffer.finish()               # trailing line without '\n'
            # Stream end: the ledger row (and the ring) get the full outcome.
            _record_outcome(requestlog, record, started,
                            error_class=error_class,
                            upstream_model=backend.upstream_name,
                            upstream_status=resp.status_code,
                            upstream_ms=upstream_ms,
                            ttft_ms=ttft_ms,
                            usage=usage_sniffer.usage)

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers=headers)
