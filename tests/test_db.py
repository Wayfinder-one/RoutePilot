"""SQLite schema: fresh v7 install, destructive v6→v7 upgrade, version guards.

v7 removes the SQLite decision log/snapshot tables and the JEV ``api_key``
column.  The v6 fixture builders below embed the retired DDL literally: they are
test-only history, and production code keeps no old DDL after the upgrade.
"""
import json
import sqlite3

import pytest

from router.store.db import (
    JEV_SERVICES_DDL,
    SCHEMA,
    SCHEMA_VERSION,
    _upgrade_v6_to_v7,
    connect,
    migrate,
)

#: v6 的三张决策日志表 + 索引（升级后必须全部消失；生产代码不再保留这些 DDL）。
V6_REQUEST_LOGS_DDL = """
    CREATE TABLE request_logs (
        id TEXT PRIMARY KEY,
        ts TEXT NOT NULL,
        decision TEXT NOT NULL,
        model TEXT,
        target_id TEXT,
        tree_id TEXT,
        tree_revision TEXT,
        snapshot_revision TEXT,
        policy_path TEXT,
        selection_reason TEXT,
        sticky_hit INTEGER,
        session_hash TEXT,
        jev_model TEXT,
        jev_choice TEXT,
        jev_top REAL,
        jev_margin REAL,
        jev_ms REAL,
        waves INTEGER,
        reasoning TEXT,
        routing_ms REAL,
        upstream_model TEXT,
        upstream_status INTEGER,
        upstream_ms REAL,
        ttft_ms REAL,
        total_ms REAL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        outcome_state TEXT,
        error_class TEXT
    )
    """
V6_REQUEST_LOGS_INDEX_DDL = "CREATE INDEX idx_request_logs_ts ON request_logs(ts)"
V6_GRAPH_SNAPSHOTS_DDL = """
    CREATE TABLE graph_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        tree_id TEXT,
        snapshot_revision TEXT,
        graph TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
V6_REQUEST_SNAPSHOTS_DDL = """
    CREATE TABLE request_snapshots (
        request_id TEXT PRIMARY KEY,
        graph_snapshot_id TEXT NOT NULL,
        trace TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
V6_REQUEST_SNAPSHOTS_INDEX_DDL = (
    "CREATE INDEX idx_request_snapshots_graph ON request_snapshots(graph_snapshot_id)"
)
V6_JEV_SERVICES_DDL = """
    CREATE TABLE jev_services (
        id TEXT PRIMARY KEY,
        base_url TEXT NOT NULL,
        model TEXT NOT NULL,
        key_env TEXT NOT NULL DEFAULT '',
        api_key TEXT NOT NULL DEFAULT '',
        timeout_seconds REAL NOT NULL,
        instructions TEXT NOT NULL,
        state_template TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        context_tokens INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """

DECISION_LOG_TABLES = frozenset({"request_logs", "graph_snapshots", "request_snapshots"})
DECISION_LOG_INDEXES = frozenset({"idx_request_logs_ts", "idx_request_snapshots_graph"})
V7_JEV_COLUMNS = frozenset({
    "id", "base_url", "model", "key_env", "timeout_seconds", "instructions",
    "state_template", "input_tokens", "context_tokens", "updated_at",
})


def create_v6_decision_log_tables(conn):
    """创建 v6 的账本/快照表与索引（生产代码已删除这些 DDL）。"""
    conn.execute(V6_REQUEST_LOGS_DDL)
    conn.execute(V6_REQUEST_LOGS_INDEX_DDL)
    conn.execute(V6_GRAPH_SNAPSHOTS_DDL)
    conn.execute(V6_REQUEST_SNAPSHOTS_DDL)
    conn.execute(V6_REQUEST_SNAPSHOTS_INDEX_DDL)


def create_v6_jev_service(conn, *, service_id="j1", api_key="old-secret",
                          base_url="http://jev", model="d1"):
    """按 v6 显式列名插入一行：密钥只存在于旧数据库列。"""
    conn.execute(
        "INSERT INTO jev_services (id, base_url, model, key_env, api_key, "
        "timeout_seconds, instructions, state_template, input_tokens, "
        "context_tokens, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (service_id, base_url, model, "ROUTER_JEV_API_KEY", api_key, 7.5,
         "请选择最匹配的模型。", "【请求】\n{request}", 1024, 4096,
         "2026-11-01T10:00:00+08:00"),
    )


