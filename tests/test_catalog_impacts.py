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
from dataclasses import replace as dataclass_replace

import pytest
from fastapi.testclient import TestClient

from router.api import routes_jev, routes_models
from router.api.app import create_app
from router.control.impacts import provider_impacts
from router.core_client import CoreBatchError
from router.routing.graph.model import TargetNode
from router.routing.tree import tree_from_dict
from router.store.db import connect
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ChangeRepo, ModelRepo, ProviderRepo
from router.store.routing_trees import GraphStorageError
from tests.routing_fixtures import (
    kernel_state,
    core_app,
    core_app_for,
    portable_tree_dict,
    test_settings,
)

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
    return (kernel_state(app).graph_repo.graphs / f"{tree_id}.json").read_bytes()


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
    return kernel_state(app).graph_repo.get(tree_id)


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
    # 跨服务目录写：真实 create_app + 进程内内核（共享同一图目录）。内核实例留在
    # ``app.test_kernel`` 供故障注入用例（内核侧图写失败、临时文件清理）。
    kernel = core_app(tmp_path)
    app = create_app(
        test_settings(tmp_path),
        core_transport=core_app_for(tmp_path, app=kernel))
    app.test_kernel = kernel
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision

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
    assert kernel_state(app).snapshot_set.default_snapshot.backends["fast"].upstream_name == "new-upstream"
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
    assert "fast" not in kernel_state(app).snapshot_set.default_snapshot.backends
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision


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
    """内核批次写第二份文件失败：内核回滚整批，SQLite/审计/发布集都不变。

    跨服务后图写入在内核：故障注入点从外围仓储移到大内核仓储。内核批次自身
    恢复已替换的第一份文件，外围 SQLite 写事务整体回滚，两者不会留下半批。
    """
    app, client = two_canvases
    second = portable_tree_dict("second-fast")
    second["jev"]["id"] = "jev-b"
    created = client.post("/admin/trees", json=_strip_enabled(second))
    assert created.status_code == 201, created.text
    tree_ids = ("main", "second-fast", "other")
    before = _canvas_state(app, tree_ids)
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    assert preview.status_code == 409
    assert {item["id"]
            for item in preview.json()["detail"]["affected_trees"]} == {
        "main", "second-fast"}
    confirmation = confirmation_from(preview)

    kernel_repo = kernel_state(app).graph_repo
    real_replace = kernel_repo._replace_staged
    calls = {"count": 0}

    def failing_replace(staged_path, path):
        calls["count"] += 1
        if calls["count"] >= 2:            # 第二份文件（默认图最后写）失败
            raise OSError("injected graph write failure")
        return real_replace(staged_path, path)

    monkeypatch.setattr(kernel_repo, "_replace_staged", failing_replace)

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    assert calls["count"] == 2
    # 数据库 / 审计 / 快照 / 三个画布文件全部保持原值（含内核已替换的第一份）。
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert _canvas_state(app, tree_ids) == before


def test_candidate_validation_failure_writes_nothing(tmp_path):
    """provider 推理协议改为 openai 让画布的 null-wire 档位非法 → 422 全回滚。"""
    kernel = core_app(tmp_path)
    app = create_app(
        test_settings(tmp_path), core_transport=core_app_for(tmp_path, app=kernel))
    app.test_kernel = kernel
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision

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
    assert kernel_state(app).snapshot_set.default_snapshot.backends["fast"].url == \
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
    before_audit = _audit_count(app)
    bare_impacts = provider_impacts(kernel_state(app).graph_repo.list(), "bare-prov")
    assert [item["id"] for item in bare_impacts] == ["bare-canvas"]
    assert bare_impacts[0]["usage"]["models"] == {"use": []}
    deleted_bare = client.delete("/admin/providers/bare-prov")
    assert deleted_bare.status_code == 200, deleted_bare.text
    assert _db_row(app, "providers", "name", "bare-prov") is None
    assert _canvas_bytes(app, "bare-canvas") == before_bare
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
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
    assert secret not in kernel_state(app).snapshot_set.default_snapshot.content


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
    kernel = core_app(tmp_path)
    app = create_app(
        test_settings(tmp_path), core_transport=core_app_for(tmp_path, app=kernel))
    app.test_kernel = kernel
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
    assert {tree.id for tree in kernel_state(app).graph_repo.list()} == {"main", "closed"}

    before_provider = _db_row(app, "providers", "name", "local")
    before_files = _canvas_state(app, ("main", "closed"))
    before_audit = _audit_count(app)
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    # 所有画布仍可读（fix 前闭画布已被写坏，这里会抛 GraphStorageError）。
    assert {tree.id for tree in kernel_state(app).graph_repo.list()} == {"main", "closed"}
    assert client.get("/admin/trees").status_code == 200


