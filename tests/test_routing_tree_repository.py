import hashlib
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from router.routing.model import tree_from_dict, tree_revision_of
from router.store.db import connect, migrate
from router.store.routing_trees import REVISION_TREE, RoutingTreeRepo
from tests.routing_fixtures import jev_dict, tree_dict


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "v3.db"))
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn):
    return RoutingTreeRepo(conn)


def test_create_defaults_closed_and_stores_canonical_behavior_document(repo, conn):
    source = tree_from_dict(tree_dict(enabled=True))
    tree = repo.create(source)
    assert tree.enabled is False
    assert tree.tree_revision == tree_revision_of(source)
    raw = conn.execute("SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0]
    assert hashlib.sha256(raw.encode()).hexdigest() == tree.tree_revision
    document = json.loads(raw)
    assert document["sticky"] == {"enabled": True, "ttl_seconds": 1800}
    assert document["root"]["type"] == "jev"
    assert conn.execute("SELECT COUNT(*) FROM config_revisions WHERE kind = ?", (REVISION_TREE,)).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(source)


def test_update_changes_content_but_never_enabled(repo):
    tree = repo.create(tree_from_dict(tree_dict()))
    repo.enable(tree.id)
    changed = tree_from_dict(tree_dict(root=jev_dict("renamed-node"), sticky={"enabled": False, "ttl_seconds": 60}))
    updated = repo.update(replace(changed, enabled=False))
    assert updated.enabled is True
    assert updated.root.id == "renamed-node"
    assert updated.sticky.enabled is False
    assert updated.tree_revision != tree.tree_revision


def test_candidate_order_changes_revision(repo):
    raw = tree_dict()
    first = repo.create(tree_from_dict(raw))
    raw["root"]["options"] = list(reversed(raw["root"]["options"]))
    second = repo.update(tree_from_dict(raw))
    assert second.tree_revision != first.tree_revision
    assert [option.key for option in second.root.options] == ["strong", "fast"]


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

    threads = [threading.Thread(target=worker, args=(connection, tree_id)) for connection, tree_id in zip(connections, (first.id, second.id))]
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
