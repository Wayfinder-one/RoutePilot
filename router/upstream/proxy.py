"""Upstream transport for ``/v1/chat/completions``: POST, SSE relay, lease.

Resource ownership (one owner per resource, no guessing):

* ``send()`` (non-streaming) releases the routing lease in its own method
  scope and never returns an open upstream response.
* ``stream()`` hands a ``StreamingResponse`` to the ASGI server only for a
  successful upstream status.  That response's generator owns **both** the
  httpx response and the lease and closes each exactly once in its ``finally``.
  Every ``stream()`` exit that does not return the generator (transport
  failure, cancellation, 4xx/5xx status) closes/releases in method scope.

The routing handler no longer tracks who releases: the old
``release_in_handler`` flag is gone, and both callables are wrapped in a
one-shot guard so a path change cannot double-release.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from router.api.request_presenter import (
    OUTCOME_CANCELLED,
    OUTCOME_CLIENT_DISCONNECTED,
    OUTCOME_COMPLETED,
    OUTCOME_INCOMPLETE,
    OUTCOME_UPSTREAM_ERROR,
    OutcomeRecorder,
)
from router.reasoning import ReasoningError, apply_reasoning
from router.upstream.breaker import CircuitBreaker
from router.upstream.catalog import Backend

logger = logging.getLogger(__name__)

MAX_SSE_LINE_BYTES = 1024 * 1024
STREAM_TIMEOUT = httpx.Timeout(60.0, read=None)


def prepare_upstream_request(body: dict, backend: Backend, reasoning_level):
    """Apply the backend's upstream model name and (best-effort) reasoning body.

    A rejected reasoning injection degrades to a passthrough request with the
    ``unsupported`` warning; it never fails the request.
    """
    upstream_body = {**body, "model": backend.upstream_name}
    if reasoning_level is None:
        return upstream_body, None
    try:
        return apply_reasoning(upstream_body, backend.reasoning, reasoning_level), None
    except ReasoningError as exc:
        logger.warning("reasoning fallback model=%s level=%s: %s",
                       backend.upstream_name, reasoning_level, exc)
        return upstream_body, "unsupported"


def _once(callback: Callable[[], None]) -> Callable[[], None]:
    """Wrap a release callable so it runs at most once for this exchange."""
    called = False

    def run() -> None:
        nonlocal called
        if called:
            return
        called = True
        callback()

    return run


def _upstream_error(headers) -> JSONResponse:
    return JSONResponse(status_code=502, content={"error": {"message": "upstream_error"}},
                        headers=headers)


async def _is_disconnected(request) -> bool:
    if request is None:
        return False
    try:
        return bool(await request.is_disconnected())
    except BaseException:
        return False


async def _close_upstream(response) -> None:
    try:
        await response.aclose()
    except Exception:
        logger.warning("upstream stream close failed", exc_info=True)


class _SseUsage:
    """Sniff ``usage`` out of upstream SSE ``data:`` lines without buffering all."""

    def __init__(self):
        self._buf = b""
        self.usage: dict | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            newline = self._buf.find(b"\n")
            if newline < 0:
                break
            line, self._buf = self._buf[:newline], self._buf[newline + 1:]
            self._scan(line)
        if len(self._buf) > MAX_SSE_LINE_BYTES:
            self._buf = b""

    def finish(self) -> None:
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
            value = json.loads(payload)
        except (ValueError, TypeError):
            return
        if isinstance(value, dict) and isinstance(value.get("usage"), dict):
            self.usage = value["usage"]


class UpstreamProxy:
    """One upstream exchange for one routed decision."""

    def __init__(self, client: httpx.AsyncClient, breaker: CircuitBreaker):
        self.client = client
        self.breaker = breaker

    async def send(self, *, backend: Backend, body: dict, headers: dict,
                   model: str, outcome: OutcomeRecorder, release) -> JSONResponse:
        """Non-streaming POST; the lease is released in this method scope."""
        release_once = _once(release)
        started = time.monotonic()
        try:
            response = await self.client.post(
                backend.url, json=body, headers=backend.headers)
            upstream_ms = (time.monotonic() - started) * 1000.0
            if response.status_code >= 500:
                self.breaker.failure(model)
                outcome.finish(
                    outcome_state=OUTCOME_UPSTREAM_ERROR, error_class="upstream_error",
                    upstream_model=backend.upstream_name,
                    upstream_status=response.status_code, upstream_ms=upstream_ms)
                return _upstream_error(headers)
            if response.status_code < 400:
                self.breaker.success(model)
            try:
                content = response.json()
            except json.JSONDecodeError:
                content = {"raw": response.text}
            outcome.finish(
                outcome_state=OUTCOME_COMPLETED,
                error_class=None if response.status_code < 400 else "client_4xx",
                upstream_model=backend.upstream_name,
                upstream_status=response.status_code, upstream_ms=upstream_ms,
                usage=content.get("usage") if isinstance(content, dict) else None)
            return JSONResponse(status_code=response.status_code, content=content,
                                headers=headers)
        except asyncio.CancelledError:
            outcome.finish(
                outcome_state=OUTCOME_CANCELLED, error_class="cancelled",
                upstream_model=backend.upstream_name,
                upstream_ms=(time.monotonic() - started) * 1000.0)
            raise
        except httpx.HTTPError:
            self.breaker.failure(model)
            outcome.finish(
                outcome_state=OUTCOME_UPSTREAM_ERROR, error_class="upstream_error",
                upstream_model=backend.upstream_name,
                upstream_ms=(time.monotonic() - started) * 1000.0)
            return _upstream_error(headers)
        finally:
            release_once()

    async def stream(self, *, backend: Backend, body: dict, headers: dict,
                     model: str, outcome: OutcomeRecorder, request,
                     release) -> StreamingResponse | JSONResponse:
        """Streaming POST; a successful response hands ownership to the relay."""
        release_once = _once(release)
        handed_off = False
        started = time.monotonic()
        try:
            upstream_request = self.client.build_request(
                "POST", backend.url, json=body, headers=backend.headers,
                timeout=STREAM_TIMEOUT,
            )
            response = await self.client.send(upstream_request, stream=True)
            upstream_ms = (time.monotonic() - started) * 1000.0
            if response.status_code >= 400:
                try:
                    raw = await response.aread()
                    if response.status_code >= 500:
                        self.breaker.failure(model)
                        outcome.finish(
                            outcome_state=OUTCOME_UPSTREAM_ERROR,
                            error_class="upstream_error",
                            upstream_model=backend.upstream_name,
                            upstream_status=response.status_code,
                            upstream_ms=upstream_ms)
                        return _upstream_error(headers)
                    try:
                        content = json.loads(raw)
                    except (ValueError, TypeError):
                        content = {"raw": raw.decode("utf-8", "replace")}
                    outcome.finish(
                        outcome_state=OUTCOME_COMPLETED, error_class="client_4xx",
                        upstream_model=backend.upstream_name,
                        upstream_status=response.status_code, upstream_ms=upstream_ms)
                    return JSONResponse(status_code=response.status_code,
                                        content=content, headers=headers)
                finally:
                    # This method owns the response on the non-streaming exits.
                    await _close_upstream(response)
            self.breaker.success(model)
            streamed = StreamingResponse(
                self._relay(
                    response, backend=backend, model=model, outcome=outcome,
                    request=request, upstream_started=started,
                    upstream_ms=upstream_ms, release=release_once),
                media_type="text/event-stream",
                headers=headers,
            )
            handed_off = True
            return streamed
        except asyncio.CancelledError:
            outcome.finish(
                outcome_state=OUTCOME_CANCELLED, error_class="cancelled",
                upstream_model=backend.upstream_name,
                upstream_ms=(time.monotonic() - started) * 1000.0)
            raise
        except httpx.HTTPError:
            self.breaker.failure(model)
            outcome.finish(
                outcome_state=OUTCOME_UPSTREAM_ERROR, error_class="upstream_error",
                upstream_model=backend.upstream_name,
                upstream_ms=(time.monotonic() - started) * 1000.0)
            return _upstream_error(headers)
        finally:
            # The relay generator owns the lease once the response is handed
            # off; every other exit releases here.
            if not handed_off:
                release_once()

    async def _relay(self, response, *, backend: Backend, model: str,
                     outcome: OutcomeRecorder, request, upstream_started: float,
                     upstream_ms: float, release):
        """SSE relay; owns ``response`` and the lease, closing each once here."""
        usage_sniffer = _SseUsage()
        first_chunk_at: float | None = None
        state = OUTCOME_INCOMPLETE
        try:
            if await _is_disconnected(request):
                state = OUTCOME_CLIENT_DISCONNECTED
                return
            async for chunk in response.aiter_raw():
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                usage_sniffer.feed(chunk)
                yield chunk
            state = OUTCOME_COMPLETED
        except GeneratorExit:
            state = OUTCOME_CLIENT_DISCONNECTED
            raise
        except asyncio.CancelledError:
            state = (OUTCOME_CLIENT_DISCONNECTED if await _is_disconnected(request)
                     else OUTCOME_CANCELLED)
            raise
        except Exception:
            # An upstream stream that dies after headers is an upstream fault
            # (breaker), unlike a client disconnect handled above.
            state = OUTCOME_UPSTREAM_ERROR
            self.breaker.failure(model)
            raise
        finally:
            await _close_upstream(response)
            ttft_ms = None if first_chunk_at is None else (first_chunk_at - upstream_started) * 1000.0
            usage_sniffer.finish()
            outcome.finish(
                outcome_state=state,
                error_class="upstream_error" if state == OUTCOME_UPSTREAM_ERROR else None,
                upstream_model=backend.upstream_name,
                upstream_status=response.status_code,
                upstream_ms=upstream_ms,
                ttft_ms=ttft_ms,
                usage=usage_sniffer.usage,
            )
            release()
