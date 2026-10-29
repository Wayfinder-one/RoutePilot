from contextlib import contextmanager
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.api.routes_jev import router
from router.jev.client import JevClient
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevConfigRepo
from router.store.db import connect, migrate


class Publisher:
    def __init__(self):
        self.refreshes = 0

    @contextmanager
    def locked(self):
        yield self

    def refresh(self, conn):
        self.refreshes += 1


def make_client(tmp_path, handler=None):
    path = str(tmp_path / "v3.db")
    conn = connect(path)
    migrate(conn)
    conn.close()
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(200, json={
        "answers": {"route": {"probabilities": {"候选 A": 0.4, "候选 B": 0.6}}},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    })))
    app = FastAPI()
    app.state.settings = SimpleNamespace(db_path=path)
    app.state.http = httpx.AsyncClient(transport=transport)
    app.state.jev_client = JevClient(app.state.http)
    app.state.publisher = Publisher()
    app.include_router(router, prefix="/admin")
    return TestClient(app), app


def payload():
    return {
        "base_url": "http://jev.test",
        "model": "NeoHorse-Jev-4B",
        "timeout_seconds": 5,
        "instructions": DEFAULT_INSTRUCTIONS,
        "state_template": DEFAULT_STATE_TEMPLATE,
    }


def test_config_crud_hides_keys_and_preserves_then_clears_secret(tmp_path, monkeypatch):
    client, app = make_client(tmp_path)
    response = client.put("/admin/jev", json={**payload(), "api_key": "secret"})
    assert response.status_code == 200, response.text
    assert "api_key" not in response.json()["config"]
    assert response.json()["config"]["key_source"] == "stored"
    assert client.put("/admin/jev", json=payload()).json()["config"]["key_source"] == "stored"
    monkeypatch.setenv("ROUTER_JEV_API_KEY", "environment-secret")
    cleared = client.put("/admin/jev", json={**payload(), "api_key": ""})
    assert cleared.json()["config"]["key_source"] == "environment"
    assert app.state.publisher.refreshes == 3
    conn = connect(app.state.settings.db_path)
    assert JevConfigRepo(conn).load().api_key == ""
    conn.close()


def test_test_endpoint_calls_real_configured_http_service(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.put("/admin/jev", json=payload()).status_code == 200
    response = client.post("/admin/jev/test", json={"text": "测试连接"})
    assert response.status_code == 200, response.text
    assert response.json()["selected_key"] == "b"
    assert response.json()["probabilities"] == {"a": 0.4, "b": 0.6}
    assert response.json()["usage"]["input_tokens"] == 10


def test_test_endpoint_requires_configuration(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.post("/admin/jev/test", json={}).status_code == 422
