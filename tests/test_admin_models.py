"""Admin API: model catalog, model CRUD, provider CRUD (key tri-state),
fetch-models (MockTransport), v9 migration.  Reasoning-profile validation is
still covered here through the model CRUD endpoints; the retired
/admin/pipeline reasoning health tests were removed with that endpoint
(equivalent coverage: tests/test_unified_tree_validation.py and
tests/test_routing_trees_admin.py).
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api import routes_models
from router.api.app import create_app
from router.reasoning import FORMAT_IDS, LEVELS
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.repositories import ModelRepo, ProviderRepo, seed_default
from router.store.routing_trees import RoutingTreeRepo
from router.upstream.catalog import load_backends
from tests.routing_fixtures import (
    portable_tree_dict,
    runtime_for_models,
    test_settings,
)

OPENAI_REASONING = {"reasoning": {
    "efforts": {"off": None, "low": "low", "high": "high"},
    "default": "high"}}


def make_app(tmp_path, handler=None):
    transport = httpx.MockTransport(handler) if handler is not None else None
    return create_app(test_settings(tmp_path, db_path=str(tmp_path / "t.db")), transport=transport)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


def _confirmation_from(response) -> dict:
    """409 影响预览 → 携带当前画布哈希的确认载荷。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required", response.text
    return {"trees": [
        {"id": item["id"], "document_sha256": item["document_sha256"]}
        for item in detail["affected_trees"]
    ]}


def put_with_confirmation(client, path: str, payload: dict):
    """目录写两阶段：先 409 预览，再用预览哈希确认重试。"""
    preview = client.put(path, json=payload)
    if preview.status_code != 409:
        return preview
    return client.put(path, json={
        **payload, "impact_confirmation": _confirmation_from(preview)})


# --------------------------------------------------------------------------- #
# 预置目录
# --------------------------------------------------------------------------- #
def test_model_catalog_shape(client):
    body = client.get("/admin/model-catalog").json()
    assert body["providers"], "presets must not be empty"
    assert body["levels"] == list(LEVELS)
    assert [f["id"] for f in body["formats"]] == list(FORMAT_IDS)
    for fmt in body["formats"]:
        assert fmt["zh"] and fmt["en"]
    for provider in body["providers"]:
        assert {"id", "name", "base_url", "key_env", "reasoning_format",
                "key_env_present", "models"} <= set(provider)
        assert provider["reasoning_format"] in FORMAT_IDS
        assert isinstance(provider["key_env_present"], bool)
        assert provider["models"]
        for model in provider["models"]:
            assert {"id", "name", "context_window", "max_tokens", "tier",
                    "domains", "profile", "reasoning"} <= set(model)
            reasoning = model["reasoning"]
            assert {"declared", "supported", "format", "efforts",
                    "default"} <= set(reasoning)
            # every preset declaration must be legal (and thus declared/supported)
            if model["profile"].get("reasoning") not in (None, False):
                assert reasoning["declared"] is True
                assert reasoning["supported"] is True
                assert reasoning["format"] in FORMAT_IDS


def test_catalog_key_env_present_reflects_server_env(client, monkeypatch):
    providers = client.get("/admin/model-catalog").json()["providers"]
    provider = next(p for p in providers if p["key_env"])
    monkeypatch.setenv(provider["key_env"], "x")
    again = {p["id"]: p for p in
             client.get("/admin/model-catalog").json()["providers"]}
    assert again[provider["id"]]["key_env_present"] is True
    monkeypatch.delenv(provider["key_env"])
    cleared = {p["id"]: p for p in
               client.get("/admin/model-catalog").json()["providers"]}
    assert cleared[provider["id"]]["key_env_present"] is False


# --------------------------------------------------------------------------- #
# GET /admin/models
# --------------------------------------------------------------------------- #
def test_get_models_carries_normalized_reasoning(client):
    models = {m["id"]: m for m in client.get("/admin/models").json()["models"]}
    # seeded models declare nothing -> console renders 未声明
    for model in models.values():
        assert {"declared", "supported", "format", "efforts",
                "default"} <= set(model["reasoning"])
    undeclared = models["qwen3-8b"]["reasoning"]
    assert undeclared == {"declared": False, "supported": None,
                          "format": "openai", "efforts": [], "default": None}


def test_get_models_reports_illegal_old_declaration_with_error(tmp_path):
    path = str(tmp_path / "t.db")
    make_app(tmp_path)
    conn = connect(path)
    with conn:
        conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                     (json.dumps({"reasoning": {"format": "nope",
                                                "efforts": {"high": "high"}}}),))
    conn.close()
    models = {m["id"]: m
              for m in TestClient(make_app(tmp_path)).get(
                  "/admin/models").json()["models"]}
    reasoning = models["qwen3-8b"]["reasoning"]
    assert reasoning["declared"] is False
    assert reasoning["supported"] is None
    assert reasoning["error"]


def test_invalid_old_profile_json_normalizes_to_empty_object(tmp_path):
    """非 JSON 的旧 profile TEXT 在 API 出参归一为 {}，capability 为空串。"""
    path = str(tmp_path / "t.db")
    make_app(tmp_path)
    conn = connect(path)
    with conn:
        conn.execute("UPDATE models SET profile = 'not-json' WHERE id = 'qwen3-8b'")
    conn.close()
    model = {m["id"]: m for m in TestClient(make_app(tmp_path)).get(
        "/admin/models").json()["models"]}["qwen3-8b"]
    assert model["profile"] == {}
    assert model["capability"] == ""


