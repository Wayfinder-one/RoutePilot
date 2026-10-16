from router.store.db import connect, migrate


def test_migrate_idempotent_and_wal(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    migrate(conn)
    migrate(conn)          # second run must be a no-op
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nodes", "branches", "branch_rows", "models", "providers",
            "keyword_groups", "settings", "requests", "request_signals",
            "schema_migrations"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
