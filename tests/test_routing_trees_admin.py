"""`/admin/trees` graph CRUD：保存载荷、校验分档、enable/copy/disable 语义（Task 7）。"""
from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.jev.service import JevService
from router.store.db import connect
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ModelRepo, ProviderRepo, TimePeriodRepo
from router.routing.tree import tree_from_dict
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import (
    GRAPH_CANVAS,
    graph_dict,
    huge_chain_graph,
    runtime_dict,
    runtime_for_models,
    test_settings,
    tree_dict,
)


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
    response = client.post("/admin/jev", json={
        "id": "default", "base_url": "http://jev.test", "model": "NeoHorse-Jev-4B",
        "timeout_seconds": 5, "instructions": DEFAULT_INSTRUCTIONS,
        "state_template": DEFAULT_STATE_TEMPLATE,
    })
    assert response.status_code == 201, response.text


def create_portable_tree(client, tree_id):
    raw = tree_dict(tree_id, root=graph_with_model("fast"))
    raw.update(runtime_dict())
    response = create_tree(client, raw)
    assert response.status_code == 201, response.text


def _canvas_state(app, tree_ids):
    """画布文件字节 + document_sha256：目录写不得改动任何已存画布。"""
    repo = app.state.graph_repo
    return {
        tree_id: ((repo.graphs / f"{tree_id}.json").read_bytes(),
                  repo.get(tree_id).document_sha256)
        for tree_id in tree_ids
    }


