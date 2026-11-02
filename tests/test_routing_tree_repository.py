import json
import sqlite3
import threading

import pytest

from router.routing.model import (
    canonical_json,
    tree_behavior_dict,
    tree_from_dict,
    tree_revision_of,
)
from router.routing.snapshot import build_snapshot
from router.store.db import connect, migrate
from router.store.routing_trees import REVISION_TREE, RoutingTreeRepo
from tests.routing_fixtures import tree_dict


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "v3.db"))
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn):
    return RoutingTreeRepo(conn)


def snapshot_of(tree):
    return build_snapshot(tree=tree, backends={}, time_periods=[], jev=None, capabilities={})


def test_create_defaults_closed_and_stores_complete_graph_document(repo, conn):
    raw = tree_dict()
    raw["sticky"] = {"enabled": True, "ttl_seconds": 1800}
    source = tree_from_dict(raw)
    tree = repo.create(source)
    assert tree.enabled is False
    assert tree.tree_revision == tree_revision_of(source)
    stored = conn.execute("SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0]
    assert stored == canonical_json(json.loads(stored))
    document = json.loads(stored)
    assert document["root"]["type"] == "graph"
    assert document["sticky"] == {"enabled": True, "ttl_seconds": 1800}
    assert document["timezone"] == "Asia/Shanghai"
    assert document["root"]["nodes"][0]["position"] == {"x": 0, "y": 0}
    assert "tree_revision" not in document
    assert conn.execute(
        "SELECT COUNT(*) FROM config_revisions WHERE kind = ?", (REVISION_TREE,)
    ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(source)


def test_layout_and_display_fields_survive_repository_roundtrip(conn):
    raw = tree_dict()
    raw["root"]["nodes"][0]["title"] = "请求入口"
    raw["root"]["nodes"][0]["position"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][-1]["capability"] = "展示说明"
    repo = RoutingTreeRepo(conn)
    created = repo.create(tree_from_dict(raw))
    original_revision = created.tree_revision
    loaded = repo.get("main")
    assert (loaded.root.nodes[0].position.x, loaded.root.nodes[0].position.y) == (321.0, 654.0)
    assert loaded.root.nodes[0].title == "请求入口"
    assert loaded.root.nodes[-1].capability == "展示说明"
    assert loaded.tree_revision == original_revision


def test_layout_only_update_keeps_revision_and_audits_complete_document(repo, conn):
    raw = tree_dict()
    created = repo.create(tree_from_dict(raw))
    raw["root"]["nodes"][0]["position"] = {"x": 40, "y": 80}
    raw["root"]["nodes"][0]["title"] = "入口改名"
    moved = repo.update(tree_from_dict(raw))

    assert moved.tree_revision == created.tree_revision
    assert tree_behavior_dict(moved) == tree_behavior_dict(created)
    assert moved.root.nodes[0].position.x == 40.0
    assert moved.root.nodes[0].title == "入口改名"
    assert snapshot_of(moved).snapshot_revision == snapshot_of(created).snapshot_revision

    revisions = [json.loads(row[0]) for row in conn.execute(
        "SELECT content FROM config_revisions WHERE kind = ? ORDER BY id", (REVISION_TREE,)
    )]
    assert len(revisions) == 2
    assert revisions[-1]["root"]["nodes"][0]["title"] == "入口改名"
    assert revisions[-1]["root"]["nodes"][0]["position"] == {"x": 40, "y": 80}


def test_update_changes_content_but_never_enabled(repo):
    tree = repo.create(tree_from_dict(tree_dict()))
    repo.enable(tree.id)
    raw = tree_dict(sticky={"enabled": False, "ttl_seconds": 60})
    raw["root"]["nodes"][1]["instructions"] = "重新选择能力"
    updated = repo.update(tree_from_dict(raw))
    assert updated.enabled is True
    assert updated.root.nodes[1].instructions == "重新选择能力"
    assert updated.sticky.enabled is False
    assert updated.tree_revision != tree.tree_revision


def test_question_option_order_changes_revision(repo):
    raw = tree_dict()
    first = repo.create(tree_from_dict(raw))
    raw["root"]["nodes"][1]["options"].reverse()
    second = repo.update(tree_from_dict(raw))
    assert second.tree_revision != first.tree_revision
    assert [option.key for option in second.root.nodes[1].options] == ["strong", "easy"]


def test_enable_disable_and_copy_preserve_single_active_invariant(repo):
    first = repo.create(tree_from_dict(tree_dict("a")))
    second = repo.create(tree_from_dict(tree_dict("b")))
    repo.enable(first.id)
    repo.enable(second.id)
    assert [tree.id for tree in repo.list() if tree.enabled] == ["b"]
    copied = repo.copy("b", "copy", "副本")
    assert copied.enabled is False
    assert copied.tree_revision == second.tree_revision
    assert repo.disable("b") is True
    assert not any(tree.enabled for tree in repo.list())
    assert repo.disable("missing") is False


def test_concurrent_enable_leaves_exactly_one_active(repo, tmp_path):
    first = repo.create(tree_from_dict(tree_dict("a")))
    second = repo.create(tree_from_dict(tree_dict("b")))
    path = str(tmp_path / "v3.db")
    connections = [connect(path), connect(path)]
    barrier = threading.Barrier(2)
    errors = []

    def worker(connection, tree_id):
        try:
            barrier.wait(timeout=5)
            RoutingTreeRepo(connection).enable(tree_id)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(connection, tree_id))
        for connection, tree_id in zip(connections, (first.id, second.id))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    for connection in connections:
        connection.close()
    check = connect(path)
    assert check.execute("SELECT COUNT(*) FROM routing_trees WHERE enabled = 1").fetchone()[0] == 1
    check.close()
