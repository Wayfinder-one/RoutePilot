"""决策组 envelope（Sticky / RoutingTree）与 tree_* 序列化视图；图模型在 router.routing.graph.model。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from router.routing.codec import optional_text_field, reject_unknown_fields, text_field
from router.routing.graph.model import (
    GraphRoot,
    canonical_json,
    graph_behavior_dict,
    graph_from_dict,
    graph_to_dict,
)

__all__ = [
    "RoutingTree",
    "Sticky",
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
    document_sha256: str = ""


def _sticky_from_dict(raw) -> Sticky:
    if raw is None:
        return Sticky()
    if not isinstance(raw, dict):
        raise ValueError("sticky must be an object")
    reject_unknown_fields("sticky", raw, {"enabled", "ttl_seconds"})
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
    reject_unknown_fields(
        "tree",
        raw,
        {"id", "name", "enabled", "timezone", "sticky", "root", "tree_revision",
         "updated_at", "document_sha256"},
    )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    return RoutingTree(
        id=text_field(raw.get("id"), "tree id"),
        name=optional_text_field(raw.get("name", ""), "tree name"),
        enabled=enabled,
        root=graph_from_dict(raw.get("root")),
        sticky=_sticky_from_dict(raw.get("sticky")),
        timezone=text_field(raw.get("timezone", "Asia/Shanghai"), "timezone"),
        tree_revision=optional_text_field(raw.get("tree_revision", ""), "tree_revision"),
        updated_at=optional_text_field(raw.get("updated_at", ""), "updated_at"),
        document_sha256=optional_text_field(raw.get("document_sha256", ""), "document_sha256"),
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
        "document_sha256": tree.document_sha256,
    }


def tree_revision_of(tree: RoutingTree) -> str:
    payload = canonical_json(tree_behavior_dict(tree)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
