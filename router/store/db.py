from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

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


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    applied = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
    if applied > SCHEMA_VERSION:
        raise RuntimeError("unsupported database schema; use a new database")
    if applied == SCHEMA_VERSION:
        return
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_migrations'"
    ).fetchone()
    if existing is not None:
        raise RuntimeError("unsupported database contents; use a new database")
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (SCHEMA_VERSION,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous_isolation
