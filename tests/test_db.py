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
