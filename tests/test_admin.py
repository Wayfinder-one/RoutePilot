import asyncio

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.requestlog import RequestLogger
from router.store.db import connect, migrate
from tests.routing_fixtures import test_settings


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(test_settings(tmp_path)))


def test_overview_and_request_stats_use_current_log_contract(client):
    overview = client.get("/admin/overview").json()
    assert {"daily", "routes", "total", "sticky", "confidence", "confidence_since"} <= set(overview)
    assert "evals" not in overview
    stats = client.get("/admin/requests/stats").json()
    assert stats["ring_size"] == 0
    assert stats["queue_limit"] > 0


@pytest.mark.parametrize("path", [
    "/admin/signals", "/admin/signal-questions", "/admin/tagger",
    "/admin/keyword-groups", "/admin/eval", "/admin/evals",
])
def test_legacy_signal_and_eval_apis_are_gone(client, path):
    assert client.get(path).status_code == 404


def test_time_periods_roundtrip_and_snapshot_refresh(client):
    response = client.put("/admin/time-periods", json={"periods": [{"name": "值班", "days": [0, 1], "start": "22:00", "end": "06:00"}]})
    assert response.status_code == 200, response.text
    body = client.get("/admin/time-periods").json()
    assert body["periods"][0]["name"] == "值班"
    assert "now" in body
    assert client.app.state.snapshot.time_periods[0].name == "值班"


def test_model_provider_and_settings_writes_still_publish(client):
    response = client.put("/admin/models/qwen3-8b", json={"upstream": "real-model", "profile": {"capability": "通用请求"}})
    assert response.status_code == 200, response.text
    assert client.app.state.snapshot.backends["qwen3-8b"].upstream_name == "real-model"
    assert client.app.state.snapshot.capabilities["qwen3-8b"] == "通用请求"
    response = client.put("/admin/providers/local", json={"base_url": "http://example.internal:9/v1", "key_env": ""})
    assert response.status_code == 200, response.text
    assert client.app.state.snapshot.backends["qwen3-8b"].url == "http://example.internal:9/v1/chat/completions"
    assert client.put("/admin/settings", json={"note": "hello", "n": 3}).json()["settings"]["n"] == "3"
    kinds = {row["kind"] for row in client.get("/admin/changes").json()["changes"]}
    assert {"model", "provider", "settings"} <= kinds


def test_request_recent_returns_memory_ring(client):
    body = client.get("/admin/requests/recent").json()
    assert body == {"requests": []}


def test_startup_hydrates_recent_request_logs_from_sqlite(tmp_path):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    log = RequestLogger(settings)
    log.enqueue({
        "id": "historical-1",
        "ts": "2026-11-03T18:59:00+08:00",
        "decision": "route",
        "model": "fast",
        "routing_ms": 12.0,
    })
    asyncio.run(log.flush())

    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/admin/requests/recent").json()
        stats = client.get("/admin/requests/stats").json()

    assert [item["id"] for item in body["requests"]] == ["historical-1"]
    assert body["requests"][0]["latency_ms"] == 12.0
    assert "text" not in body["requests"][0]
    assert "trace" not in body["requests"][0]
    assert stats["ring_size"] == 1
