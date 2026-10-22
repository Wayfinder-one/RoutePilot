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
    # v9 (2026-10-21): model catalog + per-model reasoning capability.
    # providers.reasoning_format = protocol dialect default (see
    # docs/模型目录与推理配置-2026-10-21.md §4); a model's profile.reasoning.format
    # wins over it. providers.api_key stores the console-entered key in plain
    # text (local/enterprise console behind the /admin bearer token); GET
    # endpoints only ever expose key_present/key_source, never the value.
    # Both defaults keep old rows behaviourally unchanged.
    """
    ALTER TABLE providers ADD COLUMN reasoning_format TEXT NOT NULL DEFAULT 'openai';
    ALTER TABLE providers ADD COLUMN api_key TEXT NOT NULL DEFAULT '';
    """,
    # v10 (2026-10-21, audit §5): request_signals was created in v1 but never
    # written (signal detail lives in the memory ring); drop the dead table.
    """
    DROP TABLE IF EXISTS request_signals;
    DROP INDEX IF EXISTS idx_reqsig_rid;
    """,
    # v11 (2026-10-21): session store removed — routing judges every request
    # independently (docs/superpowers/specs/2026-10-21-remove-session-stickiness.md).
    """
    DROP TABLE IF EXISTS sessions;
    """,
    # v12 (2026-10-22): the requests row is slim and its column names now
    # match what they hold: `node_id` held the STATION id and `branch_id` the
    # LEAF id (the old nodes/branches tables are long gone). The dead
    # `tree_path` column is dropped with the rebuild (the calculation log
    # became memory-only in 2026-10-20).
    """
    CREATE TABLE requests_v12 (
        id TEXT PRIMARY KEY, ts TEXT NOT NULL, text TEXT NOT NULL,
        decision TEXT NOT NULL, model TEXT, station_id TEXT, leaf_id TEXT,
        score REAL, latency_ms REAL);
    INSERT INTO requests_v12
        (id, ts, text, decision, model, station_id, leaf_id, score, latency_ms)
        SELECT id, ts, text, decision, model, node_id, branch_id, score,
               latency_ms FROM requests;
    DROP TABLE requests;
    ALTER TABLE requests_v12 RENAME TO requests;
    CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
    """,
]


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False: under uvicorn, FastAPI runs this dependency's
    # generator (and its teardown) across different threadpool threads. Every
    # connection is request-scoped and never used concurrently, so relaxing the
    # thread check is safe; WAL + busy_timeout handle cross-connection writers.
    # The timeout argument IS the busy timeout (how long a writer waits for a
    # lock) — no second PRAGMA needed.
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _statements(script: str):
    """Split a migration script into complete SQL statements.

    ``sqlite3.complete_statement`` knows about string literals and comments,
    so a ';' inside a string does not split the statement.
    """
    buf = ""
    for line in script.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                yield stmt
            buf = ""
    tail = buf.strip()
    if tail:
        yield tail


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations; each one is atomic.

    A migration and its ``schema_migrations`` row are written in the SAME
    transaction: a script that fails half-way leaves the database untouched
    (no half-applied DDL, no version row), so the next start retries it
    cleanly instead of failing on "duplicate column".
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(version INTEGER PRIMARY KEY)")
    applied = int(conn.execute("SELECT COALESCE(MAX(version), 0) "
                                "FROM schema_migrations").fetchone()[0])
    previous_isolation = conn.isolation_level
    conn.isolation_level = None          # explicit transaction control
    try:
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= applied:
                continue
            try:
                conn.execute("BEGIN")
                for stmt in _statements(sql):
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_migrations (version) "
                             "VALUES (?)", (i,))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
    finally:
        conn.isolation_level = previous_isolation
