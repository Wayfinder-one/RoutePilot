import pytest

from router.jev.service import JevService
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
    JevWindow,
)
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo


def service(service_id="jev-a", model="d1-a", base_url="http://jev-a.test"):
    return JevService(
        id=service_id, base_url=base_url, model=model,
        key_env="JEV_A_API_KEY", input_tokens=2048, context_tokens=4096,
    )


def build_v5_database(path, settings, *, create_settings=True):
    """Build a legacy v5 database: version 5, singleton ``jev_config`` + settings."""
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (5)")
    conn.execute("""CREATE TABLE jev_config (
        id INTEGER PRIMARY KEY CHECK (id = 1), base_url TEXT NOT NULL,
        model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '',
        timeout_seconds REAL NOT NULL, instructions TEXT NOT NULL,
        state_template TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    if create_settings:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO settings VALUES (?, ?)", settings)
    conn.execute("""INSERT INTO jev_config VALUES
        (1, 'http://old-jev.test', 'd1-old', 'old-secret', 5.0, '指令',
         '【请求】{request}', '2026-11-01T00:00:00+00:00')""")
    conn.commit()
    return conn


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


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        # 旧窗口读路径读时收口：context 256 -> 512，input 99999 -> context。
        ((("jev_input_tokens", "99999"), ("jev_context_tokens", "256")),
         (MIN_CONTEXT_TOKENS, MIN_CONTEXT_TOKENS)),
        # 低于下限 -> 各自下限。
        ((("jev_input_tokens", "-5"), ("jev_context_tokens", "256")),
         (MIN_INPUT_TOKENS, MIN_CONTEXT_TOKENS)),
        # input 超过合法 context -> 收口到 context。
        ((("jev_input_tokens", "99999"), ("jev_context_tokens", "4096")),
         (DEFAULT_CONTEXT_TOKENS, DEFAULT_CONTEXT_TOKENS)),
        # 非数字 -> 回落默认值（与旧窗口读路径一致）。
        ((("jev_input_tokens", "not-a-number"), ("jev_context_tokens", "not-a-number")),
         (DEFAULT_INPUT_TOKENS, DEFAULT_CONTEXT_TOKENS)),
        # settings 缺这两个键 -> 默认值。
        ((), (DEFAULT_INPUT_TOKENS, DEFAULT_CONTEXT_TOKENS)),
    ],
)
def test_v5_out_of_range_window_migrates_clamped_and_listable(tmp_path, settings, expected):
    conn = build_v5_database(str(tmp_path / "old.db"), settings)
    migrate(conn)
    services = JevServiceRepo(conn).list()  # 严格校验：非法窗口会让这里永久 ValueError
    assert [item.id for item in services] == ["default"]
    loaded = services[0]
    assert (loaded.input_tokens, loaded.context_tokens) == expected
    JevWindow(loaded.input_tokens, loaded.context_tokens)


def test_v5_missing_settings_table_falls_back_to_default_window(tmp_path):
    conn = build_v5_database(str(tmp_path / "old.db"), (), create_settings=False)
    migrate(conn)
    loaded = JevServiceRepo(conn).get("default")
    assert (loaded.input_tokens, loaded.context_tokens) == (
        DEFAULT_INPUT_TOKENS, DEFAULT_CONTEXT_TOKENS)


def test_save_none_api_key_preserves_key_when_endpoint_and_model_unchanged(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(), api_key="secret-sample")
    repo.save(service(), api_key=None)  # 仅更新非密钥字段
    assert repo.stored_api_key(service()) == "secret-sample"


@pytest.mark.parametrize("changed", [
    {"base_url": "http://jev-b.test"},
    {"model": "d1-b"},
])
def test_save_none_api_key_drops_old_key_when_endpoint_or_model_changes(tmp_path, changed):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(), api_key="secret-sample")
    repo.save(service(**changed), api_key=None)
    row = conn.execute(
        "SELECT base_url, model, api_key FROM jev_services WHERE id = 'jev-a'"
    ).fetchone()
    assert row["api_key"] == ""
    assert repo.stored_api_key(service(**changed)) == ""


def test_save_empty_api_key_clears_stored_key(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(), api_key="secret-sample")
    repo.save(service(), api_key="")
    assert repo.stored_api_key(service()) == ""
    assert conn.execute(
        "SELECT api_key FROM jev_services WHERE id = 'jev-a'").fetchone()["api_key"] == ""


@pytest.mark.parametrize("changed", [
    {"base_url": "http://jev-b.test"},
    {"model": "d1-b"},
])
def test_stored_api_key_requires_endpoint_and_model_match(tmp_path, changed):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(), api_key="secret-sample")
    assert repo.stored_api_key(service(**changed)) == ""
    assert repo.stored_api_key(service()) == "secret-sample"