def build_v6_database(path):
    """真实 v6 库：当前 schema 去掉 v7 增量，保留三张日志表与带 api_key 的 JEV 表。"""
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for statement in SCHEMA:
        if statement == JEV_SERVICES_DDL:
            continue
        conn.execute(statement)
    conn.execute(V6_JEV_SERVICES_DDL)
    create_v6_decision_log_tables(conn)
    conn.execute("INSERT INTO schema_migrations VALUES (6)")
    conn.commit()
    return conn


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def table_names(conn):
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not row[0].startswith("sqlite_")
    }


def index_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def schema_version(conn):
    return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]


# --------------------------------------------------------------------------- #
# fresh install
# --------------------------------------------------------------------------- #
def test_v7_schema_is_current_only_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "fresh.db"))
    migrate(conn)
    migrate(conn)
    tables = table_names(conn)
    assert tables == {
        "models", "providers", "time_periods", "jev_services", "changes",
        "settings", "users", "user_sessions", "breaker", "schema_migrations",
    }
    assert not DECISION_LOG_TABLES & tables
    assert not DECISION_LOG_INDEXES & index_names(conn)
    assert "routing_trees" not in tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert schema_version(conn) == SCHEMA_VERSION
    assert SCHEMA_VERSION == 7
    conn.close()


def test_migration_removes_decision_log_tables_and_jev_keys(tmp_path):
    conn = connect(str(tmp_path / "old.db"))
    migrate(conn)
    assert not table_names(conn) & DECISION_LOG_TABLES
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jev_services)")}
    assert "api_key" not in columns
    assert {"id", "base_url", "model", "key_env", "instructions"} <= columns
    assert columns == V7_JEV_COLUMNS
    conn.close()


def test_fresh_install_records_v7_and_keeps_provider_secrets(tmp_path):
    """Provider Key 仍由外围 SQLite 管理；只有 JEV Key 被移出数据库。"""
    conn = connect(str(tmp_path / "fresh.db"))
    migrate(conn)
    assert schema_version(conn) == 7
    assert "api_key" in column_names(conn, "providers")
    conn.close()


# --------------------------------------------------------------------------- #
# v6 → v7
# --------------------------------------------------------------------------- #
def test_v6_upgrade_preserves_jev_config_but_drops_secret_and_logs(tmp_path):
    conn = build_v6_database(str(tmp_path / "v6.db"))
    create_v6_jev_service(conn, service_id="j1", api_key="old-secret")
    conn.execute(
        "INSERT INTO request_logs (id, ts, decision, model) "
        "VALUES ('legacy-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b')")
    conn.execute(
        "INSERT INTO graph_snapshots (snapshot_id, tree_id, snapshot_revision, graph, created_at) "
        "VALUES ('g1', 'main', 'r1', '{}', '2026-11-01T10:00:00+08:00')")
    conn.execute(
        "INSERT INTO request_snapshots (request_id, graph_snapshot_id, trace, created_at) "
        "VALUES ('legacy-row', 'g1', '[]', '2026-11-01T10:00:00+08:00')")
    conn.commit()
    assert DECISION_LOG_TABLES <= table_names(conn)

    migrate(conn)

    assert not DECISION_LOG_TABLES & table_names(conn)
    assert not DECISION_LOG_INDEXES & index_names(conn)
    assert schema_version(conn) == 7
    row = conn.execute("SELECT * FROM jev_services WHERE id = 'j1'").fetchone()
    assert row["base_url"] == "http://jev"
    assert row["model"] == "d1"
    assert row["key_env"] == "ROUTER_JEV_API_KEY"
    assert row["timeout_seconds"] == 7.5
    assert (row["input_tokens"], row["context_tokens"]) == (1024, 4096)
    assert "api_key" not in row.keys()
    assert column_names(conn, "jev_services") == V7_JEV_COLUMNS
    assert "old-secret" not in json.dumps(dict(row))
    # 重复迁移幂等：版本不涨、非密钥行不动。
    migrate(conn)
    assert schema_version(conn) == 7
    assert conn.execute("SELECT COUNT(*) FROM jev_services").fetchone()[0] == 1
    conn.close()


