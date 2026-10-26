"""Shared routing runtime: signal preparation + tree evaluation for every path.

The online ``/v1`` hot path, ``/admin/try`` and eval replay all funnel through
this one async flow so they cannot drift into three slightly different routing
decisions (design §6):

1. **no enabled tree** -> :class:`NoActiveTreeError` BEFORE any tagging or
   decision work (``/v1`` answers 503 ``no_active_tree``);
2. **prepare the whole required signal pack once** — the snapshot's static
   ``required_signals`` drives it: a pure natural-language tree skips tagging
   entirely, a rule-only tree tags with an empty trimmed scope so no model
   forward pass runs, a mixed tree tags exactly once;
3. **strictly validate** the pack (:func:`validate_signal_pack`): a missing or
   malformed required signal is a 503 ``signal_preparation_failed``, never a
   normal branch Miss;
4. **evaluate** the snapshot's compiled tree with ``PreparedSignalReader`` and
   the ``DecisionRunner``.

The DecisionRunner's own exceptions are NOT caught here: ``nl_choice`` converts
them into a local ``nl_error`` Miss (a node-level failure, not a signal
preparation failure).  The runtime aggregates ``nl_call=True`` trace events into
``nl_calls``/``decision_ms`` — no shared counters, so concurrent requests can
never mix evidence.

Pure module: no DB, no FastAPI. The immutable snapshot is the only config read.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from router.decision.runner import DecisionRunner
from router.pipeline.errors import NoActiveTreeError, SignalPreparationError
from router.pipeline.evaluator import EvalResult, TreeEvaluator
from router.pipeline.reader import PreparedSignalReader, validate_signal_pack
from router.pipeline.snapshot import RoutingSnapshot, thaw_data
from router.signals.tagger import Tagger, TagRequest


@dataclass
class RuntimeRequest:
    """One request to route: the canonical text plus tagging inputs."""

    text: str
    headers: dict = field(default_factory=dict)
    n_messages: int = 1


@dataclass
class RoutingResult:
    """One routing outcome plus the evidence a response/ledger records."""

    outcome: EvalResult
    signals: dict
    trace: tuple[dict, ...]
    signal_ms: float
    decision_ms: float
    nl_calls: int
    input: dict
    questions_sha256: str | None
    tagger_info: dict
    snapshot_revision: str


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _event_duration_ms(event: dict) -> float:
    """One nl_call event's decision time (never trusts a malformed value)."""
    value = event.get("duration_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value) and value >= 0:
        return float(value)
    return 0.0


class RoutingRuntime:
    """The single routing flow shared by /v1, /admin/try and eval replay."""

    def __init__(self, tagger: Tagger, runner: DecisionRunner) -> None:
        self._tagger = tagger
        self._runner = runner

    async def route(self, snapshot: RoutingSnapshot,
                    request: RuntimeRequest) -> RoutingResult:
        """Route one request on one immutable snapshot."""
        tree = snapshot.tree
        if tree is None:
            raise NoActiveTreeError("no routing tree is enabled")

        signals: dict = {}
        input_info: dict = {}
        questions_sha256: str | None = None
        tagger_info: dict = {}
        started = time.perf_counter()
        # A tree that references no ordinary signal (the pure natural-language
        # case) needs no tagging at all: the tagger is not called.  Everything
        # else gets ONE batched tag call; trimming is a snapshot policy, and
        # "no trimming" means the full-pack semantics (scope=None).
        if snapshot.required_signals:
            request_scope = snapshot.scope if snapshot.jev_trim else None
            try:
                tag_result = await self._tagger.tag(TagRequest(
                    text=request.text,
                    headers=dict(request.headers or {}),
                    n_messages=request.n_messages,
                    scope=request_scope,
                    # Fixed question config from the immutable snapshot; each
                    # request gets its own mutable deep copy (MappingProxy is
                    # not a dict and JEV may need to own it).
                    questions=thaw_data(snapshot.questions)))
            except SignalPreparationError:
                raise
            except Exception as exc:            # noqa: BLE001 - infra failure
                raise SignalPreparationError(
                    f"signal preparation failed: {type(exc).__name__}: {exc}"
                ) from exc
            signals = dict(tag_result.signals or {})
            input_info = dict(tag_result.input or {})
            questions_sha256 = tag_result.questions_sha256
            tagger_info = dict(tag_result.tagger_info or {})
        signal_ms = _elapsed_ms(started)

        # Strict boundary: a broken pack is a 503, never a branch Miss.
        validate_signal_pack(snapshot.required_signals, signals)

        outcome = await TreeEvaluator(
            PreparedSignalReader(signals), self._runner).evaluate(
                tree.root, request.text)
        nl_events = [event for event in outcome.trace
                     if event.get("nl_call")]
        return RoutingResult(
            outcome=outcome,
            signals=signals,
            trace=outcome.trace,
            signal_ms=signal_ms,
            decision_ms=round(sum(_event_duration_ms(event)
                                  for event in nl_events), 3),
            nl_calls=len(nl_events),
            input=input_info,
            questions_sha256=questions_sha256,
            tagger_info=tagger_info,
            snapshot_revision=snapshot.snapshot_revision)