def test_dialect_flip_invalidating_active_canvas_is_422_not_503(tmp_path):
    """CRITICAL 1（活动画布变体）：fix 前候选写盘后重读活动画布抛
    ``GraphStorageError`` → 503；严格候选校验必须在写盘前把它变成 422 并回滚。"""
    kernel = core_app(tmp_path)
    app = create_app(
        test_settings(tmp_path), core_transport=core_app_for(tmp_path, app=kernel))
    app.test_kernel = kernel
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert {tree.id for tree in kernel_state(app).graph_repo.list()} == {"main"}


def test_canvas_appearing_between_route_scan_and_commit_returns_graph_conflict(
        two_canvases, monkeypatch):
    """CRITICAL 2：路由层影响扫描之后、提交加锁之前新建的画布若引用被改模型，
    必须在锁内、事务内 recheck 时变成 409 ``graph_conflict``；fix 前数据库会被
    改写而新画布漏同步。"""
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    real_impacts = routes_models.model_impacts
    raced = {"done": False, "sha": None}

    def racing_impacts(trees, model_id):
        impacts = real_impacts(trees, model_id)   # 旧清单：racer 画布还不存在
        if not raced["done"]:
            raced["done"] = True
            created = kernel_state(app).graph_repo.create(
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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision


def test_canvas_mutated_between_impact_scan_and_stage_returns_graph_conflict(
        two_canvases, monkeypatch):
    """影响清单生成后、内核批次核对基线前画布被改写 → 409 ``graph_conflict``。

    跨服务后「暂存前基线门」由内核图批次承担：内核发现任一基线过期就整批拒绝，
    SQLite 目录写事务回滚，审计/发布集不变。
    """
    app, client = two_canvases
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

    real_batch = app.state.core.replace_graph_batch
    raced = {}

    async def racing_batch(writes):
        # 目录行已在 SQLite 事务内写入、内核批次尚未执行时，另一个进程改写活动
        # 画布；候选仍以旧 document_sha256 为基线。
        if not raced:
            current = kernel_state(app).graph_repo.get("main")
            replaced = kernel_state(app).graph_repo.update(
                dataclass_replace(current, name="mutated-after-impacts"),
                base_document_sha256=current.document_sha256)
            raced["sha"] = replaced.document_sha256
        return await real_batch(writes)

    monkeypatch.setattr(app.state.core, "replace_graph_batch", racing_batch)

    blocked = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "graph_conflict"
    assert detail["actual_document_sha256"] == raced["sha"]
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert _canvas_bytes(app, "other") == before["other"]


def test_confirmed_write_touching_only_inactive_canvas_keeps_the_route_set(
        two_canvases, monkeypatch):
    """确认写只改了非活动画布：发布的路由集内容/revision 保持不变。

    内核图批次总是按完整启用集合重建（Task 7 语义），但当候选全是草稿时重建
    结果与旧路由集内容一致，因此 revision 不变、活动图的运行配置不被牵连。
    """
    app, client = two_canvases
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision

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
    assert kernel_state(app).snapshot_set.default_snapshot.jev.base_url == "http://jev-a-v2.test"
    assert kernel_state(app).snapshot_set.default_snapshot.jev.model == "d1-a-v2"
    assert (kernel_state(app).snapshot_set.default_snapshot.jev_input_tokens,
            kernel_state(app).snapshot_set.default_snapshot.jev_context_tokens) == (3072, 8192)
    assert kernel_state(app).snapshot_set.snapshot_revision != before_revision
    changes = _changes(app)
    assert len(changes) == before_audit + 1
    assert changes[-1]["kind"] == "jev"
    # 更新审计只记录变化的非密钥目录字段（v7 起目录里没有密钥或密钥意图）。
    assert changes[-1]["detail"] == {
        "action": "update", "id": "jev-a", "model": "d1-a-v2",
        "base_url": "http://jev-a-v2.test",
        "instructions": instructions,
        "state_template": state_template,
        "timeout_seconds": 7.5,
        "input_tokens": 3072,
        "context_tokens": 8192,
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
    # 目录服务同步进画布快照：key_env 固定内核环境变量名（v7 不再存密钥）。
    assert runtime.jev.key_env == "ROUTER_JEV_API_KEY"
    assert _tree(app, "other").runtime.jev.id == "jev-b"
    assert _tree(app, "other").runtime.jev.base_url == "http://jev-json.test"
    assert (kernel_state(app).snapshot_set.default_snapshot.jev_input_tokens,
            kernel_state(app).snapshot_set.default_snapshot.jev_context_tokens) == (768, 3072)


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
    assert kernel_state(app).snapshot_set.default_snapshot.jev.base_url == "http://jev-b.test"
    assert (kernel_state(app).snapshot_set.default_snapshot.jev_input_tokens,
            kernel_state(app).snapshot_set.default_snapshot.jev_context_tokens) == (2048, 8192)
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
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
        assert kernel_state(app).snapshot_set.snapshot_revision == before_revision


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
         "timeout_seconds": 4.5, "synced_trees": []},
        {"action": "delete", "id": "jev-c", "replacement": None,
         "synced_trees": []},
    ]


