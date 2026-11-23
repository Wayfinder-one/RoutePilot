import json
import sqlite3

import pytest

from router.store.db import (
    GRAPH_SNAPSHOTS_DDL,
    JEV_SERVICES_DDL,
    REQUEST_SNAPSHOTS_DDL,
    REQUEST_SNAPSHOTS_INDEX_DDL,
    SCHEMA,
    SCHEMA_VERSION,
    connect,
    migrate,
)
from router.store.jev_services import JevServiceRepo

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


CONFIG_REVISIONS_DDL = (
    "CREATE TABLE config_revisions ("
    "id INTEGER PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL, "
    "content TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(kind, sha256))",
    "CREATE INDEX idx_config_revisions_kind ON config_revisions(kind, id)",
)


def build_v3_database(path):
    """Create a v3 database: current schema plus the retired config_revisions ledger."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for statement in SCHEMA:
        conn.execute(statement)
    for statement in CONFIG_REVISIONS_DDL:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations VALUES (3)")
    conn.commit()
    return conn


#: v5 之前的单例 JEV 配置表（v6 迁移把这一行搬进 ``jev_services``）。
LEGACY_JEV_CONFIG_DDL = """
    CREATE TABLE jev_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        base_url TEXT NOT NULL,
        model TEXT NOT NULL,
        api_key TEXT NOT NULL DEFAULT '',
        timeout_seconds REAL NOT NULL,
        instructions TEXT NOT NULL,
        state_template TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """


def build_v4_database(path):
    """真实 v4 库：当前 schema 去掉 v5/v6 增量，保留旧单例 ``jev_config``。

    这样 v4→v6 的迁移链会真正执行「单例 jev_config + settings 窗口 → default
    服务」的数据迁移，而不是只补建表。
    """
    skip = {
        JEV_SERVICES_DDL,
        GRAPH_SNAPSHOTS_DDL,
        REQUEST_SNAPSHOTS_DDL,
        REQUEST_SNAPSHOTS_INDEX_DDL,
    }
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for statement in SCHEMA:
        if statement in skip:
            continue
        conn.execute(statement)
    conn.execute(LEGACY_JEV_CONFIG_DDL)
    conn.execute("INSERT INTO schema_migrations VALUES (4)")
    conn.commit()
    return conn


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def schema_version(conn):
    return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]


def test_v6_schema_is_current_only_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    migrate(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "models", "providers", "time_periods", "jev_services",
        "request_logs", "changes", "settings", "users",
        "user_sessions", "breaker", "schema_migrations",
        "graph_snapshots", "request_snapshots",
    } <= tables
    assert "routing_trees" not in tables
    assert not {"keyword_groups", "sessions", "stations", "nodes", "requests", "request_ledger", "evals"} & tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    assert SCHEMA_VERSION == 6
    conn.close()


def test_empty_database_is_created_directly_at_v6_with_waves(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    assert schema_version(conn) == 6
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
    assert schema_version(conn) == 6
    assert "waves" in column_names(conn, "request_logs")
    row = dict(conn.execute("SELECT * FROM request_logs").fetchone())
    assert row["id"] == "old-row"
    assert row["model"] == "qwen3-8b"
    assert row["outcome_state"] == "completed"
    assert row["waves"] is None
    migrate(conn)
    assert schema_version(conn) == 6
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
    assert schema_version(conn) == 6
    assert "waves" in column_names(conn, "request_logs")
    assert conn.execute("SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0] == root
    conn.close()


def test_v2_upgrade_to_v6_keeps_graph_rows_logs_and_waves(tmp_path):
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
    assert schema_version(conn) == 6
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
    assert "jev_services" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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


def test_v3_upgrade_drops_empty_config_revisions_and_records_v6(tmp_path):
    path = str(tmp_path / "v3.db")
    old = build_v3_database(path)
    assert old.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 0
    old.close()

    conn = connect(path)
    migrate(conn)
    assert schema_version(conn) == 6
    objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "config_revisions" not in objects
    assert "idx_config_revisions_kind" not in objects
    assert {"models", "providers", "request_logs", "changes", "users"} <= objects
    migrate(conn)
    assert schema_version(conn) == 6
    conn.close()


def test_v3_upgrade_refuses_nonempty_config_revisions(tmp_path):
    path = str(tmp_path / "v3.db")
    old = build_v3_database(path)
    old.execute(
        "INSERT INTO config_revisions (kind, sha256, content, created_at) "
        "VALUES ('tree', ?, '{\"root\":1}', '2026-11-01T10:00:00+08:00')",
        ("a" * 64,),
    )
    old.commit()
    old.close()

    conn = connect(path)
    with pytest.raises(RuntimeError, match="export"):
        migrate(conn)
    assert schema_version(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 1
    conn.close()


def test_v4_singleton_jev_config_migrates_into_default_service_and_keeps_key(tmp_path):
    """v4 单例 + settings 窗口 → v6 ``default`` 服务，且旧密钥在元数据保存后仍在。

    回归锁定：迁移若原样复制带尾斜杠的 legacy ``base_url``，``JevService`` 的
    规范化地址与之不相等，``stored_api_key()`` 判定端点变化，下一次只改元数据的
    ``save(api_key=None)`` 就会把密钥清空。
    """
    path = str(tmp_path / "v4.db")
    old = build_v4_database(path)
    old.execute(
        "INSERT INTO jev_config (id, base_url, model, api_key, timeout_seconds, "
        "instructions, state_template, updated_at) VALUES "
        "(1, 'http://old-jev.test/', 'NeoHorse-Jev-4B', 'legacy-secret', 7.5, "
        "'请选择最匹配的模型。', '【请求】\n{request}', "
        "'2026-11-01T10:00:00+08:00')")
    old.execute("INSERT INTO settings VALUES ('jev_input_tokens', '2048')")
    old.execute("INSERT INTO settings VALUES ('jev_context_tokens', '8192')")
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)

    assert schema_version(conn) == 6
    assert "jev_config" not in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    repo = JevServiceRepo(conn)
    services = repo.list()
    assert [service.id for service in services] == ["default"]
    service = services[0]
    # 端点按 JevService/JevSettings 规则规范化（strip + 去掉末尾 `/`）。
    assert service.base_url == "http://old-jev.test"
    assert service.model == "NeoHorse-Jev-4B"
    assert service.key_env == "ROUTER_JEV_API_KEY"
    assert (service.input_tokens, service.context_tokens) == (2048, 8192)
    assert repo.stored_api_key(service) == "legacy-secret"

    # 只改元数据（省略 api_key）不得误判端点变化而清空密钥。
    repo.save(service, api_key=None)
    assert repo.stored_api_key(repo.get("default")) == "legacy-secret"
    conn.close()


def test_v4_upgrade_adds_snapshots_and_keeps_request_logs(tmp_path):
    path = str(tmp_path / "v4.db")
    old = connect(path)
    migrate(old)
    old.execute(
        "INSERT INTO request_logs (id, ts, decision, model, outcome_state) "
        "VALUES ('old-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b', 'completed')"
    )
    for table in ("request_snapshots", "graph_snapshots"):
        old.execute(f"DROP TABLE IF EXISTS {table}")
    old.execute("DELETE FROM schema_migrations")
    old.execute("INSERT INTO schema_migrations VALUES (4)")
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
    assert {"graph_snapshots", "request_snapshots"} <= {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert conn.execute("SELECT model FROM request_logs WHERE id='old-row'").fetchone()[0] == "qwen3-8b"
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_request_snapshots_graph'"
    ).fetchone() is not None
    # 重复迁移幂等：版本不涨、旧行不动。
    migrate(conn)
    assert schema_version(conn) == 6
    assert conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0] == 1
    conn.close()
