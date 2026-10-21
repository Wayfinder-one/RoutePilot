from router.store.db import connect, migrate


def test_migrate_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    migrate(conn)          # second run must be a no-op
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"stations", "nodes", "branches", "branch_rows", "models", "providers",
            "keyword_groups", "settings", "requests",
            "changes", "evals",
            "schema_migrations"} <= tables
    assert "request_signals" not in tables      # v10 dropped the dead table
    assert "sessions" not in tables      # v11 dropped it
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
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