# --------------------------------------------------------------------------- #
# POST /admin/models
# --------------------------------------------------------------------------- #
def test_create_model_records_catalog_state_and_audit(client, app):
    r = client.post("/admin/models", json={
        "id": "my-model", "upstream": "upstream-v1", "provider": "local",
        "profile": OPENAI_REASONING})
    assert r.status_code == 201, r.text
    model = r.json()["model"]
    assert model["id"] == "my-model" and model["upstream"] == "upstream-v1"
    assert isinstance(model["profile"], dict)
    assert model["profile"] == OPENAI_REASONING
    assert isinstance(model["capability"], str)
    assert isinstance(model["reasoning"], dict)
    assert model["reasoning"]["declared"] is True
    assert model["reasoning"]["format"] == "openai"
    assert client.get("/admin/models").json()["models"]
    # 零启用图：路由集为空，目录写不再投影到 snapshot。
    assert app.state.snapshot_set.snapshots == {}
    # audit row
    changes = client.get("/admin/changes").json()["changes"]
    assert changes[0]["kind"] == "model"
    assert changes[0]["detail"]["action"] == "create"


def test_create_model_duplicate_is_409(client):
    body = {"id": "dup", "upstream": "u", "provider": "local"}
    assert client.post("/admin/models", json=body).status_code == 201
    assert client.post("/admin/models", json=body).status_code == 409


@pytest.mark.parametrize("body", [
    {"id": "", "upstream": "u", "provider": "local"},
    {"id": "-bad", "upstream": "u", "provider": "local"},
    {"id": "has space", "upstream": "u", "provider": "local"},
    {"id": "a" * 65, "upstream": "u", "provider": "local"},
    {"id": "ok", "upstream": "", "provider": "local"},
    {"id": "ok", "upstream": "a\nb", "provider": "local"},
    {"id": "ok", "upstream": "x" * 201, "provider": "local"},
    {"id": "ok", "upstream": "u", "provider": "nope"},
    {"id": "ok", "upstream": "u"},
])
def test_create_model_validation_422(client, body):
    assert client.post("/admin/models", json=body).status_code == 422


@pytest.mark.parametrize("profile", [
    {"reasoning": {"format": "openai", "efforts": {"high": None}}},
    {"reasoning": {"format": "nope", "efforts": {"high": "high"}}},
    {"reasoning": {"efforts": {"off": None}}},
    {"reasoning": {"efforts": {"high": "high"}, "default": "max"}},
    {"reasoning": {"format": "qwen", "efforts": {"on": None},
                   "supports_reasoning_effort": True}},
    "not-a-json-object",
])
def test_create_model_rejects_illegal_profile(client, profile):
    r = client.post("/admin/models", json={
        "id": "bad-profile", "upstream": "u", "provider": "local",
        "profile": profile})
    assert r.status_code == 422
    assert client.get("/admin/models").json()["models"]
    assert "bad-profile" not in {m["id"] for m in
                                client.get("/admin/models").json()["models"]}


