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


def _port_kind(source_node: GraphNode, port_name: str) -> str | None:
    for port in output_ports(source_node):
        if port.name == port_name:
            return port.kind
    return None


def conditionInvalidReason(edge: Edge, source_node: GraphNode) -> str | None:
    kind = _port_kind(source_node, edge.source_port)
    if kind is None:
        return f"source port {edge.source_port!r} does not exist on node {source_node.id!r}"
    condition = edge.condition
    if kind == "number":
        if condition is None:
            return "numeric source port requires a comparison condition"
        if condition.op not in _NUMERIC_OPS:
            return f"numeric source port cannot use operator {condition.op!r}"
        threshold = condition.value
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)):
            return "condition threshold must be a finite number"
        if abs(float(threshold)) > _THRESHOLD_LIMIT:
            return f"condition threshold magnitude must not exceed {_THRESHOLD_LIMIT:g}"
        return None
    if condition is None:
        return None
    if condition.op != "is":
        return f"boolean source port cannot use operator {condition.op!r}"
    if not isinstance(condition.value, bool):
        return "boolean condition value must be true or false"
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
    reason = conditionInvalidReason(edge, source_node)
    if reason is not None:
        return ConditionResult(raw, "invalid", UNKNOWN, reason)
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
