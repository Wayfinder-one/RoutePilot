"""管理面多 JEV 服务目录 API：CRUD、密钥通道、逐条连接测试。

覆盖 spec「多 JEV 目录」：列表/详情/更新/删除/测试都只读数据库目录，不读活动
画布；公共投影不含 api_key，`key_source` 是 stored|env|none；旧单配置端点已删除。
"""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.routes_jev import router
from router.jev.client import JevClient
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import portable_tree_dict, test_settings


def _migrated(path):
    conn = connect(path)
    migrate(conn)
    conn.close()


class Publisher:
    """本文件只测目录 CRUD，不需要真实发布骨架。"""

    def __init__(self):
        self.published = []

    @contextmanager
    def locked(self):
        yield self

    def prepare(self, conn, tree, fingerprint):
        return SimpleNamespace(tree=tree, fingerprint=fingerprint)

    def publish(self, prepared):
        self.published.append(prepared)


def make_client(tmp_path, handler=None, requests=None):
    path = str(tmp_path / "v3.db")
    _migrated(path)

    def default_handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json={
            "answers": {"route": {"type": "choice",
                                  "probabilities": {"候选 A": 0.4, "候选 B": 0.6}}},
            "usage": {"input_tokens": 10, "output_tokens": 0},
        })

    transport = httpx.MockTransport(handler or default_handler)
    app = FastAPI()
    app.state.settings = SimpleNamespace(db_path=path)
    app.state.graph_repo = RoutingTreeRepo(tmp_path / "routing-graph")
    app.state.graph_repo.initialize()
    app.state.http = httpx.AsyncClient(transport=transport)
    app.state.jev_client = JevClient(app.state.http)
    app.state.publisher = Publisher()
    app.include_router(router, prefix="/admin")
    return TestClient(app), app


def service_payload(service_id="jev-a", **overrides):
    payload = {
        "id": service_id,
        "base_url": "http://" + service_id + ".test",
        "model": "d1-a",
        "key_env": "JEV_TEST_API_KEY",
        "input_tokens": 1024,
        "context_tokens": 4096,
    }
    payload.update(overrides)
    return payload


def _services(app):
    conn = connect(app.state.settings.db_path)
    try:
        return JevServiceRepo(conn).list()
    finally:
        conn.close()


# ── 多服务目录契约 ────────────────────────────────────────────────────
def test_two_jev_services_have_independent_windows_and_no_keys(tmp_path):
    client, _app = make_client(tmp_path)
    for service_id, model, tokens in (("jev-a", "d1-a", 1024), ("jev-b", "d1-b", 2048)):
        response = client.post("/admin/jev", json={
            "id": service_id, "base_url": "http://" + service_id + ".test",
            "model": model, "key_env": "JEV_TEST_API_KEY",
            "input_tokens": tokens, "context_tokens": 4096,
        })
        assert response.status_code == 201, response.text
        assert "api_key" not in response.text
    services = client.get("/admin/jev").json()["services"]
    assert [(s["id"], s["input_tokens"]) for s in services] == [
        ("jev-a", 1024), ("jev-b", 2048)]
    assert client.get("/admin/jev/window").status_code == 404


def test_create_get_update_delete_roundtrip(tmp_path):
    client, app = make_client(tmp_path)
    created = client.post("/admin/jev", json=service_payload(
        base_url="http://jev-a.test", api_key="stored-secret"))
    assert created.status_code == 201, created.text
    body = created.json()["service"]
    assert body["id"] == "jev-a"
    assert body["key_source"] == "stored"
    assert body["key_present"] is True
    assert "stored-secret" not in created.text
    assert body["instructions"] == DEFAULT_INSTRUCTIONS
    assert body["state_template"] == DEFAULT_STATE_TEMPLATE
    assert (body["input_tokens"], body["context_tokens"]) == (1024, 4096)

    fetched = client.get("/admin/jev/jev-a")
    assert fetched.status_code == 200
    assert fetched.json()["service"]["model"] == "d1-a"

    updated = client.put("/admin/jev/jev-a", json={
        "input_tokens": 2048, "context_tokens": 8192})
    assert updated.status_code == 200, updated.text
    assert (updated.json()["service"]["input_tokens"],
            updated.json()["service"]["context_tokens"]) == (2048, 8192)
    # 省略 api_key 且端点不变：密钥保留。
    assert updated.json()["service"]["key_source"] == "stored"
    conn = connect(app.state.settings.db_path)
    try:
        service = JevServiceRepo(conn).get("jev-a")
        assert JevServiceRepo(conn).stored_api_key(service) == "stored-secret"
    finally:
        conn.close()

    deleted = client.delete("/admin/jev/jev-a")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": "jev-a"}
    assert _services(app) == []
    assert client.get("/admin/jev/jev-a").status_code == 404
    assert client.delete("/admin/jev/jev-a").status_code == 404


