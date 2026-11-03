import asyncio
import json
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


def make_client(tmp_path, handler=None, requests=None):
    path = str(tmp_path / "v3.db")
    conn = connect(path)
    migrate(conn)
    conn.close()

    def default_handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json={
            "answers": {"route": {"type": "choice", "probabilities": {"候选 A": 0.4, "候选 B": 0.6}}},
            "usage": {"input_tokens": 10, "output_tokens": 0},
        })

    transport = httpx.MockTransport(handler or default_handler)
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
    requests = []
    client, _ = make_client(tmp_path, requests=requests)
    assert client.put("/admin/jev", json=payload()).status_code == 200
    response = client.post("/admin/jev/test", json={"text": "测试连接"})
    assert response.status_code == 200, response.text
    assert response.json()["selected_key"] == "b"
    assert response.json()["probabilities"] == {"a": 0.4, "b": 0.6}
    assert response.json()["usage"]["input_tokens"] == 10

    assert len(requests) == 1
    assert requests[0].url == "http://jev.test/v1/systemone"
    body = json.loads(requests[0].content)
    assert body["model"] == "NeoHorse-Jev-4B"
    assert list(body["questions"]) == ["route"]
    question = body["questions"]["route"]
    assert question["type"] == "choice"
    assert question["instructions"] == DEFAULT_INSTRUCTIONS
    assert question["criteria"] == {
        "候选 A": "用于验证决策服务连通性的第一个候选。",
        "候选 B": "用于验证决策服务连通性的第二个候选。",
    }


def test_test_endpoint_requires_configuration(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.post("/admin/jev/test", json={}).status_code == 422


def test_test_endpoint_huge_probability_returns_502_not_500(tmp_path):
    body = {
        "answers": {
            "route": {
                "type": "choice",
                "probabilities": {"候选 A": 10 ** 1000, "候选 B": 1.0},
            }
        }
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client, app = make_client(tmp_path, handler=handler)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        assert client.put("/admin/jev", json=payload()).status_code == 200
        response = client.post("/admin/jev/test", json={"text": "测试连接"})
        assert response.status_code == 502, response.text
        assert "route" in response.json()["detail"]
    finally:
        asyncio.run(app.state.http.aclose())
