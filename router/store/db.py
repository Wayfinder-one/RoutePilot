from __future__ import annotations

import json
import sqlite3

from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MAX_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
)

SCHEMA_VERSION = 6

# v5：请求图快照。图按内容 SHA-256 去重，trace 单独按请求 ID 保存；
# ``request_logs`` 仍然只有轻量账本列。
GRAPH_SNAPSHOTS_DDL = """
    CREATE TABLE graph_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        tree_id TEXT,
        snapshot_revision TEXT,
        graph TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
REQUEST_SNAPSHOTS_DDL = """
    CREATE TABLE request_snapshots (
        request_id TEXT PRIMARY KEY,
        graph_snapshot_id TEXT NOT NULL,
        trace TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
REQUEST_SNAPSHOTS_INDEX_DDL = """
    CREATE INDEX idx_request_snapshots_graph ON request_snapshots(graph_snapshot_id)
    """
SNAPSHOT_SCHEMA = (GRAPH_SNAPSHOTS_DDL, REQUEST_SNAPSHOTS_DDL, REQUEST_SNAPSHOTS_INDEX_DDL)

# v6：多 JEV 服务目录。窗口属于服务本身；``api_key`` 只存于数据库，不进入画布。
JEV_SERVICES_DDL = """
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

SCHEMA = (
    """
    CREATE TABLE providers (
        name TEXT PRIMARY KEY,
        base_url TEXT NOT NULL,
        key_env TEXT NOT NULL DEFAULT '',
        reasoning_format TEXT NOT NULL DEFAULT 'openai',
        api_key TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE models (
        id TEXT PRIMARY KEY,
        upstream TEXT NOT NULL,
        provider TEXT NOT NULL,
        profile TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)
    """,
    """
    CREATE TABLE time_periods (
        name TEXT PRIMARY KEY,
        days TEXT NOT NULL,
        start TEXT NOT NULL,
        end TEXT NOT NULL
    )
    """,
    JEV_SERVICES_DDL,
    """
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
    """,
    """
    CREATE INDEX idx_request_logs_ts ON request_logs(ts)
    """,
    """
    CREATE TABLE changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_changes_ts ON changes(ts)
    """,
    """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    )
    """,
    """
    CREATE TABLE user_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_user_sessions_user ON user_sessions(user_id)
    """,
    """
    CREATE INDEX idx_user_sessions_exp ON user_sessions(expires_at)
    """,
    """
    CREATE TABLE breaker (
        model TEXT PRIMARY KEY,
        failures INTEGER NOT NULL DEFAULT 0,
        opened_at REAL
    )
    """,
    *SNAPSHOT_SCHEMA,
)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def legacy_tree_ids(conn) -> list[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='routing_trees'").fetchone() is None:
        return []
    legacy = []
    for row in conn.execute("SELECT id, root FROM routing_trees ORDER BY id"):
        # 位置索引同时兼容 connect() 的 sqlite3.Row 与裸 sqlite3.Connection 的行元组。
        try:
            root_type = json.loads(row[1]).get("root", {}).get("type")
        except (ValueError, AttributeError, IndexError, TypeError):
            root_type = None
        if root_type != "graph":
            legacy.append(row[0])
    return legacy


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row[0].startswith("sqlite_")
    }


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _in_transaction(conn: sqlite3.Connection, action) -> None:
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    begun = False
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            begun = True
            action()
            conn.execute("COMMIT")
            begun = False
        except BaseException:
            if begun:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous_isolation


def _create_current_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (SCHEMA_VERSION,))


def _upgrade_v1_to_v2(conn: sqlite3.Connection) -> None:
    if "waves" not in _column_names(conn, "request_logs"):
        conn.execute("ALTER TABLE request_logs ADD COLUMN waves INTEGER")
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (2,))


def _upgrade_v2_to_v3(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (3,))


def _upgrade_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Drop the retired config-revision ledger; refuse to lose unexported rows."""
    if "config_revisions" in _table_names(conn):
        rows = int(conn.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0])
        if rows:
            raise RuntimeError(
                f"config_revisions still holds {rows} row(s); export them before upgrading "
                "(for example: sqlite3 router.db \".mode insert config_revisions\" "
                "\"SELECT * FROM config_revisions;\" > config-revisions.sql), then "
                "DELETE FROM config_revisions and retry"
            )
        conn.execute("DROP INDEX IF EXISTS idx_config_revisions_kind")
        conn.execute("DROP TABLE config_revisions")
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (4,))


