"""输出端口注册表与输入槽位推导（槽位排序、next_slot、边类型判定）。"""
from __future__ import annotations

from dataclasses import dataclass

from router.routing.codec import MAX_SLOT_INDEX, slot_sort_key
from router.routing.graph.model import (
    ConditionNode,
    Edge,
    GraphNode,
    GraphRoot,
    LogicNode,
    QuestionNode,
    StartNode,
    TargetNode,
)


@dataclass(frozen=True)
class Port:
    name: str
    kind: str


def output_ports(node: GraphNode) -> tuple[Port, ...]:
    if isinstance(node, StartNode):
        return (Port("out", "boolean"),)
    if isinstance(node, QuestionNode):
        if node.question_type == "choice":
            ports = [Port(f"selected.{option.key}", "boolean") for option in node.options]
            ports += [Port(f"prob.{option.key}", "number") for option in node.options]
            ports += [Port("p_top", "number"), Port("margin", "number")]
            return tuple(ports)
        if node.question_type == "noul":
            return (Port("yes", "number"), Port("no", "number"))
        if node.question_type == "score":
            return (Port("value", "number"),)
        raise ValueError(f"unknown question_type {node.question_type!r}")
    if isinstance(node, (LogicNode, ConditionNode)):
        return (Port("out", "boolean"),)
    if isinstance(node, TargetNode):
        return ()
    raise TypeError(f"unsupported node {node!r}")


def _find_node(root: GraphRoot, node_id: str):
    for node in root.nodes:
        if node.id == node_id:
            return node
    raise ValueError(f"unknown node {node_id!r}")


def next_slot(root: GraphRoot, node_id: str) -> str:
    node = _find_node(root, node_id)
    if isinstance(node, (StartNode, ConditionNode)):
        raise ValueError(f"node {node_id!r} of type {type(node).__name__} has no input slots")
    occupied = {slot_sort_key(edge.target_port) for edge in root.edges if edge.target == node_id}
    number = 1
    while number in occupied:
        number += 1
    if number > MAX_SLOT_INDEX:
        raise ValueError(
            f"node {node_id!r} has no free input slot (limit {MAX_SLOT_INDEX})"
        )
    return f"in_{number}"


def edge_type(root: GraphRoot, edge: Edge) -> str:
    node = _find_node(root, edge.source)
    for port in output_ports(node):
        if port.name == edge.source_port:
            return port.kind
    raise ValueError(f"node {node.id!r} has no output port {edge.source_port!r}")
