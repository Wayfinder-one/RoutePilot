"""Peripheral SQLite schema.

v7 removed the SQLite decision-log tables (``request_logs``/``graph_snapshots``/
``request_snapshots``) and the JEV ``api_key`` column: the peripheral keeps only
the recent decision events in memory, and the kernel reads the JEV key from its
environment variable.  Fresh installs create the v7 schema directly; the only
in-place upgrade supported by this build is v6 → v7 (older databases must be
reset/exported explicitly, their historical decision logs are not migrated).
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 7

#: Oldest database version this build upgrades in place.  Earlier versions still
#: contain retired tables/config shapes and are rejected instead of silently
#: rewritten (no historical decision logs are migrated — design §9).
UPGRADABLE_VERSION = 6

_JEV_SERVICES_TEMPLATE = """
    CREATE TABLE {table} (
        id TEXT PRIMARY KEY,
        base_url TEXT NOT NULL,
        model TEXT NOT NULL,
        key_env TEXT NOT NULL DEFAULT '',
        timeout_seconds REAL NOT NULL,
        instructions TEXT NOT NULL,
        state_template TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        context_tokens INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """

# v7：多 JEV 服务目录的非密钥字段。密钥只由内核环境变量读取，不进入数据库。
JEV_SERVICES_DDL = _JEV_SERVICES_TEMPLATE.format(table="jev_services")

#: 重建 ``jev_services`` 时显式复制的非密钥列（绝不含旧 ``api_key``）。
JEV_SERVICES_COLUMNS = (
    "id", "base_url", "model", "key_env", "timeout_seconds", "instructions",
    "state_template", "input_tokens", "context_tokens", "updated_at",
)

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
)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


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


def _upgrade_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Drop the SQLite decision logs and rebuild ``jev_services`` without a key.

    Destructive by design (design §9): historical decision logs/snapshots are not
    migrated anywhere, and the stored JEV key is discarded — deployers set the
    kernel's ``ROUTER_JEV_API_KEY`` explicitly.  Only the explicit non-secret
    columns are copied, so no old key value can survive the rebuild.
    """
    conn.execute("DROP INDEX IF EXISTS idx_request_logs_ts")
    conn.execute("DROP INDEX IF EXISTS idx_request_snapshots_graph")
    for table in ("request_snapshots", "graph_snapshots", "request_logs"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    if "api_key" in _column_names(conn, "jev_services"):
        columns = ", ".join(JEV_SERVICES_COLUMNS)
        conn.execute(_JEV_SERVICES_TEMPLATE.format(table="jev_services_v7"))
        conn.execute(
            f"INSERT INTO jev_services_v7 ({columns}) SELECT {columns} FROM jev_services")
        conn.execute("DROP TABLE jev_services")
        conn.execute("ALTER TABLE jev_services_v7 RENAME TO jev_services")
    conn.execute("INSERT INTO schema_migrations VALUES (?)", (SCHEMA_VERSION,))


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
    if applied < UPGRADABLE_VERSION:
        raise RuntimeError(
            f"unsupported database schema version {applied}; this build upgrades version "
            f"{UPGRADABLE_VERSION} in place only (historical decision logs are not migrated). "
            "Back up or export the database, then use a new database")
    if "jev_services" not in tables:
        raise RuntimeError("unsupported database contents; use a new database")
    _in_transaction(conn, lambda: _upgrade_v6_to_v7(conn))
