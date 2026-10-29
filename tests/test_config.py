from router.config import Settings


def test_defaults():
    settings = Settings.from_env()
    assert settings.db_path == "router.db"
    assert settings.port == 8400
    assert settings.jev_url == ""
    assert settings.jev_model == ""
    assert settings.jev_timeout == 10.0
    assert settings.session_max_entries == 10000
    assert not hasattr(settings, "tagger")


def test_env_override(monkeypatch):
    monkeypatch.setenv("ROUTER_DB", "/tmp/v3.db")
    monkeypatch.setenv("ROUTER_PORT", "9000")
    monkeypatch.setenv("ROUTER_JEV_URL", "http://jev.test")
    monkeypatch.setenv("ROUTER_JEV_MODEL", "NeoHorse-Jev-4B")
    monkeypatch.setenv("ROUTER_JEV_API_KEY", "secret")
    monkeypatch.setenv("ROUTER_JEV_TIMEOUT", "4.5")
    monkeypatch.setenv("ROUTER_SESSION_MAX_ENTRIES", "123")
    settings = Settings.from_env()
    assert settings.db_path == "/tmp/v3.db"
    assert settings.port == 9000
    assert settings.jev_url == "http://jev.test"
    assert settings.jev_model == "NeoHorse-Jev-4B"
    assert settings.jev_api_key == "secret"
    assert settings.jev_timeout == 4.5
    assert settings.session_max_entries == 123


def test_user_auth_defaults_and_override(monkeypatch):
    assert Settings().user_auth is False
    for name in ("ROUTER_USER_AUTH", "ROUTER_ADMIN_EMAIL", "ROUTER_ADMIN_PASSWORD", "ROUTER_SESSION_TTL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().user_auth is True
    monkeypatch.setenv("ROUTER_USER_AUTH", "0")
    monkeypatch.setenv("ROUTER_ADMIN_EMAIL", "root@example.com")
    monkeypatch.setenv("ROUTER_ADMIN_PASSWORD", "root-pass-1")
    monkeypatch.setenv("ROUTER_SESSION_TTL_HOURS", "12")
    settings = Settings.from_env()
    assert settings.user_auth is False
    assert settings.admin_email == "root@example.com"
    assert settings.admin_password == "root-pass-1"
    assert settings.session_ttl_hours == 12
