import pytest

from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevConfigRepo, JevSettings
from router.store.db import connect, migrate


def settings(api_key="secret"):
    return JevSettings("http://jev.test", "NeoHorse-Jev-4B", api_key, 8.0, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE)


def test_repository_roundtrip_and_public_config_hide_key(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    repo = JevConfigRepo(conn)
    assert repo.load() is None
    repo.save(settings())
    loaded = repo.load()
    assert loaded == settings()
    public = loaded.public_dict(env_key_present=False)
    assert "api_key" not in public
    assert public["key_present"] is True
    assert public["key_source"] == "stored"
    conn.close()


def test_environment_key_is_reported_without_exposing_it():
    public = settings(api_key="").public_dict(env_key_present=True)
    assert public["key_present"] is True
    assert public["key_source"] == "environment"


@pytest.mark.parametrize("overrides", [
    {"base_url": "ftp://jev.test"},
    {"base_url": "http://jev.test?x=1"},
    {"model": ""},
    {"timeout_seconds": 0},
    {"timeout_seconds": 121},
    {"instructions": " "},
    {"state_template": "没有占位符"},
    {"state_template": "{unknown}"},
])
def test_invalid_settings_are_rejected(overrides):
    values = {
        "base_url": "http://jev.test", "model": "m", "api_key": "",
        "timeout_seconds": 5.0, "instructions": DEFAULT_INSTRUCTIONS,
        "state_template": DEFAULT_STATE_TEMPLATE,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        JevSettings(**values)
