from __future__ import annotations

import json
import sqlite3

SCHEMA_VERSION = 2

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
    """
    CREATE TABLE routing_trees (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
        root TEXT NOT NULL,
        tree_revision TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX idx_routing_trees_single_enabled
        ON routing_trees(enabled) WHERE enabled = 1
    """,
    """
    CREATE INDEX idx_routing_trees_updated ON routing_trees(updated_at, id)
    """,
    """
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
    """,
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
    CREATE TABLE config_revisions (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(kind, sha256)
    )
    """,
    """
    CREATE INDEX idx_config_revisions_kind ON config_revisions(kind, id)
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
        try:
            root_type = json.loads(row["root"]).get("root", {}).get("type")
        except (ValueError, AttributeError):
            root_type = None
        if root_type != "graph":
            legacy.append(row["id"])
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
    if applied != 1 or not {"routing_trees", "request_logs"} <= tables:
        raise RuntimeError("unsupported database contents; use a new database")
    legacy = legacy_tree_ids(conn)
    if legacy:
        raise RuntimeError(
            "legacy routing trees require explicit reset; back up the database, then "
            "DELETE FROM routing_trees if these old routing configs are no longer needed: "
            + ", ".join(legacy)
        )
    _in_transaction(conn, lambda: _upgrade_v1_to_v2(conn))
