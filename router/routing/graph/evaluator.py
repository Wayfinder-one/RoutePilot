"""Graph evaluation: three-valued fixed point, deterministic slots, waves.

``GraphEvaluator`` is a pure decision pipeline over an immutable
``RoutingSnapshot``.  It owns graph semantics only: condition initialisation,
gate fixed points, slot traces, question activation, wave staging, candidate
priority and target selection.  It never reads session affinity, never acquires
a lease and never emits scheduling/presentation events.

A failed wave voids the whole round (no port fills, all candidates discarded)
and routes to ``error_fallback``.  JEV calls catch only ``JevError``; a
programming defect (for example a malformed question built from an invalid
graph) propagates unchanged.

Trace events are the console replay contract defined in spec §3.9.  They carry
machine fields only; human-readable presentation is applied at the API edge.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

from router.jev.client import (
    ChoiceAnswer,
    DecisionQuestion,
    JevAnswer,
    JevCandidate,
    JevError,
    NoulAnswer,
    ScoreAnswer,
)
from router.jev.window import apply_window
from router.routing.conditions import matching_periods
from router.routing.graph.conditions import UNKNOWN, ConditionResult, evaluate_condition
from router.routing.graph.model import (
    ConditionNode,
    Edge,
    LogicNode,
    QuestionNode,
    StartNode,
    TargetNode,
)
from router.routing.snapshot import RoutingSnapshot
from router.routing.types import (
    Candidate,
    NoActiveTreeError,
    RoutingMiss,
    RoutingRequest,
    WaveTrace,
)

_MAX_WAVES = 32
_ANSWER_TYPES = {"choice": ChoiceAnswer, "noul": NoulAnswer, "score": ScoreAnswer}


@dataclass(frozen=True)
class GraphOutcome:
    """The complete graph decision before any scheduling concern.

    ``fallback_reason`` keeps the externally visible value (``error_fallback``)
    for both a failed wave and a normal run without an activated target.
    ``failure_reason`` is diagnostics only (the failed wave's error text).
    """

    target: TargetNode
    candidates: tuple[Candidate, ...]
    waves: tuple[WaveTrace, ...]
    answers: Mapping[str, JevAnswer]
    fallback_reason: str | None
    failure_reason: str | None

    @property
    def kind(self) -> str:
        """Trace ``final`` kind: failed wave / activated target / no candidate."""
        if self.failure_reason is not None:
            return "error_fallback"
        return "target" if self.candidates else "otherwise"

    @property
    def selection_reason(self) -> str:
        """Public selection reason before sticky is applied."""
        return "target" if self.fallback_reason is None else "error_fallback"


def combine_gate(operator, values):
    """Three-valued logic gate combination (spec §3.4, brief verbatim)."""
    if operator == "not":
        return UNKNOWN if len(values) != 1 or values[0] == UNKNOWN else not values[0]
    if operator == "and":
        if any(value is False for value in values):
            return False
        if len(values) < 2 or any(value == UNKNOWN for value in values):
            return UNKNOWN
        return True
    if operator == "or":
        if any(value is True for value in values):
            return True
        if len(values) < 2 or any(value == UNKNOWN for value in values):
            return UNKNOWN
        return False
    # 校验层已拦截非法算子；这里显式失败，绝不把未知算子静默当作 OR。
    raise ValueError(f"unknown logic operator {operator!r}")


def _condition_document(edge: Edge):
    condition = edge.condition
    if condition is None:
        return None
    return {"op": condition.op, "value": condition.value}


def _answer_ports(node: QuestionNode, answer: JevAnswer) -> dict:
    """Fill a question node's output ports from one typed answer (brief verbatim)."""
    if isinstance(answer, ChoiceAnswer):
        keys = [option.key for option in node.options]
        if set(answer.probabilities) != set(keys):
            raise JevError(f"JEV probabilities for {node.id!r} do not match the configured options")
        option_order = {option.key: index for index, option in enumerate(node.options)}
        ranking = sorted(
            node.options,
            key=lambda option: (-answer.probabilities[option.key], option_order[option.key]),
        )
        ports = {"prob." + option.key: float(answer.probabilities[option.key]) for option in node.options}
        ports.update({"selected." + option.key: option.key == ranking[0].key for option in node.options})
        ports["p_top"] = ports["prob." + ranking[0].key]
        ports["margin"] = ports["p_top"] - ports["prob." + ranking[1].key]
        return ports
    if isinstance(answer, NoulAnswer):
        return {"yes": answer.yes, "no": answer.no}
    if isinstance(answer, ScoreAnswer):
        return {"value": answer.value}
    raise JevError(f"unsupported JEV answer type {type(answer).__name__}")


class GraphEvaluator:
    def __init__(self, jev_client):
        self.jev_client = jev_client

    async def evaluate(
        self,
        snapshot: RoutingSnapshot,
        request: RoutingRequest,
        *,
        now: datetime | None = None,
        trace_sink: list[dict] | None = None,
    ) -> GraphOutcome:
        events: list[dict] = [] if trace_sink is None else trace_sink
        if snapshot.tree is None:
            raise NoActiveTreeError("no routing tree is enabled")
        tree = snapshot.tree
        compiled = snapshot.require_compiled()
        nodes = compiled.nodes
        node_order = compiled.node_order
        sorted_edges = compiled.edges
        incoming = compiled.incoming

        # 1. time / window: apply_window runs exactly once per request.
        timezone = ZoneInfo(tree.timezone)
        if now is None:
            local_now = datetime.now(timezone)
        elif now.tzinfo is None:
            local_now = now.replace(tzinfo=timezone)
        else:
            local_now = now.astimezone(timezone)
        active_periods = matching_periods(local_now, list(snapshot.time_periods))
        events.append({"event": "time", "active_periods": active_periods, "timezone": tree.timezone})
        window = apply_window(request.text, snapshot.jev_input_tokens)
        events.append({
            "event": "window",
            "tokens_before": window.tokens_before,
            "tokens_after": window.tokens_after,
            "truncated": window.truncated,
        })

        # 2. initialise every output port to UNKNOWN (None); start.out is the sole true seed.
        ports: dict[str, dict[str, object]] = {
            node_id: {port_name: None for port_name in compiled.ports[node_id]}
            for node_id in node_order
        }
        for node_id in node_order:
            if isinstance(nodes[node_id], StartNode):
                ports[node_id]["out"] = True

        # 3. phase 0: condition nodes read the (possibly truncated) request text / active periods.
        for node_id in node_order:
            node = nodes[node_id]
            if not isinstance(node, ConditionNode):
                continue
            if node.source == "text":
                matched = [needle for needle in node.contains_any if needle and needle in window.text]
            else:
                matched = [name for name in node.periods if name in active_periods]
            value = bool(matched)
            ports[node_id]["out"] = value
            events.append({
                "event": "condition",
                "node_id": node_id,
                "source": node.source,
                "matched": matched,
                "value": value,
            })

        gates: dict[str, bool] = {}
        candidates: dict[str, Candidate] = {}
        activated_questions: set[str] = set()
        executed_questions: set[str] = set()
        answers: dict[str, JevAnswer] = {}
        waves: list[WaveTrace] = []
        failed = False
        failure_reason: str | None = None

        # 4-5. round 0: fixed point → one slot evaluation per edge → activation scan.
        wave = 0
        self._resolve_gates(
            wave=wave, nodes=nodes, node_order=node_order, incoming=incoming,
            ports=ports, gates=gates, events=events,
        )
        round_results = self._trace_slots(
            wave=wave, nodes=nodes, edges=sorted_edges, ports=ports, events=events,
        )
        queue = self._activate(
            wave=wave, nodes=nodes, node_order=node_order, incoming=incoming,
            round_results=round_results, candidates=candidates,
            activated_questions=activated_questions, executed_questions=executed_questions,
            events=events,
        )

        # 6. wave loop: one merged decision call per wave, then another fixed-point round.
        while queue:
            if wave >= _MAX_WAVES:
                raise RoutingMiss(f"graph execution exceeded {_MAX_WAVES} waves")
            wave += 1
            started = time.monotonic()
            try:
                if snapshot.jev is None:
                    raise JevError("JEV is not configured")
                questions = {
                    node_id: self._decision_question(nodes[node_id]) for node_id in queue
                }
                raw_answers = await self.jev_client.choose_many(snapshot.jev, window.text, questions)
                staged_ports, staged_answers = self._stage_answers(
                    queue, nodes, questions, raw_answers,
                )
            except JevError as exc:  # only JEV failures void the wave; defects propagate
                duration_ms = (time.monotonic() - started) * 1000
                reason = str(exc) or type(exc).__name__
                waves.append(WaveTrace(wave, tuple(queue), duration_ms, True, reason))
                events.append({
                    "event": "wave",
                    "wave": wave,
                    "questions": list(queue),
                    "duration_ms": duration_ms,
                    "failed": True,
                    "error": reason,
                })
                candidates.clear()
                failed = True
                failure_reason = reason
                break

            duration_ms = float(staged_answers[queue[0]].duration_ms)
            for node_id in queue:
                ports[node_id].update(staged_ports[node_id])
                answers[node_id] = staged_answers[node_id]
                executed_questions.add(node_id)
            waves.append(WaveTrace(wave, tuple(queue), duration_ms, False, None))
            events.append({
                "event": "wave",
                "wave": wave,
                "questions": list(queue),
                "duration_ms": duration_ms,
                "failed": False,
                "error": None,
            })
            for node_id in queue:
                events.append(self._question_event(
                    wave, nodes[node_id], staged_answers[node_id], staged_ports[node_id],
                ))
            self._resolve_gates(
                wave=wave, nodes=nodes, node_order=node_order, incoming=incoming,
                ports=ports, gates=gates, events=events,
            )
            round_results = self._trace_slots(
                wave=wave, nodes=nodes, edges=sorted_edges, ports=ports, events=events,
            )
            queue = self._activate(
                wave=wave, nodes=nodes, node_order=node_order, incoming=incoming,
                round_results=round_results, candidates=candidates,
                activated_questions=activated_questions, executed_questions=executed_questions,
                events=events,
            )

        # 7. One fallback target handles both a failed wave and a normal run
        # with no activated target. The trace kind keeps the cause for replay,
        # while the public selection reason is always error_fallback.
        if failed:
            target = self._required_target(nodes, tree.root.error_fallback, "error_fallback")
            kind = "error_fallback"
            fallback_reason: str | None = "error_fallback"
            final_candidates: tuple[Candidate, ...] = ()
        elif candidates:
            final_candidates = tuple(sorted(
                candidates.values(), key=lambda item: (-item.priority, item.target_id),
            ))
            winner = final_candidates[0]
            target = nodes[winner.target_id]
            kind = "target"
            fallback_reason = None
        else:
            target = self._required_target(nodes, tree.root.error_fallback, "error_fallback")
            kind = "otherwise"
            fallback_reason = "error_fallback"
            final_candidates = ()
        if failed:
            # 失败原因先于 final 记录（§3.9 事件顺序：fallback → final）。
            events.append({
                "event": "fallback",
                "target_id": target.id,
                "reason": failure_reason,
            })
        events.append({
            "event": "final",
            "target_id": target.id,
            "kind": kind,
            "candidates": [
                {
                    "target_id": candidate.target_id,
                    "priority": candidate.priority,
                    "via_node": candidate.via_node,
                    "via_port": candidate.via_port,
                    "wave": candidate.wave,
                }
                for candidate in final_candidates
            ],
            "priority": target.priority,
        })
        return GraphOutcome(
            target=target,
            candidates=final_candidates,
            waves=tuple(waves),
            answers=MappingProxyType(dict(answers)),
            fallback_reason=fallback_reason,
            failure_reason=failure_reason,
        )

    # ------------------------------------------------------------------ internals

    def _resolve_gates(self, *, wave, nodes, node_order, incoming, ports, gates, events):
        """Fixed-point three-valued gate evaluation until no gate changes."""
        changed = True
        while changed:
            changed = False
            for node_id in node_order:
                node = nodes[node_id]
                if not isinstance(node, LogicNode) or node_id in gates:
                    continue
                values = []
                inputs = []
                for edge in incoming[node_id]:
                    raw = ports[edge.source][edge.source_port]
                    result = evaluate_condition(edge, nodes[edge.source], raw)
                    values.append(result.value)
                    inputs.append({
                        "port": edge.target_port,
                        "raw": raw,
                        "condition": _condition_document(edge),
                        "status": result.status,
                    })
                value = combine_gate(node.operator, values)
                if value == UNKNOWN:
                    continue
                gates[node_id] = bool(value)
                ports[node_id]["out"] = bool(value)
                events.append({
                    "event": "gate",
                    "wave": wave,
                    "node_id": node_id,
                    "operator": node.operator,
                    "inputs": inputs,
                    "value": bool(value),
                })
                changed = True

    def _trace_slots(self, *, wave, nodes, edges, ports, events) -> dict[tuple[str, str], ConditionResult]:
        """Evaluate every edge slot once for this round and write one trace event each."""
        results: dict[tuple[str, str], ConditionResult] = {}
        for edge in edges:
            raw = ports[edge.source][edge.source_port]
            result = evaluate_condition(edge, nodes[edge.source], raw)
            results[(edge.target, edge.target_port)] = result
            events.append({
                "event": "slot",
                "wave": wave,
                "edge_id": edge.id,
                "node_id": edge.target,
                "port": edge.target_port,
                "source": edge.source,
                "source_port": edge.source_port,
                "raw": raw,
                "condition": _condition_document(edge),
                "status": result.status,
            })
        return results

    def _activate(
        self, *, wave, nodes, node_order, incoming, round_results, candidates,
        activated_questions, executed_questions, events,
    ) -> list[str]:
        """Activate questions/targets whose OR slots contain at least one true slot."""
        queue: list[str] = []
        for node_id in node_order:
            node = nodes[node_id]
            if isinstance(node, QuestionNode):
                if node_id in executed_questions or node_id in activated_questions:
                    continue
                via = self._first_true_slot(incoming[node_id], round_results)
                if via is None:
                    continue
                activated_questions.add(node_id)
                queue.append(node_id)
                events.append({
                    "event": "activate",
                    "wave": wave,
                    "node_id": node_id,
                    "via_port": via.target_port,
                })
            elif isinstance(node, TargetNode):
                if node_id in candidates:
                    continue
                via = self._first_true_slot(incoming[node_id], round_results)
                if via is None:
                    continue
                candidates[node_id] = Candidate(
                    target_id=node_id,
                    priority=node.priority,
                    via_node=via.source,
                    via_port=via.target_port,
                    wave=wave,
                )
                events.append({
                    "event": "activate",
                    "wave": wave,
                    "node_id": node_id,
                    "via_port": via.target_port,
                })
        return queue

    @staticmethod
    def _first_true_slot(edges, round_results) -> Edge | None:
        # ``edges`` is sorted by target slot number, so ties resolve to the smallest slot.
        for edge in edges:
            if round_results[(edge.target, edge.target_port)].value is True:
                return edge
        return None

    @staticmethod
    def _decision_question(node: QuestionNode) -> DecisionQuestion:
        if node.question_type == "choice":
            return DecisionQuestion(
                "choice",
                node.instructions,
                options=tuple(
                    JevCandidate(option.key, option.label, option.capability)
                    for option in node.options
                ),
            )
        if node.question_type == "noul":
            return DecisionQuestion("noul", node.instructions)
        return DecisionQuestion("score", node.instructions, criteria=node.criteria)

    @staticmethod
    def _stage_answers(queue, nodes, questions, raw_answers) -> tuple[dict, dict]:
        """Validate the whole wave before touching any port (missing/invalid ⇒ wave failure)."""
        if not isinstance(raw_answers, Mapping):
            raise JevError("JEV response must be a mapping")
        received = set(raw_answers)
        expected = set(queue)
        if received != expected:
            missing = sorted(expected - received)
            unexpected = sorted(received - expected)
            raise JevError(f"JEV answer keys mismatch (missing={missing}, unexpected={unexpected})")
        staged_ports: dict[str, dict] = {}
        staged_answers: dict[str, JevAnswer] = {}
        for node_id in queue:
            node = nodes[node_id]
            question = questions[node_id]
            expected_type = _ANSWER_TYPES[question.question_type]
            answer = raw_answers[node_id]
            if not isinstance(answer, expected_type):
                raise JevError(
                    f"JEV answer for {node_id!r} must be {expected_type.__name__}, "
                    f"got {type(answer).__name__}"
                )
            staged_ports[node_id] = _answer_ports(node, answer)
            staged_answers[node_id] = answer
        return staged_ports, staged_answers

    @staticmethod
    def _question_event(wave, node: QuestionNode, answer: JevAnswer, ports: dict) -> dict:
        event = {
            "event": "question",
            "wave": wave,
            "node_id": node.id,
            "ports": ports,
            "duration_ms": answer.duration_ms,
            "usage": answer.usage,
        }
        if isinstance(answer, ChoiceAnswer):
            event["selected"] = next(
                option.key for option in node.options if ports["selected." + option.key]
            )
            event["p_top"] = ports["p_top"]
            event["margin"] = ports["margin"]
        return event

    @staticmethod
    def _required_target(nodes, target_id, label: str) -> TargetNode:
        node = nodes.get(target_id) if target_id else None
        if not isinstance(node, TargetNode):
            raise RoutingMiss(f"{label} target {target_id!r} is not a target node")
        return node