def test_v6_upgrade_is_one_transaction(tmp_path, monkeypatch):
    """升级失败必须整体回滚：旧表和旧密钥行原样保留，版本仍是 6。"""
    import router.store.db as db_module

    conn = build_v6_database(str(tmp_path / "v6.db"))
    create_v6_jev_service(conn, service_id="j1", api_key="old-secret")
    conn.commit()
    before_tables = table_names(conn)
    before_columns = column_names(conn, "jev_services")
    before_rows = [dict(row) for row in conn.execute("SELECT * FROM jev_services")]

    def explode(connection):
        connection.execute("DROP TABLE request_logs")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(db_module, "_upgrade_v6_to_v7", explode)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate(conn)

    assert table_names(conn) == before_tables
    assert column_names(conn, "jev_services") == before_columns
    assert [dict(row) for row in conn.execute("SELECT * FROM jev_services")] == before_rows
    assert schema_version(conn) == 6
    conn.close()


def test_v6_without_api_key_column_still_drops_legacy_logs(tmp_path):
    """半升级/手工 v6 库：api_key 已缺失时只做删表，不重建 JEV 表。"""
    conn = build_v6_database(str(tmp_path / "v6.db"))
    conn.execute("ALTER TABLE jev_services DROP COLUMN api_key")
    conn.execute(
        "INSERT INTO jev_services (id, base_url, model, key_env, timeout_seconds, "
        "instructions, state_template, input_tokens, context_tokens, updated_at) "
        "VALUES ('j1', 'http://jev', 'd1', 'ROUTER_JEV_API_KEY', 7.5, 'i', "
        "'{request}', 1024, 4096, '2026-11-01T10:00:00+08:00')")
    conn.commit()

    migrate(conn)

    assert not DECISION_LOG_TABLES & table_names(conn)
    assert schema_version(conn) == 7
    assert conn.execute("SELECT model FROM jev_services WHERE id='j1'").fetchone()[0] == "d1"
    conn.close()


def test_v6_upgrade_tolerates_already_missing_log_tables(tmp_path):
    conn = build_v6_database(str(tmp_path / "v6.db"))
    for table in ("request_snapshots", "graph_snapshots", "request_logs"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("DROP INDEX IF EXISTS idx_request_logs_ts")
    conn.execute("DROP INDEX IF EXISTS idx_request_snapshots_graph")
    conn.commit()

    _upgrade_v6_to_v7(conn)
    assert schema_version(conn) == 7
    assert not DECISION_LOG_TABLES & table_names(conn)
    conn.close()


# --------------------------------------------------------------------------- #
# version guards
# --------------------------------------------------------------------------- #
def test_v6_database_without_jev_services_is_rejected(tmp_path):
    path = str(tmp_path / "v6.db")
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (6)")
    conn.executescript("""
        CREATE TABLE providers (name TEXT PRIMARY KEY, base_url TEXT NOT NULL,
            key_env TEXT NOT NULL DEFAULT '', reasoning_format TEXT NOT NULL DEFAULT 'openai',
            api_key TEXT NOT NULL DEFAULT '');
        CREATE TABLE models (id TEXT PRIMARY KEY, upstream TEXT NOT NULL,
            provider TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '{}');
    """)
    conn.commit()
    with pytest.raises(RuntimeError, match="new database"):
        migrate(conn)
    assert schema_version(conn) == 6
    assert "jev_services" not in table_names(conn)
    conn.close()


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_older_schema_versions_are_rejected_not_migrated(tmp_path, version):
    """v6 之前的历史库不做隐式升级：旧决策日志不迁移，必须显式重置/导出。"""
    path = str(tmp_path / f"v{version}.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    conn.execute("CREATE TABLE jev_config (id INTEGER PRIMARY KEY, base_url TEXT)")
    conn.commit()
    conn.close()

    conn = connect(path)
    with pytest.raises(RuntimeError, match="new database"):
        migrate(conn)
    assert schema_version(conn) == version
    assert "jev_services" not in table_names(conn)
    conn.close()


def test_future_schema_version_is_rejected(tmp_path):
    path = str(tmp_path / "future.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (19)")
    conn.execute("CREATE TABLE keyword_groups (name TEXT PRIMARY KEY, words TEXT NOT NULL)")
    conn.commit()
    conn.close()
    conn = connect(path)
    with pytest.raises(RuntimeError, match="new database"):
        migrate(conn)
    assert "jev_services" not in table_names(conn)
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
