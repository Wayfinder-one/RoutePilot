"""`/admin/trees` graph CRUD：保存载荷、校验分档、enable/copy/disable 语义（Task 7）。"""
from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import graph_dict, huge_chain_graph, test_settings, tree_dict


@pytest.fixture
def app(tmp_path):
    return create_app(
        test_settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"answers": {}})),
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def create_tree(client, raw):
    payload = {key: value for key, value in raw.items() if key != "enabled"}
    return client.post("/admin/trees", json=payload)


def save_tree(client, payload, tree_id="main"):
    current = client.get(f"/admin/trees/{tree_id}").json()["tree"]
    body = dict(payload)
    body["base_document_sha256"] = current["document_sha256"]
    return client.put(f"/admin/trees/{tree_id}", json=body)


def graph_with_model(model="qwen3-8b"):
    root = graph_dict()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": model, "reasoning": None}]
    return root


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


def test_stale_browser_save_returns_409_and_keeps_file(client):
    create_payload = tree_dict()
    create_payload.pop("enabled")
    created = client.post("/admin/trees", json=create_payload).json()["tree"]
    payload = tree_dict()
    payload.pop("enabled")
    payload["base_document_sha256"] = created["document_sha256"]
    payload["name"] = "第一次修改"
    assert client.put("/admin/trees/main", json=payload).status_code == 200

    payload["name"] = "过期修改"
    conflict = client.put("/admin/trees/main", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "graph_conflict"
    assert client.get("/admin/trees/main").json()["tree"]["name"] == "第一次修改"


def test_graph_crud_roundtrip_copy_enable_disable(client, app):
    raw = tree_dict(sticky={"enabled": True, "ttl_seconds": 1800})
    raw["root"] = graph_with_model()
    raw["root"]["nodes"][0]["title"] = "请求入口"
    raw["root"]["nodes"][0]["position"] = {"x": 40, "y": 60}
    target = next(node for node in raw["root"]["nodes"] if node["type"] == "target")
    target["capability"] = "展示说明"

    response = create_tree(client, raw)
    assert response.status_code == 201, response.text
    created = response.json()["tree"]
    assert created["enabled"] is False
    assert created["sticky"] == {"enabled": True, "ttl_seconds": 1800}
    assert create_tree(client, raw).status_code == 409

    saved = client.get("/admin/trees/main").json()["tree"]
    assert saved["root"]["nodes"][0]["position"] == {"x": 40, "y": 60}
    assert saved["root"]["nodes"][0]["title"] == "请求入口"
    saved_target = next(node for node in saved["root"]["nodes"] if node["type"] == "target")
    assert saved_target["capability"] == "展示说明"
    assert saved["root"]["nodes"][1]["position"] == {"x": 200, "y": 0}

    updated_root = copy.deepcopy(saved["root"])
    updated_root["nodes"][1]["instructions"] = "重新选择能力"
    updated_root["nodes"][-1]["position"] = {"x": 400, "y": 200}
    response = save_tree(client, {"name": "改名", "root": updated_root})
    assert response.status_code == 200, response.text
    saved = client.get("/admin/trees/main").json()["tree"]
    assert saved["name"] == "改名"
    assert saved["root"]["nodes"][1]["instructions"] == "重新选择能力"
    assert saved["root"]["nodes"][-1]["position"] == {"x": 400, "y": 200}
    assert client.put("/admin/trees/main", json={"enabled": True}).status_code == 422

    copied = client.post("/admin/trees/main/copy", json={"id": "copy", "name": "副本"})
    assert copied.status_code == 201, copied.text
    copied_tree = copied.json()["tree"]
    assert copied_tree["id"] == "copy"
    assert copied_tree["name"] == "副本"
    assert copied_tree["enabled"] is False
    assert copied_tree["root"] == saved["root"]

    configure_jev(client)
    enabled = client.post("/admin/trees/main/enable")
    assert enabled.status_code == 200, enabled.text
    assert app.state.snapshot.tree_id == "main"
    assert [tree["id"] for tree in enabled.json()["trees"] if tree["enabled"]] == ["main"]
    assert enabled.json()["snapshot_revision"] == app.state.snapshot.snapshot_revision

    disabled = client.post("/admin/trees/main/disable")
    assert disabled.status_code == 200, disabled.text
    assert app.state.snapshot.tree is None
    assert not any(tree["enabled"] for tree in disabled.json()["trees"])


def test_closed_save_skips_enable_validation_but_enabled_save_uses_it(client, app):
    configure_jev(client)
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201

    # 关闭状态：只做结构校验，未知模型可保存
    response = save_tree(client, {"root": graph_with_model("ghost")})
    assert response.status_code == 200, response.text
    response = client.post("/admin/trees/main/enable")
    assert response.status_code == 422
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])

    # 空 target.models 可保存，但 enable 拒绝
    root = graph_with_model()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []
    assert save_tree(client, {"root": root}).status_code == 200
    response = client.post("/admin/trees/main/enable")
    assert response.status_code == 422
    codes = {issue["code"] for issue in response.json()["detail"]}
    assert "target_models_required" in codes

    assert save_tree(client, {"root": graph_with_model()}).status_code == 200
    assert client.post("/admin/trees/main/enable").status_code == 200
    published = app.state.snapshot.snapshot_revision

    # 已启用组保存：按启用校验（未知模型 / 空模型 / 结构错误均 422）
    response = save_tree(client, {"root": graph_with_model("ghost")})
    assert response.status_code == 422
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published

    root = graph_with_model()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []
    response = save_tree(client, {"root": root})
    assert response.status_code == 422
    assert any(issue["code"] == "target_models_required" for issue in response.json()["detail"])

    root = graph_with_model()
    root["nodes"] = [node for node in root["nodes"] if node["type"] != "start"]
    response = save_tree(client, {"root": root})
    assert response.status_code == 422
    assert any(issue["code"] == "start_count" for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published

    assert client.post("/admin/trees/main/disable").status_code == 200


ISSUE_KEYS = {"code", "zh", "en", "node_id", "edge_id", "port", "level"}


def test_parse_failures_are_issue_arrays(client):
    response = create_tree(client, {
        "id": "bad", "name": "", "root": {
            "id": "root", "type": "graph",
            "nodes": [{"id": "n", "type": "bogus"}], "edges": [],
        },
    })
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert issue["level"] == "hard"
    assert issue["zh"] and issue["en"]
    assert client.get("/admin/trees").json()["trees"] == []

    response = create_tree(client, {"id": "old", "name": "", "root": {"id": "x", "type": "jev"}})
    assert response.status_code == 422
    assert response.json()["detail"][0]["code"] == "graph_parse_error"
    assert client.get("/admin/trees").json()["trees"] == []


def test_structure_failures_are_issue_arrays_with_locators(client):
    root = graph_with_model()
    root["nodes"].append({"id": "start-2", "type": "start", "position": {"x": 0, "y": 0}})
    response = create_tree(client, tree_dict(root=root))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert all(set(issue) == ISSUE_KEYS for issue in detail)
    assert any(issue["code"] == "start_count" for issue in detail)

    root = graph_with_model()
    root["edges"].append({
        "id": "e-ghost", "source": "start", "source_port": "out",
        "target": "ghost", "target_port": "in_1", "condition": None,
    })
    response = create_tree(client, tree_dict(root=root))
    assert response.status_code == 422, response.text
    issue = next(item for item in response.json()["detail"]
                 if item["code"] == "edge_endpoint_missing")
    assert issue["node_id"] == "ghost"
    assert issue["edge_id"] == "e-ghost"
    assert issue["port"] == "in_1"


def test_huge_chain_payload_is_422_on_create_and_update(client):
    """终审 Critical 回归：POST/PUT 1200 节点链必须是 422 + Issue 数组，绝不 500。"""
    response = create_tree(client, tree_dict("huge", root=huge_chain_graph()))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all(set(issue) == ISSUE_KEYS for issue in detail)
    assert any(issue["code"] == "limit_exceeded" for issue in detail)
    assert client.get("/admin/trees").json()["trees"] == []

    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    response = save_tree(client, {"root": huge_chain_graph()})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "limit_exceeded" for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"]["root"]["nodes"][0]["id"] == "start"


def test_invalid_timezone_rejected_on_create_and_update(client):
    raw = tree_dict(root=graph_with_model())
    raw["timezone"] = "Mars/Olympus"
    response = create_tree(client, raw)
    assert response.status_code == 422, response.text
    issue = next(item for item in response.json()["detail"] if item["code"] == "invalid_timezone")
    assert set(issue) == ISSUE_KEYS
    assert client.get("/admin/trees").json()["trees"] == []

    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    response = save_tree(client, {"timezone": "Mars/Olympus"})
    assert response.status_code == 422
    assert any(issue["code"] == "invalid_timezone" for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"]["timezone"] == "Asia/Shanghai"


def test_enable_switches_single_active_tree(client, app):
    configure_jev(client)
    for tree_id in ("a", "b"):
        assert create_tree(client, tree_dict(tree_id, root=graph_with_model())).status_code == 201

    assert client.post("/admin/trees/a/enable").status_code == 200
    assert app.state.snapshot.tree_id == "a"
    response = client.post("/admin/trees/b/enable")
    assert response.status_code == 200, response.text
    assert app.state.snapshot.tree_id == "b"
    assert [tree["id"] for tree in response.json()["trees"] if tree["enabled"]] == ["b"]
    assert client.get("/admin/trees/a").json()["tree"]["enabled"] is False

    assert client.post("/admin/trees/b/disable").status_code == 200
    assert app.state.snapshot.tree is None


def test_enable_publishes_candidate_snapshot_exactly_once(app, client, monkeypatch):
    """Task 2 契约（Task 1 review carry-forward）：启用图必须且只能发布**一次**
    候选快照 —— 走真实 SnapshotPublisher，禁止提交后再 refresh 的第二次发布。"""
    configure_jev(client)
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    publisher = app.state.publisher
    before_revision = app.state.snapshot.snapshot_revision
    published = []
    real_publish = publisher.publish

    def counting_publish(prepared):
        published.append(prepared)
        return real_publish(prepared)

    monkeypatch.setattr(publisher, "publish", counting_publish)

    response = client.post("/admin/trees/main/enable")

    assert response.status_code == 200, response.text
    assert len(published) == 1
    assert app.state.snapshot.tree_id == "main"
    assert app.state.snapshot.snapshot_revision != before_revision
    assert response.json()["snapshot_revision"] == app.state.snapshot.snapshot_revision
    status = publisher.status()
    assert status["active_id"] == "main" and status["stale"] is False
    # 候选快照指纹与文件一致：发布内容 = 刚写入的文档（内存替换，无文件操作）
    repo = RoutingTreeRepo(app.state.settings.graph_dir)
    assert status["document_sha256"] == repo.fingerprint()[1]


def test_tree_dtos_reject_unknown_fields(client):
    """Task 2 Step 3：graph 写用例的 DTO 同样 ``extra="forbid"``。

    旧实现用 ``dict`` body 并在路由里挑字段，未知字段被静默忽略（dry-run 甚至
    照常返回 200）；现在必须在进入 GraphService 之前 422。"""
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    cases = [
        ("post", "/admin/trees", {"id": "extra-tree", "name": "X", "root": graph_with_model()}),
        ("put", "/admin/trees/main", {"name": "X"}),
        ("post", "/admin/trees/main/copy", {"id": "copy-x", "name": "X"}),
        ("post", "/admin/try", {"root": graph_with_model(), "text": "hi"}),
    ]
    for method, path, payload in cases:
        response = getattr(client, method)(path, json={**payload, "bogus": 1})
        assert response.status_code == 422, (path, response.text)
        assert any(item["type"] == "extra_forbidden"
                   for item in response.json()["detail"]), path
    assert client.get("/admin/trees/extra-tree").status_code == 404