def _confirmation_from(response) -> dict:
    """409 影响预览 → 携带当前画布哈希的确认载荷。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required", response.text
    return {"trees": [
        {"id": item["id"], "document_sha256": item["document_sha256"]}
        for item in detail["affected_trees"]
    ]}


def test_unconfirmed_catalog_writes_do_not_rewrite_stored_canvases(client, app):
    """未确认的目录写只改数据库行 + 审计，绝不改写已存画布。

    严格自包含语义下数据库目录是管理面事实源：受影响画布必须先返回 409 影响
    清单，确认后才同步。非画布资源（时段/JEV 窗口）的目录写仍不碰画布。
    """
    create_portable_tree(client, "main")
    create_portable_tree(client, "second")
    assert client.post("/admin/trees/main/enable").status_code == 200
    tree_ids = ("main", "second")
    before = _canvas_state(app, tree_ids)

    conn = connect(app.state.settings.db_path)
    try:
        # 目录写不要求与画布一致：这里只准备后面要断言的目录行（provider 与 JEV），
        # 特意不登记画布引用的模型，证明启用校验只读画布自身运行配置。
        JevServiceRepo(conn).save(JevService(
            "default", "http://jev.test", "NeoHorse-Jev-4B",
            input_tokens=1024, context_tokens=4096))
        ProviderRepo(conn).upsert("local", "http://database-only.test/v1")
    finally:
        conn.close()
    # GET 直接返回数据库行（旧行为会投影活动 JSON 的 json-only.test）。
    providers = client.get("/admin/providers")
    assert providers.status_code == 200, providers.text
    assert providers.json()["providers"][0]["base_url"] == "http://database-only.test/v1"

    payload = {
        "base_url": "http://changed.test/v1",
        "key_env": "CHANGED_API_KEY",
        "reasoning_format": "openai",
    }
    preview = client.put("/admin/providers/local", json=payload)
    assert preview.status_code == 409, preview.text
    assert {item["id"]
            for item in preview.json()["detail"]["affected_trees"]} == set(tree_ids)
    # 未确认：数据库行、画布文件、运行快照全都不变。
    conn = connect(app.state.settings.db_path)
    try:
        assert ProviderRepo(conn).get("local")["base_url"] == \
            "http://database-only.test/v1"
    finally:
        conn.close()
    assert _canvas_state(app, tree_ids) == before
    assert app.state.snapshot.backends["fast"].url == \
        "http://json-only.test/v1/chat/completions"

    response = client.put("/admin/providers/local", json={
        **payload, "impact_confirmation": _confirmation_from(preview)})
    assert response.status_code == 200, response.text
    assert sorted(response.json()["synced_trees"]) == ["main", "second"]
    conn = connect(app.state.settings.db_path)
    try:
        assert ProviderRepo(conn).get("local")["base_url"] == "http://changed.test/v1"
    finally:
        conn.close()
    after_provider = _canvas_state(app, tree_ids)
    assert all(state != old for state, old in zip(after_provider.values(), before.values()))
    for tree_id in tree_ids:
        provider = next(
            entry for entry in app.state.graph_repo.get(tree_id).runtime.backends.providers
            if entry["name"] == "local")
        assert provider["base_url"] == "http://changed.test/v1"
        assert provider["key_env"] == "CHANGED_API_KEY"
    # 确认同步后活动画布被修改 → 快照按新画布配置重新发布。
    assert app.state.snapshot.backends["fast"].url == \
        "http://changed.test/v1/chat/completions"

    response = client.put("/admin/time-periods", json={"periods": [
        {"name": "DB 新时段", "days": [0], "start": "08:00", "end": "17:00"},
    ]})
    assert response.status_code == 200, response.text
    conn = connect(app.state.settings.db_path)
    try:
        assert [row["name"] for row in TimePeriodRepo(conn).load()] == ["DB 新时段"]
    finally:
        conn.close()
    assert _canvas_state(app, tree_ids) == after_provider
    assert app.state.snapshot.time_periods and [
        period.name for period in app.state.snapshot.time_periods
    ] == ["peak", "normal", "offpeak"]

    # JEV 目录同理：改窗口只改目录，画布 jev_window 不变。
    response = client.put("/admin/jev/default", json={
        "input_tokens": 2048, "context_tokens": 8192,
    })
    assert response.status_code == 200, response.text
    conn = connect(app.state.settings.db_path)
    try:
        assert (JevServiceRepo(conn).get("default").input_tokens,
                JevServiceRepo(conn).get("default").context_tokens) == (2048, 8192)
    finally:
        conn.close()
    for tree_id in tree_ids:
        runtime = app.state.graph_repo.get(tree_id).runtime
        assert (runtime.jev_input_tokens, runtime.jev_context_tokens) == (1024, 4096)
    assert (app.state.snapshot.jev_input_tokens,
            app.state.snapshot.jev_context_tokens) == (1024, 4096)
    assert _canvas_state(app, tree_ids) == after_provider


def test_catalog_writes_succeed_when_active_canvas_has_no_catalog_rows(client, app):
    """画布自包含时目录可以完全不登记画布资源：未确认目录写仍照常成功。"""
    create_portable_tree(client, "main")
    assert client.post("/admin/trees/main/enable").status_code == 200
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("DELETE FROM models")
        conn.commit()
    finally:
        conn.close()

    assert client.get("/admin/models").json()["models"] == []
    response = client.put("/admin/models/fast", json={"upstream": "edited-upstream"})
    assert response.status_code == 404, response.text

    # 画布快照仍是 JSON 事实源，不受数据库空目录影响。
    assert app.state.snapshot.backends["fast"].upstream_name == "json-upstream"
    tree = app.state.graph_repo.get("main")
    assert tree.runtime.backends.models[0]["upstream"] == "json-upstream"

    # 目录补回该模型后：写用例先 409 预览（画布运行时目录包含 fast），确认后
    # 同步该画布运行时模型项；未确认时画布保持不变。
    conn = connect(app.state.settings.db_path)
    try:
        ModelRepo(conn).create("fast", "db-upstream", "local", "{}")
    finally:
        conn.close()
    before_bytes = _canvas_state(app, ("main",))
    preview = client.put("/admin/models/fast", json={"upstream": "edited-upstream"})
    assert preview.status_code == 409, preview.text
    assert [item["id"]
            for item in preview.json()["detail"]["affected_trees"]] == ["main"]
    assert app.state.graph_repo.get("main").runtime.backends.models[0]["upstream"] == \
        "json-upstream"
    assert _canvas_state(app, ("main",)) == before_bytes

    response = client.put("/admin/models/fast", json={
        "upstream": "edited-upstream",
        "impact_confirmation": _confirmation_from(preview),
    })
    assert response.status_code == 200, response.text
    assert client.get("/admin/models").json()["models"][0]["upstream"] == \
        "edited-upstream"
    assert app.state.graph_repo.get("main").runtime.backends.models[0]["upstream"] == \
        "edited-upstream"


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
    # 新画布必须自带运行快照：root 与运行目录同时构造。
    configure_jev(client)
    raw = tree_dict(sticky={"enabled": True, "ttl_seconds": 1800},
                    root=graph_with_model())
    raw["canvas"] = {key: dict(value) for key, value in GRAPH_CANVAS.items()}
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
    # copy 必须保留完整运行快照（不只 id/name/root）：后端/决策服务/窗口/时段
    # 逐字段相等，副本才能立即通过启用校验并独立运行。
    for field in ("backends", "jev", "jev_window", "time_periods"):
        assert copied_tree[field] == saved[field], field

    enabled = client.post("/admin/trees/main/enable")
    assert enabled.status_code == 200, enabled.text
    assert app.state.snapshot.tree_id == "main"
    assert [tree["id"] for tree in enabled.json()["trees"] if tree["enabled"]] == ["main"]
    assert enabled.json()["snapshot_revision"] == app.state.snapshot.snapshot_revision

    disabled = client.post("/admin/trees/main/disable")
    assert disabled.status_code == 200, disabled.text
    assert app.state.snapshot.tree is None
    assert not any(tree["enabled"] for tree in disabled.json()["trees"])


def test_copy_rejects_invalid_source_without_file_or_audit(client, app):
    raw = tree_dict("invalid-source")
    raw["root"] = graph_with_model("not-in-snapshot")
    # 模拟修复前允许落盘的历史数据：target 模型不在该画布运行目录里。
    # 仓储只用于播种，公开 create/update 仍必须拒绝这类新写入。
    app.state.graph_repo.create(tree_from_dict(raw))
    source_before = _canvas_state(app, ("invalid-source",))
    target = app.state.graph_repo.graphs / "invalid-copy.json"
    assert not target.exists()
    conn = connect(app.state.settings.db_path)
    audit_before = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    conn.close()

    response = client.post("/admin/trees/invalid-source/copy", json={
        "id": "invalid-copy", "name": "无效副本",
    })

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert not target.exists()
    assert _canvas_state(app, ("invalid-source",)) == source_before
    conn = connect(app.state.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == audit_before
    conn.close()


def test_save_uses_full_canvas_validation_in_every_lifecycle(client, app):
    configure_jev(client)
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {"root": graph_with_model("ghost")})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"] == before

    root = graph_with_model()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []
    response = save_tree(client, {"root": root})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "target_models_required"
               for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"] == before

    assert save_tree(client, {"root": graph_with_model()}).status_code == 200
    assert client.post("/admin/trees/main/enable").status_code == 200
    published = app.state.snapshot.snapshot_revision

    response = save_tree(client, {"root": graph_with_model("ghost")})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published

    root = graph_with_model()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []
    response = save_tree(client, {"root": root})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "target_models_required"
               for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published

    root = graph_with_model()
    root["nodes"] = [node for node in root["nodes"] if node["type"] != "start"]
    response = save_tree(client, {"root": root})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "start_count" for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published


ISSUE_KEYS = {"code", "zh", "en", "node_id", "edge_id", "port", "level",
              "params"}


def test_parse_failures_are_issue_arrays(client):
    # 必须携带完整运行字段，否则解析器在「四字段必须成组」处就拒绝，永远走不到
    # 坏 root 的图解析错误。这里断言具体的解析 detail，证明坏节点/旧类型确实被解析。
    response = create_tree(client, tree_dict("bad", root={
        "id": "root", "type": "graph",
        "nodes": [{"id": "n", "type": "bogus"}], "edges": [],
    }))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert issue["level"] == "hard"
    assert issue["zh"] and issue["en"]
    assert "unknown node type 'bogus'" in issue["zh"]
    assert "unknown node type 'bogus'" in issue["en"]
    assert "runtime fields must appear together" not in issue["zh"]
    assert client.get("/admin/trees").json()["trees"] == []

    response = create_tree(client, tree_dict("old", root={"id": "x", "type": "jev"}))
    assert response.status_code == 422, response.text
    legacy = response.json()["detail"][0]
    assert set(legacy) == ISSUE_KEYS
    assert legacy["code"] == "graph_parse_error"
    assert "graph.type must be 'graph', got 'jev'" in legacy["zh"]
    assert "runtime fields must appear together" not in legacy["zh"]
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


@pytest.mark.parametrize(("field", "message"), [
    ("backends", "backends must be an object"),
    ("jev_window", "jev_window must be an object"),
    ("time_periods", "time_periods must be a list"),
])
def test_update_explicit_null_flat_runtime_field_is_parse_error(client, field, message):
    """四个扁平运行字段的显式 null 交给解析器，按字段类型 422，且画布不变。"""
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {field: None})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and len(detail) == 1
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert message in issue["zh"]
    assert client.get("/admin/trees/main").json()["tree"] == before


def test_update_explicit_jev_null_is_rejected_for_question_graph(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {"jev": None})

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "decision_service_unavailable"
               for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"] == before


def test_create_question_graph_without_jev_is_rejected(client):
    raw = tree_dict(root=graph_with_model())
    raw["jev"] = None

    response = create_tree(client, raw)

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "decision_service_unavailable"
               for issue in response.json()["detail"])
    assert client.get("/admin/trees").json()["trees"] == []


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


def test_update_jev_service_change_requires_matching_window(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]
    new_jev = copy.deepcopy(before["jev"])
    new_jev["id"] = "other-service"

    response = save_tree(client, {"jev": new_jev})

    assert response.status_code == 422, response.text
    issue = response.json()["detail"][0]
    assert issue["code"] == "graph_parse_error"
    assert "jev_window is required when changing JEV service id" in issue["zh"]
    assert client.get("/admin/trees/main").json()["tree"] == before


def test_update_jev_service_change_accepts_service_and_matching_window(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]
    new_jev = copy.deepcopy(before["jev"])
    new_jev["id"] = "other-service"
    new_window = {"input_tokens": 2048, "context_tokens": 8192}

    response = save_tree(client, {"jev": new_jev, "jev_window": new_window})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["jev"]["id"] == "other-service"
    assert response.json()["tree"]["jev_window"] == new_window


def test_update_same_jev_without_window_keeps_stored_window(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]
    changed = copy.deepcopy(before["jev"])
    changed["instructions"] = "只修改同一服务的提示词"

    response = save_tree(client, {"jev": changed})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["jev"]["instructions"] == "只修改同一服务的提示词"
    assert response.json()["tree"]["jev_window"] == before["jev_window"]


def test_clear_jev_without_questions_does_not_require_window(client):
    assert create_tree(client, tree_dict(root=_direct_graph("fast"))).status_code == 201

    response = save_tree(client, {"jev": None})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["jev"] is None


# --------------------------------------------------------------------------- #
# 运行字段按补丁合并：缺失继承已存画布，显式值覆盖
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


def _portable_payload(tree_id, model_id):
    raw = tree_dict(tree_id, root=_direct_graph(model_id))
    raw.update(runtime_for_models([model_id]))
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


def test_update_without_runtime_inherits_every_stored_runtime_field(client):
    _active, inactive = _active_and_inactive(client)

    response = save_tree(client, {"root": inactive["root"]}, tree_id="b")

    assert response.status_code == 200, response.text
    assert _model_ids(response.json()["tree"]) == ["old-model"]
    assert response.json()["tree"]["jev"] == inactive["jev"]
    assert response.json()["tree"]["jev_window"] == inactive["jev_window"]

    # 只给部分运行字段：给出的覆盖，其余继承已存画布。
    partial = save_tree(client, {"jev_window": {"input_tokens": 2048, "context_tokens": 4096}},
                        tree_id="b")
    assert partial.status_code == 200, partial.text
    assert partial.json()["tree"]["jev_window"] == {
        "input_tokens": 2048, "context_tokens": 4096}
    assert partial.json()["tree"]["jev"] == inactive["jev"]


def test_update_with_full_flat_runtime_fields_replaces_snapshot(client):
    _active, _inactive = _active_and_inactive(client)
    runtime = runtime_for_models(["replaced-model"])

    response = save_tree(client, {"root": _direct_graph("replaced-model"), **runtime},
                         tree_id="b")

    assert response.status_code == 200, response.text
    assert _model_ids(response.json()["tree"]) == ["replaced-model"]
