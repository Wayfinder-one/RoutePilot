"""槽位条件求值：六种数值算子与 boolean `is`，三值结果加 invalid。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from router.routing.graph.model import Condition, Edge, GraphNode
from router.routing.graph.ports import output_ports

UNKNOWN = "unknown"
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte", "eq", "neq"})
_THRESHOLD_LIMIT = 1e9


@dataclass(frozen=True)
class ConditionResult:
    raw: object
    status: str
    value: object
    reason: str | None = None


@dataclass(frozen=True)
class ConditionProblem:
    """A condition defect: ``code`` is the contract, ``message`` is diagnostics only.

    ``validate`` maps ``code`` to the structural Issue code; the English
    ``message`` is what evaluation traces report. Because the machine code is
    chosen structurally (never by parsing ``message``), a copy edit cannot
    change an Issue code.
    """

    code: str
    message: str


def _port_kind(source_node: GraphNode, port_name: str) -> str | None:
    for port in output_ports(source_node):
        if port.name == port_name:
            return port.kind
    return None


def conditionInvalidReason(edge: Edge, source_node: GraphNode) -> ConditionProblem | None:
    """Return the machine-coded defect of ``edge``'s condition, or ``None`` if valid."""
    kind = _port_kind(source_node, edge.source_port)
    if kind is None:
        return ConditionProblem(
            "source_port_missing",
            f"source port {edge.source_port!r} does not exist on node {source_node.id!r}")
    condition = edge.condition
    if kind == "number":
        if condition is None:
            return ConditionProblem(
                "numeric_condition_missing",
                "numeric source port requires a comparison condition")
        if condition.op not in _NUMERIC_OPS:
            return ConditionProblem(
                "numeric_operator_invalid",
                f"numeric source port cannot use operator {condition.op!r}")
        threshold = condition.value
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)):
            return ConditionProblem(
                "numeric_threshold_not_finite",
                "condition threshold must be a finite number")
        if abs(float(threshold)) > _THRESHOLD_LIMIT:
            return ConditionProblem(
                "numeric_threshold_out_of_range",
                f"condition threshold magnitude must not exceed {_THRESHOLD_LIMIT:g}")
        return None
    if condition is None:
        return None
    if condition.op != "is":
        return ConditionProblem(
            "boolean_operator_invalid",
            f"boolean source port cannot use operator {condition.op!r}")
    if not isinstance(condition.value, bool):
        return ConditionProblem(
            "boolean_value_invalid",
            "boolean condition value must be true or false")
    return None


def _compare(op: str, value: float, threshold: float) -> bool:
    if op == "gt":
        return value > threshold
    if op == "gte":
        return value >= threshold
    if op == "lt":
        return value < threshold
    if op == "lte":
        return value <= threshold
    if op == "eq":
        return value == threshold
    if op == "neq":
        return value != threshold
    raise ValueError(f"unknown numeric operator {op!r}")


def evaluate_condition(edge: Edge, source_node: GraphNode, raw) -> ConditionResult:
    problem = conditionInvalidReason(edge, source_node)
    if problem is not None:
        return ConditionResult(raw, "invalid", UNKNOWN, problem.message)
    kind = _port_kind(source_node, edge.source_port)
    condition = edge.condition
    if kind == "boolean":
        if raw is None:
            return ConditionResult(raw, "unknown", UNKNOWN)
        if not isinstance(raw, bool):
            return ConditionResult(raw, "invalid", UNKNOWN, f"boolean port value must be a boolean, got {type(raw).__name__}")
        expected = True if condition is None else condition.value
        passed = raw == expected
        return ConditionResult(raw, "true" if passed else "false", passed)
    if raw is None:
        return ConditionResult(raw, "unknown", UNKNOWN)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return ConditionResult(raw, "invalid", UNKNOWN, f"number port value must be a finite number, got {raw!r}")
    passed = _compare(condition.op, float(raw), float(condition.value))
    return ConditionResult(raw, "true" if passed else "false", passed)
