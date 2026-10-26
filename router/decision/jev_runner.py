"""JEV-backed :class:`DecisionRunner` adapter (async, serial, wall-clocked).

The routing tree calls one ``nl_choice`` node at a time but the JEV model is a
single in-process GPU model: concurrent forwards would interleave.  This
adapter owns that constraint so the tree evaluator never deals with threads,
locks or timeouts:

* **serial gate** — an :class:`asyncio.Lock` wraps the whole blocking call, so
  one request's NL steps queue behind each other and later NL steps cannot
  overlap a still-running forward;
* **off the event loop** — the blocking ``tagger.score_choice`` runs in
  :func:`asyncio.to_thread`; the routing loop keeps serving other requests;
* **timeout that never lies** — the wait is wrapped in
  ``asyncio.wait_for(asyncio.shield(operation), timeout)``.  A timeout only
  reports ``TimeoutError`` to the routing layer (which records ``nl_error`` and
  follows the node's ``otherwise``/``Miss``); the worker thread keeps the lock
  until the forward really finishes.  ``wait_for`` cannot stop a GPU call, so
  claiming otherwise would be wrong: the next NL call still waits its turn;
* **real duration** — ``ChoiceResult.duration_ms`` is the wall-clock time of
  the whole (possibly queued) call, recorded in the request trace;
* **evidence** — ``ChoiceResult.model_info`` merges ``tagger.info()`` (model
  fingerprints / revision for the snapshot) with the decoding temperature.

This module knows nothing about trees, routing models or ``otherwise``: it only
converts :class:`ChoiceRequest` -> ``score_choice`` arguments ->
:class:`ChoiceResult`.  ``JevTagger.score_choice`` itself is untouched
(2026-10-26 unified routing tree, Task 5).
"""

from __future__ import annotations

import asyncio
import time

from router.decision.runner import ChoiceRequest, ChoiceResult


class JevDecisionRunner:
    """Run the JEV ``choice`` question for routing-tree NL nodes."""

    def __init__(self, tagger, *, timeout: float | None = None):
        self._tagger = tagger
        self._timeout = timeout
        self._lock = asyncio.Lock()

    def info(self) -> dict:
        """The tagger's version evidence (used by the routing snapshot)."""
        info = getattr(self._tagger, "info", lambda: {})()
        return dict(info)

    async def _call(self, request: ChoiceRequest) -> dict:
        async with self._lock:
            candidates = {option.key: option.text for option in request.options}
            return await asyncio.to_thread(
                self._tagger.score_choice,
                request.state, request.instructions, candidates)

    async def choose(self, request: ChoiceRequest) -> ChoiceResult:
        started = time.monotonic()
        operation = self._call(request)
        if self._timeout is None:
            raw = await operation
        else:
            raw = await asyncio.wait_for(
                asyncio.shield(operation), timeout=self._timeout)
        return ChoiceResult(
            probabilities=raw["probabilities"],
            input=raw.get("input") or {},
            model_info={**self.info(), "temperature": raw.get("temperature")},
            duration_ms=(time.monotonic() - started) * 1000)
