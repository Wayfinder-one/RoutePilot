"""OpenAI-compatible entry: tag -> evaluate -> forward.

JEV P1-B: a request reads the immutable ``app.state.snapshot`` ONCE at entry
and uses that same snapshot for the trimming scope, the decision and every
ledger version field. A concurrent ``PUT /admin/pipeline`` / model write
publishes a new snapshot with one assignment; the in-flight request keeps the
old one, so the ledger revision always describes the configuration that
actually produced the decision (previously the tree was read before tagging
and the versions after, which could disagree).

JEV P1-C: each request finishes in exactly one terminal state, recorded in
``request_ledger.outcome_state`` by an exactly-once recorder:
``completed / upstream_error / client_disconnected / cancelled / incomplete``.
The streaming relay reports ``completed`` only when the upstream iterator is
exhausted normally; GeneratorExit (client went away) and CancelledError are
classified separately and re-raised, never swallowed.

The ledger record is enqueued before the upstream call (the half row) and
mutated in place once the response is known; the flush overwrites the row by
id. No request text, signals or PII reach the ledger.
"""

from __future__ import annotations

import asyncio
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

# ── terminal states (JEV P1-C §3) ───────────────────────────────────────────
OUTCOME_COMPLETED = "completed"
OUTCOME_UPSTREAM_ERROR = "upstream_error"
OUTCOME_CLIENT_DISCONNECTED = "client_disconnected"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_STATES = (OUTCOME_COMPLETED, OUTCOME_UPSTREAM_ERROR,
                  OUTCOME_CLIENT_DISCONNECTED, OUTCOME_CANCELLED,
                  OUTCOME_INCOMPLETE)

#: Optional experiment/arm attribution supplied by the caller (window A/B,
#: cheap-vs-strong rollouts, ...). Kept small and opaque: the ledger only
#: groups by it.
EXPERIMENT_ARM_HEADER = "x-sr-experiment-arm"
MAX_EXPERIMENT_ARM = 64


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


