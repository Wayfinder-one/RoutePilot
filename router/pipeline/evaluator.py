"""Asynchronous unified routing-tree evaluator (Hit/Miss semantics).

One embedded tree, four node types, one evaluation rule set (2026-10-26 unified
routing tree, Task 4).  The evaluator is a pure async walker: it owns no
database, no web framework and no model framework.  Its only collaborators are

* a **signal reader** — ``read(key) -> signal dict`` — the strict
  :class:`router.pipeline.reader.PreparedSignalReader` in production (a missing
  or malformed required signal raises ``SignalPreparationError`` and must abort
  the request, never become a normal miss);
* a **decision runner** — :class:`router.decision.runner.DecisionRunner` — used
  by ``nl_choice`` nodes only; the concrete adapter owns threading/timeouts and
  every reached node calls it at most once.

Semantics (design doc §5):

* ``when`` only decides whether a node is entered.  A failed condition returns
  a ``Miss`` immediately and **never** executes that node's own ``otherwise`` —
  the parent keeps walking its remaining siblings.
* ``branch`` walks its ordered children.  A ``Miss`` continues; a weighted
  ``Hit`` is kept as a candidate; a direct ``Hit`` (``score is None``) returns
  immediately and discards earlier weighted candidates.  After the loop the
  highest-score weighted candidate wins (ties keep the earlier one); only when
  nothing hit does the branch run its own ``otherwise``.
* ``nl_choice`` calls the runner once with the node's ordered candidates,
  validates the distribution, ranks by ``(-probability, configuration index)``,
  applies ``p_top >= min_prob`` and ``margin >= min_margin`` (equality passes)
  and enters the selected target.  A failed call/gate or a selected child
  ``Miss`` has **no** fallback candidate: it goes to the node's ``otherwise``
  (or becomes the ``Miss``).  No second runner call, never a second candidate.
* a selected target's ``Hit`` is passed through unchanged (including a weighted
  ``score``), so a branch applies its normal direct/weighted rule to it.

Trace events (new tuples, child traces are never mutated).  Every event is a
plain dict with ``node_id`` and ``event``; the standard reason codes are design
doc §10's (``condition_false`` / ``below_confidence`` / ``nl_low_probability``
/ ``nl_ambiguous`` / ``nl_error`` / ``selected_child_miss`` / ``otherwise_hit``
plus ``below_min_score`` / ``no_hit`` for the signal side):

* ``condition`` — a ``when`` check: signal/op/value, ``m``, ``conf``,
  ``conf_min``, ``pass`` and the failure ``reason``.
* ``hit`` / ``miss`` — a terminal node: kind/model/reasoning/score; a weighted
  terminal also carries ``min_score`` and the ``leaf_score`` ``lines``.
* ``nl_choice`` — one per real runner call (``nl_call=True``): ordered
  ``candidates`` keys, ``probabilities`` (candidate order), ``ranking``,
  ``selected_key``, thresholds, ``p_top``/``margin``, ``reason`` (or null),
  ``duration_ms`` plus the runner's ``input``/``model_info`` evidence.
* ``selected_child_miss`` — the chosen NL target returned a ``Miss``.
* ``otherwise_hit`` / ``otherwise_miss`` — an entered branch/NL node ran its
  explicit fallback.

Pure module: no torch, no sqlite, no FastAPI.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

from router.decision.runner import (ChoiceOption, ChoiceRequest, DecisionRunner)
from router.pipeline.matching import is_rule_signal, match_row
from router.pipeline.scoring import leaf_score
from router.pipeline.tree import Block, Branch, NLChoice, Node, Route

#: Default confidence floor for a model-signal ``when`` (design doc §4.1: the
#: default keeps the legacy signal-tree value; rule signals ignore the floor).
DEFAULT_CONF_MIN = 0.70

#: Tolerance for "the runner returned a normalised distribution".
PROBABILITY_SUM_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Hit:
    """The tree reached a terminal target (``route`` or ``block``)."""

    kind: str
    model: str | None
    reasoning: str | None
    node_id: str
    score: float | None
    trace: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Miss:
    """The evaluated path produced no terminal target."""

    reason: str
    trace: tuple[dict, ...] = ()


EvalResult = Hit | Miss


@dataclass(frozen=True)
class EvalContext:
    """Per-request evaluation state (the canonical request text)."""

    request_text: str


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _runner_duration_ms(result, measured_ms: float) -> float:
    """The adapter's own wall time when it reported one, else our measurement."""
    reported = getattr(result, "duration_ms", None)
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) \
            and math.isfinite(reported) and reported >= 0:
        return float(reported)
    return measured_ms


