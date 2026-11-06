import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.control.validation import build_enable_context
from router.routing.document import parse_graph_document
from router.routing.graph.model import TargetNode
from router.routing.graph.validate import validate_graph_for_enable, validate_graph_structure
from router.routing.model import tree_from_dict
from router.store.db import connect, migrate
from router.store.repositories import seed_default
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import graph_dict, test_settings, tree_dict

REPO_ROOT = Path(__file__).resolve().parent.parent
JEV_ENV = {"jev_url": "http://jev.test", "jev_model": "NeoHorse-Jev-4B"}


def direct_graph(models=("qwen3-8b",)):
    """start → target 的直连图（无问题节点，不需要决策服务）。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
            {"id": "direct", "type": "target", "priority": 1,
             "position": {"x": 200, "y": 0},
             "models": [{"model": model, "reasoning": None} for model in models]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def test_health_console_and_empty_tree_boot(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/console/index.html").status_code == 200
    assert app.state.snapshot.tree is None
    assert client.get("/admin/trees").json()["trees"] == []
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "no_active_tree"
    assert not hasattr(app.state, "tagger")


def prepare_active_tree(tmp_path, raw):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_default(conn)
    conn.close()
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(raw))
    repo.enable("main")
    return settings


def test_missing_snapshot_is_not_ready_and_disabled_snapshot_is_ready(tmp_path):
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    (repo.root / "current").write_text("missing\n", encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        assert client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
        }).status_code == 503
        (repo.root / "current").write_bytes(b"")
        asyncio.run(app.state.graph_reloader.poll_once())
        assert client.get("/readyz").status_code == 200


def test_startup_accepts_valid_direct_target(tmp_path):
    settings = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = create_app(settings)
    assert app.state.snapshot.tree_id == "main"
    targets = [node for node in app.state.snapshot.tree.root.nodes if isinstance(node, TargetNode)]
    assert [(node.id, [item.model for item in node.models]) for node in targets] == [
        ("direct", ["qwen3-8b"]),
    ]


def test_startup_with_invalid_active_tree_is_not_ready(tmp_path):
    raw = tree_dict(root=direct_graph())
    del raw["root"]["error_fallback"]
    settings = prepare_active_tree(tmp_path, raw)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_valid_snapshot"


def test_graph_tree_with_questions_without_service_config_is_not_ready(tmp_path):
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][2]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    settings = prepare_active_tree(tmp_path, raw)
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert "decision_service_unavailable" in ready.json()["last_load_error"]


def test_environment_jev_config_is_seeded_once(tmp_path):
    settings = test_settings(tmp_path, jev_url="http://jev.test", jev_model="NeoHorse-Jev-4B", jev_api_key="secret")
    app = create_app(settings)
    assert app.state.snapshot.jev.model == "NeoHorse-Jev-4B"
    assert app.state.snapshot.jev.api_key == "secret"


def shipped_graph(name):
    content = (REPO_ROOT / "configs" / name).read_bytes()
    return parse_graph_document(
        content,
        tree_id="default-jev",
        enabled=False,
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_shipped_configs_are_v3_export_documents():
    for name in ("default_routing_tree.json", "production_routing_tree.json"):
        assert validate_graph_structure(shipped_graph(name)) == [], name


def test_shipped_graph_configs_pass_enable_validation_with_seeded_catalog(tmp_path):
    """钉住发布配置引用的模型 id：seed 目录里改名/删除模型会在这里失败，而不只是结构通过。"""
    conn = connect(str(tmp_path / "shipped.db"))
    migrate(conn)
    seed_default(conn)
    try:
        context = replace(build_enable_context(conn), jev_available=True)  # JEV 打桩：只校验配置存在性
    finally:
        conn.close()
    for name in ("default_routing_tree.json", "production_routing_tree.json"):
        assert validate_graph_for_enable(shipped_graph(name), context) == [], name


class TryHandler:
    """MockTransport 分流：jev.test 回答图问题，其余请求落到假 OpenAI 上游。"""

    def __init__(self):
        self.jev_paths = []

    def __call__(self, request):
        if request.url.host == "jev.test":
            self.jev_paths.append(request.url.path)
            return httpx.Response(200, json={
                "answers": {"q": {"type": "choice", "probabilities": {"轻量": 0.85, "增强": 0.15}}},
                "usage": {"input_tokens": 10},
            })
        return httpx.Response(200, json={"choices": [], "usage": {}})


def question_graph(model="qwen3-8b"):
    """start → choice(轻量/增强) → safe target；target 引用已登记的默认模型。"""
    raw = graph_dict()
    for node in raw["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": model, "reasoning": None}]
    return raw


def composed_try_app(tmp_path, handler):
    """真实组合根：engine/jev_client/breaker 全部由 create_app 构造，测试不注入 engine。"""
    settings = prepare_active_tree(tmp_path, tree_dict(root=question_graph()))
    return create_app(replace(settings, **JEV_ENV), transport=httpx.MockTransport(handler))


def test_try_through_app_composition_routes_graph(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"tree_id": "main", "text": "你好"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is False
    assert handler.jev_paths == ["/v1/systemone"]
    assert [(item["target_id"], item["priority"], item["wave"]) for item in body["outcome"]["candidates"]] == [
        ("safe", 1, 1),
    ]
    assert body["outcome"]["waves"][0]["questions"] == ["q"]
    assert body["answers"]["q"]["ranking"] == ["easy", "strong"]


def test_try_through_app_composition_target_without_backend_is_503(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    for _ in range(app.state.breaker.threshold):
        app.state.breaker.failure("qwen3-8b")
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"tree_id": "main", "text": "你好"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "target 'safe' has no available backend"
    assert handler.jev_paths == ["/v1/systemone"]