def _as_int(value) -> int | None:
    """int-or-NULL coercion for optional contract integers (bool is not int)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chunk_summary(info: dict) -> str | None:
    """Human-readable chunk-config digest for the ledger (P2-D).

    Built from the tagger's frozen ``tagger_info`` keys (P2-A contract):
    ``"8x512/noisy_or"`` = 8 planned segments of 512 tokens aggregated with
    noisy-or; plain ``"off"`` when the tagger reports chunking disabled; NULL
    when it reports nothing (non-JEV taggers).
    """
    agg = info.get("chunk_agg")
    segments = info.get("chunk_segments")
    size = info.get("chunk_size")
    if str(agg) == "off" or str(segments) == "off":
        return "off"
    seg_int = _as_int(segments)
    size_int = _as_int(size)
    if seg_int is None and size_int is None and agg is None:
        return None
    if seg_int is not None and size_int is not None:
        shape = f"{seg_int}x{size_int}"
    elif seg_int is not None:
        shape = f"{seg_int}seg"
    elif size_int is not None:
        shape = f"{size_int}tok"
    else:
        shape = ""
    parts = [part for part in (shape, None if agg is None else str(agg))
             if part]
    return "/".join(parts) or None


def _chunk_fields(result, info: dict) -> dict:
    """Chunked-input attribution columns (migration v16).

    Tolerant reads of the optional P2-A contract (``TagResult.input`` plus the
    frozen ``tagger_info``): old taggers or chunking-off requests leave NULLs —
    a ledger row must never fail because a tagger does not implement chunking.

    ``input_chunk_segments`` records the segments that were ACTUALLY scored
    (``chunk_scored_segments``), because the column feeds online cost
    attribution — how much work this request really caused. The planned
    segment count is recoverable from the mode/config side, so it is only a
    fallback when the tagger reports no actual count (older/mid-rollout packs).
    """
    window = getattr(result, "input", None)
    window = window if isinstance(window, dict) else {}
    mode = window.get("chunk_strategy")
    planned = _as_int(window.get("chunk_segments"))
    scored = _as_int(window.get("chunk_scored_segments"))
    return {
        "jev_chunk": _chunk_summary(info),
        "input_chunk_mode": mode if isinstance(mode, str) else None,
        "input_chunk_segments": scored if scored is not None else planned,
        "input_chunk_prompt_tokens": _as_int(
            window.get("chunk_prompt_tokens")),
    }


def _tagger_fingerprint(result) -> dict:
    """Ledger version/attribution columns from the per-tag fingerprint (P1-A).

    ``TagResult.questions_sha256`` is captured by the tagger at tag() start and
    ``TagResult.tagger_info`` is its frozen ``info()`` snapshot — both describe
    the model/question pack that actually scored THIS request, so no tagger
    state is re-read after the response (which would race a hot edit). Taggers
    that do not provide them (heuristic/checkpoint/prompt) leave the columns
    NULL. The v16 chunk columns (P2-A/D) ride on the same two objects.

    """
    info = getattr(result, "tagger_info", None)
    if not isinstance(info, dict):
        info = {}
    question_sha = getattr(result, "questions_sha256", None)
    if question_sha is None:                   # tolerate info-only taggers
        question_sha = info.get("questions_sha256")
    return {
        # v15 provenance column + v14 compatibility column (same canonical sha)
        "questions_revision": question_sha,
        "questions_sha256": question_sha,
        "jev_engine": info.get("engine"),
        "jev_model": info.get("model_name") or info.get("model_dir"),
        "jev_window": info.get("window"),
        "jev_temperature": info.get("temperature"),
        # v16 chunked-input attribution (NULL for non-chunking taggers)
        **_chunk_fields(result, info),
    }


def _version_fields(snapshot, result) -> dict:
    """Reproduction columns: the snapshot's revisions + the tagger fingerprint.

    Nothing is read from app.state/tagger here beyond the already-materialized
    TagResult (P1-B: no post-tagging re-read, no request-time DB access).
    """
    return {
        "pipeline_version": snapshot.pipeline_version,
        "pipeline_revision": snapshot.pipeline_revision,
        "backends_revision": snapshot.backends_revision,
        "capability_revision": snapshot.capability_revision,
        **_tagger_fingerprint(result),
    }


def _experiment_arm(request: Request) -> str | None:
    arm = request.headers.get(EXPERIMENT_ARM_HEADER)
    if not arm:
        return None
    arm = arm.strip()[:MAX_EXPERIMENT_ARM]
    return arm or None


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


async def _is_disconnected(request) -> bool:
    """Client-disconnect probe used by the streaming relay (P1-C)."""
    if request is None:
        return False
    try:
        return bool(await request.is_disconnected())
    except BaseException:                      # noqa: BLE001 - never break the relay
        return False


class _OutcomeRecorder:
    """Exactly-once terminal update for one request_ledger record (P1-C §3).

    Every exit path calls :meth:`finish`; the first call wins and the guard
    makes a duplicate (double-finally, aborted stream, handler + relay racing)
    a no-op instead of a second queued update. The ring dict and the flush
    event are the same object, so the console and the ledger agree.
    """

    __slots__ = ("requestlog", "record", "started", "_done")

    def __init__(self, requestlog, record: dict, started: float):
        self.requestlog = requestlog
        self.record = record
        self.started = started
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def finish(self, *, outcome_state: str,
               error_class: str | None = None,
               upstream_model: str | None = None,
               upstream_status: int | None = None,
               upstream_ms: float | None = None,
               ttft_ms: float | None = None,
               usage: dict | None = None) -> bool:
        if outcome_state not in OUTCOME_STATES:
            raise ValueError(f"unknown outcome_state {outcome_state!r}")
        if self._done:
            return False
        self._done = True
        tokens = _usage_fields(usage)
        observed = any(value is not None for value in tokens.values())
        self.record.update({
            "upstream_model": upstream_model,
            "upstream_status": upstream_status,
            "upstream_ms": upstream_ms,
            "ttft_ms": ttft_ms,
            "total_ms": (time.monotonic() - self.started) * 1000.0,
            "error_class": error_class,
            "outcome_state": outcome_state,
            "usage_observed": 1 if observed else 0,
            **tokens,
        })
        self.requestlog.enqueue_result(self.record)
        return True


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
    # JEV P1-B: ONE snapshot for the whole request — trimming scope, decision,
    # backend resolution and ledger provenance all come from this object. A
    # concurrent admin write replaces app.state.snapshot atomically; this
    # request keeps the configuration it started with.
    snapshot = request.app.state.snapshot
    tagger = request.app.state.tagger
    requestlog = request.app.state.requestlog
    # Ledger `routing_ms`: from the fully read body to the finished decision.
    routing_started = time.monotonic()
    # Pipeline-aware trimming (docs/pipeline-aware-trimming.md): score only the
    # JEV questions this tree can read. The tagger ignores the hint when it does
    # not implement trimming (heuristic/checkpoint/prompt modes).
    scope = (compute_signal_scope(snapshot.stations)
             if snapshot.jev_trim else None)
    result = await tagger.tag(TagRequest(
        text=text, headers=dict(request.headers), n_messages=len(messages),
        scope=scope, capability=snapshot.capability))
    # Every request is judged on its own: tag -> pipeline decision. There is
    # no session state (2026-10-21: session stickiness removed — vanilla
    # clients carry no reliable conversation identity; see
    # docs/superpowers/specs/2026-10-21-remove-session-stickiness.md).
    decision, trace = evaluate(snapshot.stations, result.signals)
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
            backend = resolve_backend(snapshot.backends, decision.model)
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
    # The record is enqueued BEFORE the upstream call (the requests half row is
    # written as soon as the decision is known) and mutated in place once the
    # outcome is known — see _OutcomeRecorder. Detail (text/signals/trace)
    # stays memory-only: the ledger flush selects only metadata columns.
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
        # Ledger metadata known already at decision time (design §3.1/P1-B).
        "routing_ms": routing_ms,
        **_version_fields(snapshot, result),
        "experiment_arm": _experiment_arm(request),
        # Outcome placeholders (filled exactly once by the recorder).
        "upstream_model": None, "upstream_status": None,
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "upstream_ms": None, "ttft_ms": None, "total_ms": None,
        "error_class": None,
        "outcome_state": OUTCOME_INCOMPLETE,
        "quality_score": None, "quality_evaluator": None,
        "usage_observed": 0,
    }
    requestlog.enqueue(record)
    outcome = _OutcomeRecorder(requestlog, record, started)

    if decision.kind == "block":
        # A decision that never calls an upstream is a COMPLETED request (the
        # action outcome — block — is what the caller received).
        outcome.finish(outcome_state=OUTCOME_COMPLETED)
        return JSONResponse(status_code=403, content={
            "error": {"message": BLOCK_MESSAGE, "code": "blocked_by_policy"}})
    if decision.kind != "route" or not decision.model:
        outcome.finish(outcome_state=OUTCOME_COMPLETED, error_class="no_route")
        return JSONResponse(status_code=502,
                            content={"error": {"message": "no_route"}})
    if backend is None:
        outcome.finish(outcome_state=OUTCOME_COMPLETED,
                       error_class="unknown_model")
        return JSONResponse(status_code=502,
                            content={"error": {"message": "unknown_model"}})

    breaker = request.app.state.breaker
    if breaker.is_open(decision.model):
        outcome.finish(outcome_state=OUTCOME_COMPLETED,
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
                                 breaker, decision.model, outcome=outcome,
                                 request=request, started=started)
        resp = await client.post(backend.url, json=upstream_body,
                                 headers=backend.headers)
    except httpx.HTTPError:
        breaker.failure(decision.model)
        outcome.finish(outcome_state=OUTCOME_UPSTREAM_ERROR,
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
        outcome.finish(outcome_state=OUTCOME_UPSTREAM_ERROR,
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
    outcome.finish(
        outcome_state=OUTCOME_COMPLETED,
        error_class=None if resp.status_code < 400 else "client_4xx",
        upstream_model=backend.upstream_name,
        upstream_status=resp.status_code,
        upstream_ms=upstream_ms,
        usage=content.get("usage") if isinstance(content, dict) else None)
    return JSONResponse(status_code=resp.status_code, content=content,
                        headers=headers)


def _relay(resp, *, outcome, request, backend, upstream_started: float,
           upstream_ms: float):
    """The real SSE relay: classify the terminal state, then finalize once.

    Returned as an async generator so the streaming tests can drive every
    terminal path (normal EOF, upstream exception, client disconnect before the
    first chunk, mid-stream disconnect via ``aclose()``, cancellation) without
    a network stack. ``GeneratorExit``/``CancelledError`` are re-raised: the
    disconnect/cancel must propagate, only the ledger classification differs.
    """
    async def generator():
        usage_sniffer = _SseUsage()
        first_chunk_at: float | None = None
        state = OUTCOME_INCOMPLETE
        try:
            # A client that already went away before the first chunk: nothing
            # will be sent, so the request is a disconnect, not a completion.
            if await _is_disconnected(request):
                state = OUTCOME_CLIENT_DISCONNECTED
                return
            async for chunk in resp.aiter_raw():
                if first_chunk_at is None:       # TTFT: first byte of the body
                    first_chunk_at = time.monotonic()
                usage_sniffer.feed(chunk)
                yield chunk
            # Only a normally exhausted upstream iterator is "completed".
            state = OUTCOME_COMPLETED
        except GeneratorExit:
            # The consumer closed us (client disconnected / response aborted).
            state = OUTCOME_CLIENT_DISCONNECTED
            raise
        except asyncio.CancelledError:
            # Task cancellation; a concurrent detected disconnect is still a
            # disconnect. Re-raise so the cancellation is never swallowed.
            state = (OUTCOME_CLIENT_DISCONNECTED
                     if await _is_disconnected(request) else OUTCOME_CANCELLED)
            raise
        except Exception:                        # noqa: BLE001 - upstream/transport
            state = OUTCOME_UPSTREAM_ERROR
            raise
        finally:
            try:
                await resp.aclose()
            except Exception:                    # noqa: BLE001 - close is best effort
                logger.warning("upstream stream close failed", exc_info=True)
            ttft_ms = (None if first_chunk_at is None
                       else (first_chunk_at - upstream_started) * 1000.0)
            usage_sniffer.finish()               # trailing line without '\n'
            outcome.finish(
                outcome_state=state,
                error_class=("upstream_error"
                             if state == OUTCOME_UPSTREAM_ERROR else None),
                upstream_model=backend.upstream_name,
                upstream_status=resp.status_code,
                upstream_ms=upstream_ms,
                ttft_ms=ttft_ms,
                usage=usage_sniffer.usage)

    return generator()


async def _stream(client, backend, body, headers, breaker, model, *,
                  outcome, request, started):
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
            outcome.finish(outcome_state=OUTCOME_UPSTREAM_ERROR,
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
        outcome.finish(outcome_state=OUTCOME_COMPLETED,
                       error_class="client_4xx",
                       upstream_model=backend.upstream_name,
                       upstream_status=resp.status_code,
                       upstream_ms=upstream_ms)
        return JSONResponse(status_code=resp.status_code, content=content,
                            headers=headers)
    breaker.success(model)
    return StreamingResponse(
        _relay(resp, outcome=outcome, request=request, backend=backend,
               upstream_started=upstream_started, upstream_ms=upstream_ms),
        media_type="text/event-stream", headers=headers)
