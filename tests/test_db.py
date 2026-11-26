"""SQLite schema: fresh v7 install, continuous v1→v7 upgrade chain, version guards.

v7 removes the SQLite decision log/snapshot tables and the JEV ``api_key``
column (design §9).  Databases written by older builds are upgraded in place
through the sequential v1→v2→…→v6 chain followed by the destructive v6→v7
cleanup, so an existing installation never has to be reset.

The v1–v6 builders below embed the retired DDL literally: they are test-only
history, and production code keeps no old DDL beyond the upgrade steps.
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

# v1 的 ``request_logs`` 还没有 v2 增加的 ``waves`` 列。
WAVES_LINE = "        waves INTEGER,\n"
V1_REQUEST_LOGS_DDL = V6_REQUEST_LOGS_DDL.replace(WAVES_LINE, "")

#: v1/v2 的图配置表（v7 外围不再读它，但历史库里有；升级链不得销毁已有数据）。
ROUTING_TREES_DDL = """
    CREATE TABLE routing_trees (
        id TEXT PRIMARY KEY, name TEXT, enabled INTEGER, root TEXT,
        tree_revision TEXT, updated_at TEXT
    )
    """
ROUTING_TREES_SINGLE_ENABLED_INDEX_DDL = (
    "CREATE UNIQUE INDEX idx_routing_trees_single_enabled "
    "ON routing_trees(enabled) WHERE enabled = 1"
)
ROUTING_TREES_UPDATED_INDEX_DDL = (
    "CREATE INDEX idx_routing_trees_updated ON routing_trees(updated_at, id)"
)

#: v3 的退役配置修订账本（v3→v4 删除；有未导出数据时拒绝升级）。
CONFIG_REVISIONS_DDL = (
    "CREATE TABLE config_revisions ("
    "id INTEGER PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL, "
    "content TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(kind, sha256))",
    "CREATE INDEX idx_config_revisions_kind ON config_revisions(kind, id)",
)

#: v5 的单例 JEV 配置表（v5→v6 把这一行搬进 ``jev_services``）。
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

DECISION_LOG_TABLES = frozenset({"request_logs", "graph_snapshots", "request_snapshots"})
DECISION_LOG_INDEXES = frozenset({"idx_request_logs_ts", "idx_request_snapshots_graph"})
V7_JEV_COLUMNS = frozenset({
    "id", "base_url", "model", "key_env", "timeout_seconds", "instructions",
    "state_template", "input_tokens", "context_tokens", "updated_at",
})


def _create_common_tables(conn):
    """各历史版本共有的基础表（不含 v6/v7 的 ``jev_services``）。"""
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for statement in SCHEMA:
        if statement == JEV_SERVICES_DDL:
            continue
        conn.execute(statement)


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


def insert_legacy_jev_config(conn, *, api_key="old-secret",
                             base_url="http://old-jev.test/", model="NeoHorse-Jev-4B"):
    """插入 v5 的单例 JEV 配置行（旧密钥只存在于旧数据库列）。"""
    conn.execute(
        "INSERT INTO jev_config (id, base_url, model, api_key, timeout_seconds, "
        "instructions, state_template, updated_at) VALUES "
        "(1, ?, ?, ?, 7.5, '请选择最匹配的模型。', '【请求】\n{request}', "
        "'2026-11-01T10:00:00+08:00')",
        (base_url, model, api_key),
    )


def graph_tree_root():
    """v1/v2 时代的一张普通图配置（``root.type == "graph"``）。"""
    return json.dumps({
        "id": "main", "name": "主图", "enabled": False, "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "root": {
            "id": "root", "type": "graph", "nodes": [{"id": "start", "type": "start"}],
            "edges": [], "error_fallback": None,
        },
    })


def legacy_tree_root():
    """v1/v2 时代淘汰的旧格式路由配置：``root`` 里没有 ``type == "graph"``。

    历史驱动在 v1→v2 与 v2→v3 之前会拒绝这种库（旧格式无法被新图运行时读取），
    要求运维显式导出后 ``DELETE FROM routing_trees``；升级链必须保留该保护。
    """
    return json.dumps({
        "id": "main", "name": "旧配置", "enabled": False,
        "root": {"config": {"model": "qwen3-8b", "upstream": "http://old-upstream"}},
    })


def insert_routing_tree(conn, *, root=None, tree_id="main"):
    """按 v1/v2 的显式列名插入一行图配置（默认是合法 ``graph`` 文档）。"""
    conn.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES (?, ?, 0, ?, ?, '2026-11-01T10:00:00+08:00')",
        (tree_id, "主图", graph_tree_root() if root is None else root, "r" * 64),
    )


def build_v1_database(path, *, routing_root=None):
    """v1 库：基础表 + routing_trees + 没有 ``waves`` 列的 ``request_logs``。

    ``routing_root`` 给定时按显式列插入一行图配置（用于构造旧格式遗留库）。
    """
    conn = connect(path)
    _create_common_tables(conn)
    conn.execute(ROUTING_TREES_DDL)
    conn.execute(ROUTING_TREES_SINGLE_ENABLED_INDEX_DDL)
    conn.execute(ROUTING_TREES_UPDATED_INDEX_DDL)
    conn.execute(V1_REQUEST_LOGS_DDL)
    conn.execute(V6_REQUEST_LOGS_INDEX_DDL)
    conn.execute("INSERT INTO schema_migrations VALUES (1)")
    if routing_root is not None:
        insert_routing_tree(conn, root=routing_root)
    conn.commit()
    return conn


def build_v2_database(path, *, routing_root=None):
    conn = build_v1_database(path, routing_root=routing_root)
    conn.execute("ALTER TABLE request_logs ADD COLUMN waves INTEGER")
    conn.execute("UPDATE schema_migrations SET version = 2")
    conn.commit()
    return conn


def build_v3_database(path):
    """v3 库：基础表 + ``request_logs`` + 退役的 ``config_revisions`` 账本。"""
    conn = connect(path)
    _create_common_tables(conn)
    conn.execute(V6_REQUEST_LOGS_DDL)
    conn.execute(V6_REQUEST_LOGS_INDEX_DDL)
    for statement in CONFIG_REVISIONS_DDL:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations VALUES (3)")
    conn.commit()
    return conn


def build_v4_database(path):
    """v4 库：基础表 + ``request_logs`` + 旧单例 ``jev_config``（无快照表）。"""
    conn = connect(path)
    _create_common_tables(conn)
    conn.execute(V6_REQUEST_LOGS_DDL)
    conn.execute(V6_REQUEST_LOGS_INDEX_DDL)
    conn.execute(LEGACY_JEV_CONFIG_DDL)
    conn.execute("INSERT INTO schema_migrations VALUES (4)")
    conn.commit()
    return conn


def build_v5_database(path):
    """v5 库：v4 + 请求图快照表；``jev_services`` 尚未出现。"""
    conn = build_v4_database(path)
    conn.execute(V6_GRAPH_SNAPSHOTS_DDL)
    conn.execute(V6_REQUEST_SNAPSHOTS_DDL)
    conn.execute(V6_REQUEST_SNAPSHOTS_INDEX_DDL)
    conn.execute("UPDATE schema_migrations SET version = 5")
    conn.commit()
    return conn


def build_v6_database(path):
    """真实 v6 库：基础表 + 带 ``api_key`` 的 ``jev_services`` + 三张日志表。"""
    conn = connect(path)
    _create_common_tables(conn)
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


def schema_versions(conn):
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def all_dump(conn):
    return "\n".join(conn.iterdump())


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
# continuous upgrade chain v1 → … → v6 → v7
# --------------------------------------------------------------------------- #
def test_v1_database_upgrades_through_full_chain_to_v7(tmp_path):
    """v1 老库一次 ``migrate()`` 走完 v1→…→v6→v7，且每一步都记录自己的版本行。"""
    path = str(tmp_path / "v1.db")
    root = graph_tree_root()
    old = build_v1_database(path)
    assert "waves" not in column_names(old, "request_logs")
    old.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES ('main', '主图', 0, ?, ?, '2026-11-01T10:00:00+08:00')",
        (root, "r" * 64),
    )
    old.execute(
        "INSERT INTO request_logs (id, ts, decision, model, outcome_state) "
        "VALUES ('old-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b', 'completed')")
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)

    # v1→v2 补 waves、v2→v3 记账、v3→v4 删 config_revisions、v4→v5 建快照表、
    # v5→v6 建 JEV 目录、v6→v7 清理：每一步都留下自己的 schema_migrations 行。
    assert schema_versions(conn) == [1, 2, 3, 4, 5, 6, 7]
    assert schema_version(conn) == SCHEMA_VERSION == 7
    tables = table_names(conn)
    assert not DECISION_LOG_TABLES & tables
    assert not DECISION_LOG_INDEXES & index_names(conn)
    assert "jev_services" in tables
    assert column_names(conn, "jev_services") == V7_JEV_COLUMNS
    # v1 没有 jev_config，因此 v5→v6 建出的是空目录，v7 只做去密钥重建。
    assert conn.execute("SELECT COUNT(*) FROM jev_services").fetchone()[0] == 0
    # 历史图配置表不属于 v7 清理范围：链上不得销毁已有数据。
    assert conn.execute(
        "SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0] == root
    # 重复迁移幂等：版本不涨、非密钥行不动。
    migrate(conn)
    assert schema_version(conn) == 7
    assert schema_versions(conn) == [1, 2, 3, 4, 5, 6, 7]
    conn.close()


def test_v2_legitimate_graph_upgrades_through_full_chain_to_v7(tmp_path):
    """v2 老库 + 合法 ``root.type == "graph"`` 图配置：一路升到 v7。"""
    path = str(tmp_path / "v2.db")
    root = graph_tree_root()
    old = build_v2_database(path, routing_root=root)
    assert "waves" in column_names(old, "request_logs")
    old.close()

    conn = connect(path)
    migrate(conn)

    assert schema_versions(conn) == [2, 3, 4, 5, 6, 7]
    assert schema_version(conn) == SCHEMA_VERSION == 7
    assert conn.execute(
        "SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0] == root
    conn.close()


def _assert_legacy_guard_rejected(conn, *, expected_version):
    """旧格式图配置必须挡住升级：报错点名 reset、版本不前进、旧 schema 还在。"""
    versions_before = schema_versions(conn)
    tables_before = table_names(conn)
    with pytest.raises(RuntimeError, match="legacy routing trees") as excinfo:
        migrate(conn)
    assert schema_version(conn) == expected_version
    assert schema_versions(conn) == versions_before
    # 拒绝发生在任何升级步骤之前：旧 schema 原样保留，没有 v7 标记。
    assert table_names(conn) == tables_before
    assert "jev_services" not in tables_before
    assert "graph_snapshots" not in tables_before
    return str(excinfo.value)


def test_v1_legacy_graph_document_blocks_upgrade_before_v2(tmp_path):
    """v1 库里的旧格式 routing config：历史驱动在 v1→v2 前拒绝（先导出/重置）。"""
    path = str(tmp_path / "v1-legacy.db")
    root = legacy_tree_root()
    old = build_v1_database(path, routing_root=root)
    old.close()

    conn = connect(path)
    message = _assert_legacy_guard_rejected(conn, expected_version=1)
    assert "DELETE FROM routing_trees" in message
    assert "main" in message
    # v1 schema 仍完整：没有 waves 列，routing_trees 的旧文档未被改写。
    assert "waves" not in column_names(conn, "request_logs")
    assert conn.execute(
        "SELECT root FROM routing_trees WHERE id = 'main'").fetchone()[0] == root
    conn.close()


def test_v2_legacy_graph_document_blocks_upgrade_before_v3(tmp_path):
    """v2 库里的旧格式 routing config：历史驱动在 v2→v3 前同样拒绝。"""
    path = str(tmp_path / "v2-legacy.db")
    root = legacy_tree_root()
    old = build_v2_database(path, routing_root=root)
    old.close()

    conn = connect(path)
    message = _assert_legacy_guard_rejected(conn, expected_version=2)
    assert "DELETE FROM routing_trees" in message
    # v2 schema（含 waves）仍在，且没有被记上 v3。
    assert "waves" in column_names(conn, "request_logs")
    assert 3 not in schema_versions(conn)
    conn.close()


def test_legacy_tree_ids_reports_only_non_graph_documents(tmp_path):
    """``legacy_tree_ids`` 按历史实现识别：缺失/无 ``root.type == "graph"`` 都是遗留。"""
    from router.store.db import legacy_tree_ids

    conn = build_v1_database(str(tmp_path / "trees.db"))
    insert_routing_tree(conn, tree_id="graph", root=graph_tree_root())
    insert_routing_tree(conn, tree_id="legacy", root=legacy_tree_root())
    insert_routing_tree(conn, tree_id="bad-json", root="{not-json")
    conn.execute(
        "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
        "VALUES ('null-root', '空', 0, NULL, NULL, NULL)")
    conn.commit()

    assert legacy_tree_ids(conn) == ["bad-json", "legacy", "null-root"]

    # 历史库没有 routing_trees 表时返回空列表（不因缺表崩溃）。
    plain = connect(str(tmp_path / "plain.db"))
    plain.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    plain.commit()
    assert legacy_tree_ids(plain) == []
    conn.close()
    plain.close()


def test_v3_database_upgrades_to_v7_and_drops_config_revisions(tmp_path):
    path = str(tmp_path / "v3.db")
    old = build_v3_database(path)
    assert old.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 0
    old.close()

    conn = connect(path)
    migrate(conn)

    assert schema_versions(conn) == [3, 4, 5, 6, 7]
    assert schema_version(conn) == 7
    objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "config_revisions" not in objects
    assert "idx_config_revisions_kind" not in objects
    assert not DECISION_LOG_TABLES & table_names(conn)
    assert column_names(conn, "jev_services") == V7_JEV_COLUMNS
    migrate(conn)
    assert schema_version(conn) == 7
    conn.close()


def test_v3_upgrade_refuses_nonempty_config_revisions(tmp_path):
    """v3→v4 的「先导出再删」保护仍然生效：拒绝时不越级、不丢数据。"""
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
    # 事务回滚：v3 之后没有任何步骤落库（快照表都没有建）。
    assert "graph_snapshots" not in table_names(conn)
    assert "jev_services" not in table_names(conn)
    conn.close()


def test_v4_to_v5_step_adds_snapshots_and_keeps_request_logs(tmp_path):
    """v4→v5 的效果在 v7 已不可见（快照表随后被删除），这里直接验证该步本身。"""
    import router.store.db as db_module

    conn = build_v4_database(str(tmp_path / "v4.db"))
    conn.execute(
        "INSERT INTO request_logs (id, ts, decision, model) "
        "VALUES ('old-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b')")
    conn.commit()

    db_module._upgrade_v4_to_v5(conn)
    conn.commit()

    assert {"graph_snapshots", "request_snapshots"} <= table_names(conn)
    assert "idx_request_snapshots_graph" in index_names(conn)
    assert schema_version(conn) == 5
    assert conn.execute(
        "SELECT model FROM request_logs WHERE id = 'old-row'").fetchone()[0] == "qwen3-8b"
    conn.close()


def test_v5_database_upgrades_to_v7_and_strips_jev_secret(tmp_path):
    """v5 单例 jev_config → v6 ``default`` 服务 → v7 去掉密钥但保留非密钥配置。"""
    path = str(tmp_path / "v5.db")
    old = build_v5_database(path)
    insert_legacy_jev_config(old)
    old.execute("INSERT INTO settings VALUES ('jev_input_tokens', '2048')")
    old.execute("INSERT INTO settings VALUES ('jev_context_tokens', '8192')")
    old.execute(
        "INSERT INTO request_logs (id, ts, decision, model) "
        "VALUES ('legacy-row', '2026-11-01T10:00:00+08:00', 'route', 'qwen3-8b')")
    old.execute(
        "INSERT INTO graph_snapshots (snapshot_id, tree_id, snapshot_revision, graph, created_at) "
        "VALUES ('g1', 'main', 'r1', '{}', '2026-11-01T10:00:00+08:00')")
    old.execute(
        "INSERT INTO request_snapshots (request_id, graph_snapshot_id, trace, created_at) "
        "VALUES ('legacy-row', 'g1', '[]', '2026-11-01T10:00:00+08:00')")
    old.commit()
    old.close()

    conn = connect(path)
    migrate(conn)

    assert schema_versions(conn) == [5, 6, 7]
    assert schema_version(conn) == 7
    assert not DECISION_LOG_TABLES & table_names(conn)
    assert not DECISION_LOG_INDEXES & index_names(conn)
    assert "jev_config" not in table_names(conn)
    row = dict(conn.execute("SELECT * FROM jev_services").fetchone())
    assert row["id"] == "default"
    # 端点按 v5→v6 的规范化规则落库（strip + 去掉末尾 `/`）。
    assert row["base_url"] == "http://old-jev.test"
    assert row["model"] == "NeoHorse-Jev-4B"
    assert row["key_env"] == "ROUTER_JEV_API_KEY"
    assert row["timeout_seconds"] == 7.5
    assert row["instructions"] == "请选择最匹配的模型。"
    assert row["state_template"] == "【请求】\n{request}"
    assert (row["input_tokens"], row["context_tokens"]) == (2048, 8192)
    assert row["updated_at"] == "2026-11-01T10:00:00+08:00"
    assert column_names(conn, "jev_services") == V7_JEV_COLUMNS
    assert "old-secret" not in json.dumps(row, ensure_ascii=False)
    assert "old-secret" not in all_dump(conn)
    migrate(conn)
    assert schema_version(conn) == 7
    assert conn.execute("SELECT COUNT(*) FROM jev_services").fetchone()[0] == 1
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

    assert schema_versions(conn) == [6, 7]
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
    assert "old-secret" not in all_dump(conn)
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


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_versioned_database_without_expected_tables_is_rejected(tmp_path, version):
    """残缺的历史库不猜内容：缺该版本应有的表就拒绝，不落任何升级步骤。"""
    path = str(tmp_path / f"junk-v{version}.db")
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
    conn.execute("INSERT INTO schema_migrations VALUES (8)")
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
