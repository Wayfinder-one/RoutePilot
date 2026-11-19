import hashlib
import json
import threading

import pytest

from router.routing.tree import tree_behavior_dict, tree_from_dict, tree_revision_of
from router.routing.snapshot import build_snapshot
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphConflictError,
    GraphReadOnlyError,
    GraphStorageError,
    RoutingTreeRepo,
)
from tests.routing_fixtures import tree_dict


@pytest.fixture
def repo(tmp_path):
    repository = RoutingTreeRepo(tmp_path / "routing-graph")
    repository.initialize()
    return repository


def snapshot_of(tree):
    return build_snapshot(tree=tree, backends={}, time_periods=[], jev=None, capabilities={})


def test_create_stores_export_document_and_external_identity(repo):
    created = repo.create(tree_from_dict(tree_dict()))
    path = repo.root / "graphs" / "main.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["kind"] == "sr-routing-graph" and document["version"] == 3
    assert "id" not in document and "enabled" not in document
    assert created.document_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert repo.active_id() is None
    assert created.enabled is False


def test_duplicate_create_is_rejected(repo):
    repo.create(tree_from_dict(tree_dict()))
    with pytest.raises(GraphAlreadyExistsError):
        repo.create(tree_from_dict(tree_dict()))


def test_stale_or_missing_base_hash_cannot_overwrite(repo):
    created = repo.create(tree_from_dict(tree_dict()))
    before = (repo.root / "graphs" / "main.json").read_bytes()
    with pytest.raises(GraphConflictError) as stale:
        repo.update(tree_from_dict(tree_dict()), base_document_sha256="0" * 64)
    assert stale.value.actual_document_sha256 == created.document_sha256
    with pytest.raises(GraphConflictError):
        repo.update(tree_from_dict(tree_dict()), base_document_sha256="")
    assert (repo.root / "graphs" / "main.json").read_bytes() == before


def test_read_only_repo_cannot_write_and_bad_pointer_is_not_disabled(repo):
    reader = RoutingTreeRepo(repo.root, writer=False)
    with pytest.raises(GraphReadOnlyError):
        reader.initialize()
    with pytest.raises(GraphReadOnlyError):
        reader.create(tree_from_dict(tree_dict("other")))
    with pytest.raises(GraphReadOnlyError):
        reader.enable("main")
    with pytest.raises(GraphReadOnlyError):
        reader.disable("main")
    (repo.root / "current").write_text("../main\n", encoding="utf-8")
    with pytest.raises(GraphStorageError):
        reader.fingerprint()
    (repo.root / "current").unlink()
    with pytest.raises(GraphStorageError):
        reader.fingerprint()


def test_atomic_replace_never_exposes_partial_json(repo):
    repo.create(tree_from_dict(tree_dict()))
    path = repo.root / "graphs" / "main.json"
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                document = json.loads(path.read_bytes())
                assert document["root"]["type"] == "graph"
            except BaseException as exc:
                errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(30):
            raw = tree_dict()
            raw["name"] = f"图-{index}"
            current = repo.get("main")
            repo.update(tree_from_dict(raw), base_document_sha256=current.document_sha256)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert errors == []


def test_layout_and_display_fields_survive_repository_roundtrip(repo):
    raw = tree_dict()
    raw["root"]["nodes"][0]["title"] = "请求入口"
    raw["root"]["nodes"][0]["position"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][-1]["capability"] = "展示说明"
    created = repo.create(tree_from_dict(raw))
    loaded = repo.get("main")
    assert (loaded.root.nodes[0].position.x, loaded.root.nodes[0].position.y) == (321.0, 654.0)
    assert loaded.root.nodes[0].title == "请求入口"
    assert loaded.root.nodes[-1].capability == "展示说明"
    assert loaded.tree_revision == created.tree_revision


