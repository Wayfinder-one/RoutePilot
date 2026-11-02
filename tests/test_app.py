import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.routing.graph.model import TargetNode
from router.routing.model import tree_from_dict
from router.store.db import connect, migrate
from router.store.repositories import seed_default
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import graph_dict, tree_dict


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
        "otherwise_target": "direct", "error_fallback": "direct",
    }


def test_health_console_and_empty_tree_boot(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "v3.db")))
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
    path = str(tmp_path / "v3.db")
    conn = connect(path)
    migrate(conn)
    seed_default(conn)
    repo = RoutingTreeRepo(conn)
    repo.create(tree_from_dict(raw))
    repo.enable("main")
    conn.close()
    return path


def test_startup_accepts_valid_direct_target(tmp_path):
    path = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = create_app(Settings(db_path=path))
    assert app.state.snapshot.tree_id == "main"
    targets = [node for node in app.state.snapshot.tree.root.nodes if isinstance(node, TargetNode)]
    assert [(node.id, [item.model for item in node.models]) for node in targets] == [
        ("direct", ["qwen3-8b"]),
    ]


def test_startup_refuses_invalid_active_tree(tmp_path):
    raw = tree_dict(root=direct_graph())
    del raw["root"]["error_fallback"]
    path = prepare_active_tree(tmp_path, raw)
    with pytest.raises(RuntimeError, match="fallback_missing"):
        create_app(Settings(db_path=path))


def test_startup_refuses_jev_tree_without_service_config(tmp_path):
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][2]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    path = prepare_active_tree(tmp_path, raw)
    with pytest.raises(RuntimeError, match="decision_service_unavailable"):
        create_app(Settings(db_path=path))


def test_environment_jev_config_is_seeded_once(tmp_path):
    path = str(tmp_path / "v3.db")
    settings = Settings(db_path=path, jev_url="http://jev.test", jev_model="NeoHorse-Jev-4B", jev_api_key="secret")
    app = create_app(settings)
    assert app.state.snapshot.jev.model == "NeoHorse-Jev-4B"
    assert app.state.snapshot.jev.api_key == "secret"
