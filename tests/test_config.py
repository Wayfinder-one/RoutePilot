from router.config import Settings


def test_defaults():
    s = Settings.from_env()
    assert s.db_path == "router.db"
    assert s.tagger == "heuristic"
    assert s.port == 8400


def test_env_override(monkeypatch):
    monkeypatch.setenv("ROUTER_DB", "/tmp/x.db")
    monkeypatch.setenv("ROUTER_PORT", "9000")
    s = Settings.from_env()
    assert s.db_path == "/tmp/x.db"
    assert s.port == 9000


def test_static_admin_token_removed(monkeypatch):
    """2026-10-26: ROUTER_ADMIN_TOKEN 已移除，Settings 不再有该字段。"""
    assert not hasattr(Settings(), "admin_token")
    monkeypatch.setenv("ROUTER_ADMIN_TOKEN", "sekret")     # 旧 env 被忽略
    s = Settings.from_env()
    assert not hasattr(s, "admin_token")


def test_user_auth_default_asymmetry(monkeypatch):
    """设计 decision：dataclass 默认 False（既有测试/直接构造的兼容路径），
    from_env 默认 True（生产默认要求登录）。这个不对称是 spec §6 的冻结项。"""
    assert Settings().user_auth is False
    for name in ("ROUTER_USER_AUTH", "ROUTER_ADMIN_EMAIL",
                 "ROUTER_ADMIN_PASSWORD", "ROUTER_SESSION_TTL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    from_env = Settings.from_env()
    assert from_env.user_auth is True
    assert from_env.admin_email == "admin@test.com"
    assert from_env.admin_password == "1qaz@WSX"
    assert from_env.session_ttl_hours == 168


def test_user_auth_env_override(monkeypatch):
    monkeypatch.setenv("ROUTER_USER_AUTH", "0")
    monkeypatch.setenv("ROUTER_ADMIN_EMAIL", "root@example.com")
    monkeypatch.setenv("ROUTER_ADMIN_PASSWORD", "root-pass-1")
    monkeypatch.setenv("ROUTER_SESSION_TTL_HOURS", "12")
    s = Settings.from_env()
    assert s.user_auth is False
    assert s.admin_email == "root@example.com"
    assert s.admin_password == "root-pass-1"
    assert s.session_ttl_hours == 12


def test_jev_window_default_and_env(monkeypatch):
    """Plan item 2: ROUTER_JEV_WINDOW overrides the window; unset stays empty so
    JevTagger derives the legacy head-only "<max_length>:0" spec (deployed
    "1024:0"), keeping a custom ROUTER_JEV_MAX_LENGTH on the old behavior."""
    monkeypatch.delenv("ROUTER_JEV_WINDOW", raising=False)
    assert Settings.from_env().jev_window == ""
    monkeypatch.setenv("ROUTER_JEV_WINDOW", "256:768")
    assert Settings.from_env().jev_window == "256:768"