def test_layout_only_update_keeps_revision_but_changes_file_hash(repo):
    raw = tree_dict()
    created = repo.create(tree_from_dict(raw))
    before = (repo.root / "graphs" / "main.json").read_bytes()
    raw["root"]["nodes"][0]["position"] = {"x": 40, "y": 80}
    raw["root"]["nodes"][0]["title"] = "入口改名"
    moved = repo.update(tree_from_dict(raw), base_document_sha256=created.document_sha256)

    assert moved.tree_revision == created.tree_revision
    assert tree_behavior_dict(moved) == tree_behavior_dict(created)
    assert moved.root.nodes[0].position.x == 40.0
    assert moved.root.nodes[0].title == "入口改名"
    assert snapshot_of(moved).snapshot_revision == snapshot_of(created).snapshot_revision
    assert (repo.root / "graphs" / "main.json").read_bytes() != before


def test_update_changes_content_but_never_enabled(repo):
    tree = repo.create(tree_from_dict(tree_dict()))
    repo.enable(tree.id)
    raw = tree_dict(sticky={"enabled": False, "ttl_seconds": 60})
    raw["root"]["nodes"][1]["instructions"] = "重新选择能力"
    updated = repo.update(tree_from_dict(raw), base_document_sha256=repo.get("main").document_sha256)
    assert updated.enabled is True
    assert updated.root.nodes[1].instructions == "重新选择能力"
    assert updated.sticky.enabled is False
    assert updated.tree_revision != tree.tree_revision


def test_question_option_order_changes_revision(repo):
    raw = tree_dict()
    first = repo.create(tree_from_dict(raw))
    raw["root"]["nodes"][1]["options"].reverse()
    second = repo.update(tree_from_dict(raw), base_document_sha256=first.document_sha256)
    assert second.tree_revision != first.tree_revision
    assert [option.key for option in second.root.nodes[1].options] == ["strong", "easy"]


def test_copy_preserves_layout_display_and_capability(repo):
    raw = tree_dict("source")
    raw["root"]["nodes"][0]["title"] = "请求入口"
    raw["root"]["nodes"][0]["position"] = {"x": 40, "y": 60}
    raw["root"]["nodes"][-1]["capability"] = "展示说明"
    source = repo.create(tree_from_dict(raw))

    copied = repo.copy("source", "copy", "副本")

    assert copied.enabled is False
    assert copied.name == "副本"
    assert copied.tree_revision == source.tree_revision
    assert copied.root.nodes[0].title == "请求入口"
    assert (copied.root.nodes[0].position.x, copied.root.nodes[0].position.y) == (40.0, 60.0)
    assert copied.root.nodes[-1].capability == "展示说明"


def test_enable_disable_and_copy_preserve_single_active_invariant(repo):
    first = repo.create(tree_from_dict(tree_dict("a")))
    second = repo.create(tree_from_dict(tree_dict("b")))
    repo.enable(first.id)
    repo.enable(second.id)
    assert [tree.id for tree in repo.list() if tree.enabled] == ["b"]
    copied = repo.copy("b", "copy", "副本")
    assert copied.enabled is False
    assert copied.tree_revision == second.tree_revision
    assert repo.disable("a") is True
    assert [tree.id for tree in repo.list() if tree.enabled] == ["b"]
    assert repo.disable("b") is True
    assert not any(tree.enabled for tree in repo.list())
    assert repo.disable("missing") is False


def test_invalid_document_and_missing_active_file_raise_storage_error(repo):
    repo.create(tree_from_dict(tree_dict()))
    path = repo.root / "graphs" / "main.json"
    path.write_bytes(b"{broken")
    with pytest.raises(GraphStorageError):
        repo.get("main")
    path.write_bytes(tree_document_bytes(repo, "main"))
    (repo.root / "current").write_text("missing\n", encoding="utf-8")
    with pytest.raises(GraphStorageError):
        repo.active()


def tree_document_bytes(repo, tree_id):
    from router.routing.document import graph_document_bytes
    return graph_document_bytes(tree_from_dict(tree_dict(tree_id)))
