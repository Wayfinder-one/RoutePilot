import json
import pytest
from router.core_config import CoreSettings


def write_config(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_core_settings_load_routepilot_sink(tmp_path):
    settings = CoreSettings.from_file(write_config(tmp_path, {
        "server": {"host": "0.0.0.0", "port": 8400},
        "graphs": {"directory": "/data/graphs"},
        "jev": {"api_key_env": "ROUTER_JEV_API_KEY"},
        "logging": {
            "type": "routepilot",
            "url": "http://console:8401/internal/v1/decision-logs",
            "timeout_ms": 1000,
        },
    }))
    assert settings.graph_dir == "/data/graphs"
    assert settings.logging.type == "routepilot"
    assert settings.logging.timeout_ms == 1000


def test_core_settings_allow_disabled_and_reject_unknown_sink(tmp_path):
    disabled = CoreSettings.from_file(write_config(tmp_path, {
        "graphs": {"directory": "/graphs"}, "logging": {"type": "disabled"},
    }))
    assert disabled.logging.url == ""
    with pytest.raises(ValueError, match="logging.type"):
        CoreSettings.from_file(write_config(tmp_path, {
            "logging": {"type": "victorialogs", "url": "http://logs:9428"},
        }))


def test_routepilot_sink_requires_http_url(tmp_path):
    with pytest.raises(ValueError, match="logging.url"):
        CoreSettings.from_file(write_config(tmp_path, {
            "logging": {"type": "routepilot", "url": "file:///tmp/log"},
        }))