# --------------------------------------------------------------------------- #
# 13. 秘密纪律：影响清单 / 替代目录 / 审计 / 画布 / 快照都不含密钥
# --------------------------------------------------------------------------- #
def test_jev_impacts_replacements_and_audit_never_expose_secrets(two_canvases):
    app, client = two_canvases
    secret = "sk-jev-impact-must-not-leak"
    # v7 起目录不再有密钥通道：带 api_key 的写请求是未知字段（422），零写入。
    before_row = _db_row(app, "jev_services", "id", "jev-a")
    rejected = client.put("/admin/jev/jev-a", json={"model": "d1-a-v2",
                                                    "api_key": secret})
    assert rejected.status_code == 422, rejected.text
    assert _db_row(app, "jev_services", "id", "jev-a") == before_row

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
        kernel_state(app).snapshot_set.default_snapshot.content,
    ])
    assert secret not in payloads
    for item in delete_preview.json()["detail"]["replacements"]:
        assert "api_key" not in item
        assert "key_env" not in item
    # 目录行无 api_key 列，审计/响应也不含密钥字段。
    conn = connect(app.state.settings.db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jev_services)")}
    finally:
        conn.close()
    assert "api_key" not in columns
    jev_changes = [change for change in _changes(app) if change["kind"] == "jev"]
    assert "api_key" not in json.dumps(jev_changes, ensure_ascii=False)
    assert "api_key" not in update_preview.text
    assert "api_key" not in delete_preview.text


# --------------------------------------------------------------------------- #
# 14. 发布门：只同步非活动画布时不发布新快照
# --------------------------------------------------------------------------- #
def test_jev_sync_of_only_inactive_canvas_keeps_the_route_set(
        two_canvases, monkeypatch):
    """JEV 同步只改非活动画布：完整启用集合重建后的 revision 保持不变。"""
    app, client = two_canvases
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
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
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
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
            created = kernel_state(app).graph_repo.create(tree_from_dict(document))
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
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision


# --------------------------------------------------------------------------- #
# 16. 发布门必须在写锁内决定 + 纯数据库写不读活动指针
# --------------------------------------------------------------------------- #
def _emulate_concurrent_default_switch(app, tree_id):
    """模拟另一副本的默认图切换落盘：只写文件生命周期，不发布内存快照。

    Task 5 起多图独立启停：默认切换写目标图 ``enabled=True, default=True``，
    旧默认图保持启用但改为非默认（再启用一张图不会停用其它图）。
    """
    repo = kernel_state(app).graph_repo
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
        two_canvases):
    """v4：生命周期写会改写目标文件里的 enabled/default，确认哈希随之失效。

    v3 时代切换默认图只写 ``current`` 指针，画布字节不变；v4 文档自带生命周期
    字段，因此「预览后、提交前默认图被切换」必须 409 ``graph_conflict``（实际哈希
    是写后的新哈希），客户端只能重新预览确认。重新确认后内核写入活动画布，外围
    快照刷新必须发布新的默认图，而不是停留在旧的 main。

    跨服务后不再有「提交写锁」可插入并发动作：生命周期写发生在预览与确认之间，
    过期确认会先被 SQLite 事务内的 recheck 拒绝。
    """
    app, client = two_canvases
    preview = client.put("/admin/models/backup", json={"upstream": "raced-upstream"})
    assert preview.status_code == 409, preview.text
    assert [item["id"] for item in preview.json()["detail"]["affected_trees"]] == [
        "other"]
    before_revision = kernel_state(app).snapshot_set.snapshot_revision

    # 预览之后、确认之前：另一个副本切换默认图（写文件生命周期，不发布内存快照）。
    _emulate_concurrent_default_switch(app, "other")

    stale = client.put("/admin/models/backup", json={
        "upstream": "raced-upstream",
        "impact_confirmation": confirmation_from(preview),
    })

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "graph_conflict"
    # 实际哈希必须是生命周期改写后的新文档哈希，而不是客户端提交的旧哈希。
    assert stale.json()["detail"]["actual_document_sha256"] == hashlib.sha256(
        _canvas_bytes(app, "other")).hexdigest()
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert _tree(app, "other").enabled is True

    # 重新预览并确认：生命周期已稳定，提交后发布新的默认图。
    preview = client.put("/admin/models/backup", json={"upstream": "raced-upstream"})
    assert preview.status_code == 409, preview.text
    response = client.put("/admin/models/backup", json={
        "upstream": "raced-upstream",
        "impact_confirmation": confirmation_from(preview),
    })

    assert response.status_code == 200, response.text
    assert response.json()["synced_trees"] == ["other"]
    assert kernel_state(app).snapshot_set.default_snapshot.tree_id == "other"
    assert kernel_state(app).snapshot_set.snapshot_revision != before_revision
    assert kernel_state(app).snapshot_set.default_snapshot.backends["backup"].upstream_name == "raced-upstream"
    status = kernel_state(app).publisher.status()
    assert status["default_tree_id"] == "other"
    assert status["enabled_tree_ids"] == ["main", "other"]
    assert status["snapshot_revision"] == kernel_state(app).snapshot_set.snapshot_revision
    assert _tree(app, "other").enabled is True
    assert _tree(app, "main").enabled is True
    assert _tree(app, "main").default is False


