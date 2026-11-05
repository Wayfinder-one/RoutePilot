import json
import sqlite3

import pytest

from router.store.db import SCHEMA, SCHEMA_VERSION, connect, migrate
from router.store.repositories import ConfigRevisionRepo

WAVES_LINE = "        waves INTEGER,\n"


def build_v1_database(path):
    """Create a v1 database: current schema without the v2 request_logs.waves column."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for statement in SCHEMA:
        conn.execute(statement.replace(WAVES_LINE, ""))
    conn.execute(
        "CREATE TABLE routing_trees (id TEXT PRIMARY KEY, name TEXT, enabled INTEGER, "
        "root TEXT, tree_revision TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_routing_trees_single_enabled "
        "ON routing_trees(enabled) WHERE enabled = 1"
    )
    conn.execute("CREATE INDEX idx_routing_trees_updated ON routing_trees(updated_at, id)")
    conn.execute("INSERT INTO schema_migrations VALUES (1)")
    conn.commit()
    return conn


def build_v2_database(path):
    conn = build_v1_database(path)
    conn.execute("ALTER TABLE request_logs ADD COLUMN waves INTEGER")
    conn.execute("UPDATE schema_migrations SET version = 2")
    conn.commit()
    return conn


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def schema_version(conn):
    return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]


def test_v3_schema_is_current_only_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    migrate(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "models", "providers", "time_periods", "jev_config",
        "request_logs", "config_revisions", "changes", "settings", "users",
        "user_sessions", "breaker", "schema_migrations",
    } <= tables
    assert "routing_trees" not in tables
    assert not {"keyword_groups", "sessions", "stations", "nodes", "requests", "request_ledger", "evals"} & tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    assert SCHEMA_VERSION == 3
    conn.close()


def test_empty_database_is_created_directly_at_v3_with_waves(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    assert schema_version(conn) == 3
    assert "waves" in column_names(conn, "request_logs")
    assert "routing_trees" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()


def test_v1_upgrade_adds_waves_and_keeps_old_log_rows(tmp_path):
    path = str(tmp_path / "v1.db")
    old = build_v1_database(path)
    assert "waves" not in column_names(old, "request_logs")
    old.execute(
        "INSERT INTO request_logs (id, ts, decision, model, outcome_state) "
        "VALUES ('old-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b', 'completed')"
    )
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)
    assert schema_version(conn) == 3
    assert "waves" in column_names(conn, "request_logs")
    row = dict(conn.execute("SELECT * FROM request_logs").fetchone())
    assert row["id"] == "old-row"
    assert row["model"] == "qwen3-8b"
    assert row["outcome_state"] == "completed"
    assert row["waves"] is None
    migrate(conn)
    assert schema_version(conn) == 3
    conn.close()


def test_v1_upgrade_keeps_existing_graph_trees(tmp_path):
    path = str(tmp_path / "v1.db")
    old = build_v1_database(path)
    root = json.dumps({
        "id": "main", "name": "主图", "enabled": False, "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "root": {
            "id": "root", "type": "graph", "nodes": [{"id": "start", "type": "start"}],
            "edges": [], "error_fallback": None,
        },
    })
    old.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES ('main', '主图', 0, ?, ?, '2026-11-01T10:00:00+08:00')",
        (root, "r" * 64),
    )
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)
    assert schema_version(conn) == 3
    assert "waves" in column_names(conn, "request_logs")
    assert conn.execute("SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0] == root
    conn.close()


def test_v2_upgrade_to_v3_keeps_graph_rows_logs_and_waves(tmp_path):
    path = str(tmp_path / "v2.db")
    old = build_v2_database(path)
    old.execute(
        "INSERT INTO request_logs (id, ts, decision, model, outcome_state, waves) "
        "VALUES ('v2-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b', 'completed', 2)"
    )
    root = json.dumps({
        "id": "main", "name": "主图", "enabled": False, "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "root": {
            "id": "root", "type": "graph", "nodes": [{"id": "start", "type": "start"}],
            "edges": [], "error_fallback": None,
        },
    })
    old.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES ('main', '主图', 0, ?, ?, '2026-11-01T10:00:00+08:00')",
        (root, "r" * 64),
    )
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)
    assert schema_version(conn) == 3
    assert conn.execute("SELECT waves FROM request_logs WHERE id = 'v2-row'").fetchone()[0] == 2
    assert conn.execute("SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0] == root
    conn.close()


def test_v1_with_legacy_tree_refuses_upgrade_and_stays_at_v1(tmp_path):
    path = str(tmp_path / "v1.db")
    old = build_v1_database(path)
    legacy_root = json.dumps({
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "root": {"id": "legacy-root", "type": "jev", "instructions": "旧决策", "options": []},
    })
    old.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES ('legacy-tree', '旧决策组', 0, ?, ?, '2026-11-01T10:00:00+08:00')",
        (legacy_root, "l" * 64),
    )
    old.commit()
    old.close()

    conn = connect(path)
    with pytest.raises(RuntimeError) as error:
        migrate(conn)
    message = str(error.value)
    assert "legacy-tree" in message
    assert "explicit reset" in message
    assert "waves" not in column_names(conn, "request_logs")
    assert schema_version(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM routing_trees").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("root", ['{"id": "broken"', json.dumps({"id": "nev", "type": "jev"})])
def test_v1_legacy_detection_rejects_unparseable_or_non_graph_roots(tmp_path, root):
    path = str(tmp_path / "v1.db")
    old = build_v1_database(path)
    old.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES ('legacy-tree', '旧决策组', 0, ?, ?, '2026-11-01T10:00:00+08:00')",
        (root, "l" * 64),
    )
    old.commit()
    old.close()

    conn = connect(path)
    with pytest.raises(RuntimeError, match="explicit reset"):
        migrate(conn)
    assert "waves" not in column_names(conn, "request_logs")
    assert schema_version(conn) == 1
    conn.close()


def test_request_logs_contains_no_request_text_or_raw_session(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    columns = column_names(conn, "request_logs")
    assert {"id", "tree_revision", "snapshot_revision", "policy_path", "sticky_hit", "session_hash", "jev_choice", "jev_top", "jev_margin", "selection_reason", "outcome_state"} <= columns
    assert "waves" in columns
    assert not {"answers", "text", "signals", "trace", "session_id", "api_key", "jev_probabilities", "fallback_reason"} & columns
    conn.close()


def test_old_versioned_database_is_rejected_not_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (19)")
    conn.execute("CREATE TABLE keyword_groups (name TEXT PRIMARY KEY, words TEXT NOT NULL)")
    conn.commit()
    conn.close()
    conn = connect(path)
    with pytest.raises(RuntimeError, match="new database"):
        migrate(conn)
    assert "jev_config" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()


def test_unversioned_nonempty_database_is_rejected(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE models (id TEXT PRIMARY KEY)")
    conn.close()
    conn = connect(path)
    with pytest.raises(RuntimeError, match="new database"):
        migrate(conn)
    conn.close()


def test_config_revisions_are_append_only_and_deduped(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    repo = ConfigRevisionRepo(conn)
    first = repo.record("tree", '{"root":1}')
    assert repo.record("tree", '{"root":1}') == first
    second = repo.record("tree", '{"root":2}')
    assert second != first
    assert repo.get("tree", first)["content"] == '{"root":1}'
    assert repo.latest("tree")["sha256"] == second
    conn.close()
