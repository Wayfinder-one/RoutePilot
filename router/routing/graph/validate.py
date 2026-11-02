"""Graph v3 校验器：结构校验（保存/试算/导入）与启用校验（启用/已启用组保存）。

纯函数：不打印、不抛业务异常（解析错误由 Task 2 的严格解析负责，dict 输入的解析失败
折叠为 `graph_parse_error`）。Issue 列表按 (level, code, node_id, edge_id, port) 稳定
排序，同图不同输入顺序得到相同集合。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
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
)
from router.routing.graph.ports import output_ports, slot_sort_key
from router.routing.model import RoutingTree, graph_from_dict, tree_from_dict

_LOGIC_OPERATORS = frozenset({"and", "or", "not"})
_LIMIT_NODES = 200
_LIMIT_EDGES = 400
_LIMIT_QUESTIONS = 32
_LIMIT_CHAIN = 8
_LIMIT_OPTIONS = 255
_CRITERIA_MIN = 2
_CRITERIA_MAX = 10


def _finite_level(level: object) -> bool:
    """与 model.py 同型的有限性判定：超量级 int 在 float() 处溢出 → 视为非法。"""
    try:
        return math.isfinite(float(level))
    except OverflowError:
        return False


@dataclass(frozen=True)
class Issue:
    """一条校验问题；node_id/edge_id/port 按错误类型尽量给出，供控制台定位（spec §5.1）。"""

    code: str
    zh: str
    en: str
    node_id: str = ""
    edge_id: str = ""
    port: str = ""
    level: str = "hard"


@dataclass(frozen=True)
class EnableContext:
    """启用校验上下文：模型目录、推理档位、时段定义与 JEV 配置存在性（不做在线探测）。"""

    known_models: frozenset[str] | set[str] = frozenset()
    capabilities: Mapping[str, str] = field(default_factory=dict)
    model_reasoning: Mapping[str, str | None] = field(default_factory=dict)
    known_periods: frozenset[str] | set[str] = frozenset()
    jev_available: bool = False


def _finish(issues: list[Issue]) -> list[Issue]:
    issues.sort(key=lambda item: (item.level, item.code, item.node_id, item.edge_id, item.port, item.zh, item.en))
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
    """DFS 找回边：返回 (node_id, edge_id, target_port) 列表，即成环处。"""
    state: dict[str, int] = {}
    back: list[tuple[str, str, str]] = []

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for target, edge_id, port in sorted(adjacency.get(node_id, ()), key=lambda item: (item[0], item[1])):
            current = state.get(target, 0)
            if current == 1:
                back.append((target, edge_id, port))
            elif current == 0:
                visit(target)
        state[node_id] = 2

    for node_id in sorted(adjacency):
        if state.get(node_id, 0) == 0:
            visit(node_id)
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
    context = {"node_id": edge.target, "edge_id": edge.id, "port": edge.target_port}
    tail = f"{edge.source}.{edge.source_port} → {edge.target}.{edge.target_port}"
    if "requires a comparison condition" in reason:
        return Issue(
            "numeric_edge_condition_missing",
            f"数值输入必须配置比较条件：{tail}",
            f"Numeric input requires a comparison condition: {tail}",
            **context,
        )
    if reason.startswith("numeric") or "threshold" in reason:
        return Issue(
            "numeric_edge_condition_invalid",
            f"数值比较条件非法（算子非法、阈值非有限或量级超限）：{tail}",
            f"Invalid numeric condition (bad operator, non-finite or out-of-range threshold): {tail}",
            **context,
        )
    return Issue(
        "boolean_edge_condition_invalid",
        f"布尔边条件既不能为空（= true）也不能是非法 is 条件：{tail}",
        f"Boolean edge condition must be null (=true) or a valid is condition: {tail}",
        **context,
    )


def validate_graph_structure(value) -> list[Issue]:
    """结构校验（保存/试算/导入）：时区、start、id、端点、槽位、环、条件、节点配置、兜底与限额。"""
    root, timezone = _coerce(value)
    if root is None:
        return _finish([Issue(
            "graph_parse_error",
            "图解析失败：root 不符合 sr-routing-graph v3 结构（未知字段或类型非法）",
            "Graph parse failed: root does not conform to the sr-routing-graph v3 structure (unknown fields or invalid types)",
        )])
    issues: list[Issue] = []
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError, OSError):
            issues.append(Issue(
                "invalid_timezone",
                f"时区非法：{timezone} 不是可用的 IANA 时区",
                f"Invalid timezone: {timezone} is not a usable IANA timezone",
            ))

    by_id: dict[str, GraphNode] = {}
    duplicate_nodes: set[str] = set()
    for node in root.nodes:
        if node.id in by_id:
            duplicate_nodes.add(node.id)
        else:
            by_id[node.id] = node
    for node_id in sorted(duplicate_nodes):
        issues.append(Issue("duplicate_node_id", f"节点 id 重复：{node_id}", f"Duplicate node id: {node_id}", node_id=node_id))

    seen_edge_ids: set[str] = set()
    duplicate_edges: set[str] = set()
    for edge in root.edges:
        if edge.id in seen_edge_ids:
            duplicate_edges.add(edge.id)
        seen_edge_ids.add(edge.id)
    for edge_id in sorted(duplicate_edges):
        issues.append(Issue("duplicate_edge_id", f"边 id 重复：{edge_id}", f"Duplicate edge id: {edge_id}", edge_id=edge_id))

    starts = sum(isinstance(node, StartNode) for node in by_id.values())
    if starts != 1:
        issues.append(Issue(
            "start_count",
            f"start 节点数量必须为 1（当前 {starts} 个）",
            f"Exactly one start node is required (found {starts})",
        ))

    for node in sorted(by_id.values(), key=lambda item: item.id):
        if isinstance(node, LogicNode):
            if node.operator not in _LOGIC_OPERATORS:  # 解析层已拦截，这里兜住直接构造的节点
                issues.append(Issue(
                    "logic_operator_invalid",
                    f"判断门算子非法：{node.operator}（{node.id}）",
                    f"Invalid logic operator: {node.operator} ({node.id})",
                    node_id=node.id,
                ))
        elif isinstance(node, ConditionNode):
            if node.source == "text" and not node.contains_any:
                issues.append(Issue(
                    "condition_config_invalid",
                    f"条件节点配置非法：source=text 需要非空 contains_any（{node.id}）",
                    f"Invalid condition node: source=text requires a non-empty contains_any ({node.id})",
                    node_id=node.id,
                ))
            if node.source == "periods" and not node.periods:
                issues.append(Issue(
                    "condition_config_invalid",
                    f"条件节点配置非法：source=periods 需要非空时段列表（{node.id}）",
                    f"Invalid condition node: source=periods requires a non-empty period list ({node.id})",
                    node_id=node.id,
                ))
        elif isinstance(node, QuestionNode):
            if not node.instructions.strip():
                issues.append(Issue(
                    "question_incomplete",
                    f"问题说明不能为空：{node.id}",
                    f"Question instructions must not be empty: {node.id}",
                    node_id=node.id,
                ))
            if node.question_type == "choice":
                if len(node.options) < 2:
                    issues.append(Issue(
                        "question_incomplete",
                        f"choice 选项至少需要 2 个（当前 {len(node.options)} 个）：{node.id}",
                        f"A choice question needs at least 2 options (found {len(node.options)}): {node.id}",
                        node_id=node.id,
                    ))
                if len(node.options) > _LIMIT_OPTIONS:
                    issues.append(Issue(
                        "limit_exceeded",
                        f"选项数量超过上限 {_LIMIT_OPTIONS}（当前 {len(node.options)} 个）：{node.id}",
                        f"Too many options: limit is {_LIMIT_OPTIONS} (found {len(node.options)}): {node.id}",
                        node_id=node.id,
                    ))
                seen_keys: set[str] = set()
                seen_labels: set[str] = set()
                for option in node.options:
                    if not option.key.strip():
                        issues.append(Issue(
                            "question_incomplete",
                            f"选项 key 不能为空：{node.id}",
                            f"Option key must not be empty: {node.id}",
                            node_id=node.id,
                        ))
                    elif option.key in seen_keys:
                        issues.append(Issue(
                            "question_incomplete",
                            f"选项 key 重复：{option.key}（{node.id}）",
                            f"Duplicate option key: {option.key} ({node.id})",
                            node_id=node.id,
                        ))
                    seen_keys.add(option.key)
                    if not option.label.strip():
                        issues.append(Issue(
                            "question_incomplete",
                            f"选项 label 不能为空：{option.key}（{node.id}）",
                            f"Option label must not be empty: {option.key} ({node.id})",
                            node_id=node.id,
                        ))
                    elif option.label in seen_labels:
                        issues.append(Issue(
                            "question_incomplete",
                            f"选项 label 重复：{option.label}（{node.id}）",
                            f"Duplicate option label: {option.label} ({node.id})",
                            node_id=node.id,
                        ))
                    seen_labels.add(option.label)
                    if not option.capability.strip():
                        issues.append(Issue(
                            "question_incomplete",
                            f"选项 capability 不能为空：{option.key}（{node.id}）",
                            f"Option capability must not be empty: {option.key} ({node.id})",
                            node_id=node.id,
                        ))
            if node.question_type == "score":
                if node.criteria is None:
                    issues.append(Issue(
                        "score_criteria_required",
                        f"score 问题必须配置评分档 criteria：{node.id}",
                        f"A score question requires criteria levels: {node.id}",
                        node_id=node.id,
                    ))
                else:
                    levels = node.criteria
                    broken = len(levels) < _CRITERIA_MIN or len(levels) > _CRITERIA_MAX or any(
                        isinstance(level, bool) or not isinstance(level, (int, float)) or not _finite_level(level)
                        for level in levels
                    )
                    if broken:
                        issues.append(Issue(
                            "score_criteria_invalid",
                            f"score 的 criteria 必须是 {_CRITERIA_MIN}–{_CRITERIA_MAX} 个有限数值：{node.id}",
                            f"Score criteria must be {_CRITERIA_MIN} to {_CRITERIA_MAX} finite numbers: {node.id}",
                            node_id=node.id,
                        ))
        elif isinstance(node, TargetNode) and node.priority < 1:
            issues.append(Issue(
                "target_priority_invalid",
                f"target priority 必须是正整数：{node.id}",
                f"Target priority must be a positive integer: {node.id}",
                node_id=node.id,
            ))

    seen_priorities: set[int] = set()
    for node in sorted((item for item in by_id.values() if isinstance(item, TargetNode)), key=lambda item: item.id):
        if node.priority in seen_priorities:
            issues.append(Issue(
                "target_priority_invalid",
                f"target priority 必须唯一：{node.id}",
                f"Target priority must be unique: {node.id}",
                node_id=node.id,
            ))
        seen_priorities.add(node.priority)

    for field_name in ("otherwise_target", "error_fallback"):
        ref = getattr(root, field_name)
        if not ref:
            issues.append(Issue(
                "fallback_missing",
                f"{field_name} 未配置：{'正常执行结束但没有 target 激活时' if field_name == 'otherwise_target' else '决策调用失败或超时、整波作废时'}需要兜底目标",
                f"{field_name} is not configured: a fallback target is required",
                port=field_name,
            ))
        else:
            node = by_id.get(ref)
            if node is None:
                issues.append(Issue(
                    "fallback_missing",
                    f"{field_name} 指向的节点不存在：{ref}",
                    f"{field_name} points to a missing node: {ref}",
                    node_id=ref,
                    port=field_name,
                ))
            elif not isinstance(node, TargetNode):
                issues.append(Issue(
                    "fallback_missing",
                    f"{field_name} 必须指向 target 节点：{ref}",
                    f"{field_name} must point to a target node: {ref}",
                    node_id=ref,
                    port=field_name,
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
                "edge_endpoint_missing",
                f"边的起点节点不存在：{edge.source}（{edge.id}）",
                f"Edge source node does not exist: {edge.source} ({edge.id})",
                node_id=edge.source, edge_id=edge.id, port=edge.source_port,
            ))
        if target is None:
            issues.append(Issue(
                "edge_endpoint_missing",
                f"边的目标节点不存在：{edge.target}（{edge.id}）",
                f"Edge target node does not exist: {edge.target} ({edge.id})",
                node_id=edge.target, edge_id=edge.id, port=edge.target_port,
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
                "edge_port_missing",
                f"输出端口不存在：{edge.source}.{edge.source_port}（{edge.id}）",
                f"Output port does not exist: {edge.source}.{edge.source_port} ({edge.id})",
                node_id=edge.source, edge_id=edge.id, port=edge.source_port,
            ))
        if not slot_ok:
            issues.append(Issue(
                "edge_port_missing",
                f"输入槽位不存在：{edge.target}.{edge.target_port}（{edge.id}）",
                f"Input slot does not exist: {edge.target}.{edge.target_port} ({edge.id})",
                node_id=edge.target, edge_id=edge.id, port=edge.target_port,
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
                        "slot_conflict",
                        f"同一槽位存在多条入边：{target_id}.{port}",
                        f"Multiple incoming edges on the same slot: {target_id}.{port}",
                        node_id=target_id, edge_id=edge.id, port=port,
                    ))

    for node_id, edge_id, port in _cycle_back_edges(back_edges):
        issues.append(Issue(
            "edge_cycle",
            f"图中有环：{node_id}",
            f"The graph contains a cycle at {node_id}",
            node_id=node_id, edge_id=edge_id, port=port,
        ))

    for node in sorted(by_id.values(), key=lambda item: item.id):
        incoming_count = len(incoming[node.id])
        if isinstance(node, LogicNode):
            if node.operator == "not" and incoming_count != 1:
                issues.append(Issue(
                    "logic_slot_count",
                    f"NOT 必须恰好有 1 个输入（当前 {incoming_count} 个）：{node.id}",
                    f"NOT needs exactly 1 input (found {incoming_count}): {node.id}",
                    node_id=node.id,
                ))
            if node.operator in {"and", "or"} and incoming_count < 2:
                issues.append(Issue(
                    "logic_slot_count",
                    f"AND / OR 至少需要 2 个输入（当前 {incoming_count} 个）：{node.id}",
                    f"AND / OR need at least 2 inputs (found {incoming_count}): {node.id}",
                    node_id=node.id,
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
            issues.append(Issue(
                "question_unreachable",
                f"问题节点从 start 与第 0 阶段条件均不可达：{node.id}",
                f"Question node is reachable from neither the start node nor any phase-0 condition: {node.id}",
                node_id=node.id,
            ))

    if len(root.nodes) > _LIMIT_NODES:
        issues.append(Issue(
            "limit_exceeded",
            f"节点数超过上限 {_LIMIT_NODES}（当前 {len(root.nodes)} 个）",
            f"Too many nodes: limit is {_LIMIT_NODES} (found {len(root.nodes)})",
        ))
    if len(root.edges) > _LIMIT_EDGES:
        issues.append(Issue(
            "limit_exceeded",
            f"边数超过上限 {_LIMIT_EDGES}（当前 {len(root.edges)} 条）",
            f"Too many edges: limit is {_LIMIT_EDGES} (found {len(root.edges)})",
        ))
    question_count = sum(isinstance(node, QuestionNode) for node in by_id.values())
    if question_count > _LIMIT_QUESTIONS:
        issues.append(Issue(
            "limit_exceeded",
            f"问题节点超过上限 {_LIMIT_QUESTIONS}（当前 {question_count} 个）",
            f"Too many question nodes: limit is {_LIMIT_QUESTIONS} (found {question_count})",
        ))
    chain = _question_chain_depth(by_id, adjacency, preds)
    if chain > _LIMIT_CHAIN:
        issues.append(Issue(
            "limit_exceeded",
            f"最长问题链 {chain} 波，超过上限 {_LIMIT_CHAIN} 波",
            f"Longest question chain is {chain} waves, limit is {_LIMIT_CHAIN}",
        ))
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
                issues.append(Issue(
                    "target_models_required",
                    f"target 未配置任何模型：{node.id}",
                    f"Target has no models configured: {node.id}",
                    node_id=node.id,
                ))
            for item in node.models:
                if item.model not in context.known_models:
                    issues.append(Issue(
                        "unknown_model",
                        f"目标引用了未登记模型：{item.model}（{node.id}）",
                        f"Target references an unknown model: {item.model} ({node.id})",
                        node_id=node.id,
                    ))
                else:
                    supported = (context.model_reasoning or {}).get(item.model)
                    if item.reasoning != supported:
                        issues.append(Issue(
                            "reasoning_unsupported",
                            f"模型不支持所选推理档位：{item.model} 支持 {supported!r}，目标配置 {item.reasoning!r}（{node.id}）",
                            f"Model does not support the selected reasoning tier: {item.model} supports {supported!r}, target uses {item.reasoning!r} ({node.id})",
                            node_id=node.id,
                        ))
        elif isinstance(node, ConditionNode) and node.source == "periods":
            for name in sorted(set(node.periods)):
                if name not in context.known_periods:
                    issues.append(Issue(
                        "unknown_period",
                        f"条件引用未定义时段：{name}（{node.id}）",
                        f"Condition references an undefined period: {name} ({node.id})",
                        node_id=node.id,
                    ))
    has_questions = any(isinstance(node, QuestionNode) for node in by_id.values())
    if has_questions and not context.jev_available:
        issues.append(Issue(
            "decision_service_unavailable",
            "图包含问题节点，但决策服务（JEV）未配置或不可用",
            "The graph contains question nodes but the decision service (JEV) is not configured or unavailable",
        ))
    return _finish(issues)
