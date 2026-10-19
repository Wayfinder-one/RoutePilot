"""Pipeline health checks for the explicit-tree model.

PUT /admin/pipeline runs these and rejects hard issues with 422. Evaluators
assume a healthy tree; this module keeps that assumption true.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.model import (ACTIONS, STATION_TYPES, Station, TreeNode,
                                   Weight)
from router.signals.registry import SIGNALS


@dataclass
class Issue:
    code: str
    zh: str
    en: str
    station_id: str = ""
    node_id: str = ""
    level: str = "hard"             # 'hard' rejects the save (422); 'warn' does not


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


def _check_node(node: TreeNode, station: Station, known_models: set[str],
                *, is_root: bool, siblings_seen: set[tuple]) -> list[Issue]:
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
    # ---- leaf / fork ----
    if node.kind == "fork":
        if not node.children:
            issues.append(Issue("empty_fork", "分叉节点没有子节点",
                                "Fork node has no children", station.id, node.id))
        child_seen: set[tuple] = set()
        for child in node.children:
            issues.extend(_check_node(child, station, known_models,
                                      is_root=False, siblings_seen=child_seen))
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
        if node.weights:
            issues.extend(_check_weights(node, station))
            if not any(w.weight > 0 for w in node.weights):
                issues.append(Issue("zero_weight", "加权条件权重全为 0",
                                    "All weights are zero", station.id, node.id))
            if node.min_score is None:
                issues.append(Issue("missing_min_score",
                                    "加权叶子没有配置达成率门槛",
                                    "Weighted leaf has no attainment floor",
                                    station.id, node.id, level="warn"))
    return issues


def _is_unconditional_leaf(station: Station) -> bool:
    root = station.root
    return (root is not None and root.signal is None
            and root.kind == "leaf" and not root.weights)


def check_pipeline(stations: list[Station], known_models: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    if not stations:
        issues.append(Issue("empty_pipeline", "管道为空", "Pipeline is empty"))
        return issues
    for station in stations:
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
        issues.extend(_check_node(station.root, station, known_models,
                                  is_root=True, siblings_seen=set()))
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
