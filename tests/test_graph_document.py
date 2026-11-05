import hashlib
import json

import pytest

from router.routing.document import graph_document, graph_document_bytes, parse_graph_document
from router.routing.model import tree_from_dict, tree_revision_of
from tests.routing_fixtures import tree_dict


def load(content, **overrides):
    values = {
        "tree_id": "file-main", "enabled": True,
        "updated_at": "2026-11-03T00:00:00+00:00",
        "document_sha256": hashlib.sha256(content).hexdigest(),
    }
    values.update(overrides)
    return parse_graph_document(content, **values)


def test_v3_document_roundtrip_preserves_display_and_external_metadata():
    raw = tree_dict()
    raw["root"]["nodes"][0]["title"] = "入口"
    raw["root"]["nodes"][0]["position"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][-1]["capability"] = "展示说明"
    source = tree_from_dict(raw)

    content = graph_document_bytes(source)
    document = json.loads(content)
    assert set(document) == {"kind", "version", "name", "timezone", "sticky", "root"}
    assert document["kind"] == "sr-routing-graph"
    assert document["version"] == 3
    assert "id" not in document and "enabled" not in document

    loaded = load(content)
    assert loaded.id == "file-main" and loaded.enabled is True
    assert loaded.root.nodes[0].position.x == 321
    assert loaded.root.nodes[-1].capability == "展示说明"
    assert loaded.tree_revision == tree_revision_of(source)
    assert loaded.document_sha256 == hashlib.sha256(content).hexdigest()


def test_layout_only_change_changes_file_hash_not_behavior_revision():
    raw = tree_dict()
    first = tree_from_dict(raw)
    raw["root"]["nodes"][0]["position"] = {"x": 40, "y": 80}
    second = tree_from_dict(raw)
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
