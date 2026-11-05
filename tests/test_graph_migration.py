import hashlib
import json
import sqlite3

import pytest

from router.jev.config import JevConfigRepo, JevSettings
from router.routing.model import canonical_json, tree_from_dict, tree_revision_of, tree_storage_dict
from router.store.db import connect, migrate
from router.store.graph_migration import export_sqlite_graphs
from router.store.repositories import ModelRepo, seed_default
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import tree_dict


def legacy_database(path, *, active=True, broken=False, mutate=None):
    conn = connect(str(path))
    migrate(conn)
    seed_default(conn)
    ModelRepo(conn).create("fast", "fast", "local", "{}")
    JevConfigRepo(conn).save(JevSettings("http://jev.test", "test-jev"))
    conn.execute(
        "CREATE TABLE routing_trees (id TEXT PRIMARY KEY, name TEXT, enabled INTEGER, "
        "root TEXT, tree_revision TEXT, updated_at TEXT)"
    )
    raw = tree_dict()
    raw["enabled"] = active
    if mutate is not None:
        mutate(raw)
    source = tree_from_dict(raw)
    root = canonical_json(tree_storage_dict(source))
    if broken:
        root = '{"id": "broken"'
    conn.execute(
        "INSERT INTO routing_trees VALUES (?, ?, ?, ?, ?, ?)",
        (source.id, source.name, 1 if active else 0, root, source.tree_revision, ""),
    )
    conn.commit()
    conn.close()
    return source


def test_export_preserves_every_tree_and_active_pointer(tmp_path):
    db = tmp_path / "old.db"
    source = legacy_database(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    result = export_sqlite_graphs(str(db), str(tmp_path / "exported"))

    assert result == {"tree_ids": ["main"], "active_id": "main"}
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    repo = RoutingTreeRepo(tmp_path / "exported")
    assert tree_storage_dict(repo.active()) == tree_storage_dict(source)
    assert repo.active().tree_revision == tree_revision_of(source)
    assert (tmp_path / "exported" / "current").read_text().strip() == "main"


def test_export_without_active_tree_has_null_pointer(tmp_path):
    db = tmp_path / "old.db"
    legacy_database(db, active=False)
    result = export_sqlite_graphs(str(db), str(tmp_path / "exported"))
    assert result == {"tree_ids": ["main"], "active_id": None}
    assert (tmp_path / "exported" / "current").read_bytes() == b""


def test_export_never_overwrites_or_deletes_source(tmp_path):
    source = tmp_path / "old.db"
    source.write_bytes(b"keep-source")
    destination = tmp_path / "exported"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        export_sqlite_graphs(str(source), str(destination))
    assert source.read_bytes() == b"keep-source"


def test_export_rejects_broken_source_without_leaving_output(tmp_path):
    db = tmp_path / "old.db"
    legacy_database(db, broken=True)
    destination = tmp_path / "exported"
    with pytest.raises((ValueError, json.JSONDecodeError)):
        export_sqlite_graphs(str(db), str(destination))
    assert not destination.exists()
    assert not list(tmp_path.glob(".exported.export-*"))


def remove_fallback(raw):
    del raw["root"]["error_fallback"]


def use_unknown_model(raw):
    raw["root"]["nodes"][-1]["models"] = [{"model": "missing-model", "reasoning": None}]


@pytest.mark.parametrize("active", [True, False])
def test_export_rejects_structurally_invalid_tree_without_output(tmp_path, active):
    db = tmp_path / "old.db"
    legacy_database(db, active=active, mutate=remove_fallback)
    destination = tmp_path / "exported"

    with pytest.raises(ValueError, match="fallback_missing"):
        export_sqlite_graphs(str(db), str(destination))

    assert not destination.exists()
    assert not list(tmp_path.glob(".exported.export-*"))


def test_export_rejects_enable_invalid_active_tree_without_output(tmp_path):
    db = tmp_path / "old.db"
    legacy_database(db, active=True, mutate=use_unknown_model)
    destination = tmp_path / "exported"

    with pytest.raises(ValueError, match="unknown_model"):
        export_sqlite_graphs(str(db), str(destination))

    assert not destination.exists()
    assert not list(tmp_path.glob(".exported.export-*"))


def test_export_allows_closed_draft_with_unknown_model(tmp_path):
    db = tmp_path / "old.db"
    legacy_database(db, active=False, mutate=use_unknown_model)

    result = export_sqlite_graphs(str(db), str(tmp_path / "exported"))

    assert result == {"tree_ids": ["main"], "active_id": None}
    repo = RoutingTreeRepo(tmp_path / "exported")
    assert repo.active_id() is None
    assert repo.get("main").root.nodes[-1].models[0].model == "missing-model"


def test_export_rejects_multiple_active_graphs(tmp_path):
    db = tmp_path / "old.db"
    legacy_database(db)
    conn = sqlite3.connect(db)
    source = tree_from_dict(tree_dict("second"))
    conn.execute(
        "INSERT INTO routing_trees VALUES (?, ?, ?, ?, ?, ?)",
        ("second", source.name, 1, canonical_json(tree_storage_dict(source)), source.tree_revision, ""),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="multiple active"):
        export_sqlite_graphs(str(db), str(tmp_path / "exported"))
    assert not (tmp_path / "exported").exists()
