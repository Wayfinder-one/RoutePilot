import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from tests.routing_fixtures import jev_dict, target_dict, tree_dict


@pytest.fixture
def app(tmp_path):
    def handler(request):
        if request.url.host == "jev.test":
            return httpx.Response(200, json={"answers": {"route": {"probabilities": {"轻量模型": 0.2, "增强模型": 0.8}}}, "usage": {"input_tokens": 20}})
        return httpx.Response(200, json={"choices": []})

    return create_app(Settings(db_path=str(tmp_path / "v3.db")), transport=httpx.MockTransport(handler))


@pytest.fixture
def client(app):
    return TestClient(app)


def direct_payload(tree_id="main", model="qwen3-8b"):
    return tree_dict(tree_id, root=target_dict("direct", (model,)))


def create_tree(client, raw):
    payload = {key: value for key, value in raw.items() if key != "enabled"}
    return client.post("/admin/trees", json=payload)


def test_tree_crud_copy_enable_disable(client, app):
    response = create_tree(client, direct_payload())
    assert response.status_code == 201, response.text
    assert response.json()["tree"]["enabled"] is False
    assert response.json()["tree"]["sticky"]["enabled"] is True
    assert create_tree(client, direct_payload()).status_code == 409
    assert client.put("/admin/trees/main", json={**direct_payload(), "enabled": True}).status_code == 422
    copied = client.post("/admin/trees/main/copy", json={"id": "copy", "name": "副本"})
    assert copied.status_code == 201
    assert copied.json()["tree"]["enabled"] is False
    enabled = client.post("/admin/trees/main/enable")
    assert enabled.status_code == 200, enabled.text
    assert app.state.snapshot.tree_id == "main"
    assert [tree["id"] for tree in enabled.json()["trees"] if tree["enabled"]] == ["main"]
    disabled = client.post("/admin/trees/main/disable")
    assert disabled.status_code == 200
    assert app.state.snapshot.tree is None


def test_enable_validates_fallback_and_model(client):
    raw = direct_payload()
    del raw["root"]["models"]
    assert create_tree(client, raw).status_code == 201
    response = client.post("/admin/trees/main/enable")
    assert response.status_code == 422
    assert any(issue["code"] == "target_models_required" for issue in response.json()["detail"])
    missing = direct_payload("missing", "no-such-model")
    assert create_tree(client, missing).status_code == 201
    assert client.post("/admin/trees/missing/enable").status_code == 422


def configure_jev(client):
    for model, capability in (("qwen3-8b", "简单通用请求"), ("qwen3-32b", "复杂推理和代码")):
        response = client.put(f"/admin/models/{model}", json={"profile": {"capability": capability}})
        assert response.status_code == 200, response.text
    response = client.put("/admin/jev", json={
        "base_url": "http://jev.test", "model": "NeoHorse-Jev-4B",
        "timeout_seconds": 5, "instructions": DEFAULT_INSTRUCTIONS,
        "state_template": DEFAULT_STATE_TEMPLATE,
    })
    assert response.status_code == 200, response.text


def test_jev_tree_enable_and_dry_run_try(client, app):
    configure_jev(client)
    raw = tree_dict()
    raw["root"]["options"][0]["target"]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    raw["root"]["options"][1]["target"]["models"] = [{"model": "qwen3-32b", "reasoning": None}]
    raw["root"]["fallback"]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    assert create_tree(client, raw).status_code == 201
    assert client.post("/admin/trees/main/enable").status_code == 200
    online_revision = app.state.snapshot.snapshot_revision
    response = client.post("/admin/try", json={"tree_id": "main", "text": "写一个复杂系统", "session_id": "try-session"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"]["model"] == "qwen3-32b"
    assert body["outcome"]["selection_reason"] == "jev"
    assert body["jev"]["selected_key"] == "strong"
    assert body["jev"]["p_top"] == 0.8
    assert app.state.snapshot.snapshot_revision == online_revision
    assert len(app.state.affinity) == 0
    assert app.state.balancer.inflight("qwen3-32b") == 0


def test_try_closed_tree_does_not_publish_it(client, app):
    assert create_tree(client, direct_payload()).status_code == 201
    response = client.post("/admin/try", json={"tree_id": "main", "text": "hello"})
    assert response.status_code == 200, response.text
    assert response.json()["outcome"]["model"] == "qwen3-8b"
    assert response.json()["outcome"]["selection_reason"] == "direct"
    assert app.state.snapshot.tree is None
