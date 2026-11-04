"""Graph v3 数据模型：五类节点、边、条件与两个序列化视图（storage/behavior）。"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    x: float
    y: float

    def __post_init__(self):
        for name in ("x", "y"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"position.{name} must be a finite number")
            try:
                value = float(value)
            except OverflowError:
                raise ValueError(f"position.{name} must be a finite number") from None
            if not math.isfinite(value):
                raise ValueError(f"position.{name} must be a finite number")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class ModelTarget:
    model: str
    reasoning: str | None = None


@dataclass(frozen=True)
class QuestionOption:
    key: str
    label: str
    capability: str


@dataclass(frozen=True)
class StartNode:
    id: str
    title: str | None = None
    position: Position | None = None


@dataclass(frozen=True)
class QuestionNode:
    id: str
    question_type: str
    instructions: str
    options: tuple[QuestionOption, ...] = ()
    criteria: tuple[float, ...] | None = None
    title: str | None = None
    position: Position | None = None


@dataclass(frozen=True)
class LogicNode:
    id: str
    operator: str
    title: str | None = None
    position: Position | None = None


@dataclass(frozen=True)
class ConditionNode:
    id: str
    source: str
    contains_any: tuple[str, ...] = ()
    periods: tuple[str, ...] = ()
    title: str | None = None
    position: Position | None = None


@dataclass(frozen=True)
class TargetNode:
    id: str
    models: tuple[ModelTarget, ...]
    priority: int
    strategy: str = "least_inflight"
    capability: str | None = None
    title: str | None = None
    position: Position | None = None


GraphNode = StartNode | QuestionNode | LogicNode | ConditionNode | TargetNode


@dataclass(frozen=True)
class Condition:
    op: str
    value: float | bool

    def __post_init__(self):
        numeric = {"gt", "gte", "lt", "lte", "eq", "neq"}
        if self.op not in numeric | {"is"}:
            raise ValueError(f"condition.op must be one of gt/gte/lt/lte/eq/neq/is, got {self.op!r}")
        if self.op == "is":
            if not isinstance(self.value, bool):
                raise ValueError(f"condition.value must be a boolean for op is, got {self.value!r}")
            return
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError(f"condition.value must be a finite number for op {self.op}, got {self.value!r}")
        try:
            value = float(self.value)
        except OverflowError:
            raise ValueError(f"condition.value must be a finite number for op {self.op}, got {self.value!r}") from None
        if not math.isfinite(value):
            raise ValueError(f"condition.value must be a finite number for op {self.op}, got {self.value!r}")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class Edge:
    id: str
    source: str
    source_port: str
    target: str
    target_port: str
    condition: Condition | None = None


@dataclass(frozen=True)
class GraphRoot:
    id: str
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[Edge, ...] = ()
    error_fallback: str | None = None


_QUESTION_TYPES = ("choice", "noul", "score")
_LOGIC_OPERATORS = ("and", "or", "not")
_CONDITION_SOURCES = ("text", "periods")


def _unknown(kind: str, raw: dict, allowed: set[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{kind} has unknown fields: {', '.join(unknown)}")


def _text(value, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{field} must be a {'string' if empty else 'nonempty string'}")
    return value


def _display(raw: dict, kind: str) -> str | None:
    value = raw.get("title")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{kind}.title must be a string")
    return value


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    return value


def _position(raw: dict, kind: str) -> Position | None:
    raw_position = raw.get("position")
    if raw_position is None:
        return None
    if not isinstance(raw_position, dict):
        raise ValueError(f"{kind}.position must be an object with x/y")
    _unknown(f"{kind}.position", raw_position, {"x", "y"})
    return Position(_number(raw_position.get("x"), f"{kind}.position.x"), _number(raw_position.get("y"), f"{kind}.position.y"))


def _node_id(raw: dict) -> str:
    return _text(raw.get("id"), "node id")


def _condition_from_dict(raw) -> Condition | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("edge condition must be an object")
    _unknown("edge condition", raw, {"op", "value"})
    op = raw.get("op")
    if op == "is":
        value = raw.get("value")
        if not isinstance(value, bool):
            raise ValueError("condition.value must be a boolean for op is")
        return Condition(op, value)
    return Condition(_text(op, "condition.op"), _number(raw.get("value"), "condition.value"))


def _model_target_from_dict(raw) -> ModelTarget:
    if not isinstance(raw, dict):
        raise ValueError("target model must be an object")
    _unknown("target model", raw, {"model", "reasoning"})
    reasoning = raw.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("target model reasoning must be a string")
    return ModelTarget(_text(raw.get("model"), "target model name"), reasoning)


def _option_from_dict(raw) -> QuestionOption:
    if not isinstance(raw, dict):
        raise ValueError("question option must be an object")
    _unknown("question option", raw, {"key", "label", "capability"})
    for field in ("key", "label", "capability"):
        if not isinstance(raw.get(field), str):
            raise ValueError(f"question option {field} must be a string")
    return QuestionOption(raw["key"], raw["label"], raw["capability"])


def _criteria_from_dict(raw) -> tuple[float, ...]:
    if not isinstance(raw, list):
        raise ValueError("score criteria must be a list of finite numbers")
    return tuple(_number(level, "score criteria level") for level in raw)


def _string_list(raw, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(_text(item, f"{field} item", empty=True) for item in raw)


def _node_from_dict(raw) -> GraphNode:
    if not isinstance(raw, dict):
        raise ValueError("node must be an object")
    node_type = raw.get("type")
    if node_type == "start":
        _unknown("start node", raw, {"id", "type", "title", "position"})
        return StartNode(_node_id(raw), _display(raw, "start"), _position(raw, "start"))
    if node_type == "question":
        _unknown("question node", raw, {"id", "type", "question_type", "instructions", "title", "options", "criteria", "position"})
        question_type = raw.get("question_type")
        if question_type not in _QUESTION_TYPES:
            raise ValueError(f"question.question_type must be one of {'/'.join(_QUESTION_TYPES)}, got {question_type!r}")
        options = raw.get("options", [])
        if not isinstance(options, list):
            raise ValueError("question.options must be a list")
        criteria = raw.get("criteria")
        # 每类问题只接受自己的配置字段（spec §2.3）：惰性字段若进入 behavior canonical，
        # 语义无变化的编辑也会 bump revision、让粘性失效（违反 §2.8）。
        if question_type != "choice" and options:
            raise ValueError(f"{question_type} question must not declare options")
        if question_type != "score" and criteria is not None:
            raise ValueError(f"{question_type} question must not declare criteria")
        return QuestionNode(
            _node_id(raw),
            question_type,
            _text(raw.get("instructions"), "question.instructions", empty=True),
            tuple(_option_from_dict(option) for option in options),
            _criteria_from_dict(criteria) if criteria is not None else None,
            _display(raw, "question"),
            _position(raw, "question"),
        )
    if node_type == "logic":
        _unknown("logic node", raw, {"id", "type", "operator", "title", "position"})
        operator = raw.get("operator")
        if operator not in _LOGIC_OPERATORS:
            raise ValueError(f"logic.operator must be one of {'/'.join(_LOGIC_OPERATORS)}, got {operator!r}")
        return LogicNode(_node_id(raw), operator, _display(raw, "logic"), _position(raw, "logic"))
    if node_type == "condition":
        _unknown("condition node", raw, {"id", "type", "source", "contains_any", "periods", "title", "position"})
        source = raw.get("source")
        if source not in _CONDITION_SOURCES:
            raise ValueError(f"condition.source must be one of {'/'.join(_CONDITION_SOURCES)}, got {source!r}")
        return ConditionNode(
            _node_id(raw),
            source,
            _string_list(raw.get("contains_any", []), "condition.contains_any"),
            _string_list(raw.get("periods", []), "condition.periods"),
            _display(raw, "condition"),
            _position(raw, "condition"),
        )
    if node_type == "target":
        _unknown("target node", raw, {"id", "type", "models", "priority", "strategy", "capability", "title", "position"})
        models = raw.get("models", [])
        if not isinstance(models, list):
            raise ValueError("target.models must be a list")
        priority = raw.get("priority")
        # 类型在这里保持「数字」层校验（bool/非数字/非有限 → 解析错误）；
        # 整数性留给 validate，好让 §5.2 的 target_priority_invalid 覆盖 1.5 这类值。
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            raise ValueError("target.priority must be an integer")
        if isinstance(priority, float) and not math.isfinite(priority):
            raise ValueError("target.priority must be an integer")
        strategy = raw.get("strategy", "least_inflight")
        if strategy != "least_inflight":
            raise ValueError(f"target.strategy must be least_inflight, got {strategy!r}")
        capability = raw.get("capability")
        if capability is not None and not isinstance(capability, str):
            raise ValueError("target.capability must be a string")
        return TargetNode(
            _node_id(raw),
            tuple(_model_target_from_dict(model) for model in models),
            priority,
            strategy,
            capability,
            _display(raw, "target"),
            _position(raw, "target"),
        )
    raise ValueError(f"unknown node type {node_type!r}")


def _edge_from_dict(raw) -> Edge:
    if not isinstance(raw, dict):
        raise ValueError("edge must be an object")
    _unknown("edge", raw, {"id", "source", "source_port", "target", "target_port", "condition"})
    return Edge(
        _text(raw.get("id"), "edge id"),
        _text(raw.get("source"), "edge source"),
        _text(raw.get("source_port"), "edge source_port"),
        _text(raw.get("target"), "edge target"),
        _text(raw.get("target_port"), "edge target_port"),
        _condition_from_dict(raw.get("condition")),
    )


def _fallback_ref(raw: dict) -> str | None:
    value = raw.get("error_fallback")
    if value is None:
        # v3 files written before the fallback merge used otherwise_target for
        # normal no-target completion. Read it as the unified fallback, but
        # never emit it again.
        value = raw.get("otherwise_target")
    if value is None:
        return None
    return _text(value, "graph error_fallback")


def graph_from_dict(raw) -> GraphRoot:
    if not isinstance(raw, dict):
        raise ValueError("graph must be an object")
    _unknown("graph", raw, {"id", "type", "nodes", "edges", "error_fallback", "otherwise_target"})
    if raw.get("type") != "graph":
        raise ValueError(f"graph.type must be 'graph', got {raw.get('type')!r}")
    nodes = raw.get("nodes", [])
    edges = raw.get("edges", [])
    if not isinstance(nodes, list):
        raise ValueError("graph.nodes must be a list")
    if not isinstance(edges, list):
        raise ValueError("graph.edges must be a list")
    return GraphRoot(
        _text(raw.get("id"), "graph id"),
        tuple(_node_from_dict(node) for node in nodes),
        tuple(_edge_from_dict(edge) for edge in edges),
        _fallback_ref(raw),
    )


def _node_to_dict(node: GraphNode) -> dict:
    if isinstance(node, StartNode):
        result: dict = {"id": node.id, "type": "start"}
    elif isinstance(node, QuestionNode):
        result = {"id": node.id, "type": "question", "question_type": node.question_type, "instructions": node.instructions}
        if node.options:
            result["options"] = [{"key": option.key, "label": option.label, "capability": option.capability} for option in node.options]
        if node.criteria is not None:
            result["criteria"] = list(node.criteria)
    elif isinstance(node, LogicNode):
        result = {"id": node.id, "type": "logic", "operator": node.operator}
    elif isinstance(node, ConditionNode):
        result = {"id": node.id, "type": "condition", "source": node.source}
        if node.contains_any:
            result["contains_any"] = list(node.contains_any)
        if node.periods:
            result["periods"] = list(node.periods)
    elif isinstance(node, TargetNode):
        result = {
            "id": node.id,
            "type": "target",
            "models": [{"model": item.model, "reasoning": item.reasoning} for item in node.models],
            "priority": node.priority,
        }
        if node.strategy != "least_inflight":
            result["strategy"] = node.strategy
        if node.capability is not None:
            result["capability"] = node.capability
    else:
        raise TypeError(f"unsupported node {node!r}")
    if node.title is not None:
        result["title"] = node.title
    if node.position is not None:
        result["position"] = {"x": node.position.x, "y": node.position.y}
    return result


def _edge_to_dict(edge: Edge) -> dict:
    condition = edge.condition
    return {
        "id": edge.id,
        "source": edge.source,
        "source_port": edge.source_port,
        "target": edge.target,
        "target_port": edge.target_port,
        "condition": None if condition is None else {"op": condition.op, "value": condition.value},
    }


def graph_to_dict(root: GraphRoot) -> dict:
    return {
        "id": root.id,
        "type": "graph",
        "nodes": [_node_to_dict(node) for node in root.nodes],
        "edges": [_edge_to_dict(edge) for edge in root.edges],
        "error_fallback": root.error_fallback,
    }


def _node_behavior_dict(node: GraphNode) -> dict:
    if isinstance(node, StartNode):
        return {"id": node.id, "type": "start"}
    if isinstance(node, QuestionNode):
        result = {"id": node.id, "type": "question", "question_type": node.question_type, "instructions": node.instructions}
        # 只把该类型的语义字段写进 behavior：解析层已禁止跨类型字段，
        # 这里再按类型投影一次，防止直接构造的节点制造惰性 revision 变化。
        if node.question_type == "choice" and node.options:
            result["options"] = [{"key": option.key, "label": option.label, "capability": option.capability} for option in node.options]
        if node.question_type == "score" and node.criteria is not None:
            result["criteria"] = list(node.criteria)
        return result
    if isinstance(node, LogicNode):
        return {"id": node.id, "type": "logic", "operator": node.operator}
    if isinstance(node, ConditionNode):
        result = {"id": node.id, "type": "condition", "source": node.source}
        if node.contains_any:
            result["contains_any"] = list(node.contains_any)
        if node.periods:
            result["periods"] = list(node.periods)
        return result
    if isinstance(node, TargetNode):
        return {
            "id": node.id,
            "type": "target",
            "models": [{"model": item.model, "reasoning": item.reasoning} for item in node.models],
            "priority": node.priority,
            "strategy": node.strategy,
        }
    raise TypeError(f"unsupported node {node!r}")


def _edge_behavior_dict(edge: Edge) -> dict:
    condition = edge.condition
    return {
        "source": edge.source,
        "source_port": edge.source_port,
        "target": edge.target,
        "target_port": edge.target_port,
        "condition": None if condition is None else {"op": condition.op, "value": condition.value},
    }


def graph_behavior_dict(root: GraphRoot) -> dict:
    from router.routing.graph.ports import slot_sort_key

    return {
        "type": "graph",
        "error_fallback": root.error_fallback,
        "nodes": [_node_behavior_dict(node) for node in sorted(root.nodes, key=lambda node: node.id)],
        "edges": [_edge_behavior_dict(edge) for edge in sorted(
            root.edges,
            key=lambda edge: (edge.target, slot_sort_key(edge.target_port), edge.source, edge.source_port),
        )],
    }


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_revision_of(value) -> str:
    if isinstance(value, GraphRoot):
        payload = graph_behavior_dict(value)
    elif hasattr(value, "root") and hasattr(value, "timezone") and hasattr(value, "sticky"):
        # 单一来源：envelope 的 canonical 结构与 tree_behavior_dict 完全一致。
        # 局部导入避免 router.routing.model ↔ graph.model 的循环依赖。
        from router.routing.model import tree_behavior_dict

        payload = tree_behavior_dict(value)
    else:
        raise TypeError("graph_revision_of expects a RoutingTree or a GraphRoot")
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
