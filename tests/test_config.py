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


def test_admin_token_env(monkeypatch):
    monkeypatch.setenv("ROUTER_ADMIN_TOKEN", "sekret")
    assert Settings.from_env().admin_token == "sekret"
    monkeypatch.delenv("ROUTER_ADMIN_TOKEN")
    assert Settings.from_env().admin_token == ""


def test_jev_window_default_and_env(monkeypatch):
    """Plan item 2: ROUTER_JEV_WINDOW overrides the window; unset stays empty so
    JevTagger derives the legacy head-only "<max_length>:0" spec (deployed
    "1024:0"), keeping a custom ROUTER_JEV_MAX_LENGTH on the old behavior."""
    monkeypatch.delenv("ROUTER_JEV_WINDOW", raising=False)
    assert Settings.from_env().jev_window == ""
    monkeypatch.setenv("ROUTER_JEV_WINDOW", "256:768")
    assert Settings.from_env().jev_window == "256:768"
