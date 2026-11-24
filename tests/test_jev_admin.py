"""管理面多 JEV 服务目录 API：CRUD、密钥通道、逐条连接测试。

覆盖 spec「多 JEV 目录」：列表/详情/更新/删除/测试都只读数据库目录，不读活动
画布；公共投影不含 api_key，`key_source` 是 stored|env|none；旧单配置端点已删除。
本文件的画布目录为空，因此目录写不发生同步（``synced_trees`` 恒为空）；带影响
画布的两阶段确认同步见 ``tests/test_catalog_impacts.py``。
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.routes_jev import router
from router.jev.client import JevClient
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.jev.window import MAX_TOKENS, MIN_CONTEXT_TOKENS, MIN_INPUT_TOKENS
from router.routing.snapshot import SnapshotPublisher
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import portable_tree_dict, test_settings


def _migrated(path):
    conn = connect(path)
    migrate(conn)
    conn.close()


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
    app.state.snapshot_set = None
    app.state.publisher = SnapshotPublisher(app, app.state.graph_repo)
    app.state.http = httpx.AsyncClient(transport=transport)
    app.state.jev_client = JevClient(app.state.http)
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
    assert updated.json()["synced_trees"] == []
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
    assert deleted.json() == {
        "deleted": "jev-a", "replacement_jev_id": None, "synced_trees": []}
    assert _services(app) == []
    assert client.get("/admin/jev/jev-a").status_code == 404
    assert client.delete("/admin/jev/jev-a").status_code == 404


def test_catalog_reads_never_echo_secret_from_post_or_put(tmp_path):
    """POST 新建与 PUT 轮换后的明文密钥都不得出现在任何目录读响应里。"""
    client, _app = make_client(tmp_path)
    created = client.post("/admin/jev", json=service_payload(api_key="post-secret"))
    assert created.status_code == 201, created.text
    assert "post-secret" not in client.get("/admin/jev").text
    assert "post-secret" not in client.get("/admin/jev/jev-a").text

    updated = client.put("/admin/jev/jev-a", json={"api_key": "put-secret"})
    assert updated.status_code == 200, updated.text
    listing = client.get("/admin/jev").text
    item = client.get("/admin/jev/jev-a").text
    assert "post-secret" not in listing + item
    assert "put-secret" not in listing + item


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
    client, app = make_client(tmp_path)
    assert client.get("/admin/jev/missing").status_code == 404
    assert client.put("/admin/jev/missing", json={"model": "m"}).status_code == 404
    assert client.delete("/admin/jev/missing").status_code == 404
    assert client.post("/admin/jev/missing/test", json={}).status_code == 404
    # 资源存在性先于 body 校验：缺服务时密钥类型 / id / 字段值都不能抢先报 422。
    for body in ({"api_key": 5}, {"api_key": None}, {"id": "other"},
                 {"base_url": "not-a-url"}, {"model": ""}):
        response = client.put("/admin/jev/missing", json=body)
        assert response.status_code == 404, (body, response.text)
        assert "unknown jev service missing" in response.json()["detail"]
    assert _services(app) == []


def test_removed_singleton_endpoints_are_405(tmp_path):
    """旧单配置入口已删且不能静默回退：路径命中但方法不存在 = 405。"""
    client, _app = make_client(tmp_path)
    assert client.put("/admin/jev", json=service_payload()).status_code == 405
    assert client.post("/admin/jev/test", json={"text": "测试"}).status_code == 405
    # 独立窗口入口没有对应资源路径，保持 404。
    assert client.get("/admin/jev/window").status_code == 404


def _detail_text(response):
    """422 的 detail 既可能是值校验字符串，也可能是 Pydantic 错误项列表。"""
    detail = response.json()["detail"]
    if isinstance(detail, str):
        return detail
    return " ".join(f"{item.get('loc')} {item.get('msg')}" for item in detail)


#: 非法字段矩阵：同一组值同时驱动 POST（完整 payload）与 PUT（局部 body），
#: 期望片段来自端点背后的真实校验（JevService/JevSettings/JevWindow 的
#: ValueError、``extra="forbid"`` 与 ``_validate_api_key``）。
INVALID_FIELD_CASES = [
    pytest.param("base_url", "http://jev.test?x=1", "base_url", id="base-url-query"),
    pytest.param("base_url", "http://jev.test#frag", "base_url", id="base-url-fragment"),
    pytest.param("base_url", 123, "base_url", id="base-url-non-string"),
    pytest.param("model", "", "model", id="model-empty"),
    pytest.param("model", "   ", "model", id="model-blank"),
    pytest.param("model", 5, "model", id="model-non-string"),
    pytest.param("key_env", "bad-env", "key_env", id="key-env-dash"),
    pytest.param("key_env", "1ABC", "key_env", id="key-env-leading-digit"),
    pytest.param("timeout_seconds", 0, "timeout_seconds", id="timeout-zero"),
    pytest.param("timeout_seconds", 121, "timeout_seconds", id="timeout-above-max"),
    pytest.param("timeout_seconds", "abc", "timeout_seconds", id="timeout-non-numeric"),
    pytest.param("instructions", " ", "instructions", id="instructions-blank"),
    pytest.param("instructions", 5, "instructions", id="instructions-non-string"),
    pytest.param("state_template", "没有占位符", "state_template",
                 id="state-template-no-request"),
    pytest.param("state_template", "{unknown}", "state_template",
                 id="state-template-wrong-field"),
    pytest.param("state_template", "{request", "state_template",
                 id="state-template-unbalanced"),
    pytest.param("state_template", 5, "state_template",
                 id="state-template-non-string"),
    pytest.param("input_tokens", 8192, "input_tokens", id="input-above-context"),
    pytest.param("input_tokens", True, "input_tokens", id="input-bool"),
    pytest.param("input_tokens", "2048", "input_tokens", id="input-string"),
    pytest.param("input_tokens", MIN_INPUT_TOKENS - 1, "input_tokens",
                 id="input-below-min"),
    pytest.param("input_tokens", 10 ** 9, "input_tokens", id="input-above-max"),
    pytest.param("context_tokens", MIN_CONTEXT_TOKENS - 1, "context_tokens",
                 id="context-below-min"),
    pytest.param("context_tokens", MAX_TOKENS + 1, "context_tokens",
                 id="context-above-max"),
    pytest.param("context_tokens", True, "context_tokens", id="context-bool"),
    pytest.param("context_tokens", "4096", "context_tokens", id="context-string"),
    pytest.param("api_key", 5, "api_key", id="api-key-non-string"),
    pytest.param("unknown", 1, "Extra inputs are not permitted", id="unknown-field"),
]


@pytest.mark.parametrize(("field", "value", "fragment"), INVALID_FIELD_CASES)
def test_post_rejects_invalid_field_and_catalog_stays_empty(tmp_path, field, value, fragment):
    client, app = make_client(tmp_path)
    response = client.post("/admin/jev", json=service_payload(**{field: value}))
    assert response.status_code == 422, response.text
    assert fragment in _detail_text(response), response.text
    assert client.get("/admin/jev").json() == {"services": []}
    assert _services(app) == []


@pytest.mark.parametrize(("field", "value", "fragment"), INVALID_FIELD_CASES)
def test_put_rejects_invalid_field_and_catalog_is_unchanged(
        tmp_path, field, value, fragment):
    client, app = make_client(tmp_path)
    created = client.post("/admin/jev", json=service_payload(api_key="stored-secret"))
    assert created.status_code == 201, created.text
    baseline = _services(app)
    response = client.put("/admin/jev/jev-a", json={field: value})
    assert response.status_code == 422, response.text
    assert fragment in _detail_text(response), response.text
    assert _services(app) == baseline
    conn = connect(app.state.settings.db_path)
    try:
        assert JevServiceRepo(conn).stored_api_key(baseline[0]) == "stored-secret"
    finally:
        conn.close()


@pytest.mark.parametrize("service_id", ["-bad", "a b", "", "bad id!"])
def test_post_rejects_invalid_id_and_catalog_stays_empty(tmp_path, service_id):
    client, app = make_client(tmp_path)
    response = client.post("/admin/jev", json=service_payload(id=service_id))
    assert response.status_code == 422, response.text
    assert "invalid JEV service id" in _detail_text(response)
    assert _services(app) == []


@pytest.mark.parametrize("body", [
    {"id": "-bad"}, {"id": "a b"}, {"id": ""}, {"id": "other"}, {"id": None},
])
def test_put_rejects_id_that_does_not_match_path(tmp_path, body):
    client, app = make_client(tmp_path)
    assert client.post("/admin/jev", json=service_payload()).status_code == 201
    baseline = _services(app)
    response = client.put("/admin/jev/jev-a", json=body)
    assert response.status_code == 422, response.text
    assert "id in body must match path" in _detail_text(response)
    assert _services(app) == baseline


def test_impact_confirmation_is_irrelevant_without_affected_canvases(tmp_path):
    """无受影响画布时不需要确认：POST 忽略它，PUT/DELETE 也不因它报错。

    确认协议的功能性行为（预览 409 / 确认同步）由
    ``tests/test_catalog_impacts.py`` 的双画布用例覆盖。
    """
    client, app = make_client(tmp_path)
    response = client.post("/admin/jev", json={
        **service_payload(), "impact_confirmation": {"acknowledged": True}})
    assert response.status_code == 201, response.text
    assert "impact_confirmation" not in response.text
    assert _services(app)[0].model == "d1-a"

    updated = client.put("/admin/jev/jev-a", json={
        "model": "d1-a2", "impact_confirmation": {"trees": []}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["synced_trees"] == []
    assert updated.json()["service"]["model"] == "d1-a2"

    deleted = client.request("DELETE", "/admin/jev/jev-a", json={
        "impact_confirmation": {"trees": []}})
    assert deleted.status_code == 200, deleted.text
    assert _services(app) == []


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
    repo.create(tree_from_dict({**document, "enabled": True, "default": True}))
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