def test_db_only_write_does_not_read_legacy_current_pointer(two_canvases):
    """与图无关的纯数据库写不得因为盘上遗留的 ``current`` 指针变成 503。

    v4 仓储没有指针；回归锁定旧文件里的垃圾内容不参与任何读取。
    """
    app, client = two_canvases
    (kernel_state(app).graph_repo.root / "current").write_text("bad id!\n", encoding="utf-8")
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
    """在确认写的 SQLite 写事务开启之前执行一次副作用。

    跨服务后「写锁」是 SQLite 的 ``BEGIN IMMEDIATE``（内核图写由内核批次自己
    串行）；路由层校验与事务内 recheck 之间的并发写用模块级 ``write_transaction``
    的包装注入，语义与旧的 publish 锁等价：副作用发生在 recheck 之前。
    """
    from router.control import remote_catalog_sync as remote

    real_transaction = remote.write_transaction
    done = {"value": False}

    def racing_write_transaction(conn):
        if not done["value"]:
            done["value"] = True
            action()
        return real_transaction(conn)

    monkeypatch.setattr(remote, "write_transaction", racing_write_transaction)
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
def test_kernel_publication_failure_rolls_back_the_whole_catalog_write(
        two_canvases, monkeypatch):
    """内核批次落盘后的发布失败：整批回滚 + SQLite 回滚，两侧都不留半份写。

    跨服务后发布发生在内核批次提交的同一次调用里（Task 7 语义）；发布失败让内核
    回滚本批文件，外围收到 503 后回滚 SQLite —— 目录行、审计、画布与快照全部
    保持写入前状态。
    """
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    def fail_publish(prepared):
        raise GraphStorageError("injected snapshot publication failure")

    monkeypatch.setattr(kernel_state(app).publisher, "publish", fail_publish)

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert _canvas_state(app, ("main", "other")) == before


def test_audit_failure_inside_transaction_rolls_back_everything(
        two_canvases, monkeypatch):
    """事务内审计失败：候选文件恢复、数据库行回滚、快照不发布。"""
    app, client = two_canvases
    before = _canvas_state(app, ("main", "other"))
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    def failing_record(self, ts, kind, detail):
        raise RuntimeError(f"injected audit failure for {kind}")
    monkeypatch.setattr(ChangeRepo, "record", failing_record)

    failing_client = TestClient(app, raise_server_exceptions=False)
    response = failing_client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 500, response.text
    assert _db_row(app, "models", "id", "fast") == before_row
    assert _audit_count(app) == before_audit
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert _canvas_state(app, ("main", "other")) == before


