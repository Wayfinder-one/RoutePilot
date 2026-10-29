from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from router.routing.model import Branch, Jev, RoutingTree, Target, tree_revision_of, walk_nodes


@dataclass(frozen=True)
class Issue:
    code: str
    zh: str
    en: str
    node_id: str = ""
    level: str = "hard"


@dataclass(frozen=True)
class EnableContext:
    known_models: set[str]
    capabilities: dict[str, str]
    model_reasoning: dict[str, dict | None]
    known_periods: set[str]
    jev_available: bool


def _issue(issues: list[Issue], code: str, zh: str, en: str, node_id: str = "") -> None:
    issues.append(Issue(code, zh, en, node_id))


def validate_tree_structure(tree: RoutingTree) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[str] = set()
    for node in walk_nodes(tree.root):
        if node.id in seen:
            _issue(issues, "duplicate_node_id", f"节点 id 重复：{node.id}", "Node ids must be unique", node.id)
        seen.add(node.id)
        if isinstance(node, Jev):
            keys = [option.key for option in node.options]
            labels = [option.label for option in node.options]
            if len(keys) != len(set(keys)):
                _issue(issues, "duplicate_option_key", "候选 key 必须唯一", "Option keys must be unique", node.id)
            if len(labels) != len(set(labels)):
                _issue(issues, "duplicate_option_label", "候选名称必须唯一", "Option labels must be unique", node.id)
    return issues


def _target_issues(target: Target, context: EnableContext, issues: list[Issue], *, capability_required: bool) -> None:
    if not target.models:
        _issue(issues, "target_models_required", "模型目标至少需要一个模型", "A target needs at least one model", target.id)
        return
    for item in target.models:
        if item.model not in context.known_models:
            _issue(issues, "unknown_model", f"未知模型：{item.model}", f"Unknown model: {item.model}", target.id)
            continue
        if item.reasoning is not None:
            capability = context.model_reasoning.get(item.model)
            efforts = {entry.get("id") for entry in (capability or {}).get("efforts", [])}
            if not capability or not capability.get("supported") or item.reasoning not in efforts:
                _issue(issues, "reasoning_unsupported", f"模型 {item.model} 不支持推理档位 {item.reasoning}", "Unsupported reasoning level", target.id)
    if capability_required and not target.capability.strip():
        capability = ""
        if len(target.models) == 1:
            capability = context.capabilities.get(target.models[0].model, "")
        if not capability.strip():
            _issue(issues, "capability_required", "JEV 候选需要非空能力说明；多模型目标必须在 target 上配置", "A JEV candidate needs a capability description", target.id)


def validate_tree_for_enable(tree: RoutingTree, context: EnableContext) -> list[Issue]:
    issues = validate_tree_structure(tree)
    try:
        ZoneInfo(tree.timezone)
    except ZoneInfoNotFoundError:
        _issue(issues, "invalid_timezone", f"未知时区：{tree.timezone}", "Invalid timezone")
    for node in walk_nodes(tree.root):
        if node.when is not None:
            for period in node.when.periods:
                if period not in context.known_periods:
                    _issue(issues, "unknown_period", f"未知时间段：{period}", f"Unknown time period: {period}", node.id)
        if isinstance(node, Target):
            _target_issues(node, context, issues, capability_required=False)
        elif isinstance(node, Branch):
            if not node.children:
                _issue(issues, "branch_children_required", "分支至少需要一个子节点", "A branch needs children", node.id)
            if node.otherwise is None:
                _issue(issues, "fallback_required", "分支必须有最终兜底", "A branch needs a fallback", node.id)
        elif isinstance(node, Jev):
            if not 2 <= len(node.options) <= 255:
                _issue(issues, "option_count", "JEV 节点需要 2–255 个候选", "A JEV node needs 2–255 options", node.id)
            if node.fallback is None:
                _issue(issues, "fallback_required", "JEV 节点必须有兜底模型目标", "A JEV node needs a fallback target", node.id)
            if not context.jev_available:
                _issue(issues, "jev_unavailable", "使用 JEV 节点前必须完成 JEV 服务配置", "JEV is not configured", node.id)
            for option in node.options:
                _target_issues(option.target, context, issues, capability_required=True)
    return issues


__all__ = ["EnableContext", "Issue", "validate_tree_for_enable", "validate_tree_structure", "tree_revision_of"]
