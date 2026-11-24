import hashlib
import json
from dataclasses import replace

import pytest

from router.routing.document import VERSION, graph_document, graph_document_bytes, parse_graph_document
from router.routing.tree import tree_from_dict, tree_revision_of
from tests.routing_fixtures import tree_dict


def load(content, **overrides):
    values = {
        "tree_id": "file-main",
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


def _document(raw, **overrides):
    """把 fixture dict 投影成文件文档（v4 必填字段齐全）。"""
    document = {
        "kind": "sr-routing-graph", "version": VERSION,
        "name": raw.get("name", ""),
        "departments": list(raw.get("departments", [])),
        "enabled": raw.get("enabled", False),
        "default": raw.get("default", False),
        "timezone": raw["timezone"], "sticky": raw["sticky"], "root": raw["root"],
        **{field: raw[field] for field in ("backends", "jev", "jev_window", "time_periods")},
    }
    document.update(overrides)
    return document


def test_v4_document_roundtrip_preserves_display_and_external_metadata():
    raw = tree_dict(enabled=True, default=True, departments=["财务部"])
    raw["canvas"]["start"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][0]["title"] = "入口"
    raw["root"]["nodes"][-1]["title"] = "展示说明"
    source = tree_from_dict(raw)

    content = graph_document_bytes(source)
    document = json.loads(content)
    assert set(document) == {
        "kind", "version", "name", "departments", "enabled", "default",
        "timezone", "sticky", "root", "canvas",
        "backends", "jev", "jev_window", "time_periods",
    }
    assert document["kind"] == "sr-routing-graph"
    assert document["version"] == 4
    assert "id" not in document
    assert document["departments"] == ["财务部"]
    assert document["enabled"] is True and document["default"] is True
    assert document["canvas"]["start"] == {"x": 321, "y": 654}

    loaded = load(content)
    assert loaded.id == "file-main" and loaded.enabled is True and loaded.default is True
    assert loaded.departments == ("财务部",)
    assert loaded.root.nodes[0].position.x == 321
    assert loaded.root.nodes[-1].title == "展示说明"
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
    ("kind", "legacy"), ("version", 3), ("version", "4"), ("id", "embedded"),
    ("extra", {}), ("default", True),
])
def test_document_rejects_non_v4_and_unknown_outer_fields(field, value):
    document = graph_document(tree_from_dict(tree_dict()))
    document[field] = value
    with pytest.raises(ValueError):
        load(json.dumps(document).encode())


@pytest.mark.parametrize("field", [
    "kind", "version", "name", "departments", "enabled", "default",
    "timezone", "sticky", "root",
])
def test_document_rejects_every_missing_outer_field(field):
    document = graph_document(tree_from_dict(tree_dict()))
    del document[field]

    with pytest.raises(ValueError):
        load(json.dumps(document).encode())


@pytest.mark.parametrize("field", ["backends", "jev", "jev_window", "time_periods"])
def test_document_rejects_missing_or_partial_runtime_fields(field):
    """四个运行字段必须成组出现；缺任何一个都拒绝（包括四者全缺）。"""
    for removed in ({field}, {"backends", "jev", "jev_window", "time_periods"}):
        document = graph_document(tree_from_dict(tree_dict()))
        for name in removed:
            del document[name]
        with pytest.raises(ValueError, match="runtime fields must appear together"):
            load(json.dumps(document).encode())


def test_deeply_nested_json_maps_to_value_error_not_recursion_error():
    """JSON 解码边界唯一一次把 RecursionError 归一为 ValueError（任意嵌套输入）。"""
    nested = b"[" * 200_000 + b"]" * 200_000
    content = (
        b'{"kind":"sr-routing-graph","version":4,"name":"x","departments":[],'
        b'"enabled":false,"default":false,"timezone":"UTC",'
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
    content = json.dumps(_document(raw), ensure_ascii=False).encode("utf-8")
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
    content = json.dumps(_document(raw, canvas=canvas), ensure_ascii=False).encode("utf-8")
    with pytest.raises(ValueError, match=message):
        load(content)


def test_canvas_edits_do_not_change_the_behavior_revision():
    raw = _migrated(tree_dict())
    base = tree_from_dict(raw)
    moved = json.loads(graph_document_bytes(base))
    moved["canvas"] = {node["id"]: {"x": 10, "y": 20} for node in moved["root"]["nodes"]}
    shifted = load(json.dumps(moved, ensure_ascii=False).encode("utf-8"))
    assert shifted.tree_revision == tree_revision_of(base)


# ── v4：部门与生命周期 envelope ─────────────────────────────────────────


def test_v4_roundtrip_contains_departments_and_lifecycle():
    raw = tree_dict(enabled=True, departments=["财务部", "法务部"])
    raw["default"] = True
    tree = tree_from_dict(raw)
    assert tree.departments == ("法务部", "财务部")
    assert tree.enabled is True and tree.default is True
    document = graph_document(tree)
    assert document["version"] == 4
    assert document["departments"] == ["法务部", "财务部"]
    assert document["enabled"] is True and document["default"] is True
    loaded = parse_graph_document(
        graph_document_bytes(tree), tree_id=tree.id,
        updated_at=tree.updated_at, document_sha256=tree.document_sha256)
    assert loaded.departments == tree.departments
    assert loaded.enabled is True and loaded.default is True


def test_departments_affect_revision_but_lifecycle_does_not():
    base = tree_from_dict(tree_dict(departments=[]))
    department = replace(base, departments=("财务部",))
    enabled = replace(base, enabled=True, default=True)
    assert tree_revision_of(department) != tree_revision_of(base)
    assert tree_revision_of(enabled) == tree_revision_of(base)


def test_v4_rejects_duplicate_department_and_disabled_default():
    duplicate = tree_dict(departments=["财务部", "财务部"])
    with pytest.raises(ValueError, match="duplicate department"):
        tree_from_dict(duplicate)
    invalid = tree_dict(enabled=False)
    invalid["default"] = True
    with pytest.raises(ValueError, match="default graph must be enabled"):
        tree_from_dict(invalid)
