import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.service import JevService
from router.routing.document import graph_document_bytes, parse_graph_document
from router.routing.graph.model import TargetNode
from router.routing.graph.validate import validate_graph_for_enable, validate_graph_structure
from router.control.runtime import enable_context_for_runtime
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.repositories import SettingsRepo, seed_default
from router.store.routing_trees import RoutingTreeRepo
from router.upstream.runtime_keys import runtime_model_key, tree_scope
from tests.routing_fixtures import (
    graph_dict,
    portable_tree_dict,
    test_settings,
    tree_dict,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JEV_ENV = {"jev_url": "http://jev.test", "jev_model": "NeoHorse-Jev-4B"}


def direct_graph(models=("qwen3-8b",)):
    """start → target 的直连图（无问题节点，不需要决策服务）。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
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
    assert app.state.snapshot_set.snapshots == {}
    assert app.state.snapshot_set.default_snapshot is None
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
    repo.create(tree_from_dict({**raw, "enabled": True, "default": True}))
    return settings


def test_missing_snapshot_is_not_ready_and_disabled_snapshot_is_ready(tmp_path):
    """损坏的启用文档 -> 启动快照不可用；改为关闭态文档 -> 空快照就绪。"""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    path = repo.graphs / "main.json"
    path.write_bytes(b"{broken")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        assert client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
        }).status_code == 503
        path.write_bytes(graph_document_bytes(tree_from_dict(tree_dict())))
        asyncio.run(app.state.graph_reloader.poll_once())
        assert client.get("/readyz").status_code == 200


def test_startup_accepts_valid_direct_target(tmp_path):
    settings = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = create_app(settings)
    assert app.state.snapshot_set.default_snapshot.tree_id == "main"
    targets = [node for node in app.state.snapshot_set.default_snapshot.tree.root.nodes if isinstance(node, TargetNode)]
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
    raw["jev"] = None  # 画布显式声明“不使用 JEV”：问题节点必须被启用校验拒绝
    settings = prepare_active_tree(tmp_path, raw)
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert "decision_service_unavailable" in ready.json()["last_load_error"]


def test_environment_jev_service_is_seeded_once(tmp_path):
    """空目录首次启动：环境 URL/model/api_key 创建 ``default`` 服务。"""
    settings = test_settings(tmp_path, jev_url="http://jev.test", jev_model="NeoHorse-Jev-4B", jev_api_key="secret")
    app = create_app(settings)
    # 无启用图：路由集为空，种子只写数据库目录，不进入运行快照。
    assert app.state.snapshot_set.snapshots == {}
    assert app.state.snapshot_set.default_snapshot is None
    conn = connect(settings.db_path)
    try:
        services = JevServiceRepo(conn).list()
        assert [(service.id, service.model) for service in services] == [
            ("default", "NeoHorse-Jev-4B")]
        assert JevServiceRepo(conn).stored_api_key(services[0]) == "secret"
    finally:
        conn.close()

    # 第二次启动不重复种子/不覆盖管理结果。
    create_app(settings)
    conn = connect(settings.db_path)
    try:
        assert len(JevServiceRepo(conn).list()) == 1
    finally:
        conn.close()


def test_first_boot_without_environment_seeds_when_environment_arrives_later(tmp_path):
    """首启无环境：不写标记；之后带环境启动仍会播种一次。"""
    settings = test_settings(tmp_path)
    create_app(settings)
    conn = connect(settings.db_path)
    try:
        assert JevServiceRepo(conn).list() == []
        assert SettingsRepo(conn).get("jev_catalog_seeded") is None
    finally:
        conn.close()

    create_app(replace(settings, **JEV_ENV))

    conn = connect(settings.db_path)
    try:
        assert [service.id for service in JevServiceRepo(conn).list()] == ["default"]
        assert SettingsRepo(conn).get("jev_catalog_seeded") == "1"
    finally:
        conn.close()


def test_deleting_all_jev_services_never_resurrects_the_environment_seed(tmp_path):
    """环境种子只发生一次：全部删除后重启也不会把 ``default`` 复活。"""
    settings = test_settings(tmp_path, **JEV_ENV)
    create_app(settings)
    conn = connect(settings.db_path)
    try:
        assert [service.id for service in JevServiceRepo(conn).list()] == ["default"]
        assert JevServiceRepo(conn).delete("default") is True
        assert JevServiceRepo(conn).list() == []
    finally:
        conn.close()

    create_app(settings)  # 目录为空但标记仍在 → 不再播种

    conn = connect(settings.db_path)
    try:
        assert JevServiceRepo(conn).list() == []
    finally:
        conn.close()


def test_partial_environment_jev_is_a_configuration_error(tmp_path):
    """URL/model 只配一半仍是启动配置错误（保持原语义）。"""
    with pytest.raises(RuntimeError, match="configured together"):
        create_app(test_settings(tmp_path, jev_url="http://jev.test"))


def test_environment_seed_never_overwrites_an_existing_catalog(tmp_path):
    settings = test_settings(tmp_path, **JEV_ENV)
    conn = connect(settings.db_path)
    migrate(conn)
    JevServiceRepo(conn).save(JevService("custom", "http://custom.test", "custom-model"))
    conn.close()

    app = create_app(settings)

    conn = connect(settings.db_path)
    try:
        assert [service.id for service in JevServiceRepo(conn).list()] == ["custom"]
    finally:
        conn.close()
    # 目录没有被环境种子覆盖；空路由集不读取目录。
    assert app.state.snapshot_set.snapshots == {}


def test_embedded_runtime_jev_wins_over_environment_seed(tmp_path):
    """环境种子只写目录；活动图内嵌 JEV 仍是运行时事实源。"""
    settings = test_settings(tmp_path, **JEV_ENV)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    document = portable_tree_dict("main")
    repo.create(tree_from_dict({**document, "enabled": True, "default": True}))

    app = create_app(settings)

    assert app.state.snapshot_set.default_snapshot.jev.base_url == "http://jev-json.test"
    assert app.state.snapshot_set.default_snapshot.jev.model == "json-jev"
    conn = connect(settings.db_path)
    try:
        services = JevServiceRepo(conn).list()
        assert [(service.id, service.model) for service in services] == [
            ("default", "NeoHorse-Jev-4B")]
    finally:
        conn.close()
    stored = app.state.graph_repo.get("main")
    assert stored.runtime.jev.model == "json-jev"


def shipped_document(name):
    """读取并严格解析随仓库发布的图文档（不改写任何字段）。"""
    content = (REPO_ROOT / "configs" / name).read_bytes()
    return parse_graph_document(
        content,
        tree_id="default-jev",
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_shipped_configs_are_v3_export_documents():
    for name in ("default_routing_tree.json", "production_routing_tree.json"):
        assert validate_graph_structure(shipped_document(name)) == [], name


def test_shipped_graph_configs_pass_enable_validation_with_own_runtime():
    """发布配置必须自包含通过启用校验：模型/provider/时段/JEV 都来自文档自身。"""
    for name in ("default_routing_tree.json", "production_routing_tree.json"):
        tree = shipped_document(name)
        assert validate_graph_for_enable(
            tree, enable_context_for_runtime(tree.runtime)) == [], name


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
    # /admin/try 是 Auto 图路径：熔断键必须带 try 快照的图作用域。
    key = runtime_model_key(tree_scope("main"), "qwen3-8b")
    for _ in range(app.state.breaker.threshold):
        app.state.breaker.failure(key)
    assert app.state.breaker.is_open("qwen3-8b") is False
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"tree_id": "main", "text": "你好"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "target 'safe' has no available backend"
    assert handler.jev_paths == ["/v1/systemone"]