def test_compensation_failure_is_503_catalog_sync_diverged_after_db_rollback(
        two_canvases, monkeypatch):
    """内核已提交但 SQLite 失败 → 反向批次补偿；补偿也失败必须显式 503。

    语义：SQLite 已回滚（目录行/审计不存在），外围快照刷新发生在回滚之后；
    ``catalog_sync_diverged`` 提示两侧可能不一致，绝不返回成功。
    """
    app, client = two_canvases
    before_row = _db_row(app, "models", "id", "fast")
    before_audit = _audit_count(app)
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    real_batch = app.state.core.replace_graph_batch
    calls = {"count": 0}
    db_state_seen = []

    async def batch_with_failing_compensation(writes):
        calls["count"] += 1
        if calls["count"] == 1:
            return await real_batch(writes)
        # 反向补偿发生在 SQLite ROLLBACK 之后：新连接上读不到本次目录写。
        probe = connect(app.state.settings.db_path)
        try:
            row = probe.execute(
                "SELECT upstream FROM models WHERE id = 'fast'").fetchone()
            db_state_seen.append(row["upstream"])
        finally:
            probe.close()
        raise CoreBatchError(503)

    monkeypatch.setattr(
        app.state.core, "replace_graph_batch", batch_with_failing_compensation)

    # 审计在 SQLite 事务内失败 → 内核已提交 → 反向补偿失败 → 分歧。
    def failing_record(self, ts, kind, detail):
        raise RuntimeError(f"injected audit failure for {kind}")

    monkeypatch.setattr(ChangeRepo, "record", failing_record)

    failing_client = TestClient(app, raise_server_exceptions=False)
    response = failing_client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {"code": "catalog_sync_diverged"}
    assert calls["count"] == 2
    assert db_state_seen == [before_row["upstream"]]
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
    kernel = core_app(tmp_path)
    app = create_app(
        test_settings(tmp_path), core_transport=core_app_for(tmp_path, app=kernel))
    app.test_kernel = kernel
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
    before_set = kernel_state(app).snapshot_set
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
    route_set = kernel_state(app).snapshot_set
    # 非默认启用图被改写 → 必须发布新的完整路由集（不是只等 reloader 轮询）。
    assert route_set is not before_set
    assert route_set.snapshot_revision != before_set.snapshot_revision
    assert route_set.snapshots["finance"].backends["fast"].url == \
        "http://changed.test/v1/chat/completions"
    # 默认图快照仍来自它自己的画布配置，不被别的图的目录写牵连。
    assert route_set.default_snapshot.tree_id == "main"
    assert route_set.default_snapshot.backends["fast"].url == \
        "http://main-local.test/v1/chat/completions"
    assert kernel_state(app).publisher.status()["snapshot_revision"] == \
        route_set.snapshot_revision
    # 草稿既不在候选里，也不被改写。
    assert kernel_state(app).graph_repo.get("draft").document_sha256 == before_draft


def test_draft_only_catalog_sync_does_not_republish(multi_graph_canvases):
    """只有草稿被目录写影响时：重建的路由集内容与旧路由集完全一致。

    内核图批次按完整启用集合重建（Task 7 语义），但草稿不在启用集合里，因此
    重建结果内容不变——``snapshot_revision`` 与发布集内容都必须保持不变。
    """
    app, client = multi_graph_canvases
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
    before_draft = _canvas_bytes(app, "draft")

    response = _confirmed_provider_put(client, "draft-local", {
        "base_url": "http://draft-changed.test/v1",
    })

    assert response.status_code == 200, response.text
    assert response.json()["synced_trees"] == ["draft"]
    assert _canvas_bytes(app, "draft") != before_draft
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
    assert kernel_state(app).publisher.status()["snapshot_revision"] == before_revision


# --------------------------------------------------------------------------- #
# 20. 图批次暂存后的异常必须撤销暂存文件（隐藏临时文件不得泄漏）
# --------------------------------------------------------------------------- #
def test_kernel_batch_failure_leaves_no_staged_temp_files(
        two_canvases, monkeypatch):
    """内核 ``GraphBatch`` 失败后不得把同目录暂存临时文件留在图目录里。

    跨服务后暂存在内核侧；提交失败由内核回滚批次并丢弃暂存文件，外围 SQLite
    事务回滚、发布集不变。
    """
    app, client = two_canvases
    kernel_repo = kernel_state(app).graph_repo
    before = _canvas_state(app, ("main", "other"))
    before_revision = kernel_state(app).snapshot_set.snapshot_revision
    preview = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    confirmation = confirmation_from(preview)

    def failing_replace(staged_path, path):
        raise OSError("injected failure after graph staging")
    monkeypatch.setattr(kernel_repo, "_replace_staged", failing_replace)

    response = client.put("/admin/models/fast", json={
        "upstream": "new-upstream", "impact_confirmation": confirmation})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    # 暂存文件与已提交画布都没有残留；快照也未发布。
    assert [path.name for path in kernel_repo.graphs.iterdir()
            if path.name.startswith(".")] == []
    assert _canvas_state(app, ("main", "other")) == before
    assert kernel_state(app).snapshot_set.snapshot_revision == before_revision