def _dict_or_empty(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


class TreeEvaluator:
    """Evaluate one unified routing tree to a :class:`Hit` or :class:`Miss`."""

    def __init__(self, reader, runner: DecisionRunner) -> None:
        self._reader = reader
        self._runner = runner

    async def evaluate(self, root: Node, request_text: str) -> EvalResult:
        """Walk ``root`` for one request; signal/NL failures never become Miss."""
        return await self._eval_node(root, EvalContext(request_text=request_text))

    # ── dispatch ──────────────────────────────────────────────────────────
    async def _eval_node(self, node: Node, context: EvalContext) -> EvalResult:
        entered, step = self._eval_when(node, context)
        if not entered:
            return Miss(step["reason"], (step,))
        if isinstance(node, (Route, Block)):
            return self._eval_terminal(node, context)
        if isinstance(node, Branch):
            return await self._eval_branch(node, context)
        if isinstance(node, NLChoice):
            return await self._eval_nl_choice(node, context)
        raise TypeError(f"not a routing tree node: {node!r}")

    # ── when: entering a node ─────────────────────────────────────────────
    def _eval_when(self, node: Node,
                   context: EvalContext) -> tuple[bool, dict | None]:
        """Check the node's own ``when``; returns (entered, trace step).

        A node without ``when`` is always entered and emits no event.  A model
        signal must both match and clear ``conf_min`` (default
        :data:`DEFAULT_CONF_MIN`); rule signals ignore the confidence floor.
        """
        when = node.when
        if when is None:
            return True, None
        signals = {when.signal: self._reader.read(when.signal)}
        m, conf = match_row(when.signal, when.value, signals)
        rule = is_rule_signal(when.signal)
        floor = None if rule else (
            DEFAULT_CONF_MIN if when.conf_min is None else when.conf_min)
        passed = m == 1 and (rule or conf >= floor)
        reason = None
        if not passed:
            reason = "condition_false" if m != 1 else "below_confidence"
        return passed, {"node_id": node.id, "event": "condition",
                        "signal": when.signal, "op": when.op,
                        "value": when.value, "m": m,
                        "conf": 1.0 if rule else conf, "conf_min": floor,
                        "pass": passed, "reason": reason}

    # ── terminals: route / block ──────────────────────────────────────────
    def _eval_terminal(self, node: Route | Block,
                       context: EvalContext) -> EvalResult:
        """Direct hit, or the weighted attainment rule (``leaf_score``)."""
        kind = "block" if isinstance(node, Block) else "route"
        model = None if isinstance(node, Block) else node.model
        reasoning = None if isinstance(node, Block) else node.reasoning
        if not node.weights:
            hit = Hit(kind=kind, model=model, reasoning=reasoning,
                      node_id=node.id, score=None)
            return replace(hit, trace=(
                {"node_id": hit.node_id, "event": "hit", "kind": hit.kind,
                 "model": hit.model, "reasoning": hit.reasoning,
                 "score": hit.score},))
        signals = {weight.signal: self._reader.read(weight.signal)
                   for weight in node.weights}
        scored = leaf_score(list(node.weights), signals, node.min_score)
        event = {"node_id": node.id, "event": "hit" if scored["passed"]
                 else "miss", "kind": kind, "model": model,
                 "reasoning": reasoning, "score": scored["score"],
                 "min_score": scored["min_score"], "lines": scored["lines"]}
        if not scored["passed"]:
            event["reason"] = "below_min_score"
            return Miss("below_min_score", (event,))
        hit = Hit(kind=kind, model=model, reasoning=reasoning,
                  node_id=node.id, score=scored["score"])
        return replace(hit, trace=(event,))

    # ── branch: ordered children, direct > weighted > otherwise ────────────
    async def _eval_branch(self, node: Branch,
                           context: EvalContext) -> EvalResult:
        weighted: list[Hit] = []
        traces: list[dict] = []
        for child in node.children:
            result = await self._eval_node(child, context)
            traces.extend(result.trace)
            if isinstance(result, Miss):
                continue
            if result.score is None:
                return replace(result, trace=tuple(traces))
            weighted.append(result)
        if weighted:
            best = max(weighted, key=lambda hit: hit.score)
            return replace(best, trace=tuple(traces))
        return await self._eval_otherwise(node, Miss("no_hit", tuple(traces)),
                                          context)

    # ── nl_choice: one runner call, no candidate backtracking ─────────────
    async def _eval_nl_choice(self, node: NLChoice,
                              context: EvalContext) -> EvalResult:
        request = ChoiceRequest(
            state=context.request_text, instructions=node.instructions,
            options=tuple(ChoiceOption(key=option.key, text=option.text)
                          for option in node.options))
        started = time.perf_counter()
        try:
            result = await self._runner.choose(request)
        except Exception as exc:                # noqa: BLE001 - NL -> Miss
            event = self._nl_event(
                node, reason="nl_error", duration_ms=_elapsed_ms(started),
                error=f"{type(exc).__name__}: {exc}")
            return await self._eval_otherwise(
                node, Miss("nl_error", (event,)), context)
        measured_ms = _elapsed_ms(started)
        duration_ms = _runner_duration_ms(result, measured_ms)
        probabilities = getattr(result, "probabilities", None)
        keys = [option.key for option in node.options]
        evidence = {
            "duration_ms": duration_ms,
            "input": _dict_or_empty(getattr(result, "input", None)),
            "model_info": _dict_or_empty(getattr(result, "model_info", None)),
        }

        error = self._validate_probabilities(node, probabilities)
        if error is not None:
            event = self._nl_event(
                node, reason="nl_error", error=error,
                probabilities=self._trace_probabilities(keys, probabilities),
                **evidence)
            return await self._eval_otherwise(
                node, Miss("nl_error", (event,)), context)

        ordered = {key: float(probabilities[key]) for key in keys}
        ranking = [key for _, key in sorted(
            enumerate(keys), key=lambda item: (-ordered[item[1]], item[0]))]
        p_top = ordered[ranking[0]]
        margin = p_top - ordered[ranking[1]]
        event = self._nl_event(node, reason=None, probabilities=ordered,
                               ranking=ranking, p_top=p_top, margin=margin,
                               **evidence)
        if p_top < node.min_prob:
            event["reason"] = "nl_low_probability"
            return await self._eval_otherwise(
                node, Miss("nl_low_probability", (event,)), context)
        if margin < node.min_margin:
            event["reason"] = "nl_ambiguous"
            return await self._eval_otherwise(
                node, Miss("nl_ambiguous", (event,)), context)

        selected = next(option for option in node.options
                        if option.key == ranking[0])
        event["selected_key"] = selected.key
        child = await self._eval_node(selected.target, context)
        if isinstance(child, Miss):
            marker = {"node_id": node.id, "event": "selected_child_miss",
                      "reason": "selected_child_miss",
                      "selected_key": selected.key,
                      "selected_node_id": selected.target.id}
            return await self._eval_otherwise(
                node, Miss("selected_child_miss",
                           (event,) + child.trace + (marker,)), context)
        return replace(child, trace=(event,) + child.trace)

    @staticmethod
    def _nl_event(node: NLChoice, *, reason, duration_ms: float,
                  probabilities: dict | None = None, ranking=(),
                  p_top: float | None = None, margin: float | None = None,
                  selected_key: str | None = None, error: str | None = None,
                  input: dict | None = None,
                  model_info: dict | None = None) -> dict:
        event = {"node_id": node.id, "event": "nl_choice", "nl_call": True,
                 "candidates": [option.key for option in node.options],
                 "probabilities": probabilities or {},
                 "ranking": list(ranking), "selected_key": selected_key,
                 "min_prob": node.min_prob, "min_margin": node.min_margin,
                 "p_top": p_top, "margin": margin, "reason": reason,
                 "duration_ms": duration_ms, "input": dict(input or {}),
                 "model_info": dict(model_info or {})}
        if error is not None:
            event["error"] = error
        return event

    @staticmethod
    def _validate_probabilities(node: NLChoice, probabilities) -> str | None:
        """Validate the runner answer; a non-None return is an ``nl_error``."""
        keys = [option.key for option in node.options]
        if len(keys) < 2:
            return "choice needs at least two candidates"
        if not isinstance(probabilities, dict):
            return "probabilities must be a mapping"
        if set(probabilities) != set(keys) or len(probabilities) != len(keys):
            return "probabilities must cover exactly the configured candidates"
        total = 0.0
        for key in keys:
            value = probabilities[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"probability for {key!r} must be a number"
            number = float(value)
            if not math.isfinite(number):
                return f"probability for {key!r} must be finite"
            if number < 0.0:
                return f"probability for {key!r} must be non-negative"
            total += number
        if not math.isclose(total, 1.0, rel_tol=0.0,
                            abs_tol=PROBABILITY_SUM_TOLERANCE):
            return f"probabilities must sum to 1.0, got {total!r}"
        return None

    @staticmethod
    def _trace_probabilities(keys: list[str], probabilities) -> dict:
        """JSON-safe subset of a malformed answer (trace evidence only)."""
        if not isinstance(probabilities, dict):
            return {}
        out: dict[str, float] = {}
        for key in keys:
            value = probabilities.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(value):
                out[key] = float(value)
        return out

    # ── otherwise: the explicit fallback of an entered branch/NL node ──────
    async def _eval_otherwise(self, node: Branch | NLChoice, miss: Miss,
                              context: EvalContext) -> EvalResult:
        """Run ``node.otherwise``, keeping the failure events and its trace."""
        otherwise = node.otherwise
        if otherwise is None:
            return miss
        result = await self._eval_node(otherwise, context)
        if isinstance(result, Miss):
            event = {"node_id": otherwise.id, "event": "otherwise_miss",
                     "reason": result.reason}
            return replace(result, trace=miss.trace + result.trace + (event,))
        event = {"node_id": result.node_id, "event": "otherwise_hit"}
        return replace(result, trace=miss.trace + result.trace + (event,))
