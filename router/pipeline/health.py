"""Pipeline health checks for the explicit-tree model.

PUT /admin/pipeline runs these and rejects hard issues with 422. Evaluators
assume a healthy tree; this module keeps that assumption true.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.model import (ACTIONS, STATION_TYPES, Station, TreeNode,
                                   Weight)
from router.reasoning import level_label
from router.signals.registry import SIGNALS


@dataclass
class Issue:
    code: str
    zh: str
    en: str
    station_id: str = ""
    node_id: str = ""
    level: str = "hard"             # 'hard' rejects the save (422); 'warn' does not
    #: reasoning_unsupported 的定位信息（§3.3：「detail 里带模型与档位」）
    model: str = ""
    reasoning: str = ""
    #: ignored_field 指出「配了但不会生效」的字段名
    field: str = ""


def _ignored_field(node: TreeNode, station: Station, field: str,
                   zh: str, en: str) -> Issue:
    """A field that is set but can never take effect (warn, never blocks)."""
    return Issue("ignored_field", zh, en, station.id, node.id, level="warn",
                 field=field)


def _check_weights(node: TreeNode, station: Station) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for w in node.weights:
        if w.signal not in SIGNALS:
            issues.append(Issue("unknown_signal", f"未知信号 {w.signal}",
                                f"Unknown signal {w.signal}",
                                station.id, node.id))
        elif (w.signal, w.value) in seen:
            issues.append(Issue("duplicate_weight",
                                "同一叶子重复添加了同一个信号条件",
                                "The same signal condition appears twice on one leaf",
                                station.id, node.id, level="warn"))
        elif not w.value:
            issues.append(Issue("weight_without_value",
                                "加权条件缺少取值",
                                "A weighted condition has no value",
                                station.id, node.id))
        seen.add((w.signal, w.value))
    return issues


def _check_node_ids(node: TreeNode, station: Station) -> list[Issue]:
    """Node ids must be unique inside one station tree.

    The console addresses nodes by id (drag & drop, the condition editor),
    so a duplicate id silently edits the wrong node.
    """
    issues: list[Issue] = []
    seen: set[str] = set()

    def walk(n: TreeNode) -> None:
        if n.id in seen:
            issues.append(Issue("duplicate_node_id",
                                f"节点 id 重复：{n.id}",
                                f"Duplicate node id: {n.id}",
                                station.id, n.id))
        seen.add(n.id)
        for child in n.children:
            walk(child)

    walk(node)
    return issues


def _check_node(node: TreeNode, station: Station, known_models: set[str],
                *, is_root: bool, siblings_seen: set[tuple],
                model_reasoning: dict[str, dict | None] | None = None
                ) -> list[Issue]:
    issues: list[Issue] = []
    # ---- condition ----
    if node.signal is not None:
        if node.signal not in SIGNALS:
            issues.append(Issue("unknown_signal", f"未知信号 {node.signal}",
                                f"Unknown signal {node.signal}",
                                station.id, node.id))
        if not node.value:
            issues.append(Issue("missing_value", "条件节点缺少取值",
                                "A condition node has no value",
                                station.id, node.id))
        spec = SIGNALS.get(node.signal)
        if spec is not None and spec.kind == "rule" and node.conf_min is not None:
            issues.append(Issue("rule_signal_floor",
                                "规则信号是确定性的，置信门槛不会生效",
                                "Rule signals are deterministic; the confidence "
                                "floor never applies",
                                station.id, node.id, level="warn"))
        # same condition repeated among siblings -> later one never reached
        key = (node.signal, node.value)
        if key in siblings_seen:
            issues.append(Issue("duplicate_condition",
                                "同一父节点下重复的条件，后一条永远轮不到",
                                "Duplicate condition under the same parent; the "
                                "later one is unreachable",
                                station.id, node.id, level="warn"))
        siblings_seen.add(key)
    else:
        if not is_root:
            issues.append(Issue("unconditional_child",
                                "只有根节点可以无条件；子节点没有条件会吞掉后面的兄弟",
                                "Only the root may be unconditional; an "
                                "unconditional child shadows its siblings",
                                station.id, node.id))
        if node.conf_min is not None:
            issues.append(_ignored_field(
                node, station, "conf_min",
                "无条件节点的置信门槛不会生效",
                "The confidence floor of an unconditional node never applies"))
    # ---- leaf / fork ----
    if node.kind == "fork":
        if not node.children:
            issues.append(Issue("empty_fork", "分叉节点没有子节点",
                                "Fork node has no children", station.id, node.id))
        if node.model:
            issues.append(_ignored_field(
                node, station, "model",
                "分叉节点的模型不会生效（只有叶子能路由）",
                "The model of a fork node never applies (only leaves route)"))
        if node.action != "route":
            issues.append(_ignored_field(
                node, station, "action",
                "分叉节点的动作不会生效（只有叶子能拦截/路由）",
                "The action of a fork node never applies (only leaves "
                "block or route)"))
        if node.min_score is not None:
            issues.append(_ignored_field(
                node, station, "min_score",
                "分叉节点的达成率门槛不会生效",
                "The attainment floor of a fork node never applies"))
        child_seen: set[tuple] = set()
        for child in node.children:
            issues.extend(_check_node(child, station, known_models,
                                      is_root=False, siblings_seen=child_seen,
                                      model_reasoning=model_reasoning))
    else:
        if node.children:
            issues.append(Issue("leaf_with_children", "终点节点不能再有子节点",
                                "A leaf cannot have children", station.id, node.id))
        if node.action not in ACTIONS:
            issues.append(Issue("bad_action", f"非法动作 {node.action}",
                                f"Invalid action {node.action}",
                                station.id, node.id))
        needs_model = node.action == "route"
        if needs_model and not node.model:
            issues.append(Issue("missing_model", "路由叶子没有配置模型",
                                "Route leaf has no target model",
                                station.id, node.id))
        elif needs_model and node.model not in known_models:
            issues.append(Issue("unknown_model", f"未知模型 {node.model}",
                                f"Unknown model {node.model}",
                                station.id, node.id))
        if node.action == "block" and node.model:
            issues.append(_ignored_field(
                node, station, "model",
                "拦截叶子的模型不会生效（拦截不需要上游模型）",
                "The model of a blocking leaf never applies (blocking "
                "needs no upstream model)"))
        if model_reasoning is not None and node.reasoning is not None:
            issues.extend(_check_reasoning(node, station, known_models,
                                           model_reasoning))
        if node.weights:
            issues.extend(_check_weights(node, station))
            if not any(w.weight > 0 for w in node.weights):
                issues.append(Issue("zero_weight", "加权条件权重全为 0",
                                    "All weights are zero", station.id, node.id))
            if node.min_score is None:
                issues.append(Issue(
                    "missing_min_score",
                    "加权叶子没有配置达成率门槛（未配置时按默认 0.50 计）",
                    "Weighted leaf has no attainment floor "
                    "(defaults to 0.50 when unset)",
                    station.id, node.id, level="warn"))
        elif node.min_score is not None:
            issues.append(_ignored_field(
                node, station, "min_score",
                "没有加权条件的叶子不会使用达成率门槛",
                "A leaf without weighted conditions never uses the "
                "attainment floor"))
    return issues


def _check_reasoning(node: TreeNode, station: Station, known_models: set[str],
                     model_reasoning: dict[str, dict | None]) -> list[Issue]:
    """§3.3：分树叶子的推理选择硬校验（只在调用方传了 model_reasoning 时执行）。"""
    if node.action != "route":
        return [Issue("reasoning_without_route",
                      "推理档位只能配置在「路由」叶子上",
                      "Reasoning can only be set on a route leaf",
                      station.id, node.id, model=node.model or "",
                      reasoning=node.reasoning)]
    if not node.model or node.model not in known_models:
        return []                       # unknown/resolvable model: other checks own it
    capability = model_reasoning.get(node.model)
    levels: list[str] = []
    if isinstance(capability, dict) and capability.get("declared") \
            and capability.get("supported"):
        levels = [e["id"] for e in capability.get("efforts") or []
                  if isinstance(e, dict) and "id" in e]
    if not levels:
        return [Issue("reasoning_unsupported",
                      f"模型 {node.model} 未声明/不支持推理，不能选择推理档位",
                      f"Model {node.model} does not declare reasoning support",
                      station.id, node.id, model=node.model,
                      reasoning=node.reasoning)]
    if node.reasoning not in levels:
        return [Issue("reasoning_unsupported",
                      f"模型 {node.model} 未声明档位 "
                      f"{level_label(node.reasoning)}（{node.reasoning}）；"
                      f"可选：{', '.join(levels)}",
                      f"Model {node.model} does not declare the "
                      f"{node.reasoning!r} reasoning level; "
                      f"declared: {', '.join(levels)}",
                      station.id, node.id, model=node.model,
                      reasoning=node.reasoning)]
    return []


def _is_unconditional_leaf(station: Station) -> bool:
    root = station.root
    return (root is not None and root.signal is None
            and root.kind == "leaf" and not root.weights)


def check_pipeline(stations: list[Station], known_models: set[str],
                   model_reasoning: dict[str, dict | None] | None = None
                   ) -> list[Issue]:
    """Health-check a pipeline.

    ``model_reasoning`` is optional (old callers/tests pass only the known model
    ids): when None the reasoning rules of §3.3 are skipped entirely, so the
    previous behaviour is preserved. When given, it maps model_id to the
    normalized reasoning object (``router.reasoning.normalize_reasoning``
    output, or None when undeclared).
    """
    issues: list[Issue] = []
    if not stations:
        issues.append(Issue("empty_pipeline", "管道为空", "Pipeline is empty"))
        return issues
    seen_station_ids: set[str] = set()
    for station in stations:
        if station.id in seen_station_ids:
            issues.append(Issue("duplicate_station_id",
                                f"站点 id 重复：{station.id}",
                                f"Duplicate station id: {station.id}",
                                station_id=station.id))
        seen_station_ids.add(station.id)
        if station.type not in STATION_TYPES:
            issues.append(Issue("unknown_station_type",
                                f"未知站点类型 {station.type}",
                                f"Unknown station type {station.type}",
                                station_id=station.id))
            continue
        if station.root is None:
            issues.append(Issue("empty_station", "站点没有任何节点",
                                "Station has no nodes",
                                station_id=station.id))
            continue
        # id uniqueness is checked once per whole tree, outside the recursive
        # walk: inside _check_node every ancestor would re-visit the same
        # subtree and report one nested duplicate pair once per ancestor.
        issues.extend(_check_node(station.root, station, known_models,
                                  is_root=True, siblings_seen=set(),
                                  model_reasoning=model_reasoning))
        issues.extend(_check_node_ids(station.root, station))
    # An always-hit station anywhere but last shadows every following station.
    for i, station in enumerate(stations[:-1]):
        if _is_unconditional_leaf(station):
            issues.append(Issue("station_shadow",
                                "无条件直达的站点不在末位，后面的站点永远轮不到",
                                "An unconditional hitting station is not last; "
                                "every station after it is unreachable",
                                station_id=station.id))
    last = stations[-1]
    if not _is_unconditional_leaf(last) or last.root.action != "route" \
            or not last.root.model:
        issues.append(Issue(
            "missing_fallback",
            "末站点必须是「无条件 → 命中路由」的兜底", 
            "The last station must be an unconditional route leaf (fallback)",
            station_id=last.id, node_id=last.root.id if last.root else ""))
    return issues