NON_FINITE_LITERALS = ("NaN", "Infinity", "-Infinity")


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_model_writes_reject_non_finite_profile_floats(client, app, literal):
    """NaN/Infinity 不是合法 JSON：POST/PUT 在写库前 422，行/审计/快照零改动。

    ``json.loads`` 接受这三个字面量，但 Starlette 的 JSONResponse 用
    ``allow_nan=False`` 序列化 —— 一旦写进 ``models.profile``，该行之后的
    echo/GET 直接 500。管理写路径必须落库前拒绝。
    """
    conn = connect(app.state.settings.db_path)
    try:
        before_rows = conn.execute(
            "SELECT id, upstream, provider, profile FROM models ORDER BY id").fetchall()
        before_audit = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    finally:
        conn.close()
    before_revision = app.state.snapshot_set.snapshot_revision

    # 1) profile 传 JSON 字符串：递归命中嵌套数组里的非有限值，并点名路径。
    nested = '{"metadata": {"labels": [{"score": %s}]}}' % literal
    created = client.post("/admin/models", json={
        "id": "non-finite-model", "upstream": "u", "provider": "local",
        "profile": nested})
    assert created.status_code == 422, created.text
    assert "非 JSON 兼容数值" in created.text
    assert "profile.metadata.labels[0].score" in created.text

    # 2) 原始 JSON 请求体（httpx 的 json= 会先拒绝 NaN，因此手写 content）。
    raw = ('{"upstream": "u", "profile": {"metadata": {"threshold": %s}}}'
           % literal)
    updated = client.put("/admin/models/qwen3-8b", content=raw,
                         headers={"Content-Type": "application/json"})
    assert updated.status_code == 422, updated.text
    assert "非 JSON 兼容数值" in updated.text
    assert "profile.metadata.threshold" in updated.text

    # 数据库行、审计与已发布快照均未变化（没有落库，也就不会 echo 500）。
    conn = connect(app.state.settings.db_path)
    try:
        after_rows = conn.execute(
            "SELECT id, upstream, provider, profile FROM models ORDER BY id").fetchall()
        after_audit = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    finally:
        conn.close()
    assert after_rows == before_rows
    assert after_audit == before_audit
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert client.get("/admin/models").status_code == 200

    # 合法有限浮点元数据照常保存（拒绝只针对非 JSON 兼容值）。
    ok = client.put("/admin/models/qwen3-8b", json={
        "profile": {"metadata": {"threshold": 0.5, "count": 2}}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["model"]["profile"] == {
        "metadata": {"threshold": 0.5, "count": 2}}


def test_create_model_inherits_provider_reasoning_format(client, app):
    r = client.put("/admin/providers/local", json={
        "base_url": "http://127.0.0.1:9000", "reasoning_format": "deepseek"})
    assert r.status_code == 200
    # a null wire is only legal for non-needs_wire formats like deepseek
    r = client.post("/admin/models", json={
        "id": "deep-model", "upstream": "u", "provider": "local",
        "profile": {"reasoning": {"efforts": {"off": None, "high": None}}}})
    assert r.status_code == 201, r.text
    assert r.json()["model"]["reasoning"]["format"] == "deepseek"
    # rationale: 快照来自自包含画布；这里显式把目录事实固化进画布再断言投影。
    root = _direct_graph((("deep-model", None),))
    runtime = _tree_runtime(root)
    runtime["backends"]["providers"][0]["reasoning_format"] = "deepseek"
    runtime["backends"]["providers"][0]["base_url"] = "http://127.0.0.1:9000"
    runtime["backends"]["models"][0]["profile"] = {
        "reasoning": {"efforts": {"off": None, "high": None}}}
    created = client.post("/admin/trees", json={
        "id": "deep-canvas", "name": "Deep", "root": root, **runtime})
    assert created.status_code == 201, created.text
    assert client.post("/admin/trees/deep-canvas/enable").status_code == 200
    assert app.state.snapshot_set.default_snapshot.backends["deep-model"].reasoning["format"] == "deepseek"


# --------------------------------------------------------------------------- #
# PUT / DELETE models
# --------------------------------------------------------------------------- #
def test_put_model_validates_profile_and_keeps_old_one_on_error(client):
    good = client.put("/admin/models/qwen3-8b",
                      json={"profile": OPENAI_REASONING})
    assert good.status_code == 200
    before = good.json()["model"]["profile"]
    bad = client.put("/admin/models/qwen3-8b", json={"profile": {
        "reasoning": {"format": "openai", "efforts": {"high": None}}}})
    assert bad.status_code == 422
    assert client.get("/admin/models").json()["models"]
    after = {m["id"]: m for m in
             client.get("/admin/models").json()["models"]}["qwen3-8b"]
    assert after["profile"] == before


def test_put_model_accepts_json_string_profile_and_unknown_is_404(client):
    payload = json.dumps({"reasoning": {
        "efforts": {"off": None, "high": "high"}}})
    assert client.put("/admin/models/qwen3-8b",
                      json={"profile": payload}).status_code == 200
    model = {m["id"]: m for m in
             client.get("/admin/models").json()["models"]}["qwen3-8b"]
    # DB keeps JSON TEXT, but the API always returns the normalized object
    assert model["profile"] == {"reasoning": {
        "efforts": {"off": None, "high": "high"}}}
    assert model["reasoning"]["declared"] is True
    assert client.put("/admin/models/nope", json={"upstream": "x"}).status_code == 404


def test_put_model_rejects_unknown_provider(client, app):
    """A model whose provider does not exist drops out of the backend
    snapshot (the JOIN misses it) and routed requests to it fail with 503
    no_available_backend — so the write must be rejected and the model
    must stay routable."""
    r = client.put("/admin/models/qwen3-8b", json={"provider": "ghost"})
    assert r.status_code == 422
    model = {m["id"]: m for m in
             client.get("/admin/models").json()["models"]}["qwen3-8b"]
    assert model["provider"] == "local"
    conn = connect(app.state.settings.db_path)
    try:
        assert "qwen3-8b" in load_backends(conn)
    finally:
        conn.close()


def _tree_runtime(root, *, model_overrides=None):
    """root 引用的模型 id → 自包含运行字段（可选覆写 profile）。"""
    ids: list[str] = []
    for node in root.get("nodes") or ():
        if node.get("type") != "target":
            continue
        for slot in node.get("models") or ():
            model = slot.get("model") if isinstance(slot, dict) else slot
            if isinstance(model, str) and model not in ids:
                ids.append(model)
    runtime = runtime_for_models(ids)
    for override in (model_overrides or {}).items():
        model_id, profile = override
        for model in runtime["backends"]["models"]:
            if model["id"] == model_id:
                model["profile"] = profile
    return runtime


def _direct_graph(models, *, node_id="direct", fallback="direct"):
    """start → target 的 graph v3 根；用于模型引用/推理档位回归。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": node_id, "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": reasoning}
                        for model, reasoning in models]},
        ],
        "edges": [{
            "id": f"e-start-{node_id}", "source": "start", "source_port": "out",
            "target": node_id, "target_port": "in_1", "condition": None,
        }],
        "error_fallback": fallback,
    }


def test_delete_model_referenced_by_a_tree_is_409_then_free_model_ok(client, app):
    """删除被引用模型：未确认 409 影响清单（每幅画布列出 target 使用位置），
    确认缺替代 → 422，确认带替代 → 200 并同步所有受影响画布。"""
    route_root = _direct_graph((("qwen3-8b", None),))
    created = client.post("/admin/trees", json={
        "id": "ref-tree", "name": "Ref", "root": route_root,
        **_tree_runtime(route_root)})
    assert created.status_code == 201, created.text
    nl_root = {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "q", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位",
             "options": [
                 {"key": "fast", "label": "轻量", "capability": "简单任务"},
                 {"key": "deep", "label": "增强", "capability": "复杂任务"},
             ]},
            {"id": "fast-target", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
            {"id": "deep-target", "type": "target", "priority": 2,
             "models": [{"model": "qwen3-32b", "reasoning": None}]},
        ],
        "edges": [
            {"id": "e-start-q", "source": "start", "source_port": "out",
             "target": "q", "target_port": "in_1", "condition": None},
            {"id": "e-q-fast", "source": "q", "source_port": "selected.fast",
             "target": "fast-target", "target_port": "in_1", "condition": None},
            {"id": "e-q-deep", "source": "q", "source_port": "selected.deep",
             "target": "deep-target", "target_port": "in_1", "condition": None},
        ],
        "error_fallback": "deep-target",
    }
    created = client.post("/admin/trees", json={
        "id": "nl-ref", "name": "NL Ref", "root": nl_root,
        **_tree_runtime(nl_root)})
    assert created.status_code == 201, created.text

    r = client.delete("/admin/models/qwen3-8b")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert {item["id"] for item in detail["affected_trees"]} == {"ref-tree", "nl-ref"}
    usages = {item["id"]: item["usage"] for item in detail["affected_trees"]}
    assert usages["ref-tree"]["targets"] == ["direct"]
    assert usages["nl-ref"]["targets"] == ["fast-target"]
    assert all(usage["runtime_catalog"] is True for usage in usages.values())
    assert "qwen3-8b" in {m["id"] for m in
                          client.get("/admin/models").json()["models"]}

    # 确认但缺替代模型 → 422（target 仍引用）；确认带替代模型 → 200 且同步两幅画布。
    confirmation = _confirmation_from(r)
    missing = client.request("DELETE", "/admin/models/qwen3-8b", json={
        "impact_confirmation": confirmation})
    assert missing.status_code == 422
    deleted = client.request("DELETE", "/admin/models/qwen3-8b", json={
        "replacement_model_id": "qwen3-32b",
        "impact_confirmation": confirmation})
    assert deleted.status_code == 200, deleted.text
    assert sorted(deleted.json()["synced_trees"]) == ["nl-ref", "ref-tree"]
    for tree_id in ("ref-tree", "nl-ref"):
        target_models = {
            slot.model
            for node in app.state.graph_repo.get(tree_id).root.nodes
            for slot in getattr(node, "models", ())
        }
        assert "qwen3-8b" not in target_models
        assert "qwen3-32b" in target_models

    assert client.delete("/admin/models/nope").status_code == 404
    # unreferenced model deletes fine
    assert client.post("/admin/models", json={
        "id": "free", "upstream": "u", "provider": "local"}).status_code == 201
    assert client.delete("/admin/models/free").status_code == 200
    assert "free" not in {m["id"] for m in
                          client.get("/admin/models").json()["models"]}


# --------------------------------------------------------------------------- #
# Providers: key tri-state / key_source / never returning the secret
# --------------------------------------------------------------------------- #
def _activate_provider_canvas(client, base_url, model="qwen3-8b", provider="local",
                              key_env=""):
    root = _direct_graph(((model, None),))
    runtime = _tree_runtime(root)
    runtime["backends"]["providers"][0].update({
        "name": provider, "base_url": base_url, "key_env": key_env,
    })
    runtime["backends"]["models"][0]["provider"] = provider
    created = client.post("/admin/trees", json={
        "id": "provider-canvas", "name": "Provider", "root": root, **runtime})
    assert created.status_code == 201, created.text
    assert client.post("/admin/trees/provider-canvas/enable").status_code == 200


def test_provider_key_tristate_and_headers(client, app):
    base = {"base_url": "http://127.0.0.1:9000"}
    _activate_provider_canvas(client, base["base_url"])
    r = put_with_confirmation(
        client, "/admin/providers/local", {**base, "api_key": "sk-secret-1"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"]["key_present"] is True
    assert r.json()["provider"]["key_source"] == "stored"
    assert "sk-secret-1" not in r.text
    assert app.state.snapshot_set.default_snapshot.backends["qwen3-8b"].headers["Authorization"] == \
        "Bearer sk-secret-1"

    # omitted -> kept
    r = put_with_confirmation(client, "/admin/providers/local", base)
    assert r.status_code == 200, r.text
    assert r.json()["provider"]["key_present"] is True
    assert r.json()["provider"]["key_source"] == "stored"
    assert client.get("/admin/providers").text.count("sk-secret-1") == 0
    assert app.state.snapshot_set.default_snapshot.backends["qwen3-8b"].headers["Authorization"] == \
        "Bearer sk-secret-1"

    # "" -> cleared
    r = put_with_confirmation(
        client, "/admin/providers/local", {**base, "api_key": ""})
    assert r.status_code == 200, r.text
    provider = r.json()["provider"]
    assert provider["key_present"] is False
    assert provider["key_source"] == "none"
    assert "Authorization" not in app.state.snapshot_set.default_snapshot.backends["qwen3-8b"].headers


def test_provider_key_source_env_and_stored_priority(client, app, monkeypatch):
    monkeypatch.setenv("SR_TEST_KEY", "env-secret")
    r = client.put("/admin/providers/envprov", json={
        "base_url": "http://e/v1", "key_env": "SR_TEST_KEY"})
    assert r.status_code == 200
    assert r.json()["provider"]["key_source"] == "env"
    assert r.json()["provider"]["key_present"] is True
    assert client.post("/admin/models", json={
        "id": "env-model", "upstream": "u",
        "provider": "envprov"}).status_code == 201
    _activate_provider_canvas(client, "http://e/v1", model="env-model",
                              provider="envprov", key_env="SR_TEST_KEY")
    assert app.state.snapshot_set.default_snapshot.backends["env-model"].headers["Authorization"] == \
        "Bearer env-secret"
    # a stored key wins over the environment (contract §5.3)
    stored = put_with_confirmation(client, "/admin/providers/envprov", {
        "base_url": "http://e/v1", "api_key": "stored-secret"})
    assert stored.status_code == 200, stored.text
    assert app.state.snapshot_set.default_snapshot.backends["env-model"].headers["Authorization"] == \
        "Bearer stored-secret"
    # clearing the stored key falls back to the env
    cleared = put_with_confirmation(client, "/admin/providers/envprov", {
        "base_url": "http://e/v1", "api_key": ""})
    assert cleared.status_code == 200, cleared.text
    assert app.state.snapshot_set.default_snapshot.backends["env-model"].headers["Authorization"] == \
        "Bearer env-secret"


def test_put_provider_validation_and_audit(client):
    bad = [
        {"base_url": ""},
        {"base_url": "http://x/v1", "reasoning_format": "nope"},
        {"base_url": "http://x/v1", "key_env": "bad env"},
        {"base_url": "http://x/v1", "key_env": "1NO"},
    ]
    for body in bad:
        assert client.put("/admin/providers/local", json=body).status_code == 422
    assert client.put("/admin/providers/-bad",
                      json={"base_url": "http://x/v1"}).status_code == 422
    r = client.put("/admin/providers/local", json={
        "base_url": "http://x/v1", "reasoning_format": "deepseek",
        "api_key": "sk-super-secret"})
    assert r.status_code == 200
    changes = client.get("/admin/changes")
    assert "sk-super-secret" not in changes.text
    row = changes.json()["changes"][0]
    assert row["kind"] == "provider"
    assert row["detail"]["api_key"] == "set"
    provider = {p["name"]: p for p in
                client.get("/admin/providers").json()["providers"]}["local"]
    assert provider["reasoning_format"] == "deepseek"
    assert "api_key" not in provider


def test_delete_provider_409_when_referenced_and_ok_after(client):
    r = client.delete("/admin/providers/local")
    assert r.status_code == 409
    assert r.json()["detail"]["models"]
    assert client.delete("/admin/providers/nope").status_code == 404
    assert client.put("/admin/providers/tmp-prov", json={
        "base_url": "http://tmp/v1"}).status_code == 200
    assert client.post("/admin/models", json={
        "id": "tmp-model", "upstream": "u",
        "provider": "tmp-prov"}).status_code == 201
    assert client.delete("/admin/providers/tmp-prov").status_code == 409
    assert client.delete("/admin/models/tmp-model").status_code == 200
    assert client.delete("/admin/providers/tmp-prov").status_code == 200


# --------------------------------------------------------------------------- #
# POST /admin/fetch-models
# --------------------------------------------------------------------------- #
def test_fetch_models_uses_provider_credentials_and_mock_transport(tmp_path):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"data": [
            {"id": "m1", "name": "Model One"}, {"id": "m2"}]})

    client = TestClient(make_app(tmp_path, handler))
    client.put("/admin/providers/local", json={
        "base_url": "http://127.0.0.1:9000", "api_key": "sk-provider"})
    r = client.post("/admin/fetch-models", json={"provider": "local"})
    assert r.status_code == 200
    assert r.json() == {"endpoint": "http://127.0.0.1:9000/models",
                        "models": [{"id": "m1", "name": "Model One"},
                                   {"id": "m2", "name": "m2"}]}
    assert seen["auth"] == "Bearer sk-provider"
    assert seen["timeout"]["read"] == 10.0          # contract: 10s timeout
    # body credentials win over the stored ones; trailing slash is normalised
    r = client.post("/admin/fetch-models", json={
        "base_url": "https://api.example.com/v1/", "api_key": "sk-body"})
    assert r.json()["endpoint"] == "https://api.example.com/v1/models"
    assert seen["auth"] == "Bearer sk-body"


def test_fetch_models_body_key_env(tmp_path, monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": []})

    client = TestClient(make_app(tmp_path, handler))
    monkeypatch.setenv("SR_FETCH_KEY", "env-fetch")
    r = client.post("/admin/fetch-models", json={
        "base_url": "https://x/v1", "key_env": "SR_FETCH_KEY"})
    assert r.status_code == 200 and r.json()["models"] == []
    assert seen["auth"] == "Bearer env-fetch"


@pytest.mark.parametrize("handler,status", [
    (lambda request: httpx.Response(500, json={"error": "boom"}), 502),
    (lambda request: httpx.Response(200, text="not json"), 502),
    (lambda request: httpx.Response(200, json={"unexpected": True}), 502),
    (lambda request: httpx.Response(200, json=[]), 200),
])
def test_fetch_models_failure_modes(tmp_path, handler, status):
    client = TestClient(make_app(tmp_path, handler))
    r = client.post("/admin/fetch-models",
                    json={"base_url": "https://x/v1"})
    assert r.status_code == status
    if status == 502:
        assert r.json()["detail"]


def test_fetch_models_connection_error_is_502(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = TestClient(make_app(tmp_path, handler))
    r = client.post("/admin/fetch-models", json={"base_url": "https://x/v1"})
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]


def test_fetch_models_request_validation(client):
    assert client.post("/admin/fetch-models", json={}).status_code == 422
    assert client.post("/admin/fetch-models",
                       json={"provider": "nope"}).status_code == 404


def test_old_illegal_declaration_does_not_break_startup(tmp_path):
    path = str(tmp_path / "t.db")
    make_app(tmp_path)
    conn = connect(path)
    with conn:
        conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                     (json.dumps({"reasoning": {
                         "format": "nope", "efforts": {"high": "high"}}}),))
        conn.execute("DELETE FROM providers WHERE name = 'local'")
    conn.close()
    # a missing provider and an illegal declaration must both be tolerated
    app = make_app(tmp_path)
    assert app.state.snapshot_set.snapshots == {}
    client = TestClient(app)
    models = {m["id"]: m for m in client.get("/admin/models").json()["models"]}
    assert models["qwen3-8b"]["reasoning"]["declared"] is False
    assert models["qwen3-8b"]["reasoning"]["error"]


# --------------------------------------------------------------------------- #
# model writes are judged against the ACTIVE graph's embedded runtime config
# --------------------------------------------------------------------------- #
def _reasoning_tree_root(level="high"):
    return _direct_graph((("qwen3-8b", level),), node_id="reasoning-target",
                         fallback="reasoning-target")


def _declare_high(client):
    r = put_with_confirmation(
        client, "/admin/models/qwen3-8b", {"profile": OPENAI_REASONING})
    assert r.status_code == 200, r.text


LOW_ONLY_REASONING = {"reasoning": {
    "efforts": {"off": None, "low": "low"}, "default": "low"}}


def _activate_reasoning_tree(client, tree_id="reasoning-tree"):
    root = _reasoning_tree_root()
    runtime = _tree_runtime(root, model_overrides={"qwen3-8b": OPENAI_REASONING})
    created = client.post("/admin/trees", json={
        "id": tree_id, "name": "Reasoning", "root": root, **runtime})
    assert created.status_code == 201, created.text
    enabled = client.post(f"/admin/trees/{tree_id}/enable")
    assert enabled.status_code == 200, enabled.text
    return created.json()["tree"]


def test_model_write_previews_then_validates_a_closed_draft_canvas(client, app):
    """目录写不再以「活动图许可」为门槛，而是对所有受影响画布（含草稿）两阶段。

    旧契约下关闭的草稿不参与目录写校验；严格自包含画布把「数据库目录能否改写
    画布」变成显式确认：未确认 409 且零写入，确认后同步草稿的运行时模型项；
    若同步会让草稿的 target 推理档位失效，候选校验返回 422 并整体回滚。
    """
    _declare_high(client)
    root = _reasoning_tree_root()
    runtime = _tree_runtime(root, model_overrides={"qwen3-8b": OPENAI_REASONING})
    created = client.post("/admin/trees", json={
        "id": "draft-tree", "name": "Draft", "root": root, **runtime})
    assert created.status_code == 201, created.text
    graph_dir = Path(app.state.settings.graph_dir, "graphs")

    def canvas_files():
        return {path.name: path.read_bytes() for path in graph_dir.glob("*.json")}

    def stored_profile():
        conn = connect(app.state.settings.db_path)
        try:
            return json.loads(conn.execute(
                "SELECT profile FROM models WHERE id = 'qwen3-8b'").fetchone()[0])
        finally:
            conn.close()

    before_files = canvas_files()
    preview = client.put(
        "/admin/models/qwen3-8b", json={"upstream": "edited-upstream"})

    assert preview.status_code == 409, preview.text
    detail = preview.json()["detail"]
    assert [item["id"] for item in detail["affected_trees"]] == ["draft-tree"]
    assert detail["affected_trees"][0]["enabled"] is False
    assert stored_profile() == OPENAI_REASONING
    assert canvas_files() == before_files

    confirmed = client.put("/admin/models/qwen3-8b", json={
        "upstream": "edited-upstream",
        "impact_confirmation": _confirmation_from(preview),
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["synced_trees"] == ["draft-tree"]
    draft = app.state.graph_repo.get("draft-tree")
    assert draft.enabled is False
    assert draft.runtime.backends.models[0]["upstream"] == "edited-upstream"
    assert draft.runtime.backends.models[0]["profile"] == OPENAI_REASONING

    # 让草稿 target 的 high 档位失效的 profile 更新：确认后候选校验 422 并回滚。
    before_files = canvas_files()
    before_profile = stored_profile()
    preview = client.put(
        "/admin/models/qwen3-8b", json={"profile": LOW_ONLY_REASONING})
    assert preview.status_code == 409, preview.text
    rejected = client.put("/admin/models/qwen3-8b", json={
        "profile": LOW_ONLY_REASONING,
        "impact_confirmation": _confirmation_from(preview),
    })

    assert rejected.status_code == 422, rejected.text
    assert any(issue["code"] == "reasoning_unsupported"
               for issue in rejected.json()["detail"])
    assert stored_profile() == before_profile
    assert canvas_files() == before_files


def test_model_write_keeping_the_referenced_reasoning_level_is_200(client):
    _declare_high(client)
    _activate_reasoning_tree(client)
    r = put_with_confirmation(client, "/admin/models/qwen3-8b", {"profile": {
        "reasoning": {"efforts": {"off": None, "high": "high"},
                      "default": "high"}}})
    assert r.status_code == 200, r.text
    model = next(m for m in client.get("/admin/models").json()["models"]
                 if m["id"] == "qwen3-8b")
    assert model["reasoning"]["declared"] is True
    assert [entry["id"] for entry in model["reasoning"]["efforts"]] == [
        "off", "high"]


def test_model_update_rolls_back_when_active_graph_becomes_invalid(client, app):
    """非法提供方写入返回 422，快照不得被替换。"""
    before = app.state.snapshot_set.snapshot_revision
    response = client.put("/admin/models/qwen3-8b", json={"provider": "missing-provider"})
    assert response.status_code == 422
    assert app.state.snapshot_set.snapshot_revision == before


def test_delete_referenced_model_is_409_and_changes_nothing(client, app):
    """删除被引用模型 → 409，且该用例不产生审计行、不改变快照 revision。"""
    root = _direct_graph((("qwen3-8b", None),))
    assert client.post("/admin/trees", json={
        "id": "ref-tree", "name": "Ref", "root": root,
        **_tree_runtime(root)}).status_code == 201
    before_revision = app.state.snapshot_set.snapshot_revision

    response = client.delete("/admin/models/qwen3-8b")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert [item["id"] for item in detail["affected_trees"]] == ["ref-tree"]
    assert detail["affected_trees"][0]["usage"] == {
        "targets": ["direct"], "runtime_catalog": True}
    conn = connect(app.state.settings.db_path)
    try:
        audit = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE kind = 'model'").fetchone()[0]
    finally:
        conn.close()
    assert audit == 0
    assert app.state.snapshot_set.snapshot_revision == before_revision


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("post", "/admin/models", {"id": "extra-model", "upstream": "u", "provider": "local"}),
    ("put", "/admin/models/qwen3-8b", {"upstream": "u"}),
    ("put", "/admin/providers/local", {"base_url": "http://x/v1"}),
])
def test_model_and_provider_dtos_reject_unknown_fields(client, method, path, payload):
    """请求 DTO 的 ``ConfigDict(extra="forbid")`` 契约：未知字段不允许通过。

    未知字段在进入写用例之前 422，且不留下任何数据库副作用。"""
    response = getattr(client, method)(path, json={**payload, "bogus": 1})

    assert response.status_code == 422, response.text
    assert any(item["type"] == "extra_forbidden" for item in response.json()["detail"])
    assert "extra-model" not in {
        model["id"] for model in client.get("/admin/models").json()["models"]}


# --------------------------------------------------------------------------- #
# 确认后的目录写：预览是纯读（不占写锁），提交在 publish 写锁内原子完成
# --------------------------------------------------------------------------- #
def _instrument_publish_lock(app, monkeypatch):
    """记录 publish 写锁的进入/退出顺序。"""
    publisher = app.state.publisher
    real_locked = publisher.locked
    trace: list[str] = []

    @contextmanager
    def recording_locked():
        trace.append("lock-enter")
        try:
            with real_locked():
                yield publisher
        finally:
            trace.append("lock-exit")

    monkeypatch.setattr(publisher, "locked", recording_locked)
    return trace


def test_delete_referenced_model_previews_without_lock_then_commits_under_lock(
        app, client, monkeypatch):
    root = _direct_graph((("qwen3-8b", None),))
    created = client.post("/admin/trees", json={
        "id": "ref-tree", "name": "Ref", "root": root, **_tree_runtime(root)})
    assert created.status_code == 201, created.text
    trace = _instrument_publish_lock(app, monkeypatch)

    preview = client.delete("/admin/models/qwen3-8b")

    assert preview.status_code == 409
    # 预览只扫描影响清单，不拿写锁（零写入的纯读阶段）。
    assert trace == []

    confirmed = client.request("DELETE", "/admin/models/qwen3-8b", json={
        "replacement_model_id": "qwen3-32b",
        "impact_confirmation": _confirmation_from(preview),
    })

    assert confirmed.status_code == 200, confirmed.text
    assert trace == ["lock-enter", "lock-exit"]
    assert confirmed.json()["synced_trees"] == ["ref-tree"]


def test_delete_free_model_commits_catalog_write_under_the_publish_lock(
        app, client, monkeypatch):
    trace = _instrument_publish_lock(app, monkeypatch)

    response = client.delete("/admin/models/qwen3-32b")

    assert response.status_code == 200, response.text
    assert response.json()["synced_trees"] == []
    # 无候选画布时仍走单写锁 + 单事务的目录写骨架。
    assert trace == ["lock-enter", "lock-exit"]


def test_provider_delete_rechecks_references_under_the_publish_lock(
        app, client, monkeypatch):
    """provider 删除的引用检查必须在写锁内、事务内重算（CRITICAL 2）。

    路由层扫描之后、加锁之前新建一幅引用该 provider 的画布：锁内 recheck 必须
    返回 409 ``provider_referenced``，provider 行/审计/画布文件都不变；trace 同时
    证明第二次「DB 模型引用检查」发生在 lock-enter 之后。
    """
    assert client.put("/admin/providers/race-prov", json={
        "base_url": "http://race.test/v1"}).status_code == 200
    before_providers = {
        p["name"]: p for p in client.get("/admin/providers").json()["providers"]}
    before_changes = len(client.get("/admin/changes").json()["changes"])

    trace = _instrument_publish_lock(app, monkeypatch)
    real_impacts = routes_models.provider_impacts
    real_db_refs = routes_models.model_ids_using_provider
    raced = {"done": False}

    def racing_impacts(trees, name):
        impacts = real_impacts(trees, name)   # 旧清单：race-canvas 还不存在
        if not raced["done"]:
            raced["done"] = True
            root = _direct_graph((("qwen3-8b", None),))
            runtime = _tree_runtime(root)
            runtime["backends"]["providers"].append({
                "name": "race-prov", "base_url": "http://race.test/v1",
                "key_env": "", "reasoning_format": "openai"})
            runtime["backends"]["models"][0]["provider"] = "race-prov"
            app.state.graph_repo.create(tree_from_dict({
                "id": "race-canvas", "name": "Race", "root": root,
                "timezone": "Asia/Shanghai",
                "sticky": {"enabled": False, "ttl_seconds": 1800},
                **runtime,
            }))
        return impacts

    def traced_db_refs(conn, name):
        trace.append("db-ref-check")
        return real_db_refs(conn, name)

    monkeypatch.setattr(routes_models, "provider_impacts", racing_impacts)
    monkeypatch.setattr(routes_models, "model_ids_using_provider", traced_db_refs)

    response = client.delete("/admin/providers/race-prov")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "provider_referenced"
    assert detail["models"] == []            # 数据库里没有模型引用它
    assert [item["id"] for item in detail["affected_trees"]] == ["race-canvas"]
    # 路由层检查在锁外；锁内 recheck 的 DB 引用检查发生在 lock-enter 之后。
    assert trace[:2] == ["db-ref-check", "lock-enter"]
    assert trace[-1] == "lock-exit"
    assert trace.count("db-ref-check") == 2
    assert "db-ref-check" in trace[2:]
    # 409 在 db_write 之前抛出：provider 行、审计、新画布都保持原值。
    assert {
        p["name"]: p for p in client.get("/admin/providers").json()["providers"]
    } == before_providers
    assert len(client.get("/admin/changes").json()["changes"]) == before_changes
    assert app.state.graph_repo.get("race-canvas") is not None


def test_provider_endpoint_change_clears_stored_key_and_headers(tmp_path):
    """端点变更且请求省略 api_key：旧密钥不得跟随到新地址（spec §5）。"""
    settings = test_settings(tmp_path, db_path=str(tmp_path / "t.db"))
    make_app(tmp_path)                       # 先建库/建表
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    document = portable_tree_dict("portable")
    repo.create(tree_from_dict({**document, "enabled": True, "default": True}))
    conn = connect(settings.db_path)
    try:
        ProviderRepo(conn).upsert(
            "local", "http://json-only.test/v1", api_key="sk-json-secret")
        # 目录写仍用数据库上下文校验活动画布：目录需登记画布引用的模型。
        ModelRepo(conn).create("fast", "json-upstream", "local", "{}")
    finally:
        conn.close()

    app = make_app(tmp_path)
    assert app.state.snapshot_set.default_snapshot.backends["fast"].headers["Authorization"] == \
        "Bearer sk-json-secret"
    client = TestClient(app)
    preview = client.put(
        "/admin/providers/local", json={"base_url": "http://new-upstream.test/v1"})
    assert preview.status_code == 409, preview.text
    # 未确认：画布与快照都不变。
    assert app.state.snapshot_set.default_snapshot.backends["fast"].url == \
        "http://json-only.test/v1/chat/completions"
    assert app.state.snapshot_set.default_snapshot.backends["fast"].headers["Authorization"] == \
        "Bearer sk-json-secret"

    response = client.put("/admin/providers/local", json={
        "base_url": "http://new-upstream.test/v1",
        "impact_confirmation": _confirmation_from(preview),
    })

    assert response.status_code == 200, response.text
    # 确认后画布运行时 provider 同步到新端点；请求省略 api_key 且端点变更 →
    # 旧密钥不再注入（DB 行被清空），快照改用新端点。
    backend = app.state.snapshot_set.default_snapshot.backends["fast"]
    assert backend.url == "http://new-upstream.test/v1/chat/completions"
    assert "Authorization" not in backend.headers
    conn = connect(app.state.settings.db_path)
    try:
        row = ProviderRepo(conn).get("local")
        assert row["base_url"] == "http://new-upstream.test/v1"
        assert row["api_key"] == ""
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 密钥通道：profile 是自由 JSON 元数据，但任何层级的密钥/派生状态都不得写入
# --------------------------------------------------------------------------- #
SECRET_KEY_SPELLINGS = ["api_key", "API-KEY", "authorization", "key_present", "key_source"]


@pytest.mark.parametrize("key", SECRET_KEY_SPELLINGS)
def test_model_writes_reject_nested_profile_secrets(client, app, key):
    """模型 POST/PUT 收到嵌套密钥 → 422，且数据库/审计/快照/图文件都不变。"""
    profile = {"metadata": {"labels": [{"tier": "x", key: "review-secret"}]}}
    before_revision = app.state.snapshot_set.snapshot_revision
    conn = connect(app.state.settings.db_path)
    try:
        before_profile = conn.execute(
            "SELECT profile FROM models WHERE id = 'qwen3-8b'").fetchone()[0]
        before_audit = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        before_models = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    finally:
        conn.close()
    graph_dir = Path(app.state.settings.graph_dir, "graphs")
    before_files = {path.name: path.read_bytes() for path in graph_dir.glob("*.json")}

    updated = client.put("/admin/models/qwen3-8b", json={"profile": profile})
    assert updated.status_code == 422, updated.text
    created = client.post("/admin/models", json={
        "id": "secret-model", "upstream": "u", "provider": "local", "profile": profile})
    assert created.status_code == 422, created.text
    assert "review-secret" not in updated.text + created.text

    conn = connect(app.state.settings.db_path)
    try:
        after_profile = conn.execute(
            "SELECT profile FROM models WHERE id = 'qwen3-8b'").fetchone()[0]
        after_audit = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        after_models = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    finally:
        conn.close()
    assert after_profile == before_profile
    assert after_audit == before_audit
    assert after_models == before_models
    assert app.state.snapshot_set.snapshot_revision == before_revision
    assert {path.name: path.read_bytes() for path in graph_dir.glob("*.json")} == before_files


@pytest.mark.parametrize("key", SECRET_KEY_SPELLINGS)
def test_model_reads_sanitize_legacy_profile_secrets_without_mutating_db(app, client, key):
    """旧库 profile 的 GET 是只读脱敏投影：密钥剔除、合法元数据保留、数据库不改写。"""
    stored_profile = {
        "metadata": {
            "labels": [{"tier": "keep", key: "review-secret"}],
            "domains": ["json"],
            "monkey": "keep",
        }
    }
    conn = connect(app.state.settings.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                (json.dumps(stored_profile),),
            )
    finally:
        conn.close()

    response = client.get("/admin/models")

    assert response.status_code == 200, response.text
    model = next(item for item in response.json()["models"] if item["id"] == "qwen3-8b")
    assert model["profile"] == {
        "metadata": {
            "labels": [{"tier": "keep"}],
            "domains": ["json"],
            "monkey": "keep",
        }
    }
    assert "review-secret" not in response.text

    conn = connect(app.state.settings.db_path)
    try:
        stored = conn.execute(
            "SELECT profile FROM models WHERE id = 'qwen3-8b'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(stored) == stored_profile, "GET 只能脱敏投影，不能改写旧库"
