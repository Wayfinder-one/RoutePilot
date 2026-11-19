import hashlib
import json

import pytest

from router.routing.document import graph_document, graph_document_bytes, parse_graph_document
from router.routing.tree import tree_from_dict, tree_revision_of
from tests.routing_fixtures import tree_dict


def load(content, **overrides):
    values = {
        "tree_id": "file-main", "enabled": True,
        "updated_at": "2026-11-03T00:00:00+00:00",
        "document_sha256": hashlib.sha256(content).hexdigest(),
    }
    values.update(overrides)
    return parse_graph_document(content, **values)


def _migrated(tree):
    """把旧式节点级 position 收进顶层 canvas（新格式 fixture 保持原样）。"""
    canvas = {}
    for node in tree["root"]["nodes"]:
        position = node.pop("position", None)
        if isinstance(position, dict):
            canvas[node["id"]] = position
    if canvas:
        tree["canvas"] = canvas
    return tree


def test_v3_document_roundtrip_preserves_display_and_external_metadata():
    raw = tree_dict()
    raw["canvas"]["start"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][0]["title"] = "入口"
    raw["root"]["nodes"][-1]["capability"] = "展示说明"
    source = tree_from_dict(raw)

    content = graph_document_bytes(source)
    document = json.loads(content)
    assert set(document) == {"kind", "version", "name", "timezone", "sticky", "root", "canvas"}
    assert document["kind"] == "sr-routing-graph"
    assert document["version"] == 3
    assert "id" not in document and "enabled" not in document
    assert document["canvas"]["start"] == {"x": 321, "y": 654}

    loaded = load(content)
    assert loaded.id == "file-main" and loaded.enabled is True
    assert loaded.root.nodes[0].position.x == 321
    assert loaded.root.nodes[-1].capability == "展示说明"
    assert loaded.tree_revision == tree_revision_of(source)
    assert loaded.document_sha256 == hashlib.sha256(content).hexdigest()


def test_layout_only_change_changes_file_hash_not_behavior_revision():
    first = tree_from_dict(tree_dict())
    moved = tree_dict()
    moved["canvas"]["start"] = {"x": 40, "y": 80}
    second = tree_from_dict(moved)
    first_bytes = graph_document_bytes(first)
    second_bytes = graph_document_bytes(second)
    assert hashlib.sha256(first_bytes).digest() != hashlib.sha256(second_bytes).digest()
    assert tree_revision_of(first) == tree_revision_of(second)


@pytest.mark.parametrize("field,value", [
    ("kind", "legacy"), ("version", 2), ("version", "3"),
    ("id", "embedded"), ("enabled", True), ("extra", {}),
])
def test_document_rejects_non_v3_and_unknown_outer_fields(field, value):
    document = graph_document(tree_from_dict(tree_dict()))
    document[field] = value
    with pytest.raises(ValueError):
        load(json.dumps(document).encode())


@pytest.mark.parametrize("field", ["kind", "version", "name", "timezone", "sticky", "root"])
def test_document_rejects_every_missing_outer_field(field):
    document = graph_document(tree_from_dict(tree_dict()))
    del document[field]

    with pytest.raises(ValueError):
        load(json.dumps(document).encode())


def test_deeply_nested_json_maps_to_value_error_not_recursion_error():
    """JSON 解码边界唯一一次把 RecursionError 归一为 ValueError（任意嵌套输入）。"""
    nested = b"[" * 200_000 + b"]" * 200_000
    content = (
        b'{"kind":"sr-routing-graph","version":3,"name":"x","timezone":"UTC",'
        b'"sticky":{"enabled":false,"ttl_seconds":1800},"root":' + nested + b"}"
    )
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        load(content)


# ── canvas key（节点级 position 严格拒绝） ─────────────────────────────


def test_document_moves_positions_to_top_level_canvas():
    raw = _migrated(tree_dict())
    assert raw["canvas"]
    expected = raw["canvas"][raw["root"]["nodes"][0]["id"]]
    source = tree_from_dict(raw)

    document = json.loads(graph_document_bytes(source))
    assert document["canvas"] == raw["canvas"]
    for node in document["root"]["nodes"]:
        assert "position" not in node, node

    loaded = load(graph_document_bytes(source))
    assert loaded.root.nodes[0].position.x == expected["x"]
    assert loaded.tree_revision == tree_revision_of(source)


def test_document_omits_canvas_when_no_node_has_a_position():
    raw = tree_dict()
    raw.pop("canvas", None)
    for node in raw["root"]["nodes"]:
        node.pop("position", None)
    document = json.loads(graph_document_bytes(tree_from_dict(raw)))
    assert "canvas" not in document


def test_document_rejects_legacy_node_position():
    raw = _migrated(tree_dict())
    raw["root"]["nodes"][0]["position"] = {"x": 1, "y": 2}
    content = json.dumps({
        "kind": "sr-routing-graph", "version": 3, "name": raw.get("name", ""),
        "timezone": raw["timezone"], "sticky": raw["sticky"], "root": raw["root"],
        "canvas": raw["canvas"],
    }, ensure_ascii=False).encode("utf-8")
    with pytest.raises(ValueError, match="unknown"):
        load(content)


@pytest.mark.parametrize("canvas, message", [
    ([], "canvas must be an object"),
    ({"start": {"x": 1}}, "canvas.start"),
    ({"ghost": {"x": 1, "y": 2}}, "canvas references unknown nodes"),
    ({"start": {"x": "1", "y": 2}}, "canvas.start.x"),
    ({"start": {"x": 1, "y": 2, "z": 3}}, "canvas.start"),
    ({"start": {"x": float("nan"), "y": 2}}, "canvas.start.x"),
])
def test_document_rejects_invalid_canvas(canvas, message):
    raw = tree_dict()
    raw.pop("canvas", None)
    content = json.dumps({
        "kind": "sr-routing-graph", "version": 3, "name": raw.get("name", ""),
        "timezone": raw["timezone"], "sticky": raw["sticky"], "root": raw["root"],
        "canvas": canvas,
    }, ensure_ascii=False).encode("utf-8")
    with pytest.raises(ValueError, match=message):
        load(content)


def test_canvas_edits_do_not_change_the_behavior_revision():
    raw = _migrated(tree_dict())
    base = tree_from_dict(raw)
    moved = json.loads(graph_document_bytes(base))
    moved["canvas"] = {node["id"]: {"x": 10, "y": 20} for node in moved["root"]["nodes"]}
    shifted = load(json.dumps(moved, ensure_ascii=False).encode("utf-8"))
    assert shifted.tree_revision == tree_revision_of(base)
