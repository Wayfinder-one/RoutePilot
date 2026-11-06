import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.routes_jev import router
from router.jev.client import JevClient
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevConfigRepo
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import tree_dict, test_settings


def direct_graph():
    """start → 单 target(qwen3-8b)：不触碰决策服务的最小启用树。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def prepare_active_tree(tmp_path, document):
    """在临时 graph 目录写入并启用一棵树，供真实 create_app() 使用。"""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(document))
    repo.enable(document["id"])
    return settings


class Publisher:
    """SnapshotPublisher 桩：本文件其余用例只关心 locked/prepare/publish 次序。"""

    def __init__(self):
        self.prepared = []
        self.published = []

    @contextmanager
    def locked(self):
        yield self

    def prepare(self, conn, tree, fingerprint):
        prepared = (tree, fingerprint)
        self.prepared.append(prepared)
        return prepared

    def publish(self, prepared):
        self.published.append(prepared)


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
    app.state.graph_repo = RoutingTreeRepo(tmp_path / "routing-graph")
    app.state.graph_repo.initialize()
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
    assert len(app.state.publisher.published) == 3
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


def test_explicit_null_field_is_validated_not_treated_as_absent(tmp_path):
    """DTO 迁移不得把「显式 null」误当成「缺省继承」：null 仍走值校验 → 422。"""
    settings = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.put("/admin/jev", json=payload()).status_code == 200

        response = client.put("/admin/jev", json={"model": None})

        assert response.status_code == 422, response.text
        assert client.get("/admin/jev").json()["config"]["model"] == "NeoHorse-Jev-4B"


def test_failed_jev_save_changes_neither_db_audit_nor_snapshot(tmp_path):
    settings = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = create_app(settings)
    conn = connect(settings.db_path)
    conn.execute("DELETE FROM models WHERE id = 'qwen3-8b'")
    conn.commit()
    conn.close()
    before = app.state.snapshot.snapshot_revision
    with TestClient(app) as client:
        response = client.put("/admin/jev", json={
            "base_url": "http://jev.test", "model": "d1-2B",
            "instructions": "route", "state_template": "{request}",
        })
    assert response.status_code == 422
    conn = connect(settings.db_path)
    assert JevConfigRepo(conn).load() is None
    assert conn.execute("SELECT COUNT(*) FROM changes WHERE kind = 'jev'").fetchone()[0] == 0
    conn.close()
    assert app.state.snapshot.snapshot_revision == before
    assert app.state.snapshot.jev is None


def _graph_tree_files(settings):
    """图目录的全部文件字节：用于证明 publish 是纯内存替换（不触碰文件）。"""
    root = Path(settings.graph_dir)
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_jev_save_publishes_candidate_snapshot_exactly_once(tmp_path, monkeypatch):
    """Task 2 契约（Task 1 review carry-forward）：真实 SnapshotPublisher 上，
    ConfigurationService 写用例 = 提交恰在 publish 之前、发布恰好一次，且发布
    本身是纯内存替换（不写数据库、不写图文件）。"""
    settings = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = create_app(settings)
    publisher = app.state.publisher
    before = app.state.snapshot.snapshot_revision
    files_before = _graph_tree_files(settings)
    published = []
    real_publish = publisher.publish

    def counting_publish(prepared):
        # 发布前复核提交可见性：候选快照绝不能依赖尚未提交的写入。
        probe = connect(settings.db_path)
        try:
            assert JevConfigRepo(probe).load().model == "NeoHorse-Jev-4B"
        finally:
            probe.close()
        published.append(prepared)
        return real_publish(prepared)

    monkeypatch.setattr(publisher, "publish", counting_publish)

    with TestClient(app) as client:
        response = client.put("/admin/jev", json=payload())

    assert response.status_code == 200, response.text
    assert len(published) == 1
    assert app.state.snapshot.jev.model == "NeoHorse-Jev-4B"
    assert app.state.snapshot.snapshot_revision != before
    assert app.state.snapshot.tree_id == "main"          # 内存替换未丢活动树
    status = publisher.status()
    assert status["ready"] and status["stale"] is False
    repo = RoutingTreeRepo(settings.graph_dir)
    assert status["document_sha256"] == repo.fingerprint()[1]
    assert _graph_tree_files(settings) == files_before
