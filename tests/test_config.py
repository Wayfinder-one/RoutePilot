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
