from __future__ import annotations

import json
from dataclasses import replace

from router.routing.graph.model import canvas_to_dict, graph_to_dict
from router.routing.runtime import RUNTIME_FIELDS, runtime_to_dict
from router.routing.tree import RoutingTree, tree_from_dict, tree_revision_of

KIND = "sr-routing-graph"
VERSION = 4
FIELDS = {
    "kind", "version", "name", "departments", "enabled", "default",
    "timezone", "sticky", "root", "canvas", *RUNTIME_FIELDS,
}


def graph_document(tree: RoutingTree) -> dict:
    document = {
        "kind": KIND,
        "version": VERSION,
        "name": tree.name,
        "departments": list(tree.departments),
        "enabled": tree.enabled,
        "default": tree.default,
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": graph_to_dict(tree.root),
    }
    canvas = canvas_to_dict(tree.root)
    if canvas:
        document["canvas"] = canvas
    document.update(runtime_to_dict(tree.runtime))
    return document


def graph_document_bytes(tree: RoutingTree) -> bytes:
    text = json.dumps(graph_document(tree), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return text.encode("utf-8")


def parse_graph_document(content: bytes, *, tree_id: str,
                         updated_at: str, document_sha256: str) -> RoutingTree:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # JSON 解码是唯一能由任意嵌套输入触发递归的边界；在这里一次性归一为
        # ValueError，存储/控制/API 层只处理 ValueError，不再各自兜 RecursionError。
        raise ValueError("graph document is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("graph document must be an object")
    if raw.get("kind") != KIND or isinstance(raw.get("version"), bool) or raw.get("version") != VERSION:
        raise ValueError("only sr-routing-graph v4 is accepted")
    missing = sorted(FIELDS - set(raw) - {"canvas", *RUNTIME_FIELDS})
    if missing:
        raise ValueError(f"missing graph document fields: {', '.join(missing)}")
    runtime_fields = set(raw) & RUNTIME_FIELDS
    if runtime_fields != RUNTIME_FIELDS:
        raise ValueError("runtime fields must appear together")
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        raise ValueError(f"unknown graph document fields: {', '.join(unknown)}")
    payload = {
        "id": tree_id,
        "name": raw["name"],
        "enabled": raw["enabled"],
        "default": raw["default"],
        "departments": raw["departments"],
        "timezone": raw["timezone"],
        "sticky": raw["sticky"],
        "root": raw["root"],
        "canvas": raw.get("canvas", {}),
        "updated_at": updated_at,
    }
    payload.update({field: raw[field] for field in RUNTIME_FIELDS})
    tree = tree_from_dict(payload)
    return replace(
        tree,
        tree_revision=tree_revision_of(tree),
        document_sha256=document_sha256,
    )
