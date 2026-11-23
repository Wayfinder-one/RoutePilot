from router.jev.service import JevService
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo


def service(service_id="jev-a", model="d1-a"):
    return JevService(
        id=service_id, base_url="http://jev-a.test", model=model,
        key_env="JEV_A_API_KEY", input_tokens=2048, context_tokens=4096,
    )


def test_service_roundtrip_has_no_secret_in_snapshot(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(), api_key="secret-sample")
    loaded = repo.get("jev-a")
    assert loaded.snapshot_dict() == {
        "id": "jev-a", "base_url": "http://jev-a.test", "model": "d1-a",
        "key_env": "JEV_A_API_KEY", "timeout_seconds": 10.0,
        "instructions": loaded.instructions, "state_template": loaded.state_template,
    }
    assert loaded.window_dict() == {"input_tokens": 2048, "context_tokens": 4096}
    assert repo.stored_api_key(loaded) == "secret-sample"
    assert "api_key" not in loaded.snapshot_dict()


def test_v5_singleton_migrates_to_default_service(tmp_path):
    path = str(tmp_path / "old.db")
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (5)")
    conn.execute("""CREATE TABLE jev_config (
        id INTEGER PRIMARY KEY CHECK (id = 1), base_url TEXT NOT NULL,
        model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '',
        timeout_seconds REAL NOT NULL, instructions TEXT NOT NULL,
        state_template TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO settings VALUES ('jev_input_tokens', '2048')")
    conn.execute("INSERT INTO settings VALUES ('jev_context_tokens', '4096')")
    conn.execute("""INSERT INTO jev_config VALUES
        (1, 'http://old-jev.test', 'd1-old', 'old-secret', 5.0, '指令',
         '【请求】{request}', '2026-11-01T00:00:00+00:00')""")
    migrate(conn)
    loaded = JevServiceRepo(conn).get("default")
    assert loaded.base_url == "http://old-jev.test"
    assert loaded.input_tokens == 2048 and loaded.context_tokens == 4096
    assert JevServiceRepo(conn).stored_api_key(loaded) == "old-secret"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='jev_config'").fetchone() is None
