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
    # v2 (M2): per-session last-turn embedding, for similarity/reask.
    """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, last_vec TEXT, updated TEXT NOT NULL);
    """,
    # v3 (M3): configuration audit trail + evaluation reports.
    """
    CREATE TABLE changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        kind TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE evals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        mode TEXT NOT NULL, total INTEGER NOT NULL, matched INTEGER NOT NULL,
        rate REAL NOT NULL, report TEXT NOT NULL);
    CREATE INDEX idx_changes_ts ON changes(ts);
    CREATE INDEX idx_evals_ts ON evals(ts);
    """,
    # v4 (M4): session continuation (last routed model) + cross-replica breaker.
    """
    ALTER TABLE sessions ADD COLUMN last_model TEXT;
    CREATE TABLE breaker (
        model TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
        opened_at REAL);
    """,
    # v5 (SPEC-TREE-4 §2.3): split nodes — nullable floor + root fork-key
    # override. Old rows stay NULL, which deserializes to None on both sides,
    # so there is no data migration.
    """
    ALTER TABLE nodes ADD COLUMN floor REAL;
    ALTER TABLE nodes ADD COLUMN split_signal TEXT;
    """,
    # v6 (SPEC-TREE-4 §4.4): split layer path per request, for the console log
    # detail. Nullable JSON text; NULL for gate/score decisions.
    """
    ALTER TABLE requests ADD COLUMN tree_path TEXT;
    """,
    # v7 (2026-10-19): explicit-tree pipeline. Each station is one JSON document
    # (ordered tree); the old nodes/branches/branch_rows tables are left in
    # place but no longer read or written.
    """
    CREATE TABLE stations (
        id TEXT PRIMARY KEY, ord INTEGER NOT NULL,
        type TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
        tree TEXT NOT NULL);
    """,
    # v8 (2026-10-20): named time periods for the time_window rule signal
    # (keyword-lexicon style: the signal value is the list of matching names).
    """
    CREATE TABLE time_periods (
        name TEXT PRIMARY KEY, days TEXT NOT NULL,
        start TEXT NOT NULL, end TEXT NOT NULL);
    """,
]


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False: under uvicorn, FastAPI runs this dependency's
    # generator (and its teardown) across different threadpool threads. Every
    # connection is request-scoped and never used concurrently, so relaxing the
    # thread check is safe; WAL + busy_timeout handle cross-connection writers.
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
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
