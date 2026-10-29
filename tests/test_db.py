import sqlite3

import pytest

from router.store.db import SCHEMA_VERSION, connect, migrate
from router.store.repositories import ConfigRevisionRepo


def test_v3_schema_is_current_only_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    migrate(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "models", "providers", "time_periods", "routing_trees", "jev_config",
        "request_logs", "config_revisions", "changes", "settings", "users",
        "user_sessions", "breaker", "schema_migrations",
    } <= tables
    assert not {"keyword_groups", "sessions", "stations", "nodes", "requests", "request_ledger", "evals"} & tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_request_logs_contains_no_request_text_or_raw_session(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
    assert {"id", "tree_revision", "snapshot_revision", "policy_path", "sticky_hit", "session_hash", "jev_choice", "jev_top", "jev_margin", "selection_reason", "outcome_state"} <= columns
    assert not {"text", "signals", "trace", "session_id", "api_key"} & columns
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
