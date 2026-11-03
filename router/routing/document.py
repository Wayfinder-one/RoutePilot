from __future__ import annotations

import json
from dataclasses import replace

from router.routing.model import RoutingTree, graph_to_dict, tree_from_dict, tree_revision_of

KIND = "sr-routing-graph"
VERSION = 3
FIELDS = {"kind", "version", "name", "timezone", "sticky", "root"}


def graph_document(tree: RoutingTree) -> dict:
    return {
        "kind": KIND,
        "version": VERSION,
        "name": tree.name,
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": graph_to_dict(tree.root),
    }


def graph_document_bytes(tree: RoutingTree) -> bytes:
    text = json.dumps(graph_document(tree), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return text.encode("utf-8")


def parse_graph_document(content: bytes, *, tree_id: str, enabled: bool,
                         updated_at: str, document_sha256: str) -> RoutingTree:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("graph document is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("graph document must be an object")
    if raw.get("kind") != KIND or isinstance(raw.get("version"), bool) or raw.get("version") != VERSION:
        raise ValueError("only sr-routing-graph v3 is accepted")
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        raise ValueError(f"unknown graph document fields: {', '.join(unknown)}")
    tree = tree_from_dict({
        "id": tree_id,
        "name": raw.get("name", ""),
        "enabled": enabled,
        "timezone": raw.get("timezone", "Asia/Shanghai"),
        "sticky": raw.get("sticky"),
        "root": raw.get("root"),
        "updated_at": updated_at,
    })
    return replace(
        tree,
        tree_revision=tree_revision_of(tree),
        document_sha256=document_sha256,
    )
