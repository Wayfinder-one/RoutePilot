import pytest

from router.store.db import connect, migrate


def test_migrate_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    migrate(conn)          # second run must be a no-op
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"stations", "nodes", "branches", "branch_rows", "models", "providers",
            "keyword_groups", "settings", "requests",
            "changes", "evals", "request_ledger", "config_revisions",
            "schema_migrations"} <= tables
    assert "request_signals" not in tables      # v10 dropped the dead table
    assert "sessions" not in tables      # v11 dropped it
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_migrations_14_15_16_create_the_action_result_ledger(tmp_path):
    """JEV plan §3.1 + P1-B/C: v14 adds request_ledger, v15 appends provenance/
    terminal-state columns and config_revisions; `requests` stays slim.

    The ledger is the iteration's deliberate addition (the 2026-10-20 decision
    to keep `requests` slim stands): one row per request with action, outcome
    and version metadata, and NO text/signals/PII columns.
    """
    from router.store.db import MIGRATIONS

    path = str(tmp_path / "t.db")
    conn = connect(path)
    migrate(conn)
    migrate(conn)                      # idempotent: re-running applies nothing

    ledger_columns = [r[1] for r in conn.execute(
        "PRAGMA table_info(request_ledger)")]
    assert ledger_columns == [
        # v14
        "id", "ts", "decision", "model", "station_id", "leaf_id", "score",
        "reasoning", "upstream_model", "upstream_status", "prompt_tokens",
        "completion_tokens", "total_tokens", "routing_ms", "upstream_ms",
        "ttft_ms", "total_ms", "jev_engine", "jev_model", "jev_window",
        "jev_temperature", "questions_sha256", "pipeline_version",
        "error_class",
        # v15 (JEV P1-B/C)
        "pipeline_revision", "questions_revision", "backends_revision",
        "outcome_state", "experiment_arm", "quality_score",
        "quality_evaluator", "usage_observed",
        # v16 (JEV P2-A/D): chunked long-input attribution
        "jev_chunk", "input_chunk_mode", "input_chunk_segments",
        "input_chunk_prompt_tokens",
        # v18 (2026-10-26): capability-routing pack revision
        "capability_revision"]
    assert not {"text", "signals", "trace"} & set(ledger_columns)

    # v14 must NOT alter the slim contract of the existing requests table.
    assert [r[1] for r in conn.execute("PRAGMA table_info(requests)")] == [
        "id", "ts", "text", "decision", "model", "station_id", "leaf_id",
        "score", "latency_ms"]

    assert conn.execute("SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()[0] == len(MIGRATIONS)
    conn.close()


def test_migration_14_upgrades_a_v13_database(tmp_path):
    """An existing database (v13) gains the ledger without touching old rows."""
    import sqlite3

    from router.store.db import MIGRATIONS

    path = str(tmp_path / "v13.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);")
    for i, script in enumerate(MIGRATIONS[:13], start=1):
        conn.executescript(script)
        conn.execute("INSERT INTO schema_migrations VALUES (?)", (i,))
    conn.execute("INSERT INTO requests (id, ts, text, decision, model, "
                 "station_id, leaf_id, score, latency_ms) "
                 "VALUES ('old', '2026-10-01T10:00:00', '', 'route', "
                 "'qwen3-8b', NULL, NULL, NULL, 1.0)")
    conn.commit()
    conn.close()

    conn = connect(path)
    migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM request_ledger").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()[0] == len(MIGRATIONS)
    conn.close()


def test_migration_15_adds_config_revisions_and_keeps_v14_rows(tmp_path):
    """v15 is additive: existing v14 ledger rows gain NULL provenance columns."""
    import sqlite3

    from router.store.db import MIGRATIONS

    path = str(tmp_path / "v14.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);")
    for i, script in enumerate(MIGRATIONS[:14], start=1):
        conn.executescript(script)
        conn.execute("INSERT INTO schema_migrations VALUES (?)", (i,))
    conn.execute("INSERT INTO request_ledger (id, ts, decision) "
                 "VALUES ('old', '2026-10-01T10:00:00', 'route')")
    conn.commit()
    conn.close()

    conn = connect(path)
    migrate(conn)
    migrate(conn)                           # idempotent on v15 too
    ledger = conn.execute("SELECT * FROM request_ledger WHERE id='old'").fetchone()
    assert ledger["outcome_state"] is None  # pre-v15 row, no invented terminal
    assert ledger["pipeline_revision"] is None
    assert ledger["usage_observed"] is None
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "config_revisions" in tables
    assert conn.execute("SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()[0] == len(MIGRATIONS)
    conn.close()


def test_migration_16_adds_chunk_columns_and_keeps_v15_rows(tmp_path):
    """v16 is additive: historical ledger rows stay readable and just report
    NULL chunk attribution; re-running the migration is a no-op."""
    import sqlite3

    from router.store.db import MIGRATIONS

    path = str(tmp_path / "v15.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);")
    for i, script in enumerate(MIGRATIONS[:15], start=1):
        conn.executescript(script)
        conn.execute("INSERT INTO schema_migrations VALUES (?)", (i,))
    conn.execute("INSERT INTO request_ledger (id, ts, decision, pipeline_revision) "
                 "VALUES ('old', '2026-10-01T10:00:00', 'route', 'sha-old')")
    conn.commit()
    conn.close()

    conn = connect(path)
    migrate(conn)
    migrate(conn)                            # idempotent on v16 too
    row = conn.execute("SELECT * FROM request_ledger WHERE id='old'").fetchone()
    assert row["pipeline_revision"] == "sha-old"     # v15 data intact
    assert row["jev_chunk"] is None
    assert row["input_chunk_mode"] is None
    assert row["input_chunk_segments"] is None
    assert row["input_chunk_prompt_tokens"] is None
    assert conn.execute("SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()[0] == len(MIGRATIONS)
    conn.close()


def test_config_revisions_are_append_only_and_deduped(tmp_path):
    """UNIQUE(kind, sha256): re-recording identical content keeps the original
    row (history is never rewritten); a different content adds a new row."""
    from router.store.repositories import ConfigRevisionRepo

    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    repo = ConfigRevisionRepo(conn)

    sha_a = repo.record("pipeline", '{"stations":[]}')
    assert sha_a == repo.record("pipeline", '{"stations":[]}')   # dedup
    sha_b = repo.record("pipeline", '{"stations":[1]}')
    assert sha_a != sha_b
    assert repo.get("pipeline", sha_a)["content"] == '{"stations":[]}'
    assert repo.latest("pipeline")["sha256"] == sha_b
    # kind is part of the identity: the same content under another kind is a
    # separate revision row.
    repo.record("questions", '{"stations":[]}')
    assert conn.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0] == 3
    conn.close()


def test_config_revisions_columns(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    assert [r[1] for r in conn.execute(
        "PRAGMA table_info(config_revisions)")] == [
        "id", "kind", "sha256", "content", "created_at"]
    conn.close()


def test_migrate_upgrades_a_v1_database(tmp_path):
    """A DB that only has migration 1 applied gains the later tables."""
    import sqlite3

    from router.store.db import MIGRATIONS

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);")
    conn.executescript(MIGRATIONS[0])
    conn.execute("INSERT INTO schema_migrations VALUES (1)")
    conn.commit()
    conn.close()
    conn = connect(path)
    migrate(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"changes", "evals"} <= tables
    assert "sessions" not in tables      # v11 dropped it
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] \
        == len(MIGRATIONS)
    conn.close()


def test_a_failing_migration_leaves_the_database_untouched(tmp_path, monkeypatch):
    """Migration + version row are one transaction: a script that fails half
    way is rolled back entirely, so the next start retries it cleanly
    (no half-applied DDL, no "duplicate column")."""
    import sqlite3

    import router.store.db as db

    original = list(db.MIGRATIONS)
    good = "CREATE TABLE ok_table (id TEXT PRIMARY KEY);"
    broken = ("CREATE TABLE half_table (id TEXT PRIMARY KEY);\n"
              "CREATE TABLE half_table (id TEXT PRIMARY KEY);")   # duplicate
    monkeypatch.setattr(db, "MIGRATIONS", original + [good, broken])

    path = str(tmp_path / "t.db")
    conn = connect(path)
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half_table" not in tables          # rolled back with the rest
    version = conn.execute(
        "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert version == len(original) + 1        # only the good one was recorded
    conn.close()

    # the fixed script applies cleanly on the next start
    monkeypatch.setattr(db, "MIGRATIONS", original + [good])
    conn = connect(path)
    migrate(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok_table" in tables
    conn.close()