def _upgrade_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Add the request/graph snapshot tables; existing ledger rows stay intact."""
    existing = _table_names(conn)
    if "graph_snapshots" not in existing:
        conn.execute(GRAPH_SNAPSHOTS_DDL)
    if "request_snapshots" not in existing:
        conn.execute(REQUEST_SNAPSHOTS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_request_snapshots_graph "
        "ON request_snapshots(graph_snapshot_id)"
    )
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (5,))


def _int_setting(conn: sqlite3.Connection, tables: set[str], key: str, default: int) -> int:
    if "settings" not in tables:
        return default
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return int(str(row[0]).strip())
    except (TypeError, ValueError):
        return default


def _clamped_legacy_window(
    conn: sqlite3.Connection, tables: set[str]
) -> tuple[int, int]:
    """把 v5 ``settings`` 里的窗口值收口到严格 ``JevWindow`` 范围。

    旧运行时在读路径上容忍越界值（按 MIN/MAX 收口并保证 input <= context）；
    v6 的 ``JevService`` 对每一行严格校验。若迁移直接落库越界值，旧表被删后
    ``JevServiceRepo.list()`` 会永久 ValueError，所以这里用与旧读路径相同的
    收口规则先规范化。
    """
    context_tokens = min(
        MAX_TOKENS,
        max(MIN_CONTEXT_TOKENS,
            _int_setting(conn, tables, "jev_context_tokens", DEFAULT_CONTEXT_TOKENS)),
    )
    input_tokens = min(
        context_tokens,
        max(MIN_INPUT_TOKENS,
            _int_setting(conn, tables, "jev_input_tokens", DEFAULT_INPUT_TOKENS)),
    )
    return input_tokens, context_tokens


def _upgrade_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Replace the singleton ``jev_config`` row with the multi-service catalog.

    旧单行配置一次性迁移为 ``default`` 服务并保留其已存密钥与窗口，随后删除
    ``jev_config``；这不是运行时兼容层，应用代码之后只读 ``jev_services``。

    legacy ``base_url`` 必须按 ``JevService``（``JevSettings``）的规范化规则落库
    （strip + 去掉末尾 ``/``）：``stored_api_key()`` 用精确的 base_url/model 比较
    判断已存密钥能否复用，原样复制带尾斜杠的旧地址会让「同一端点」被误判为不同
    端点，下一次只改元数据的 ``save(api_key=None)`` 就会清掉密钥。
    """
    tables = _table_names(conn)
    if "jev_services" not in tables:
        conn.execute(JEV_SERVICES_DDL)
    if "jev_config" in tables:
        row = conn.execute("SELECT * FROM jev_config WHERE id = 1").fetchone()
        if row is not None:
            input_tokens, context_tokens = _clamped_legacy_window(conn, tables)
            conn.execute(
                "INSERT OR REPLACE INTO jev_services (id, base_url, model, key_env, "
                "api_key, timeout_seconds, instructions, state_template, input_tokens, "
                "context_tokens, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("default", str(row["base_url"]).strip().rstrip("/"), row["model"],
                 "ROUTER_JEV_API_KEY",
                 row["api_key"], row["timeout_seconds"], row["instructions"],
                 row["state_template"], input_tokens, context_tokens,
                 row["updated_at"]),
            )
        conn.execute("DROP TABLE jev_config")
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (6,))


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    applied = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
    if applied > SCHEMA_VERSION:
        raise RuntimeError("unsupported database schema; use a new database")
    if applied == SCHEMA_VERSION:
        return
    tables = _table_names(conn)
    existing = tables - {"schema_migrations"}
    if applied == 0:
        if existing:
            raise RuntimeError("unsupported database contents; use a new database")
        _in_transaction(conn, lambda: _create_current_schema(conn))
        return
    if applied == 1:
        if not {"routing_trees", "request_logs"} <= tables:
            raise RuntimeError("unsupported database contents; use a new database")
        legacy = legacy_tree_ids(conn)
        if legacy:
            raise RuntimeError(
                "legacy routing trees require explicit reset; back up the database, then "
                "DELETE FROM routing_trees if these old routing configs are no longer needed: "
                + ", ".join(legacy)
            )
        _in_transaction(conn, lambda: _upgrade_v1_to_v2(conn))
        applied = 2
        tables = _table_names(conn)
    if applied == 2:
        if not {"routing_trees", "request_logs"} <= tables:
            raise RuntimeError("unsupported database contents; use a new database")
        legacy = legacy_tree_ids(conn)
        if legacy:
            raise RuntimeError(
                "legacy routing trees require explicit reset; back up the database, then "
                "DELETE FROM routing_trees if these old routing configs are no longer needed: "
                + ", ".join(legacy)
            )
        _in_transaction(conn, lambda: _upgrade_v2_to_v3(conn))
        applied = 3
        tables = _table_names(conn)
    if applied == 3:
        if not {"request_logs", "settings"} <= tables:
            raise RuntimeError("unsupported database contents; use a new database")
        _in_transaction(conn, lambda: _upgrade_v3_to_v4(conn))
        applied = 4
        tables = _table_names(conn)
    if applied == 4:
        if "request_logs" not in tables:
            raise RuntimeError("unsupported database contents; use a new database")
        _in_transaction(conn, lambda: _upgrade_v4_to_v5(conn))
        applied = 5
    if applied == 5:
        _in_transaction(conn, lambda: _upgrade_v5_to_v6(conn))
        return
    raise RuntimeError("unsupported database contents; use a new database")
