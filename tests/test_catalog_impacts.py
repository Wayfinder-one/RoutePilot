"""Task 4：模型 / provider 影响确认、替换删除与确认后的原子画布同步。

真实双画布 fixture（Task 5 可复用同一模式）：provider / model / JEV 都经真实
管理 API 写入数据库目录，两幅自包含画布经 ``POST /admin/trees`` 创建，其中
``main`` 为活动画布。所有断言都直接检查 HTTP 状态、数据库行、画布文件字节、
审计行与已发布快照 revision，而不是只断言响应体。
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace

import pytest
from fastapi.testclient import TestClient

from router.api import routes_jev, routes_models
from router.api.app import create_app
from router.control.config import ConfigurationService
from router.control.impacts import provider_impacts
from router.routing.graph.model import TargetNode
from router.routing.tree import tree_from_dict
from router.store.db import connect
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ChangeRepo, ModelRepo, ProviderRepo
from router.store.routing_trees import (
    GraphBatch,
    GraphChangedDuringLoad,
    GraphStorageError,
)
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
    """管理 DTO 只接受 departments；生命周期 enabled/default 由服务端/启停端点写入。"""
    payload = copy.deepcopy(document)
    payload.pop("enabled", None)
    payload.pop("default", None)
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
    # jev-b 使用与 jev-a/画布都不同的窗口：删除替换后能区分「替代服务的窗口」。
    for service_id, window in (("jev-a", (1024, 4096)), ("jev-b", (2048, 8192))):
        assert client.post("/admin/jev", json={
            "id": service_id, "base_url": "http://" + service_id + ".test",
            "model": service_id,
            "input_tokens": window[0], "context_tokens": window[1],
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
    before_revision = app.state.snapshot_set.snapshot_revision

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
    assert app.state.snapshot_set.snapshot_revision == before_revision

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
    assert app.state.snapshot_set.default_snapshot.backends["fast"].upstream_name == "new-upstream"
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
    assert "fast" not in app.state.snapshot_set.default_snapshot.backends
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
    before_revision = app.state.snapshot_set.snapshot_revision

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
    assert app.state.snapshot_set.snapshot_revision == before_revision


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
    # 删除未被 target 引用的模型不触碰槽位表：模型与显式「不指定」档位原样保留。
    assert [(slot.model, slot.reasoning)
            for slot in _target(_tree(app, "catalog-only")).models] == [
        ("alt-model", None)]


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
    before_revision = app.state.snapshot_set.snapshot_revision

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
        if calls["count"] >= 2:            # 第二份文件（默认图最后写）失败
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
    assert app.state.snapshot_set.snapshot_revision == before_revision
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
    before_revision = app.state.snapshot_set.snapshot_revision

    preview = client.put("/admin/providers/local",
                         json={"base_url": "http://new.test/v1"})

    assert preview.status_code == 409
    detail = preview.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert detail["resource"] == {"kind": "provider", "name": "local"}
    assert {item["id"] for item in detail["affected_trees"]} == {"main", "other"}
    assert all(item["usage"]["runtime_catalog"] is True
               for item in detail["affected_trees"])
    # provider 影响清单同时给出使用它的画布模型 id（非密钥目录事实）。
    usages = {item["id"]: item["usage"] for item in detail["affected_trees"]}
    assert usages["main"]["models"] == {"use": ["fast"]}
    assert usages["other"]["models"] == {"use": ["backup"]}
    assert _canvas_state(app, ("main", "other", "alt")) == before
    assert _db_row(app, "providers", "name", "local") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision

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
    assert app.state.snapshot_set.default_snapshot.backends["fast"].url == \
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

    # 只有 provider 目录条目、没有任何画布模型引用它 → 不阻塞删除（spec：
    # 只有数据库模型与画布模型引用才 block）。影响清单仍列出该画布，但删除
    # 只写目录行 + 审计，不改画布字节、不发布快照。
    assert client.put("/admin/providers/bare-prov", json={
        "base_url": "http://bare.test/v1"}).status_code == 200
    bare = portable_tree_dict("bare-canvas")
    bare["jev"]["id"] = "jev-b"
    bare["backends"]["providers"].append({
        "name": "bare-prov", "base_url": "http://bare.test/v1", "key_env": "",
        "reasoning_format": "openai",
    })
    assert client.post("/admin/trees", json=_strip_enabled(bare)).status_code == 201
    before_bare = _canvas_bytes(app, "bare-canvas")
    before_revision = app.state.snapshot_set.snapshot_revision
    before_audit = _audit_count(app)
    bare_impacts = provider_impacts(app.state.graph_repo.list(), "bare-prov")
    assert [item["id"] for item in bare_impacts] == ["bare-canvas"]
    assert bare_impacts[0]["usage"]["models"] == {"use": []}
    deleted_bare = client.delete("/admin/providers/bare-prov")
    assert deleted_bare.status_code == 200, deleted_bare.text
    assert _db_row(app, "providers", "name", "bare-prov") is None
    assert _canvas_bytes(app, "bare-canvas") == before_bare
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _audit_count(app) == before_audit + 1
    assert _changes(app)[-1]["detail"] == {
        "action": "delete", "name": "bare-prov", "synced_trees": []}

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
    assert secret not in app.state.snapshot_set.default_snapshot.content


# --------------------------------------------------------------------------- #
# 8. Review round 1：严格候选校验 + 锁内引用复核 + 发布门/基线门
# --------------------------------------------------------------------------- #
#: deepseek 方言下合法（非 off 档 wire 可为 null）、openai 方言下非法（needs_wire）。
DEEPSEEK_ONLY_PROFILE = {"reasoning": {
    "efforts": {"off": None, "high": None}, "default": "high"}}


def _deepseek_only_model(profile=None) -> dict:
    return {
        "id": "fast", "upstream": "json-upstream", "provider": "local",
        "profile": copy.deepcopy(DEEPSEEK_ONLY_PROFILE if profile is None else profile),
    }


def _assert_provider_row_and_key_unchanged(app, name, before_row):
    assert _db_row(app, "providers", "name", name) == before_row


def test_dialect_flip_invalidating_closed_canvas_is_422_and_repo_stays_readable(
        tmp_path):
    """CRITICAL 1：非活动画布的 profile 只在旧 provider 方言下合法。

    确认同步必须在暂存/写盘之前严格解析每份候选（活动与非活动都一样），返回
    422 并整体回滚；fix 前它返回 200 把闭画布写成非法文档，随后
    ``repo.list()`` / ``GET /admin/trees`` 直接 503。
    """
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "deepseek",
    }).status_code == 200

    active = portable_tree_dict("main")
    assert client.post("/admin/trees", json=_strip_enabled(active)).status_code == 201
    assert client.post("/admin/trees/main/enable").status_code == 200

    closed = portable_tree_dict("closed")
    closed["backends"]["providers"][0]["reasoning_format"] = "deepseek"
    closed["backends"]["models"][0] = _deepseek_only_model()
    for node in closed["root"]["nodes"]:
        if node["type"] == "target":
            node["models"][0]["reasoning"] = None
    assert client.post("/admin/trees", json=_strip_enabled(closed)).status_code == 201
    assert {tree.id for tree in app.state.graph_repo.list()} == {"main", "closed"}

    before_provider = _db_row(app, "providers", "name", "local")
    before_files = _canvas_state(app, ("main", "closed"))
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision

    preview = client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1", "reasoning_format": "openai"})
    assert preview.status_code == 409
    assert {item["id"] for item in preview.json()["detail"]["affected_trees"]} == {
        "main", "closed"}

    response = client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "openai",
        "impact_confirmation": confirmation_from(preview),
    })

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "graph_parse_error"
               for issue in response.json()["detail"])
    _assert_provider_row_and_key_unchanged(app, "local", before_provider)
    assert _canvas_state(app, ("main", "closed")) == before_files
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision
    # 所有画布仍可读（fix 前闭画布已被写坏，这里会抛 GraphStorageError）。
    assert {tree.id for tree in app.state.graph_repo.list()} == {"main", "closed"}
    assert client.get("/admin/trees").status_code == 200


def test_dialect_flip_invalidating_active_canvas_is_422_not_503(tmp_path):
    """CRITICAL 1（活动画布变体）：fix 前候选写盘后重读活动画布抛
    ``GraphStorageError`` → 503；严格候选校验必须在写盘前把它变成 422 并回滚。"""
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "deepseek",
    }).status_code == 200
    tree = portable_tree_dict("main")
    tree["backends"]["providers"][0]["reasoning_format"] = "deepseek"
    tree["backends"]["models"][0] = _deepseek_only_model()
    for node in tree["root"]["nodes"]:
        if node["type"] == "target":
            node["models"][0]["reasoning"] = None
    assert client.post("/admin/trees", json=_strip_enabled(tree)).status_code == 201
    assert client.post("/admin/trees/main/enable").status_code == 200

    before_provider = _db_row(app, "providers", "name", "local")
    before_file = _canvas_bytes(app, "main")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision

    preview = client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1", "reasoning_format": "openai"})
    assert preview.status_code == 409
    response = client.put("/admin/providers/local", json={
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "openai",
        "impact_confirmation": confirmation_from(preview),
    })

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "graph_parse_error"
               for issue in response.json()["detail"])
    _assert_provider_row_and_key_unchanged(app, "local", before_provider)
    assert _canvas_bytes(app, "main") == before_file
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert {tree.id for tree in app.state.graph_repo.list()} == {"main"}


def test_canvas_appearing_between_route_scan_and_commit_returns_graph_conflict(
        two_canvases, monkeypatch):
    """CRITICAL 2：路由层影响扫描之后、提交加锁之前新建的画布若引用被改模型，
    必须在锁内、事务内 recheck 时变成 409 ``graph_conflict``；fix 前数据库会被
    改写而新画布漏同步。"""
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    real_impacts = routes_models.model_impacts
    raced = {"done": False, "sha": None}

    def racing_impacts(trees, model_id):
        impacts = real_impacts(trees, model_id)   # 旧清单：racer 画布还不存在
        if not raced["done"]:
            raced["done"] = True
            created = app.state.graph_repo.create(
                tree_from_dict(portable_tree_dict("racer")))
            raced["sha"] = created.document_sha256
        return impacts

    monkeypatch.setattr(routes_models, "model_impacts", racing_impacts)

    blocked = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "graph_conflict"
    # actual 必须是新出现画布的当前哈希，而不是客户端提交的旧哈希。
    assert detail["actual_document_sha256"] == raced["sha"]
    assert _canvas_state(app, ("main", "other")) == before
    assert _tree(app, "racer").document_sha256 == raced["sha"]
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision


def test_canvas_mutated_between_impact_scan_and_stage_returns_graph_conflict(
        two_canvases, monkeypatch):
    """事务内基线门：影响清单生成后、``stage_updates`` 核对基线前画布被改写 →
    409 ``graph_conflict``（报告当前哈希）且数据库/审计/快照全部回滚。"""
    app, client = two_canvases
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision

    real_validate = ConfigurationService._validate_candidate
    raced = {}

    def racing_validate(self, candidate):
        # 影响清单已生成、目录行已写（事务内）之后，另一个进程改写活动画布；
        # 暂存的候选仍以旧 document_sha256 为基线。
        if not raced:
            current = app.state.graph_repo.get("main")
            replaced = app.state.graph_repo.update(
                dataclass_replace(current, name="mutated-after-impacts"),
                base_document_sha256=current.document_sha256)
            raced["sha"] = replaced.document_sha256
        return real_validate(self, candidate)

    monkeypatch.setattr(ConfigurationService, "_validate_candidate",
                        racing_validate)

    blocked = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "graph_conflict"
    assert detail["actual_document_sha256"] == raced["sha"]
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _canvas_bytes(app, "other") == before["other"]


def test_confirmed_write_touching_only_inactive_canvas_does_not_publish(
        two_canvases, monkeypatch):
    """发布门（负向）：确认写只改了非活动画布时不发布新快照。"""
    app, client = two_canvases
    publisher = app.state.publisher
    real_publish = publisher.publish
    published = []

    def spy(prepared):
        published.append(prepared)
        return real_publish(prepared)

    monkeypatch.setattr(publisher, "publish", spy)
    before_revision = app.state.snapshot_set.snapshot_revision
    before_main = _canvas_bytes(app, "main")
    before_other = _canvas_bytes(app, "other")

    preview = client.put("/admin/models/backup", json={
        "upstream": "backup-upstream-v2"})
    assert preview.status_code == 409
    assert [item["id"] for item in preview.json()["detail"]["affected_trees"]] == [
        "other"]

    confirmed = client.put("/admin/models/backup", json={
        "upstream": "backup-upstream-v2",
        "impact_confirmation": confirmation_from(preview),
    })

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["synced_trees"] == ["other"]
    assert published == []
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _canvas_bytes(app, "main") == before_main
    assert _canvas_bytes(app, "other") != before_other


def test_replacement_model_keeps_supported_reasoning_tier(two_canvases):
    """替换模型声明了旧档位时保留槽位档位（不落到替代模型默认档）。"""
    app, client = two_canvases
    profile = {"reasoning": {
        "efforts": {"off": None, "low": "low", "high": "high"},
        "default": "off"}}
    preview = client.put("/admin/models/backup", json={"profile": profile})
    assert preview.status_code == 409
    updated = client.put("/admin/models/backup", json={
        "profile": profile,
        "impact_confirmation": confirmation_from(preview),
    })
    assert updated.status_code == 200, updated.text
    # main 的槽位是 fast/high；backup 现在支持 high → 保留 high。
    assert _target(_tree(app, "main")).models[0].reasoning == "high"

    delete_preview = client.request(
        "DELETE", "/admin/models/fast", json={"replacement_model_id": "backup"})
    assert delete_preview.status_code == 409
    deleted = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "backup",
        "impact_confirmation": confirmation_from(delete_preview),
    })
    assert deleted.status_code == 200, deleted.text
    assert [(slot.model, slot.reasoning) for slot in _target(_tree(app, "main")).models] \
        == [("backup", "high")]


def test_replacement_model_provider_inserted_when_absent_from_canvas(two_canvases):
    """替代模型的 provider 不在画布运行时目录里时，同步时补入。"""
    app, client = two_canvases
    assert client.put("/admin/providers/alt-provider", json={
        "base_url": "http://alt.test/v1"}).status_code == 200
    assert client.post("/admin/models", json={
        "id": "alt-model", "upstream": "alt-upstream",
        "provider": "alt-provider", "profile": {}}).status_code == 201
    assert "alt-provider" not in _runtime_providers(_tree(app, "main"))

    delete_preview = client.request(
        "DELETE", "/admin/models/fast", json={"replacement_model_id": "alt-model"})
    assert delete_preview.status_code == 409
    deleted = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "alt-model",
        "impact_confirmation": confirmation_from(delete_preview),
    })
    assert deleted.status_code == 200, deleted.text

    main = _tree(app, "main")
    assert _runtime_providers(main)["alt-provider"]["base_url"] == \
        "http://alt.test/v1"
    assert _runtime_models(main)["alt-model"]["provider"] == "alt-provider"
    assert [(slot.model, slot.reasoning) for slot in _target(main).models] == [
        ("alt-model", None)]


def test_delete_model_with_unknown_replacement_is_404(two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_audit = _audit_count(app)
    response = client.request(
        "DELETE", "/admin/models/fast", json={"replacement_model_id": "nope"})
    assert response.status_code == 404
    assert _db_row(app, "models", "id", "fast") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit


# --------------------------------------------------------------------------- #
# 9. JEV 更新：未确认零写入，确认后 JEV 快照与窗口一起同步
# --------------------------------------------------------------------------- #
def test_jev_update_preview_is_pure_then_confirmed_syncs_service_and_window(
        two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_main_sha = _tree(app, "main").document_sha256
    before_other_sha = _tree(app, "other").document_sha256
    before_row = _db_row(app, "jev_services", "id", "jev-a")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision
    instructions = "你是一个模型路由器。请只选择最匹配的模型。"
    state_template = "【待判断的用户请求】\n{request}"

    payload = {
        "base_url": "http://jev-a-v2.test",
        "model": "d1-a-v2",
        "timeout_seconds": 7.5,
        "instructions": instructions,
        "state_template": state_template,
        "input_tokens": 3072,
        "context_tokens": 8192,
    }
    preview = client.put("/admin/jev/jev-a", json=payload)

    assert preview.status_code == 409, preview.text
    detail = preview.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert detail["resource"] == {"kind": "jev", "id": "jev-a"}
    assert detail["replacements"] == []
    assert [item["id"] for item in detail["affected_trees"]] == ["main"]
    impact = detail["affected_trees"][0]
    assert impact["usage"] == {"jev": "jev-a"}
    assert impact["document_sha256"] == before_main_sha
    # 两阶段预览：数据库行 / 两幅画布 / 审计 / 快照都没有任何变化。
    assert _db_row(app, "jev_services", "id", "jev-a") == before_row
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision

    confirmed = client.put("/admin/jev/jev-a", json={
        **payload, "impact_confirmation": confirmation_from(preview)})

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["synced_trees"] == ["main"]
    assert body["service"]["base_url"] == "http://jev-a-v2.test"
    assert body["service"]["model"] == "d1-a-v2"
    assert "api_key" not in body["service"]

    runtime = _tree(app, "main").runtime
    assert runtime.jev.id == "jev-a"
    assert runtime.jev.base_url == "http://jev-a-v2.test"
    assert runtime.jev.model == "d1-a-v2"
    assert runtime.jev.timeout_seconds == 7.5
    assert runtime.jev.instructions == instructions
    assert runtime.jev.state_template == state_template
    # JEV 快照与窗口必须来自同一服务：不是「旧服务配置 + 新窗口」的混合体。
    assert (runtime.jev.input_tokens, runtime.jev.context_tokens) == (3072, 8192)
    assert (runtime.jev_input_tokens, runtime.jev_context_tokens) == (3072, 8192)
    # 未受影响的画布字节与哈希完全不变。
    assert _canvas_bytes(app, "other") == before["other"]
    assert _tree(app, "other").document_sha256 == before_other_sha
    assert _canvas_bytes(app, "main") != before["main"]
    assert _db_row(app, "jev_services", "id", "jev-a")["model"] == "d1-a-v2"
    # 活动画布被同步 → 快照按新画布配置重新发布。
    assert app.state.snapshot_set.default_snapshot.jev.base_url == "http://jev-a-v2.test"
    assert app.state.snapshot_set.default_snapshot.jev.model == "d1-a-v2"
    assert (app.state.snapshot_set.default_snapshot.jev_input_tokens,
            app.state.snapshot_set.default_snapshot.jev_context_tokens) == (3072, 8192)
    assert app.state.snapshot_set.snapshot_revision != before_revision
    changes = _changes(app)
    assert len(changes) == before_audit + 1
    assert changes[-1]["kind"] == "jev"
    # 更新审计记录变化的非密钥字段与密钥意图枚举（不含密钥值）。
    assert changes[-1]["detail"] == {
        "action": "update", "id": "jev-a", "model": "d1-a-v2",
        "base_url": "http://jev-a-v2.test",
        "instructions": instructions,
        "state_template": state_template,
        "timeout_seconds": 7.5,
        "input_tokens": 3072,
        "context_tokens": 8192,
        "key_state": "cleared",
        "synced_trees": ["main"],
    }


def test_jev_window_update_replaces_snapshot_and_window_together(two_canvases):
    """只改窗口也必须整体替换 JEV 快照：窗口与快照同源，绝不混用旧服务配置。"""
    app, client = two_canvases
    preview = client.put("/admin/jev/jev-a", json={
        "input_tokens": 768, "context_tokens": 3072})
    assert preview.status_code == 409, preview.text
    assert [item["id"] for item in preview.json()["detail"]["affected_trees"]] == [
        "main"]

    confirmed = client.put("/admin/jev/jev-a", json={
        "input_tokens": 768, "context_tokens": 3072,
        "impact_confirmation": confirmation_from(preview)})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["synced_trees"] == ["main"]

    runtime = _tree(app, "main").runtime
    assert (runtime.jev_input_tokens, runtime.jev_context_tokens) == (768, 3072)
    assert (runtime.jev.input_tokens, runtime.jev.context_tokens) == (768, 3072)
    # 画布原 JEV 是 JSON fixture 的 json-jev-service 配置（id 被改成 jev-a），
    # 同步后必须整体换成数据库 jev-a 服务，而不是保留旧 base_url/model 再套新窗口。
    assert runtime.jev.base_url == "http://jev-a.test"
    assert runtime.jev.model == "jev-a"
    assert runtime.jev.key_env == ""
    assert _tree(app, "other").runtime.jev.id == "jev-b"
    assert _tree(app, "other").runtime.jev.base_url == "http://jev-json.test"
    assert (app.state.snapshot_set.default_snapshot.jev_input_tokens,
            app.state.snapshot_set.default_snapshot.jev_context_tokens) == (768, 3072)


# --------------------------------------------------------------------------- #
# 10. JEV 删除：预览替代目录，确认必须带替代服务并同步替换
# --------------------------------------------------------------------------- #
def test_jev_delete_requires_replacement_and_syncs_replacement_service(
        two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_other_sha = _tree(app, "other").document_sha256
    before_audit = _audit_count(app)

    preview = client.request("DELETE", "/admin/jev/jev-a", json={})
    assert preview.status_code == 409, preview.text
    detail = preview.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert detail["resource"] == {"kind": "jev", "id": "jev-a"}
    assert [item["id"] for item in detail["affected_trees"]] == ["main"]
    assert detail["affected_trees"][0]["usage"] == {"jev": "jev-a"}
    replacements = detail["replacements"]
    replacement_ids = [item["id"] for item in replacements]
    assert "jev-b" in replacement_ids
    assert "jev-a" not in replacement_ids
    assert all(set(item) == {"id", "base_url", "model", "input_tokens",
                             "context_tokens"} for item in replacements)
    jev_b = next(item for item in replacements if item["id"] == "jev-b")
    assert (jev_b["base_url"], jev_b["model"]) == (
        "http://jev-b.test", "jev-b")
    assert (jev_b["input_tokens"], jev_b["context_tokens"]) == (2048, 8192)
    # 预览零写入。
    assert _db_row(app, "jev_services", "id", "jev-a") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit

    # 未确认时先返回影响预览：替代服务参数不会被抢先校验成 404。
    early = client.request("DELETE", "/admin/jev/jev-a", json={
        "replacement_jev_id": "nope"})
    assert early.status_code == 409, early.text
    assert early.json()["detail"]["code"] == "impact_confirmation_required"

    confirmation = confirmation_from(preview)

    # 已确认但没有替代服务 → 422，仍零写入。
    missing = client.request("DELETE", "/admin/jev/jev-a", json={
        "impact_confirmation": confirmation})
    assert missing.status_code == 422, missing.text
    assert _db_row(app, "jev_services", "id", "jev-a") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit

    # 替代服务不存在 → 404，零写入。
    unknown = client.request("DELETE", "/admin/jev/jev-a", json={
        "replacement_jev_id": "nope", "impact_confirmation": confirmation})
    assert unknown.status_code == 404, unknown.text
    assert _db_row(app, "jev_services", "id", "jev-a") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit

    # 替代服务不能是被删服务自身 → 422。
    itself = client.request("DELETE", "/admin/jev/jev-a", json={
        "replacement_jev_id": "jev-a", "impact_confirmation": confirmation})
    assert itself.status_code == 422, itself.text
    assert _db_row(app, "jev_services", "id", "jev-a") is not None

    deleted = client.request("DELETE", "/admin/jev/jev-a", json={
        "replacement_jev_id": "jev-b",
        "impact_confirmation": confirmation,
    })

    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["deleted"] == "jev-a"
    assert body["replacement_jev_id"] == "jev-b"
    assert body["synced_trees"] == ["main"]

    main = _tree(app, "main")
    assert main.runtime.jev.id == "jev-b"
    assert main.runtime.jev.base_url == "http://jev-b.test"
    assert main.runtime.jev.model == "jev-b"
    # 替换后画布窗口 = 替代服务的窗口（jev-b 的 2048/8192），不是被删服务的。
    assert (main.runtime.jev_input_tokens,
            main.runtime.jev_context_tokens) == (2048, 8192)
    assert (main.runtime.jev.input_tokens, main.runtime.jev.context_tokens) == (
        2048, 8192)
    assert _db_row(app, "jev_services", "id", "jev-a") is None
    assert _db_row(app, "jev_services", "id", "jev-b") is not None
    # 另一幅画布（本就引用 jev-b）字节与哈希完全不变。
    assert _canvas_bytes(app, "other") == before["other"]
    assert _tree(app, "other").document_sha256 == before_other_sha
    assert app.state.snapshot_set.default_snapshot.jev.base_url == "http://jev-b.test"
    assert (app.state.snapshot_set.default_snapshot.jev_input_tokens,
            app.state.snapshot_set.default_snapshot.jev_context_tokens) == (2048, 8192)
    changes = _changes(app)
    assert len(changes) == before_audit + 1
    assert changes[-1]["kind"] == "jev"
    assert changes[-1]["detail"] == {
        "action": "delete", "id": "jev-a", "replacement": "jev-b",
        "synced_trees": ["main"],
    }


def test_jev_delete_without_references_needs_no_confirmation_or_replacement(
        two_canvases):
    app, client = two_canvases
    assert client.post("/admin/jev", json={
        "id": "jev-orphan", "base_url": "http://jev-orphan.test",
        "model": "d1-orphan"}).status_code == 201
    before = _canvas_state(app, ("main", "other"))
    before_audit = _audit_count(app)

    deleted = client.delete("/admin/jev/jev-orphan")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "deleted": "jev-orphan", "replacement_jev_id": None, "synced_trees": []}
    assert _db_row(app, "jev_services", "id", "jev-orphan") is None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit + 1


# --------------------------------------------------------------------------- #
# 11. JEV 过期 / 缺失确认哈希：409 graph_conflict，零写入
# --------------------------------------------------------------------------- #
def test_jev_stale_or_missing_confirmation_hash_is_graph_conflict(two_canvases):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "jev_services", "id", "jev-a")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision
    preview = client.put("/admin/jev/jev-a", json={"model": "d1-a-v2"})
    actual = _tree(app, "main").document_sha256
    assert confirmation_from(preview)["trees"][0]["document_sha256"] == actual

    for bad in (
            {"trees": [{"id": "main", "document_sha256": ZERO_SHA}]},
            {"trees": []},
    ):
        response = client.put("/admin/jev/jev-a", json={
            "model": "d1-a-v2", "impact_confirmation": bad})
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "graph_conflict"
        assert detail["actual_document_sha256"] == actual
        assert detail["actual_document_sha256"] != ZERO_SHA
        assert _canvas_state(app, ("main", "other")) == before
        assert _db_row(app, "jev_services", "id", "jev-a") == before_row
        assert _audit_count(app) == before_audit
        assert app.state.snapshot_set.snapshot_revision == before_revision


# --------------------------------------------------------------------------- #
# 12. JEV 审计行：create / update / delete 都记 kind=jev 且带 synced_trees
# --------------------------------------------------------------------------- #
def test_jev_create_update_delete_write_audit_rows(two_canvases):
    app, client = two_canvases
    before = _audit_count(app)

    assert client.post("/admin/jev", json={
        "id": "jev-c", "base_url": "http://jev-c.test", "model": "d1-c",
        "input_tokens": 512, "context_tokens": 2048,
    }).status_code == 201
    updated = client.put("/admin/jev/jev-c", json={"timeout_seconds": 4.5})
    assert updated.status_code == 200, updated.text
    assert updated.json()["synced_trees"] == []
    deleted = client.delete("/admin/jev/jev-c")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "deleted": "jev-c", "replacement_jev_id": None, "synced_trees": []}

    new_changes = _changes(app)[before:]
    assert [change["kind"] for change in new_changes] == ["jev", "jev", "jev"]
    assert [change["detail"] for change in new_changes] == [
        {"action": "create", "id": "jev-c", "model": "d1-c", "synced_trees": []},
        {"action": "update", "id": "jev-c", "model": "d1-c",
         "timeout_seconds": 4.5, "key_state": "preserved", "synced_trees": []},
        {"action": "delete", "id": "jev-c", "replacement": None,
         "synced_trees": []},
    ]


# --------------------------------------------------------------------------- #
# 13. 秘密纪律：影响清单 / 替代目录 / 审计 / 画布 / 快照都不含密钥
# --------------------------------------------------------------------------- #
def test_jev_impacts_replacements_and_audit_never_expose_secrets(two_canvases):
    app, client = two_canvases
    secret = "sk-jev-impact-must-not-leak"
    # 给被画布引用的服务写入真实密钥（行级 api_key 只在数据库列里）。
    rotation = _confirmed_put(client, "/admin/jev/jev-a", {"api_key": secret})
    assert rotation.status_code == 200, rotation.text
    assert secret not in rotation.text
    conn = connect(app.state.settings.db_path)
    try:
        repo = JevServiceRepo(conn)
        service = repo.get("jev-a")
        assert repo.stored_api_key(service) == secret
    finally:
        conn.close()

    update_preview = client.put("/admin/jev/jev-a", json={"model": "d1-a-v2"})
    delete_preview = client.request("DELETE", "/admin/jev/jev-a", json={})
    assert update_preview.status_code == 409, update_preview.text
    assert delete_preview.status_code == 409, delete_preview.text

    payloads = "".join([
        update_preview.text,
        delete_preview.text,
        json.dumps(_changes(app), ensure_ascii=False),
        _canvas_bytes(app, "main").decode("utf-8"),
        _canvas_bytes(app, "other").decode("utf-8"),
        app.state.snapshot_set.default_snapshot.content,
    ])
    assert secret not in payloads
    for item in delete_preview.json()["detail"]["replacements"]:
        assert "api_key" not in item
        assert "key_env" not in item
    # 审计只记录非密钥事实，不记录 api_key 状态或明文。
    jev_changes = [change for change in _changes(app) if change["kind"] == "jev"]
    assert "api_key" not in json.dumps(jev_changes, ensure_ascii=False)
    assert "api_key" not in update_preview.text
    assert "api_key" not in delete_preview.text


# --------------------------------------------------------------------------- #
# 14. 发布门：只同步非活动画布时不发布新快照
# --------------------------------------------------------------------------- #
def test_jev_sync_of_only_inactive_canvas_does_not_publish(
        two_canvases, monkeypatch):
    app, client = two_canvases
    publisher = app.state.publisher
    real_publish = publisher.publish
    published = []

    def spy(prepared):
        published.append(prepared)
        return real_publish(prepared)

    monkeypatch.setattr(publisher, "publish", spy)
    before_revision = app.state.snapshot_set.snapshot_revision
    before_main = _canvas_bytes(app, "main")
    before_other = _canvas_bytes(app, "other")

    preview = client.put("/admin/jev/jev-b", json={"model": "d1-b-v2"})
    assert preview.status_code == 409, preview.text
    assert [item["id"] for item in preview.json()["detail"]["affected_trees"]] == [
        "other"]

    confirmed = client.put("/admin/jev/jev-b", json={
        "model": "d1-b-v2",
        "impact_confirmation": confirmation_from(preview),
    })

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["synced_trees"] == ["other"]
    assert published == []
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _canvas_bytes(app, "main") == before_main
    assert _canvas_bytes(app, "other") != before_other


# --------------------------------------------------------------------------- #
# 15. 事务内 recheck：路由层扫描后新出现引用该服务的画布 → 409 零写入
# --------------------------------------------------------------------------- #
def test_canvas_appearing_between_jev_impact_scan_and_commit_is_graph_conflict(
        two_canvases, monkeypatch):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "jev_services", "id", "jev-a")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision
    preview = client.put("/admin/jev/jev-a", json={"model": "d1-a-v2"})
    confirmation = confirmation_from(preview)

    real_impacts = routes_jev.jev_impacts
    raced = {"done": False, "sha": None}

    def racing_impacts(trees, service_id):
        impacts = real_impacts(trees, service_id)  # 旧清单：racer 画布还不存在
        if not raced["done"]:
            raced["done"] = True
            document = portable_tree_dict("racer")
            document["jev"]["id"] = "jev-a"
            created = app.state.graph_repo.create(tree_from_dict(document))
            raced["sha"] = created.document_sha256
        return impacts

    monkeypatch.setattr(routes_jev, "jev_impacts", racing_impacts)

    blocked = client.put("/admin/jev/jev-a", json={
        "model": "d1-a-v2", "impact_confirmation": confirmation})

    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "graph_conflict"
    # actual 必须是新出现画布的当前哈希，而不是客户端提交的旧哈希。
    assert detail["actual_document_sha256"] == raced["sha"]
    assert _canvas_state(app, ("main", "other")) == before
    assert _tree(app, "racer").document_sha256 == raced["sha"]
    assert _db_row(app, "jev_services", "id", "jev-a") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision


# --------------------------------------------------------------------------- #
# 16. 发布门必须在写锁内决定 + 纯数据库写不读活动指针
# --------------------------------------------------------------------------- #
def _emulate_concurrent_default_switch(app, tree_id):
    """模拟另一副本的默认图切换落盘：只写文件生命周期，不发布内存快照。

    Task 5 起多图独立启停：默认切换写目标图 ``enabled=True, default=True``，
    旧默认图保持启用但改为非默认（再启用一张图不会停用其它图）。
    """
    repo = app.state.graph_repo
    target = repo.get(tree_id)
    updates = [
        (dataclass_replace(other, enabled=True, default=False),
         other.document_sha256)
        for other in repo.enabled() if other.id != tree_id and other.default
    ]
    updates.append(
        (dataclass_replace(target, enabled=True, default=True),
         target.document_sha256))
    batch = repo.stage_updates(updates)
    batch.commit()
    batch.discard()


def test_default_switch_racing_lock_acquisition_invalidates_stale_confirmation(
        two_canvases, monkeypatch):
    """v4：生命周期写会改写目标文件里的 enabled/default，确认哈希随之失效。

    v3 时代切换默认图只写 ``current`` 指针，画布字节不变；v4 文档自带生命周期
    字段，因此「预览后、加锁前默认图被切换」必须 409 ``graph_conflict``（实际哈希
    是写后的新哈希），客户端只能重新预览确认。重新确认后发布门仍必须在锁内重读
    默认图：刚成为默认图的 other 必须被发布，而不是停留在旧的 main。
    """
    app, client = two_canvases
    publisher = app.state.publisher
    real_locked = publisher.locked
    enabled = {"done": False}

    @contextmanager
    def racing_locked():
        # 候选已在路由层构造完（只含 other）；这里在拿到写锁之前完成 enable。
        if not enabled["done"]:
            enabled["done"] = True
            _emulate_concurrent_default_switch(app, "other")
        with real_locked():
            yield publisher

    monkeypatch.setattr(publisher, "locked", racing_locked)

    preview = client.put("/admin/models/backup", json={"upstream": "raced-upstream"})
    assert preview.status_code == 409, preview.text
    assert [item["id"] for item in preview.json()["detail"]["affected_trees"]] == [
        "other"]
    before_revision = app.state.snapshot_set.snapshot_revision

    stale = client.put("/admin/models/backup", json={
        "upstream": "raced-upstream",
        "impact_confirmation": confirmation_from(preview),
    })

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "graph_conflict"
    # 实际哈希必须是 enable 改写后的新文档哈希，而不是客户端提交的旧哈希。
    assert stale.json()["detail"]["actual_document_sha256"] == hashlib.sha256(
        _canvas_bytes(app, "other")).hexdigest()
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _tree(app, "other").enabled is True

    # 重新预览并确认：生命周期已稳定，发布门必须在写锁内识别出新活动画布。
    preview = client.put("/admin/models/backup", json={"upstream": "raced-upstream"})
    assert preview.status_code == 409, preview.text
    response = client.put("/admin/models/backup", json={
        "upstream": "raced-upstream",
        "impact_confirmation": confirmation_from(preview),
    })

    assert response.status_code == 200, response.text
    assert response.json()["synced_trees"] == ["other"]
    assert app.state.snapshot_set.default_snapshot.tree_id == "other"
    assert app.state.snapshot_set.snapshot_revision != before_revision
    assert app.state.snapshot_set.default_snapshot.backends["backup"].upstream_name == "raced-upstream"
    status = app.state.publisher.status()
    assert status["default_tree_id"] == "other"
    assert status["enabled_tree_ids"] == ["main", "other"]
    assert status["snapshot_revision"] == app.state.snapshot_set.snapshot_revision
    assert _tree(app, "other").enabled is True
    assert _tree(app, "main").enabled is True
    assert _tree(app, "main").default is False


def test_db_only_write_does_not_read_legacy_current_pointer(two_canvases):
    """与图无关的纯数据库写不得因为盘上遗留的 ``current`` 指针变成 503。

    v4 仓储没有指针；回归锁定旧文件里的垃圾内容不参与任何读取。
    """
    app, client = two_canvases
    (app.state.graph_repo.root / "current").write_text("bad id!\n", encoding="utf-8")
    before_audit = _audit_count(app)

    response = client.post("/admin/models", json={
        "id": "graph-irrelevant", "upstream": "irrelevant", "provider": "local"})

    assert response.status_code == 201, response.text
    assert _db_row(app, "models", "id", "graph-irrelevant") is not None
    assert _audit_count(app) == before_audit + 1


# --------------------------------------------------------------------------- #
# 17. 锁内重查被引用的目录行：路由层校验后、加锁前被删除 → 专用错误 + 零写入
# --------------------------------------------------------------------------- #
def _db_action(app, action):
    """用独立连接执行「并发请求」的写操作。"""
    conn = connect(app.state.settings.db_path)
    try:
        action(conn)
    finally:
        conn.close()


def _race_before_lock(app, monkeypatch, action):
    """在确认写的写锁获得之前执行一次副作用，模拟路由层校验与加锁之间的并发写。"""
    publisher = app.state.publisher
    real_locked = publisher.locked
    done = {"value": False}

    @contextmanager
    def racing_locked():
        if not done["value"]:
            done["value"] = True
            action()
        with real_locked():
            yield publisher

    monkeypatch.setattr(publisher, "locked", racing_locked)
    return done


def test_model_create_rechecks_provider_under_lock(two_canvases, monkeypatch):
    app, client = two_canvases
    assert client.put("/admin/providers/race-prov", json={
        "base_url": "http://race-prov.test/v1"}).status_code == 200
    before = _canvas_state(app, ("main", "other"))
    before_audit = _audit_count(app)
    _race_before_lock(app, monkeypatch, lambda: _db_action(
        app, lambda conn: ProviderRepo(conn).delete("race-prov")))

    response = client.post("/admin/models", json={
        "id": "race-model", "upstream": "race-upstream", "provider": "race-prov"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown provider race-prov"
    assert _db_row(app, "models", "id", "race-model") is None
    assert _audit_count(app) == before_audit
    assert _canvas_state(app, ("main", "other")) == before


def test_model_update_rechecks_new_provider_under_lock(two_canvases, monkeypatch):
    app, client = two_canvases
    assert client.put("/admin/providers/race-prov", json={
        "base_url": "http://race-prov.test/v1"}).status_code == 200
    before_canvas = _canvas_bytes(app, "main")
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    preview = client.put("/admin/models/fast", json={"provider": "race-prov"})
    confirmation = confirmation_from(preview)
    _race_before_lock(app, monkeypatch, lambda: _db_action(
        app, lambda conn: ProviderRepo(conn).delete("race-prov")))

    response = client.put("/admin/models/fast", json={
        "provider": "race-prov", "impact_confirmation": confirmation})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown provider race-prov"
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _canvas_bytes(app, "main") == before_canvas
    assert _audit_count(app) == before_audit


def test_delete_model_rechecks_replacement_under_lock(two_canvases, monkeypatch):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_audit = _audit_count(app)
    preview = client.delete("/admin/models/fast")
    confirmation = confirmation_from(preview)
    _race_before_lock(app, monkeypatch, lambda: _db_action(
        app, lambda conn: ModelRepo(conn).delete("qwen3-32b")))

    response = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "qwen3-32b",
        "impact_confirmation": confirmation,
    })

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "unknown model qwen3-32b"
    assert _db_row(app, "models", "id", "fast") is not None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit


def test_delete_jev_rechecks_replacement_under_lock(two_canvases, monkeypatch):
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_audit = _audit_count(app)
    preview = client.request("DELETE", "/admin/jev/jev-a", json={})
    confirmation = confirmation_from(preview)
    _race_before_lock(app, monkeypatch, lambda: _db_action(
        app, lambda conn: JevServiceRepo(conn).delete("jev-b")))

    response = client.request("DELETE", "/admin/jev/jev-a", json={
        "replacement_jev_id": "jev-b",
        "impact_confirmation": confirmation,
    })

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "unknown jev service jev-b"
    assert _db_row(app, "jev_services", "id", "jev-a") is not None
    assert _db_row(app, "jev_services", "id", "jev-b") is None
    assert _canvas_state(app, ("main", "other")) == before
    assert _audit_count(app) == before_audit


# --------------------------------------------------------------------------- #
# 18. 确认同步的提交后原子性：图批次 commit 之后的失败仍整体回滚
# --------------------------------------------------------------------------- #
def test_failure_after_graph_commit_restores_files_db_audit_and_snapshot(
        two_canvases, monkeypatch):
    """图批次 commit 之后的快照构造/指纹复核失败：文件恢复、数据库回滚、不发布。"""
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    publisher = app.state.publisher
    real_publish = publisher.publish
    published = []
    monkeypatch.setattr(
        publisher, "publish",
        lambda prepared: published.append(prepared) or real_publish(prepared))

    def fail_after_commit(self, _prepared):
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")

    monkeypatch.setattr(ConfigurationService, "_recheck_fingerprint", fail_after_commit)

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    assert published == []
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _canvas_state(app, ("main", "other")) == before


def test_audit_failure_inside_transaction_rolls_back_everything(
        two_canvases, monkeypatch):
    """事务内审计失败：候选文件恢复、数据库行回滚、快照不发布。"""
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = app.state.snapshot_set.snapshot_revision
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    def failing_record(self, ts, kind, detail):
        raise RuntimeError(f"injected audit failure for {kind}")
    monkeypatch.setattr(ChangeRepo, "record", failing_record)
    publisher = app.state.publisher
    real_publish = publisher.publish
    published = []
    monkeypatch.setattr(
        publisher, "publish",
        lambda prepared: published.append(prepared) or real_publish(prepared))

    failing_client = TestClient(app, raise_server_exceptions=False)
    response = failing_client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 500, response.text
    assert published == []
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert _canvas_state(app, ("main", "other")) == before


def test_rollback_failure_returns_503_and_refreshes_after_db_rollback(
        two_canvases, monkeypatch):
    """图文件回滚失败：503，且 ``publisher.refresh`` 必须在数据库 ROLLBACK 之后。"""
    app, client = two_canvases
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    def fail_after_commit(self, _prepared):
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")
    monkeypatch.setattr(ConfigurationService, "_recheck_fingerprint", fail_after_commit)

    def failing_rollback(self):
        raise GraphStorageError("injected graph rollback failure")
    monkeypatch.setattr(GraphBatch, "rollback", failing_rollback)

    publisher = app.state.publisher
    real_refresh = publisher.refresh
    seen = []

    def spy_refresh(conn):
        # 事务内的刷新会看到未提交的数据库状态；修复要求先把 ROLLBACK 做完。
        seen.append(conn.in_transaction)
        return real_refresh(conn)

    monkeypatch.setattr(publisher, "refresh", spy_refresh)

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    assert seen == [False]
    # 数据库仍整体回滚；审计行不存在。文件回滚失败正是本夹具的注入点。
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit


# --------------------------------------------------------------------------- #
# 19. 目录影响同步：必须重建「完整启用集合」路由集（多启用图）
# --------------------------------------------------------------------------- #
def _portable_tree_for_provider(tree_id: str, provider: str,
                                base_url: str) -> dict:
    """自包含便携画布，把运行时目录换成指定 provider（用于区分目录影响范围）。"""
    document = portable_tree_dict(tree_id)
    document["backends"]["providers"][0].update(
        {"name": provider, "base_url": base_url})
    document["backends"]["models"][0]["provider"] = provider
    return document


@pytest.fixture
def multi_graph_canvases(tmp_path):
    """三张自包含画布：

    - ``main``：启用且默认，provider ``main-local``（不受 ``local`` 目录写影响）；
    - ``finance``：启用非默认，provider ``local``（受 ``local`` 目录写影响）；
    - ``draft``：未启用，provider ``draft-local``（不受 ``local`` 影响）。

    默认图不在受影响候选里，正是旧发布门（只看默认图是否被改写）漏发布的场景。
    """
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    for tree_id, provider, base_url in (
        ("main", "main-local", "http://main-local.test/v1"),
        ("finance", "local", "http://json-only.test/v1"),
        ("draft", "draft-local", "http://draft-local.test/v1"),
    ):
        created = client.post("/admin/trees", json=_strip_enabled(
            _portable_tree_for_provider(tree_id, provider, base_url)))
        assert created.status_code == 201, created.text
    assert client.post("/admin/trees/main/enable").status_code == 200
    assert client.post("/admin/trees/finance/enable").status_code == 200
    return app, client


def _confirmed_provider_put(client, name: str, payload: dict):
    """先取 409 影响预览，再带预览里的基线哈希确认重试。"""
    preview = client.put(f"/admin/providers/{name}", json=payload)
    assert preview.status_code == 409, preview.text
    return client.put(f"/admin/providers/{name}", json={
        **payload, "impact_confirmation": confirmation_from(preview)})


def test_catalog_sync_updates_each_enabled_graph_but_not_draft(
        multi_graph_canvases):
    """目录写只改写非默认启用图时，也要重建完整启用路由集并发布。

    旧发布门只在「默认图是候选」时发布：这里默认图 ``main`` 不受 provider
    ``local`` 目录写影响，受影响的是非默认启用图 ``finance``；修复前磁盘画布
    已更新但内存路由集仍是旧快照，直到 reloader 轮询。
    """
    app, client = multi_graph_canvases
    before_set = app.state.snapshot_set
    before_draft = _tree(app, "draft").document_sha256
    assert before_set.default_tree_id == "main"
    assert sorted(before_set.snapshots) == ["finance", "main"]
    assert before_set.snapshots["finance"].backends["fast"].url == \
        "http://json-only.test/v1/chat/completions"

    response = _confirmed_provider_put(client, "local", {
        "base_url": "http://changed.test/v1",
        "reasoning_format": "openai",
    })

    assert response.status_code == 200, response.text
    assert response.json()["synced_trees"] == ["finance"]
    route_set = app.state.snapshot_set
    # 非默认启用图被改写 → 必须发布新的完整路由集（不是只等 reloader 轮询）。
    assert route_set is not before_set
    assert route_set.snapshot_revision != before_set.snapshot_revision
    assert route_set.snapshots["finance"].backends["fast"].url == \
        "http://changed.test/v1/chat/completions"
    # 默认图快照仍来自它自己的画布配置，不被别的图的目录写牵连。
    assert route_set.default_snapshot.tree_id == "main"
    assert route_set.default_snapshot.backends["fast"].url == \
        "http://main-local.test/v1/chat/completions"
    assert app.state.publisher.status()["snapshot_revision"] == \
        route_set.snapshot_revision
    # 草稿既不在候选里，也不被改写。
    assert app.state.graph_repo.get("draft").document_sha256 == before_draft


def test_draft_only_catalog_sync_does_not_republish(multi_graph_canvases):
    """只有草稿被目录写影响时：候选快照仍按完整启用集复核，但绝不发布。

    发布门看的是「候选是否改写了启用图」，不是「是否有候选」：草稿同步不得
    触发不必要的路由集替换（对象身份与 revision 都必须保持不变）。
    """
    app, client = multi_graph_canvases
    before_set = app.state.snapshot_set
    before_revision = before_set.snapshot_revision
    before_draft = _canvas_bytes(app, "draft")

    response = _confirmed_provider_put(client, "draft-local", {
        "base_url": "http://draft-changed.test/v1",
    })

    assert response.status_code == 200, response.text
    assert response.json()["synced_trees"] == ["draft"]
    assert _canvas_bytes(app, "draft") != before_draft
    assert app.state.snapshot_set is before_set
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert app.state.publisher.status()["snapshot_revision"] == before_revision


# --------------------------------------------------------------------------- #
# 20. 图批次暂存后的异常必须撤销暂存文件（隐藏临时文件不得泄漏）
# --------------------------------------------------------------------------- #
def test_stage_failure_before_batch_commit_discards_staged_temp_files(
        two_canvases, monkeypatch):
    """``stage_updates()`` 之后、批次交出之前的异常不得把暂存临时文件留在盘上。"""
    app, client = two_canvases
    repo = app.state.graph_repo
    before = _canvas_state(app, ("main", "other"))
    before_revision = app.state.snapshot_set.snapshot_revision
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    def fail_after_staging():
        raise RuntimeError("injected failure after graph staging")
    # 发布门在 stage_updates() 之后读取启用集合：这里注入异常，模拟暂存完成
    # 但批次尚未交给外层事务时的失败窗口。
    monkeypatch.setattr(repo, "enabled", fail_after_staging)

    with pytest.raises(RuntimeError, match="injected failure after graph staging"):
        client.put("/admin/models/fast", json={
            "upstream": "new-upstream", "impact_confirmation": confirmation})

    # 暂存文件与已提交画布都没有残留；快照也未发布。
    assert [path.name for path in repo.graphs.iterdir()
            if path.name.startswith(".")] == []
    assert _canvas_state(app, ("main", "other")) == before
    assert app.state.snapshot_set.snapshot_revision == before_revision
