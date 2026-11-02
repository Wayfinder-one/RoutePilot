"""决策组 envelope（Sticky / RoutingTree）与 tree_* 序列化视图；图模型在 router.routing.graph。"""
from __future__ import annotations

from dataclasses import dataclass

from router.routing.graph.conditions import (
    UNKNOWN,
    ConditionResult,
    conditionInvalidReason,
    evaluate_condition,
)
from router.routing.graph.model import (
    Condition,
    ConditionNode,
    Edge,
    GraphNode,
    GraphRoot,
    LogicNode,
    ModelTarget,
    Position,
    QuestionNode,
    QuestionOption,
    StartNode,
    TargetNode,
    canonical_json,
    graph_behavior_dict,
    graph_from_dict,
    graph_revision_of,
    graph_to_dict,
)
from router.routing.graph.ports import (
    Port,
    edge_type,
    next_slot,
    output_ports,
    slot_sort_key,
)

__all__ = [
    "UNKNOWN",
    "Condition",
    "ConditionNode",
    "ConditionResult",
    "Edge",
    "GraphNode",
    "GraphRoot",
    "LogicNode",
    "ModelTarget",
    "Port",
    "Position",
    "QuestionNode",
    "QuestionOption",
    "RoutingTree",
    "Sticky",
    "StartNode",
    "TargetNode",
    "canonical_json",
    "conditionInvalidReason",
    "edge_type",
    "evaluate_condition",
    "graph_behavior_dict",
    "graph_from_dict",
    "graph_revision_of",
    "graph_to_dict",
    "next_slot",
    "output_ports",
    "slot_sort_key",
    "tree_behavior_dict",
    "tree_from_dict",
    "tree_revision_of",
    "tree_storage_dict",
    "tree_to_dict",
]


@dataclass(frozen=True)
class Sticky:
    enabled: bool = False
    ttl_seconds: int = 1800


@dataclass(frozen=True)
class RoutingTree:
    id: str
    name: str
    enabled: bool
    root: GraphRoot
    sticky: Sticky = Sticky()
    timezone: str = "Asia/Shanghai"
    tree_revision: str = ""
    updated_at: str = ""


def _unknown(kind: str, raw: dict, allowed: set[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{kind} has unknown fields: {', '.join(unknown)}")


def _text(value, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{field} must be a {'string' if empty else 'nonempty string'}")
    return value


def _sticky_from_dict(raw) -> Sticky:
    if raw is None:
        return Sticky()
    if not isinstance(raw, dict):
        raise ValueError("sticky must be an object")
    _unknown("sticky", raw, {"enabled", "ttl_seconds"})
    enabled = raw.get("enabled", False)
    ttl = raw.get("ttl_seconds", 1800)
    if not isinstance(enabled, bool):
        raise ValueError("sticky.enabled must be a boolean")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 31536000:
        raise ValueError("sticky.ttl_seconds must be an integer in [1,31536000]")
    return Sticky(enabled, ttl)


def tree_from_dict(raw) -> RoutingTree:
    if not isinstance(raw, dict):
        raise ValueError("tree must be an object")
    _unknown("tree", raw, {"id", "name", "enabled", "timezone", "sticky", "root", "tree_revision", "updated_at"})
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    return RoutingTree(
        id=_text(raw.get("id"), "tree id"),
        name=_text(raw.get("name", ""), "tree name", empty=True),
        enabled=enabled,
        root=graph_from_dict(raw.get("root")),
        sticky=_sticky_from_dict(raw.get("sticky")),
        timezone=_text(raw.get("timezone", "Asia/Shanghai"), "timezone"),
        tree_revision=_text(raw.get("tree_revision", ""), "tree_revision", empty=True),
        updated_at=_text(raw.get("updated_at", ""), "updated_at", empty=True),
    )


def tree_storage_dict(tree: RoutingTree) -> dict:
    return {
        "id": tree.id,
        "name": tree.name,
        "enabled": tree.enabled,
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": graph_to_dict(tree.root),
    }


def tree_behavior_dict(tree: RoutingTree) -> dict:
    return {
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": graph_behavior_dict(tree.root),
    }


def tree_to_dict(tree: RoutingTree) -> dict:
    return {
        **tree_storage_dict(tree),
        "tree_revision": tree.tree_revision,
        "updated_at": tree.updated_at,
    }


def tree_revision_of(tree: RoutingTree) -> str:
    return graph_revision_of(tree)