def test_put_without_api_key_preserves_only_for_same_endpoint(tmp_path):
    client, app = make_client(tmp_path)
    client.post("/admin/jev", json=service_payload(api_key="stored-secret"))

    # 同端点省略 api_key → 保留。
    assert client.put("/admin/jev/jev-a", json={"timeout_seconds": 7}).status_code == 200
    conn = connect(app.state.settings.db_path)
    try:
        service = JevServiceRepo(conn).get("jev-a")
        assert JevServiceRepo(conn).stored_api_key(service) == "stored-secret"
    finally:
        conn.close()

    # 换端点省略 api_key → 旧密钥不得跟随到新地址。
    response = client.put("/admin/jev/jev-a", json={"base_url": "http://new-jev.test"})
    assert response.status_code == 200, response.text
    assert response.json()["service"]["key_source"] == "none"
    conn = connect(app.state.settings.db_path)
    try:
        service = JevServiceRepo(conn).get("jev-a")
        assert JevServiceRepo(conn).stored_api_key(service) == ""
    finally:
        conn.close()

    # api_key="" 显式清除。
    client.put("/admin/jev/jev-a", json={"api_key": "second-secret"})
    cleared = client.put("/admin/jev/jev-a", json={"api_key": ""})
    assert cleared.json()["service"]["key_source"] == "none"
    assert "second-secret" not in cleared.text


def test_key_source_env_when_environment_variable_is_present(tmp_path, monkeypatch):
    client, _app = make_client(tmp_path)
    response = client.post("/admin/jev", json=service_payload(key_env="JEV_TEST_API_KEY"))
    assert response.json()["service"]["key_source"] == "none"
    monkeypatch.setenv("JEV_TEST_API_KEY", "environment-secret")
    listed = client.get("/admin/jev").json()["services"]
    assert listed[0]["key_source"] == "env"
    assert listed[0]["key_present"] is True
    assert "environment-secret" not in client.get("/admin/jev").text


def test_duplicate_id_is_409(tmp_path):
    client, _app = make_client(tmp_path)
    assert client.post("/admin/jev", json=service_payload()).status_code == 201
    duplicate = client.post("/admin/jev", json=service_payload(model="other"))
    assert duplicate.status_code == 409, duplicate.text


def test_unknown_service_is_404(tmp_path):
    client, _app = make_client(tmp_path)
    assert client.get("/admin/jev/missing").status_code == 404
    assert client.put("/admin/jev/missing", json={"model": "m"}).status_code == 404
    assert client.delete("/admin/jev/missing").status_code == 404
    assert client.post("/admin/jev/missing/test", json={}).status_code == 404


def test_invalid_service_fields_are_422(tmp_path):
    client, _app = make_client(tmp_path)
    for payload in (
            service_payload(base_url="not-a-url"),
            service_payload(id="bad id!"),
            service_payload(key_env="1BAD"),
            service_payload(input_tokens=10),
            service_payload(context_tokens=10 ** 9),
            {**service_payload(), "unknown": 1},
            {**service_payload(), "api_key": 5},
            {**service_payload(), "model": None},
    ):
        response = client.post("/admin/jev", json=payload)
        assert response.status_code == 422, (payload, response.text)
    # PUT 的显式 null 不是「省略」。
    client.post("/admin/jev", json=service_payload())
    assert client.put("/admin/jev/jev-a", json={"api_key": None}).status_code == 422
    assert client.put("/admin/jev/jev-a", json={"id": "other"}).status_code == 422


def test_impact_confirmation_is_accepted_and_ignored(tmp_path):
    client, app = make_client(tmp_path)
    response = client.post("/admin/jev", json={
        **service_payload(), "impact_confirmation": {"acknowledged": True}})
    assert response.status_code == 201, response.text
    assert "impact_confirmation" not in response.text
    assert _services(app)[0].model == "d1-a"


# ── 连接测试：用数据库服务配置，不读活动画布 ──────────────────────────
def test_test_endpoint_calls_real_configured_http_service(tmp_path):
    requests = []
    client, _ = make_client(tmp_path, requests=requests)
    client.post("/admin/jev", json=service_payload(
        base_url="http://jev.test", model="NeoHorse-Jev-4B", timeout_seconds=5,
        instructions=DEFAULT_INSTRUCTIONS, state_template=DEFAULT_STATE_TEMPLATE))
    response = client.post("/admin/jev/jev-a/test", json={"text": "测试连接"})
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


def test_test_endpoint_uses_database_config_not_active_canvas(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "answers": {"route": {"type": "choice",
                                  "probabilities": {"候选 A": 0.4, "候选 B": 0.6}}},
            "usage": {"input_tokens": 3},
        })

    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    document = portable_tree_dict("portable")
    repo.create(tree_from_dict(document))
    repo.enable(document["id"])
    app = create_app(settings, transport=httpx.MockTransport(handler))
    client = TestClient(app)

    created = client.post("/admin/jev", json={
        "id": "db-jev", "base_url": "http://db-jev.test", "model": "db-model"})
    assert created.status_code == 201, created.text

    response = client.post("/admin/jev/db-jev/test", json={"text": "测试连接"})

    assert response.status_code == 200, response.text
    assert str(requests[0].url) == "http://db-jev.test/v1/systemone"
    assert json.loads(requests[0].content)["model"] == "db-model"


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
        client.post("/admin/jev", json=service_payload(base_url="http://jev.test"))
        response = client.post("/admin/jev/jev-a/test", json={"text": "测试连接"})
        assert response.status_code == 502, response.text
        assert "route" in response.json()["detail"]
    finally:
        asyncio.run(app.state.http.aclose())


def test_test_endpoint_validates_text(tmp_path):
    client, _app = make_client(tmp_path)
    client.post("/admin/jev", json=service_payload())
    assert client.post("/admin/jev/jev-a/test",
                       json={"text": "x" * 10001}).status_code == 422
    assert client.post("/admin/jev/jev-a/test", json={"text": 5}).status_code == 422
    assert client.post("/admin/jev/jev-a/test", json={"unknown": 1}).status_code == 422
