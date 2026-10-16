"""SQLite storage: WAL mode, single writer, ordered migrations.

High availability is handled outside this file (Litestream streams the WAL
to object storage; see design doc section 5.2). Everything here stays plain
SQLite so a future rqlite/LiteFS swap touches only this layer.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS = [
    """
    CREATE TABLE nodes (
        id TEXT PRIMARY KEY, ord INTEGER NOT NULL,
        type TEXT NOT NULL, name TEXT NOT NULL DEFAULT '');
    CREATE TABLE branches (
        id TEXT PRIMARY KEY, node_id TEXT NOT NULL, ord INTEGER NOT NULL,
        name TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT 'route',
        model TEXT, min_score REAL);
    CREATE TABLE branch_rows (
        id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, ord INTEGER NOT NULL,
        signal TEXT NOT NULL, value TEXT NOT NULL,
        weight REAL, conf_min REAL);
    CREATE TABLE models (
        id TEXT PRIMARY KEY, upstream TEXT NOT NULL, provider TEXT NOT NULL,
        profile TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE providers (
        name TEXT PRIMARY KEY, base_url TEXT NOT NULL,
        key_env TEXT NOT NULL DEFAULT '');
    CREATE TABLE keyword_groups (name TEXT PRIMARY KEY, words TEXT NOT NULL);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE requests (
        id TEXT PRIMARY KEY, ts TEXT NOT NULL, text TEXT NOT NULL,
        decision TEXT NOT NULL, model TEXT, node_id TEXT, branch_id TEXT,
        score REAL, latency_ms REAL);
    CREATE TABLE request_signals (
        request_id TEXT NOT NULL, signal TEXT NOT NULL,
        value TEXT, confidence REAL, is_rule INTEGER NOT NULL);
    CREATE INDEX idx_requests_ts ON requests(ts);
    CREATE INDEX idx_reqsig_rid ON request_signals(request_id);
    """,
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(version INTEGER PRIMARY KEY)")
    applied = conn.execute("SELECT COALESCE(MAX(version), 0) "
                           "FROM schema_migrations").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS, start=1):
        if i > applied:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (i,))
    conn.commit()
