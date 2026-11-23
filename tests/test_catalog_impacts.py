"""Task 4：模型 / provider 影响确认、替换删除与确认后的原子画布同步。

真实双画布 fixture（Task 5 可复用同一模式）：provider / model / JEV 都经真实
管理 API 写入数据库目录，两幅自包含画布经 ``POST /admin/trees`` 创建，其中
``main`` 为活动画布。所有断言都直接检查 HTTP 状态、数据库行、画布文件字节、
审计行与已发布快照 revision，而不是只断言响应体。
"""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.routing.graph.model import TargetNode
from router.store.db import connect
from tests.routing_fixtures import portable_tree_dict, test_settings

ZERO_SHA = "0" * 64


# --------------------------------------------------------------------------- #
# 断言辅助
# --------------------------------------------------------------------------- #
def confirmation_from(response) -> dict:
    """把一次 409 影响预览转换为确认载荷。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    return {"trees": [
        {"id": item["id"], "document_sha256": item["document_sha256"]}
        for item in detail["affected_trees"]
    ]}


def _strip_enabled(document: dict) -> dict:
    payload = copy.deepcopy(document)
    payload.pop("enabled", None)
    return payload


def _canvas_bytes(app, tree_id: str) -> bytes:
    return (app.state.graph_repo.graphs / f"{tree_id}.json").read_bytes()


def _canvas_state(app, tree_ids) -> dict[str, bytes]:
    return {tree_id: _canvas_bytes(app, tree_id) for tree_id in tree_ids}


def _db_row(app, table: str, key_column: str, key):
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count(app, sql: str, params=()) -> int:
    conn = connect(app.state.settings.db_path)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _changes(app) -> list[dict]:
    conn = connect(app.state.settings.db_path)
    try:
        return [
            {"kind": row["kind"], "detail": json.loads(row["detail"])}
            for row in conn.execute("SELECT kind, detail FROM changes ORDER BY id")
        ]
    finally:
        conn.close()


def _audit_count(app) -> int:
    return _count(app, "SELECT COUNT(*) FROM changes")


def _tree(app, tree_id: str):
    return app.state.graph_repo.get(tree_id)


def _target(tree):
    return next(node for node in tree.root.nodes if isinstance(node, TargetNode))


def _runtime_models(tree) -> dict[str, dict]:
    return {entry["id"]: entry for entry in tree.runtime.backends.models}


def _runtime_providers(tree) -> dict[str, dict]:
    return {entry["name"]: entry for entry in tree.runtime.backends.providers}


def _confirmed_put(client, path: str, payload: dict):
    """先取影响预览，再用预览里的基线哈希确认重试。"""
    preview = client.put(path, json=payload)
    if preview.status_code != 409:
        return preview
    return client.put(path, json={
        **payload, "impact_confirmation": confirmation_from(preview)})


# --------------------------------------------------------------------------- #
# 真实双画布 fixture
# --------------------------------------------------------------------------- #
def _fast_and_backup(runtime: dict) -> tuple[dict, dict]:
    fast = copy.deepcopy(runtime["backends"]["models"][0])
    backup = copy.deepcopy(fast)
    backup.update({"id": "backup", "upstream": "backup-upstream"})
    backup["profile"] = {
        "capability": "备份能力",
        "reasoning": {"efforts": {"off": None, "low": "low"}, "default": "off"},
    }
    return fast, backup


@pytest.fixture
def two_canvases(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)

    seed = portable_tree_dict("seed")
    provider = dict(seed["backends"]["providers"][0])
    provider.pop("name")
    assert client.put("/admin/providers/local", json=provider).status_code == 200

    fast, backup = _fast_and_backup(seed)
    assert client.post("/admin/models", json=fast).status_code == 201
    assert client.post("/admin/models", json=backup).status_code == 201
    for service_id in ("jev-a", "jev-b"):
        assert client.post("/admin/jev", json={
            "id": service_id, "base_url": "http://" + service_id + ".test",
            "model": service_id, "input_tokens": 1024, "context_tokens": 4096,
        }).status_code == 201

    main = portable_tree_dict("main")
    main["jev"]["id"] = "jev-a"
    for node in main["root"]["nodes"]:
        if node["type"] == "target":
            node["models"][0]["reasoning"] = "high"
    created = client.post("/admin/trees", json=_strip_enabled(main))
    assert created.status_code == 201, created.text

    other = portable_tree_dict("other")
    other["jev"]["id"] = "jev-b"
    other["backends"]["models"] = [copy.deepcopy(backup)]
    for node in other["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": "backup", "reasoning": None}]
    created = client.post("/admin/trees", json=_strip_enabled(other))
    assert created.status_code == 201, created.text

    assert client.post("/admin/trees/main/enable").status_code == 200
    return app, client


# --------------------------------------------------------------------------- #
# 1. 模型更新：未确认零写入，确认后只同步受影响画布
# --------------------------------------------------------------------------- #
def test_model_update_preview_is_pure_then_confirmed_syncs_only_affected(two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_other_sha = _tree(app, "other").document_sha256
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})

    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert detail["resource"] == {"kind": "model", "id": "fast"}
    assert detail["replacements"] == []
    assert [item["id"] for item in detail["affected_trees"]] == ["main"]
    impact = detail["affected_trees"][0]
    assert impact["enabled"] is True
    assert impact["document_sha256"] == _tree(app, "main").document_sha256
    assert impact["usage"] == {"targets": ["direct"], "runtime_catalog": True}
    # 两阶段预览：数据库行 / 两个画布文件 / 审计 / 快照都没有任何变化。
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit
    assert app.state.snapshot.snapshot_revision == before_revision

    confirmed = client.put("/admin/models/fast", json={
        "upstream": "new-upstream",
        "impact_confirmation": confirmation_from(preview),
    })

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["ok"] is True
    assert body["synced_trees"] == ["main"]
    assert body["model"]["upstream"] == "new-upstream"
    # 只改被引用的活动画布；另一幅画布字节与 document_sha256 完全不变。
    assert _canvas_bytes(app, "other") == before["other"]
    assert _tree(app, "other").document_sha256 == before_other_sha
    assert _canvas_bytes(app, "main") != before["main"]
    models = _runtime_models(_tree(app, "main"))
    assert models["fast"]["upstream"] == "new-upstream"
    assert _db_row(app, "models", "id", "fast")["upstream"] == "new-upstream"
    assert app.state.snapshot.backends["fast"].upstream_name == "new-upstream"
    # 恰好一条模型审计行，带同步画布 id。
    changes = _changes(app)
    assert len(changes) == before_audit + 1
    assert changes[-1]["kind"] == "model"
    assert changes[-1]["detail"]["action"] == "update"
    assert changes[-1]["detail"]["synced_trees"] == ["main"]


# --------------------------------------------------------------------------- #
# 2. 模型删除：预览列出替代目录，确认无替代 422，带替代成功替换
# --------------------------------------------------------------------------- #
def test_delete_model_requires_confirmation_replacement_and_syncs_targets(two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_audit = _audit_count(app)

    preview = client.request(
        "DELETE", "/admin/models/fast", json={"replacement_model_id": "backup"})

    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert [item["id"] for item in detail["affected_trees"]] == ["main"]
    replacements = detail["replacements"]
    replacement_ids = [item["id"] for item in replacements]
    assert "backup" in replacement_ids
    assert "fast" not in replacement_ids
    assert all(set(item) == {"id", "upstream", "provider"} for item in replacements)
    # 预览不写数据库、不写画布、不写审计。
    assert _db_row(app, "models", "id", "fast") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit

    confirmation = confirmation_from(preview)

    # 确认但没有替代模型：target 仍引用它 → 422，仍零写入。
    missing = client.request("DELETE", "/admin/models/fast", json={
        "impact_confirmation": confirmation})
    assert missing.status_code == 422
    assert _db_row(app, "models", "id", "fast") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit

    deleted = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "backup",
        "impact_confirmation": confirmation,
    })

    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["deleted"] == "fast"
    assert body["replacement_model_id"] == "backup"
    assert body["synced_trees"] == ["main"]
    main_tree = _tree(app, "main")
    target = _target(main_tree)
    assert [slot.model for slot in target.models] == ["backup"]
    # 旧推理档位 high 不被 backup 支持 → 使用 backup 的默认档位 off。
    assert target.models[0].reasoning == "off"
    models = _runtime_models(main_tree)
    assert "fast" not in models
    assert models["backup"]["upstream"] == "backup-upstream"
    assert "fast" not in app.state.snapshot.backends
    assert _db_row(app, "models", "id", "fast") is None
    assert _canvas_bytes(app, "other") == before["other"]
    changes = _changes(app)
    assert len(changes) == before_audit + 1
    assert changes[-1]["detail"]["action"] == "delete"
    assert changes[-1]["detail"]["synced_trees"] == ["main"]
    assert changes[-1]["detail"]["replacement_model_id"] == "backup"


# --------------------------------------------------------------------------- #
# 3. 过期确认哈希：409 graph_conflict，报告当前哈希，零写入
# --------------------------------------------------------------------------- #
def test_stale_confirmation_hash_returns_graph_conflict_and_writes_nothing(two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)
    actual = _tree(app, "main").document_sha256
    assert confirmation["trees"][0]["document_sha256"] == actual
    confirmation["trees"][0]["document_sha256"] = ZERO_SHA

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "graph_conflict"
    assert detail["actual_document_sha256"] == actual
    assert detail["actual_document_sha256"] != ZERO_SHA
    assert _canvas_state(app, ("main", "other")) == before
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot.snapshot_revision == before_revision


# --------------------------------------------------------------------------- #
# 4. target 同时引用 fast/backup：替换后按模型 ID 去重且保留槽位顺序
# --------------------------------------------------------------------------- #
def test_replacement_dedupes_target_slots_preserving_order(two_canvases):
    app, client = two_canvases
    seed = portable_tree_dict("seed")
    fast, backup = _fast_and_backup(seed)
    both = portable_tree_dict("both")
    both["jev"]["id"] = "jev-b"
    both["backends"]["models"] = [copy.deepcopy(fast), copy.deepcopy(backup)]
    for node in both["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [
                {"model": "fast", "reasoning": "high"},
                {"model": "backup", "reasoning": "low"},
            ]
    created = client.post("/admin/trees", json=_strip_enabled(both))
    assert created.status_code == 201, created.text

    preview = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "backup"})
    assert preview.status_code == 409
    assert {item["id"] for item in preview.json()["detail"]["affected_trees"]} == {
        "main", "both"}

    deleted = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "backup",
        "impact_confirmation": confirmation_from(preview)})
    assert deleted.status_code == 200, deleted.text
    assert sorted(deleted.json()["synced_trees"]) == ["both", "main"]

    target = _target(_tree(app, "both"))
    # 第一个槽位（fast/high）变成 backup/off（high 不受支持 → 默认档），
    # 第二个槽位与第一个重复 → 丢弃；顺序仍是原第一个槽位的位置。
    assert [(slot.model, slot.reasoning) for slot in target.models] == [("backup", "off")]


def test_delete_catalog_only_model_needs_no_replacement(two_canvases):
    app, client = two_canvases
    # dangling 只存在于管理目录与一幅画布的运行时目录，没有任何 target 引用。
    assert client.post("/admin/models", json={
        "id": "dangling", "upstream": "dangling-upstream",
        "provider": "local", "profile": {}}).status_code == 201
    catalog_only = portable_tree_dict("catalog-only")
    catalog_only["jev"]["id"] = "jev-b"
    catalog_only["backends"]["models"] = [
        {"id": "alt-model", "upstream": "alt-upstream", "provider": "local",
         "profile": {}},
        {"id": "dangling", "upstream": "dangling-upstream", "provider": "local",
         "profile": {}},
    ]
    for node in catalog_only["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": "alt-model", "reasoning": None}]
    assert client.post("/admin/trees", json=_strip_enabled(catalog_only)).status_code == 201

    preview = client.delete("/admin/models/dangling")
    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert [item["id"] for item in detail["affected_trees"]] == ["catalog-only"]
    assert detail["affected_trees"][0]["usage"] == {
        "targets": [], "runtime_catalog": True}

    deleted = client.request("DELETE", "/admin/models/dangling", json={
        "impact_confirmation": confirmation_from(preview)})
    assert deleted.status_code == 200, deleted.text
    models = _runtime_models(_tree(app, "catalog-only"))
    assert "dangling" not in models
    assert "alt-model" in models
    assert _target(_tree(app, "catalog-only")).models[0].model == "alt-model"


# --------------------------------------------------------------------------- #
# 5. 原子性：图写入中途失败 → 全回滚；候选校验失败 → 422 全回滚
# --------------------------------------------------------------------------- #
def test_graph_write_failure_rolls_back_database_audit_snapshot_and_files(
        two_canvases, monkeypatch):
    app, client = two_canvases
    second = portable_tree_dict("second-fast")
    second["jev"]["id"] = "jev-b"
    created = client.post("/admin/trees", json=_strip_enabled(second))
    assert created.status_code == 201, created.text
    tree_ids = ("main", "second-fast", "other")
    before = _canvas_state(app, tree_ids)
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    assert preview.status_code == 409
    assert {item["id"]
            for item in preview.json()["detail"]["affected_trees"]} == {
        "main", "second-fast"}
    confirmation = confirmation_from(preview)

    repo = app.state.graph_repo
    real_replace = repo._replace_staged
    calls = {"count": 0}

    def failing_replace(staged_path, path):
        calls["count"] += 1
        if calls["count"] >= 2:            # 第二份文件（活动画布最后写）失败
            raise OSError("injected graph write failure")
        return real_replace(staged_path, path)

    monkeypatch.setattr(repo, "_replace_staged", failing_replace)

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    assert calls["count"] == 2
    # 数据库 / 审计 / 快照 / 三个画布文件全部保持原值（含已替换的第一份文件）。
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot.snapshot_revision == before_revision
    assert _canvas_state(app, tree_ids) == before


def test_candidate_validation_failure_writes_nothing(tmp_path):
    """provider 推理协议改为 openai 让画布的 null-wire 档位非法 → 422 全回滚。"""
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "deepseek",
    }).status_code == 200
    fast = {
        "id": "fast", "upstream": "json-upstream", "provider": "local",
        "profile": {"reasoning": {
            "efforts": {"off": None, "high": None}, "default": "high"}},
    }
    assert client.post("/admin/models", json=fast).status_code == 201
    tree = portable_tree_dict("main")
    tree["backends"]["providers"][0]["reasoning_format"] = "deepseek"
    tree["backends"]["models"][0] = copy.deepcopy(fast)
    for node in tree["root"]["nodes"]:
        if node["type"] == "target":
            node["models"][0]["reasoning"] = "high"
    assert client.post("/admin/trees", json=_strip_enabled(tree)).status_code == 201
    assert client.post("/admin/trees/main/enable").status_code == 200

    before_file = _canvas_bytes(app, "main")
    before_provider = _db_row(app, "providers", "name", "local")
    before_audit = _audit_count(app)

    preview = client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1", "reasoning_format": "openai"})
    assert preview.status_code == 409
    response = client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "openai",
        "impact_confirmation": confirmation_from(preview),
    })

    assert response.status_code == 422, response.text
    issues = response.json()["detail"]
    assert any(issue["code"] == "reasoning_unsupported" for issue in issues)
    assert _canvas_bytes(app, "main") == before_file
    assert _db_row(app, "providers", "name", "local") == before_provider
    assert _audit_count(app) == before_audit


# --------------------------------------------------------------------------- #
# 6. provider 更新 / 删除
# --------------------------------------------------------------------------- #
def test_provider_update_preview_is_pure_then_confirmed_syncs_affected(two_canvases):
    app, client = two_canvases
    alt = portable_tree_dict("alt")
    alt["jev"]["id"] = "jev-b"
    alt["backends"]["providers"] = [{
        "name": "alt", "base_url": "http://alt.test/v1", "key_env": "",
        "reasoning_format": "openai",
    }]
    alt["backends"]["models"] = [{
        "id": "alt-model", "upstream": "alt-upstream", "provider": "alt",
        "profile": {},
    }]
    for node in alt["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": "alt-model", "reasoning": None}]
    assert client.post("/admin/trees", json=_strip_enabled(alt)).status_code == 201

    before = _canvas_state(app, ("main", "other", "alt"))
    before_row = _db_row(app, "providers", "name", "local")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot.snapshot_revision

    preview = client.put("/admin/providers/local",
                         json={"base_url": "http://new.test/v1"})

    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert detail["resource"] == {"kind": "provider", "name": "local"}
    assert {item["id"] for item in detail["affected_trees"]} == {"main", "other"}
    assert all(item["usage"]["runtime_catalog"] is True
               for item in detail["affected_trees"])
    assert _canvas_state(app, ("main", "other", "alt")) == before
    assert _db_row(app, "providers", "name", "local") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot.snapshot_revision == before_revision

    confirmed = client.put("/admin/providers/local", json={
        "base_url": "http://new.test/v1",
        "impact_confirmation": confirmation_from(preview),
    })

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["ok"] is True
    assert sorted(body["synced_trees"]) == ["main", "other"]
    for tree_id in ("main", "other"):
        provider = _runtime_providers(_tree(app, tree_id))["local"]
        assert provider["base_url"] == "http://new.test/v1"
    # 未受影响的画布保持原字节。
    assert _canvas_bytes(app, "alt") == before["alt"]
    assert _db_row(app, "providers", "name", "local")["base_url"] == "http://new.test/v1"
    assert app.state.snapshot.backends["fast"].url == \
        "http://new.test/v1/chat/completions"
    changes = _changes(app)
    assert len(changes) == before_audit + 1
    assert changes[-1]["kind"] == "provider"
    assert sorted(changes[-1]["detail"]["synced_trees"]) == ["main", "other"]


def test_provider_delete_is_blocked_by_db_and_canvas_refs_and_never_bypassable(
        two_canvases):
    app, client = two_canvases
    before_row = _db_row(app, "providers", "name", "local")
    before_audit = _audit_count(app)
    before = _canvas_state(app, ("main", "other"))

    blocked = client.delete("/admin/providers/local")
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["code"] == "provider_referenced"
    assert {"fast", "backup"}.issubset(set(detail["models"]))
    assert {item["id"] for item in detail["affected_trees"]} == {"main", "other"}
    assert _db_row(app, "providers", "name", "local") == before_row
    assert _audit_count(app) == before_audit
    assert _canvas_state(app, ("main", "other")) == before
    # impact_confirmation 不能绕过删除限制。
    forced = client.request("DELETE", "/admin/providers/local", json={
        "impact_confirmation": {"trees": []}})
    assert forced.status_code == 409
    assert forced.json()["detail"]["code"] == "provider_referenced"

    # 只被自包含画布引用（数据库无模型）也必须拒绝，且不可用确认绕过。
    assert client.put("/admin/providers/ghost", json={
        "base_url": "http://ghost.test/v1"}).status_code == 200
    ghost = portable_tree_dict("ghost-canvas")
    ghost["jev"]["id"] = "jev-b"
    ghost["backends"]["providers"] = [{
        "name": "ghost", "base_url": "http://ghost.test/v1", "key_env": "",
        "reasoning_format": "openai",
    }]
    ghost["backends"]["models"] = [{
        "id": "ghost-model", "upstream": "ghost-upstream", "provider": "ghost",
        "profile": {},
    }]
    for node in ghost["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": "ghost-model", "reasoning": None}]
    assert client.post("/admin/trees", json=_strip_enabled(ghost)).status_code == 201

    canvas_blocked = client.delete("/admin/providers/ghost")
    assert canvas_blocked.status_code == 409
    canvas_detail = canvas_blocked.json()["detail"]
    assert canvas_detail["code"] == "provider_referenced"
    assert canvas_detail["models"] == []
    assert [item["id"] for item in canvas_detail["affected_trees"]] == [
        "ghost-canvas"]
    forced = client.request("DELETE", "/admin/providers/ghost", json={
        "impact_confirmation": {"trees": []}})
    assert forced.status_code == 409

    # 无任何引用 → 直接删除 + 审计，不产生画布同步。
    assert client.put("/admin/providers/tmp-prov", json={
        "base_url": "http://tmp.test/v1"}).status_code == 200
    assert client.delete("/admin/providers/tmp-prov").status_code == 200
    assert _db_row(app, "providers", "name", "tmp-prov") is None
    changes = _changes(app)
    assert changes[-1]["kind"] == "provider"
    assert changes[-1]["detail"]["action"] == "delete"
    assert changes[-1]["detail"]["synced_trees"] == []


# --------------------------------------------------------------------------- #
# 7. 密钥纪律：影响清单 / 替代目录 / 审计都不出现密钥
# --------------------------------------------------------------------------- #
def test_impacts_replacements_and_audit_never_expose_secrets(two_canvases):
    app, client = two_canvases
    secret = "sk-impact-must-not-leak"
    updated = _confirmed_put(client, "/admin/providers/local", {
        "base_url": "http://json-only.test/v1",
        "key_env": "JSON_PROVIDER_API_KEY",
        "api_key": secret,
    })
    assert updated.status_code == 200, updated.text
    assert secret not in updated.text

    model_preview = client.put("/admin/models/fast", json={"upstream": "u2"})
    assert model_preview.status_code == 409
    provider_preview = client.put(
        "/admin/providers/local", json={"base_url": "http://next.test/v1"})
    assert provider_preview.status_code == 409
    delete_preview = client.request(
        "DELETE", "/admin/models/fast", json={"replacement_model_id": "backup"})
    assert delete_preview.status_code == 409

    payloads = "".join([
        model_preview.text,
        provider_preview.text,
        delete_preview.text,
        json.dumps(_changes(app), ensure_ascii=False),
        json.dumps(confirmation_from(model_preview), ensure_ascii=False),
        json.dumps(confirmation_from(provider_preview), ensure_ascii=False),
        json.dumps(confirmation_from(delete_preview), ensure_ascii=False),
    ])
    assert secret not in payloads
    # provider 审计只记录密钥状态机，不记录密钥本身。
    provider_changes = [change for change in _changes(app)
                        if change["kind"] == "provider"]
    assert provider_changes[-1]["detail"]["api_key"] == "set"
    # 画布文件/快照同样不含密钥。
    assert secret not in _canvas_bytes(app, "main").decode("utf-8")
    assert secret not in app.state.snapshot.content
