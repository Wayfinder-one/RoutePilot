"""Graph v3 校验器：结构校验（保存/试算/导入）与启用校验（启用/已启用组保存）。

纯函数：不打印、不抛业务异常（解析错误由 Task 2 的严格解析负责，dict 输入的解析失败
折叠为 `graph_parse_error`）。Issue 列表按 (level, code, node_id, edge_id, port) 稳定
排序，同图不同输入顺序得到相同集合。
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from router.routing.graph.conditions import conditionInvalidReason
from router.routing.graph.model import (
    ConditionNode,
    GraphNode,
    GraphRoot,
    LogicNode,
    QuestionNode,
    StartNode,
    TargetNode,
    graph_from_dict,
)
from router.routing.graph.ports import output_ports, slot_sort_key
from router.routing.tree import RoutingTree, tree_from_dict

_LOGIC_OPERATORS = frozenset({"and", "or", "not"})
_LIMIT_NODES = 200
_LIMIT_EDGES = 400
_LIMIT_QUESTIONS = 32
_LIMIT_CHAIN = 8
_LIMIT_OPTIONS = 255
_CRITERIA_MIN = 2
_CRITERIA_MAX = 10


def _finite_level(level: object) -> bool:
    """与 codec.finite_number 同型的有限性判定：超量级 int 在 float() 处溢出 → 视为非法。"""
    try:
        return math.isfinite(float(level))
    except OverflowError:
        return False


@dataclass(frozen=True)
class Issue:
    """一条校验问题：机器字段 + 文案参数，展示统一由 api.issue_presenter 生成。

    node_id/edge_id/port 按错误类型尽量给出，供控制台定位（spec §5.1）；
    params 只携带生成中英文案所需的插值/变体数据，本身不是 UI 文案。
    """

    code: str
    node_id: str = ""
    edge_id: str = ""
    port: str = ""
    level: str = "hard"
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class EnableContext:
    """启用校验上下文：模型目录、推理档位、时段定义与 JEV 配置存在性（不做在线探测）。"""

    known_models: frozenset[str] | set[str] = frozenset()
    model_reasoning: Mapping[str, Mapping[str, object] | None] = field(default_factory=dict)
    known_periods: frozenset[str] | set[str] = frozenset()
    jev_available: bool = False


def _params_key(params) -> tuple:
    return tuple(sorted((str(name), str(value)) for name, value in (params or {}).items()))


def _finish(issues: list[Issue]) -> list[Issue]:
    issues.sort(key=lambda item: (
        item.level, item.code, item.node_id, item.edge_id, item.port,
        _params_key(item.params),
    ))
    return issues


def _coerce(value) -> tuple[GraphRoot | None, str | None]:
    """RoutingTree → (root, timezone)；GraphRoot → (root, None)；raw dict → 解析（失败返回 (None, None)）。"""
    if isinstance(value, RoutingTree):
        return value.root, value.timezone
    if isinstance(value, GraphRoot):
        return value, None
    if isinstance(value, dict):
        try:
            if "root" in value:
                tree = tree_from_dict(value)
                return tree.root, tree.timezone
            return graph_from_dict(value), None
        except ValueError:
            return None, None
    raise TypeError(f"validate expects a RoutingTree, GraphRoot or raw dict, got {type(value).__name__}")


def _is_slot(port: str) -> bool:
    try:
        slot_sort_key(port)
    except ValueError:
        return False
    return True


def _cycle_back_edges(adjacency: dict[str, list[tuple[str, str, str]]]) -> list[tuple[str, str, str]]:
    """DFS 找回边（显式栈迭代，深度与 Python 递归上限无关）。

    返回 ``(node_id, edge_id, target_port)`` 列表，即成环处；遍历顺序与递归实现
    完全一致（节点按 id、同节点出边按 ``(target, edge_id)`` 排序）。
    """
    state: dict[str, int] = {}  # 0/缺失 = 白，1 = 灰（在栈上），2 = 黑（已完成）
    back: list[tuple[str, str, str]] = []
    for root_id in sorted(adjacency):
        if state.get(root_id, 0) != 0:
            continue
        state[root_id] = 1
        stack: list[tuple[str, Iterator[tuple[str, str, str]]]] = [(
            root_id,
            iter(sorted(adjacency.get(root_id, ()), key=lambda item: (item[0], item[1]))),
        )]
        while stack:
            node_id, children = stack[-1]
            for target, edge_id, port in children:
                current = state.get(target, 0)
                if current == 1:
                    back.append((target, edge_id, port))
                elif current == 0:
                    state[target] = 1
                    stack.append((
                        target,
                        iter(sorted(adjacency.get(target, ()), key=lambda item: (item[0], item[1]))),
                    ))
                    break
            else:
                state[node_id] = 2
                stack.pop()
    return back


def _question_chain_depth(by_id: dict[str, GraphNode], adjacency: dict[str, list[str]], preds: dict[str, list[str]]) -> int:
    """DAG 拓扑深度：start/condition 0，question = max(前驱)+1，logic = max(前驱)；取 question 最大值。"""
    indegree = {node_id: len(preds[node_id]) for node_id in by_id}
    queue = [node_id for node_id in sorted(indegree) if indegree[node_id] == 0]
    order: list[str] = []
    index = 0
    while index < len(queue):
        node_id = queue[index]
        index += 1
        order.append(node_id)
        for target in sorted(adjacency.get(node_id, ())):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    depth: dict[str, int] = {}
    for node_id in order:
        node = by_id[node_id]
        if isinstance(node, (StartNode, ConditionNode)):
            depth[node_id] = 0
        elif isinstance(node, QuestionNode):
            depth[node_id] = max((depth.get(source, 0) for source in preds[node_id]), default=0) + 1
        else:
            depth[node_id] = max((depth.get(source, 0) for source in preds[node_id]), default=0)
    questions = [depth[node.id] for node in by_id.values() if isinstance(node, QuestionNode) and node.id in depth]
    return max(questions, default=0)


def _condition_issue(edge, reason: str) -> Issue:
    """把 conditions.conditionInvalidReason 的英文 reason 映射到结构码表。"""
    context = {
        "node_id": edge.target, "edge_id": edge.id, "port": edge.target_port,
        "params": {
            "source": edge.source, "source_port": edge.source_port,
            "target": edge.target, "target_port": edge.target_port,
        },
    }
    if "requires a comparison condition" in reason:
        return Issue("numeric_edge_condition_missing", **context)
    if reason.startswith("numeric") or "threshold" in reason:
        return Issue("numeric_edge_condition_invalid", **context)
    return Issue("boolean_edge_condition_invalid", **context)


def validate_graph_structure(value) -> list[Issue]:
    """结构校验（保存/试算/导入）：时区、start、id、端点、槽位、环、条件、节点配置、兜底与限额。"""
    root, timezone = _coerce(value)
    if root is None:
        return _finish([Issue("graph_parse_error")])
    issues: list[Issue] = []
    # 限额必须先于任何图遍历：超限图直接返回 limit_exceeded，绝不做 O(超限) 的遍历/排序。
    # 节点/边任一超限即提前返回（>400 条边的路径必然 >200 个节点，两者通常同时出现）。
    if len(root.nodes) > _LIMIT_NODES or len(root.edges) > _LIMIT_EDGES:
        if len(root.nodes) > _LIMIT_NODES:
            issues.append(Issue("limit_exceeded", params={
                "kind": "nodes", "limit": _LIMIT_NODES, "count": len(root.nodes)}))
        if len(root.edges) > _LIMIT_EDGES:
            issues.append(Issue("limit_exceeded", params={
                "kind": "edges", "limit": _LIMIT_EDGES, "count": len(root.edges)}))
        return _finish(issues)
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError, OSError):
            issues.append(Issue("invalid_timezone", params={"timezone": timezone}))

    by_id: dict[str, GraphNode] = {}
    duplicate_nodes: set[str] = set()
    for node in root.nodes:
        if node.id in by_id:
            duplicate_nodes.add(node.id)
        else:
            by_id[node.id] = node
    for node_id in sorted(duplicate_nodes):
        issues.append(Issue("duplicate_node_id", node_id=node_id))

    seen_edge_ids: set[str] = set()
    duplicate_edges: set[str] = set()
    for edge in root.edges:
        if edge.id in seen_edge_ids:
            duplicate_edges.add(edge.id)
        seen_edge_ids.add(edge.id)
    for edge_id in sorted(duplicate_edges):
        issues.append(Issue("duplicate_edge_id", edge_id=edge_id))

    starts = sum(isinstance(node, StartNode) for node in by_id.values())
    if starts != 1:
        issues.append(Issue("start_count", params={"count": starts}))

    for node in sorted(by_id.values(), key=lambda item: item.id):
        if isinstance(node, LogicNode):
            if node.operator not in _LOGIC_OPERATORS:  # 解析层已拦截，这里兜住直接构造的节点
                issues.append(Issue(
                    "logic_operator_invalid", node_id=node.id,
                    params={"operator": node.operator},
                ))
        elif isinstance(node, ConditionNode):
            if node.source == "text":
                if not node.contains_any:
                    issues.append(Issue(
                        "condition_config_invalid", node_id=node.id,
                        params={"kind": "text_missing_contains"},
                    ))
                elif any(not needle.strip() for needle in node.contains_any):
                    # §2.3 要求 contains_any 是非空字符串数组；空白项是永不匹配的死条件。
                    issues.append(Issue(
                        "condition_config_invalid", node_id=node.id,
                        params={"kind": "text_blank_contains"},
                    ))
            if node.source == "periods":
                if not node.periods:
                    issues.append(Issue(
                        "condition_config_invalid", node_id=node.id,
                        params={"kind": "periods_missing"},
                    ))
                elif any(not name.strip() for name in node.periods):
                    issues.append(Issue(
                        "condition_config_invalid", node_id=node.id,
                        params={"kind": "periods_blank"},
                    ))
        elif isinstance(node, QuestionNode):
            if not node.instructions.strip():
                issues.append(Issue(
                    "question_incomplete", node_id=node.id,
                    params={"kind": "instructions"},
                ))
            if node.question_type == "choice":
                if len(node.options) < 2:
                    issues.append(Issue(
                        "question_incomplete", node_id=node.id,
                        params={"kind": "choice_min_options", "count": len(node.options)},
                    ))
                if len(node.options) > _LIMIT_OPTIONS:
                    issues.append(Issue(
                        "limit_exceeded", node_id=node.id,
                        params={"kind": "options", "limit": _LIMIT_OPTIONS,
                                "count": len(node.options)},
                    ))
                seen_keys: set[str] = set()
                seen_labels: set[str] = set()
                for option in node.options:
                    if not option.key.strip():
                        issues.append(Issue(
                            "question_incomplete", node_id=node.id,
                            params={"kind": "option_key_empty"},
                        ))
                    elif option.key in seen_keys:
                        issues.append(Issue(
                            "question_incomplete", node_id=node.id,
                            params={"kind": "option_key_duplicate", "key": option.key},
                        ))
                    seen_keys.add(option.key)
                    if not option.label.strip():
                        issues.append(Issue(
                            "question_incomplete", node_id=node.id,
                            params={"kind": "option_label_empty", "key": option.key},
                        ))
                    elif option.label in seen_labels:
                        issues.append(Issue(
                            "question_incomplete", node_id=node.id,
                            params={"kind": "option_label_duplicate",
                                    "label": option.label},
                        ))
                    seen_labels.add(option.label)
                    if not option.capability.strip():
                        issues.append(Issue(
                            "question_incomplete", node_id=node.id,
                            params={"kind": "option_capability_empty", "key": option.key},
                        ))
            if node.question_type == "score":
                if node.criteria is None:
                    issues.append(Issue("score_criteria_required", node_id=node.id))
                else:
                    levels = node.criteria
                    broken = len(levels) < _CRITERIA_MIN or len(levels) > _CRITERIA_MAX or any(
                        isinstance(level, bool) or not isinstance(level, (int, float)) or not _finite_level(level)
                        for level in levels
                    )
                    if broken:
                        issues.append(Issue(
                            "score_criteria_invalid", node_id=node.id,
                            params={"min": _CRITERIA_MIN, "max": _CRITERIA_MAX},
                        ))
        elif isinstance(node, TargetNode):
            if isinstance(node.priority, bool) or not isinstance(node.priority, int):
                # 解析层保留原始数字类型，才能把「非整数 priority」映射到 §5.2 专属码
                # 而不是笼统的 graph_parse_error。
                issues.append(Issue(
                    "target_priority_invalid", node_id=node.id,
                    params={"kind": "not_integer"},
                ))
            elif node.priority < 1:
                issues.append(Issue(
                    "target_priority_invalid", node_id=node.id,
                    params={"kind": "non_positive"},
                ))

    seen_priorities: set[int] = set()
    for node in sorted((item for item in by_id.values() if isinstance(item, TargetNode)), key=lambda item: item.id):
        priority = node.priority
        if isinstance(priority, bool) or not isinstance(priority, int):
            continue  # 已在节点循环里报过 target_priority_invalid，不重复报唯一性
        if priority in seen_priorities:
            issues.append(Issue(
                "target_priority_invalid", node_id=node.id,
                params={"kind": "duplicate"},
            ))
        seen_priorities.add(priority)

    ref = root.error_fallback
    if not ref:
        issues.append(Issue(
            "fallback_missing", port="error_fallback", params={"kind": "unset"},
        ))
    else:
        node = by_id.get(ref)
        if node is None:
            issues.append(Issue(
                "fallback_missing", node_id=ref, port="error_fallback",
                params={"kind": "missing_node"},
            ))
        elif not isinstance(node, TargetNode):
            issues.append(Issue(
                "fallback_missing", node_id=ref, port="error_fallback",
                params={"kind": "not_target"},
            ))

    adjacency: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    preds: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    back_edges: dict[str, list[tuple[str, str, str]]] = {node_id: [] for node_id in by_id}
    incoming: dict[str, list] = {node_id: [] for node_id in by_id}
    for edge in root.edges:
        source = by_id.get(edge.source)
        target = by_id.get(edge.target)
        if source is None:
            issues.append(Issue(
                "edge_endpoint_missing", node_id=edge.source, edge_id=edge.id,
                port=edge.source_port, params={"kind": "source"},
            ))
        if target is None:
            issues.append(Issue(
                "edge_endpoint_missing", node_id=edge.target, edge_id=edge.id,
                port=edge.target_port, params={"kind": "target"},
            ))
        if source is None or target is None:
            continue
        adjacency[edge.source].append(edge.target)
        preds[edge.target].append(edge.source)
        back_edges[edge.source].append((edge.target, edge.id, edge.target_port))
        incoming[edge.target].append(edge)
        source_port_ok = edge.source_port in {port.name for port in output_ports(source)}
        slot_ok = not isinstance(target, (StartNode, ConditionNode)) and _is_slot(edge.target_port)
        if not source_port_ok:
            issues.append(Issue(
                "edge_port_missing", node_id=edge.source, edge_id=edge.id,
                port=edge.source_port, params={"kind": "source"},
            ))
        if not slot_ok:
            issues.append(Issue(
                "edge_port_missing", node_id=edge.target, edge_id=edge.id,
                port=edge.target_port, params={"kind": "target"},
            ))
        if source_port_ok and slot_ok:
            reason = conditionInvalidReason(edge, source)
            if reason is not None:
                issues.append(_condition_issue(edge, reason))

    groups: dict[tuple[str, str], list] = {}
    for edge in root.edges:
        if edge.target in by_id and _is_slot(edge.target_port):
            groups.setdefault((edge.target, edge.target_port), []).append(edge)
    for (target_id, port), group in sorted(groups.items()):
        if len(group) > 1:
            keeper = min(edge.id for edge in group)
            for edge in sorted(group, key=lambda item: item.id):
                if edge.id != keeper:
                    issues.append(Issue(
                        "slot_conflict", node_id=target_id, edge_id=edge.id, port=port,
                    ))

    back_edges_found = _cycle_back_edges(back_edges)
    for node_id, edge_id, port in back_edges_found:
        issues.append(Issue(
            "edge_cycle", node_id=node_id, edge_id=edge_id, port=port,
        ))

    for node in sorted(by_id.values(), key=lambda item: item.id):
        incoming_count = len(incoming[node.id])
        if isinstance(node, LogicNode):
            if node.operator == "not" and incoming_count != 1:
                issues.append(Issue(
                    "logic_slot_count", node_id=node.id,
                    params={"kind": "not", "count": incoming_count},
                ))
            if node.operator in {"and", "or"} and incoming_count < 2:
                issues.append(Issue(
                    "logic_slot_count", node_id=node.id,
                    params={"kind": "and_or", "count": incoming_count},
                ))

    sources = [node.id for node in by_id.values() if isinstance(node, (StartNode, ConditionNode))]
    reachable: set[str] = set()
    queue = [node_id for node_id in sources]
    while queue:
        node_id = queue.pop()
        if node_id in reachable or node_id not in by_id:
            continue
        reachable.add(node_id)
        queue.extend(adjacency.get(node_id, ()))
    for node in sorted(by_id.values(), key=lambda item: item.id):
        if isinstance(node, QuestionNode) and node.id not in reachable:
            issues.append(Issue("question_unreachable", node_id=node.id))

    question_count = sum(isinstance(node, QuestionNode) for node in by_id.values())
    if question_count > _LIMIT_QUESTIONS:
        issues.append(Issue("limit_exceeded", params={
            "kind": "questions", "limit": _LIMIT_QUESTIONS, "count": question_count}))
    chain = _question_chain_depth(by_id, adjacency, preds)
    if chain > _LIMIT_CHAIN:
        issues.append(Issue("limit_exceeded", params={
            "kind": "chain", "limit": _LIMIT_CHAIN, "count": chain}))
    return _finish(issues)


def validate_graph_for_enable(value, context: EnableContext) -> list[Issue]:
    """启用校验：包含结构校验，再查模型登记、推理档位、时段定义与 JEV 配置存在性（不在线探测）。"""
    if not isinstance(context, EnableContext):
        raise TypeError(f"enable validation expects an EnableContext, got {type(context).__name__}")
    issues = validate_graph_structure(value)
    root, _ = _coerce(value)
    if root is None:
        return issues
    by_id = {}
    for node in root.nodes:
        if node.id not in by_id:
            by_id[node.id] = node
    for node in sorted(by_id.values(), key=lambda item: item.id):
        if isinstance(node, TargetNode):
            if not node.models:
                issues.append(Issue("target_models_required", node_id=node.id))
            for item in node.models:
                if item.model not in context.known_models:
                    issues.append(Issue(
                        "unknown_model", node_id=node.id,
                        params={"model": item.model},
                    ))
                else:
                    supported = (context.model_reasoning or {}).get(item.model)
                    efforts = {
                        entry.get("id")
                        for entry in (supported or {}).get("efforts", [])
                        if isinstance(entry, dict)
                    } if isinstance(supported, dict) else set()
                    if item.reasoning is not None and (
                        not isinstance(supported, dict)
                        or not supported.get("supported")
                        or item.reasoning not in efforts
                    ):
                        issues.append(Issue(
                            "reasoning_unsupported", node_id=node.id,
                            params={"model": item.model, "efforts": sorted(efforts),
                                    "reasoning": item.reasoning},
                        ))
        elif isinstance(node, ConditionNode) and node.source == "periods":
            for name in sorted(set(node.periods)):
                if name not in context.known_periods:
                    issues.append(Issue(
                        "unknown_period", node_id=node.id,
                        params={"period": name},
                    ))
    has_questions = any(isinstance(node, QuestionNode) for node in by_id.values())
    if has_questions and not context.jev_available:
        issues.append(Issue("decision_service_unavailable"))
    return _finish(issues)
