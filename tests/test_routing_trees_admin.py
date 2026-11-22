"""`/admin/trees` graph CRUD：保存载荷、校验分档、enable/copy/disable 语义（Task 7）。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.routing.tree import tree_from_dict
from router.store.db import connect
from router.store.repositories import ProviderRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import graph_dict, huge_chain_graph, runtime_dict, test_settings, tree_dict


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


def create_portable_tree(client, tree_id):
    raw = tree_dict(tree_id, root=graph_with_model("fast"))
    raw.update(runtime_dict())
    response = create_tree(client, raw)
    assert response.status_code == 201, response.text


def test_configuration_writes_update_every_graph_and_gets_prefer_active_json(client, app):
    create_portable_tree(client, "main")
    create_portable_tree(client, "second")
    assert client.post("/admin/trees/main/enable").status_code == 200

    conn = connect(app.state.settings.db_path)
    try:
        ProviderRepo(conn).upsert("local", "http://database-only.test/v1")
    finally:
        conn.close()
    providers = client.get("/admin/providers")
    assert providers.status_code == 200, providers.text
    assert providers.json()["providers"][0]["base_url"] == "http://json-only.test/v1"

    response = client.put("/admin/providers/local", json={
        "base_url": "http://changed.test/v1",
        "key_env": "CHANGED_API_KEY",
        "reasoning_format": "openai",
    })
    assert response.status_code == 200, response.text
    for tree_id in ("main", "second"):
        tree = app.state.graph_repo.get(tree_id)
        assert tree.runtime.backends.providers[0]["base_url"] == "http://changed.test/v1"
    assert app.state.snapshot.backends["fast"].url == "http://changed.test/v1/chat/completions"

    response = client.put("/admin/jev/window", json={
        "input_tokens": 2048, "context_tokens": 8192,
    })
    assert response.status_code == 200, response.text
    response = client.put("/admin/time-periods", json={"periods": [
        {"name": "JSON 新时段", "days": [0], "start": "08:00", "end": "17:00"},
    ]})
    assert response.status_code == 200, response.text
    for tree_id in ("main", "second"):
        runtime = app.state.graph_repo.get(tree_id).runtime
        assert runtime.jev_input_tokens == 2048
        assert runtime.jev_context_tokens == 8192
        assert [period.name for period in runtime.time_periods] == ["JSON 新时段"]


def test_model_edit_uses_active_json_when_database_catalog_is_empty(client, app):
    create_portable_tree(client, "main")
    assert client.post("/admin/trees/main/enable").status_code == 200
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("DELETE FROM models")
        conn.commit()
    finally:
        conn.close()

    response = client.put("/admin/models/fast", json={"upstream": "edited-upstream"})

    assert response.status_code == 200, response.text
    tree = app.state.graph_repo.get("main")
    assert tree.runtime.backends.models[0]["upstream"] == "edited-upstream"
    assert app.state.snapshot.backends["fast"].upstream_name == "edited-upstream"


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
    raw["canvas"]["start"] = {"x": 40, "y": 60}
    target = next(node for node in raw["root"]["nodes"] if node["type"] == "target")
    target["capability"] = "展示说明"

    response = create_tree(client, raw)
    assert response.status_code == 201, response.text
    created = response.json()["tree"]
    assert created["enabled"] is False
    assert created["sticky"] == {"enabled": True, "ttl_seconds": 1800}
    assert create_tree(client, raw).status_code == 409

    saved = client.get("/admin/trees/main").json()["tree"]
    assert saved["canvas"]["start"] == {"x": 40, "y": 60}
    assert saved["root"]["nodes"][0]["title"] == "请求入口"
    saved_target = next(node for node in saved["root"]["nodes"] if node["type"] == "target")
    assert saved_target["capability"] == "展示说明"
    assert saved["canvas"]["q"] == {"x": 200, "y": 0}

    updated_root = copy.deepcopy(saved["root"])
    updated_root["nodes"][1]["instructions"] = "重新选择能力"
    updated_canvas = copy.deepcopy(saved["canvas"])
    updated_canvas["safe"] = {"x": 400, "y": 200}
    response = save_tree(client, {"name": "改名", "root": updated_root, "canvas": updated_canvas})
    assert response.status_code == 200, response.text
    saved = client.get("/admin/trees/main").json()["tree"]
    assert saved["name"] == "改名"
    assert saved["root"]["nodes"][1]["instructions"] == "重新选择能力"
    assert saved["canvas"]["safe"] == {"x": 400, "y": 200}
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
    root["nodes"].append({"id": "start-2", "type": "start"})
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


# ── 显式 null 语义（base 428a35d 回归） ────────────────────────────────
# DTO 迁移后 `None` 曾同时表示「字段缺失」与「显式 null」，把 base 的 null 行为
# 退化成「继承」。以下用例锁定 base 对五个字段的原始语义。

def test_update_explicit_id_null_is_tree_id_mismatch(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    response = save_tree(client, {"id": None})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "tree id mismatch"
    assert client.get("/admin/trees/main").json()["tree"]["id"] == "main"


def test_update_explicit_name_null_clears_name(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    assert client.get("/admin/trees/main").json()["tree"]["name"] == "测试图"

    response = save_tree(client, {"name": None})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["name"] == ""
    assert client.get("/admin/trees/main").json()["tree"]["name"] == ""


def test_update_explicit_root_null_is_parse_error(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {"root": None})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert issue["zh"] == (
        "图解析失败：root 不符合 sr-routing-graph v3 结构（graph must be an object）")
    assert client.get("/admin/trees/main").json()["tree"] == before


def test_update_explicit_timezone_null_is_parse_error(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {"timezone": None})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert issue["zh"] == (
        "图解析失败：root 不符合 sr-routing-graph v3 结构（timezone must be a nonempty string）")
    assert client.get("/admin/trees/main").json()["tree"] == before


def test_update_explicit_sticky_null_resets_to_default(client):
    raw = tree_dict(
        root=graph_with_model(), sticky={"enabled": True, "ttl_seconds": 900})
    assert create_tree(client, raw).status_code == 201

    response = save_tree(client, {"sticky": None})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["sticky"] == {"enabled": False, "ttl_seconds": 1800}
    assert client.get("/admin/trees/main").json()["tree"]["sticky"] == {
        "enabled": False, "ttl_seconds": 1800}


def test_update_absent_fields_inherit_existing_values(client):
    """缺失字段仍走 patch 继承（控制台按部分字段保存时依赖此语义）。"""
    raw = tree_dict(
        root=graph_with_model(), sticky={"enabled": True, "ttl_seconds": 900})
    raw["timezone"] = "UTC"
    assert create_tree(client, raw).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {})

    assert response.status_code == 200, response.text
    after = client.get("/admin/trees/main").json()["tree"]
    for field in ("name", "timezone", "sticky", "root"):
        assert after[field] == before[field]


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


# --------------------------------------------------------------------------- #
# 密钥通道：树写 API 与旧库物化都不得让 profile 元数据里的密钥进入 JSON
# --------------------------------------------------------------------------- #
SECRET_KEY_SPELLINGS = ["api_key", "API-KEY", "authorization", "key_present", "key_source"]


def _graph_bytes(app):
    graph_dir = Path(app.state.settings.graph_dir, "graphs")
    return {path.name: path.read_bytes() for path in graph_dir.glob("*.json")}


@pytest.mark.parametrize("key", SECRET_KEY_SPELLINGS)
def test_tree_writes_reject_nested_profile_secrets(client, app, key):
    """POST/PUT 带嵌套密钥 → 422 graph_parse_error，原图响应与文件字节不变。"""
    create_portable_tree(client, "main")
    before_tree = client.get("/admin/trees/main").json()["tree"]
    before_files = _graph_bytes(app)

    payload = tree_dict("nested", root=graph_with_model("fast"))
    payload.update(runtime_dict())
    payload.pop("enabled")
    payload["backends"]["models"][0]["profile"]["metadata"] = [
        {"tier": "x", key: "review-secret"}]
    created = client.post("/admin/trees", json=payload)
    assert created.status_code == 422, created.text
    assert created.json()["detail"][0]["code"] == "graph_parse_error"
    assert "review-secret" not in created.text

    runtime = runtime_dict()
    runtime["backends"]["models"][0]["profile"]["metadata"] = [{key: "review-secret"}]
    updated = save_tree(client, runtime)
    assert updated.status_code == 422, updated.text
    assert updated.json()["detail"][0]["code"] == "graph_parse_error"
    assert "review-secret" not in updated.text

    assert client.get("/admin/trees/main").json()["tree"] == before_tree
    assert _graph_bytes(app) == before_files
    assert not (Path(app.state.settings.graph_dir) / "graphs" / "nested.json").exists()


def test_legacy_database_secret_profile_blocks_materialization(tmp_path):
    """旧库 profile 带密钥：旧图仍能启动，但保存/启用物化必须拒绝且不写文件。"""
    direct = {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
        ],
        "edges": [{"id": "e-start-direct", "source": "start", "source_port": "out",
                   "target": "direct", "target_port": "in_1", "condition": None}],
        "error_fallback": "direct",
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"answers": {}}))
    app = create_app(test_settings(tmp_path), transport=transport)
    repo = RoutingTreeRepo(app.state.settings.graph_dir)
    repo.create(tree_from_dict(tree_dict("legacy", root=direct)))
    repo.enable("legacy")
    conn = connect(app.state.settings.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                (json.dumps({"metadata": {"api_key": "review-secret"}}),))
    finally:
        conn.close()

    restarted = create_app(test_settings(tmp_path), transport=transport)
    client = TestClient(restarted)
    # 旧图（文件无内嵌运行配置）继续走宽松数据库回退：应用可启动。
    assert restarted.state.snapshot.tree_id == "legacy"
    before_files = _graph_bytes(restarted)

    created = client.post("/admin/trees", json={
        "id": "materialized", "name": "物化", "root": direct})
    assert created.status_code == 422, created.text
    assert created.json()["detail"][0]["code"] == "graph_parse_error"
    enabled = client.post("/admin/trees/legacy/enable")
    assert enabled.status_code == 422, enabled.text

    assert _graph_bytes(restarted) == before_files
    assert not (Path(restarted.state.settings.graph_dir) /
                "graphs" / "materialized.json").exists()
    assert "review-secret" not in b"".join(before_files.values()).decode("utf-8")


# --------------------------------------------------------------------------- #
# Task C：`runtime: null` 只解除「当前图继承」，随后仍按活动图/数据库物化
# --------------------------------------------------------------------------- #
def _direct_graph(model_id):
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": model_id, "reasoning": None}]},
        ],
        "edges": [{"id": "e-start-direct", "source": "start", "source_port": "out",
                   "target": "direct", "target_port": "in_1", "condition": None}],
        "error_fallback": "direct",
    }


def _runtime_with_model(model_id, *, upstream="json-upstream"):
    runtime = runtime_dict()
    runtime["backends"]["models"] = [{
        "id": model_id, "upstream": upstream, "provider": "local",
        "profile": {"capability": f"{model_id} 的能力"},
    }]
    return runtime


def _portable_payload(tree_id, model_id):
    raw = tree_dict(tree_id, root=_direct_graph(model_id))
    raw.update(_runtime_with_model(model_id))
    raw.pop("enabled")
    return raw


def _active_and_inactive(client):
    """活动图 a（active-model）+ 非活动图 b（old-model），两者内嵌配置不同。"""
    assert create_tree(client, _portable_payload("a", "active-model")).status_code == 201
    assert client.post("/admin/trees/a/enable").status_code == 200
    assert create_tree(client, _portable_payload("b", "old-model")).status_code == 201
    active = client.get("/admin/trees/a").json()["tree"]
    inactive = client.get("/admin/trees/b").json()["tree"]
    return active, inactive


def _model_ids(tree):
    return [model["id"] for model in tree["backends"]["models"]]


def test_update_explicit_null_runtime_clears_and_materializes_from_active(client, app):
    active, inactive = _active_and_inactive(client)
    assert _model_ids(inactive) == ["old-model"]

    response = save_tree(
        client, {"root": inactive["root"], "runtime": None}, tree_id="b")

    assert response.status_code == 200, response.text
    saved = response.json()["tree"]
    # 清除的是 B 自己的旧配置；随后物化活动图 A 的完整 runtime（不是跳过物化）。
    assert _model_ids(saved) == ["active-model"]
    assert saved["jev"] == active["jev"]
    assert saved["backends"]["providers"] == active["backends"]["providers"]
    assert (app.state.graph_repo.get("b").runtime.backends.models[0]["id"]
            == "active-model")
    # 活动图不受影响。
    assert _model_ids(client.get("/admin/trees/a").json()["tree"]) == ["active-model"]


def test_update_without_runtime_still_inherits_existing_graph_runtime(client):
    _active, inactive = _active_and_inactive(client)

    response = save_tree(client, {"root": inactive["root"]}, tree_id="b")

    assert response.status_code == 200, response.text
    # 未提供控制字段 = 普通缺省继承：保留 B 自己的 old-model。
    assert _model_ids(response.json()["tree"]) == ["old-model"]


def test_create_explicit_null_runtime_materializes_from_active(client):
    assert create_tree(client, _portable_payload("a", "active-model")).status_code == 201
    assert client.post("/admin/trees/a/enable").status_code == 200

    response = client.post("/admin/trees", json={
        "id": "c", "name": "新组", "root": _direct_graph("old-model"), "runtime": None,
    })

    assert response.status_code == 201, response.text
    assert _model_ids(response.json()["tree"]) == ["active-model"]


@pytest.mark.parametrize("value", ["text", [], {}, {"backends": {}}])
def test_tree_dtos_reject_non_null_runtime(client, app, value):
    _active, inactive = _active_and_inactive(client)
    updated = save_tree(client, {"root": inactive["root"], "runtime": value}, tree_id="b")
    assert updated.status_code == 422, updated.text
    assert "runtime must be null" in updated.text
    created = client.post("/admin/trees", json={
        "id": "d", "name": "D", "root": _direct_graph("old-model"), "runtime": value,
    })
    assert created.status_code == 422, created.text
    assert "runtime must be null" in created.text
    # 拒绝后不落文件。
    assert not (Path(app.state.settings.graph_dir) / "graphs" / "d.json").exists()


def test_tree_dtos_reject_runtime_null_accompanied_by_runtime_fields(client):
    _active, inactive = _active_and_inactive(client)
    runtime = _runtime_with_model("old-model")
    updated = save_tree(
        client, {"root": inactive["root"], "runtime": None, "jev": runtime["jev"]},
        tree_id="b")
    assert updated.status_code == 422, updated.text
    assert "runtime cannot accompany runtime fields" in updated.text
    created = client.post("/admin/trees", json={
        "id": "e", "name": "E", "root": _direct_graph("old-model"),
        "runtime": None, "backends": runtime["backends"],
    })
    assert created.status_code == 422, created.text
    assert "runtime cannot accompany runtime fields" in created.text
