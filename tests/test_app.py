import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.routing.model import tree_from_dict
from router.store.db import connect, migrate
from router.store.repositories import seed_default
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import jev_dict, target_dict, tree_dict


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
    path = prepare_active_tree(tmp_path, tree_dict(root=target_dict("direct", ("qwen3-8b",))))
    app = create_app(Settings(db_path=path))
    assert app.state.snapshot.tree_id == "main"
    assert app.state.snapshot.tree.root.models[0].model == "qwen3-8b"


def test_startup_refuses_invalid_active_tree(tmp_path):
    raw = tree_dict()
    del raw["root"]["fallback"]
    path = prepare_active_tree(tmp_path, raw)
    with pytest.raises(RuntimeError, match="fallback_required"):
        create_app(Settings(db_path=path))


def test_startup_refuses_jev_tree_without_service_config(tmp_path):
    raw = tree_dict(root=jev_dict())
    raw["root"]["options"][0]["target"]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    raw["root"]["options"][1]["target"]["models"] = [{"model": "qwen3-32b", "reasoning": None}]
    raw["root"]["fallback"]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    path = prepare_active_tree(tmp_path, raw)
    with pytest.raises(RuntimeError, match="jev_unavailable"):
        create_app(Settings(db_path=path))


def test_environment_jev_config_is_seeded_once(tmp_path):
    path = str(tmp_path / "v3.db")
    settings = Settings(db_path=path, jev_url="http://jev.test", jev_model="NeoHorse-Jev-4B", jev_api_key="secret")
    app = create_app(settings)
    assert app.state.snapshot.jev.model == "NeoHorse-Jev-4B"
    assert app.state.snapshot.jev.api_key == "secret"
