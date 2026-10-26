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

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.upstream import load_backends
from router.config import Settings
from router.reasoning import FORMAT_IDS, LEVELS
from router.store.db import MIGRATIONS, connect, migrate
from router.store.repositories import seed_default

OPENAI_REASONING = {"reasoning": {
    "efforts": {"off": None, "low": "low", "high": "high"},
    "default": "high"}}


def make_app(tmp_path, handler=None):
    transport = httpx.MockTransport(handler) if handler is not None else None
    return create_app(Settings(db_path=str(tmp_path / "t.db")),
                      transport=transport)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


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


# --------------------------------------------------------------------------- #
# POST /admin/models
# --------------------------------------------------------------------------- #
def test_create_model_and_swap_backend_snapshot(client, app):
    r = client.post("/admin/models", json={
        "id": "my-model", "upstream": "upstream-v1", "provider": "local",
        "profile": OPENAI_REASONING})
    assert r.status_code == 201, r.text
    model = r.json()["model"]
    assert model["id"] == "my-model" and model["upstream"] == "upstream-v1"
    assert model["reasoning"]["declared"] is True
    assert model["reasoning"]["format"] == "openai"
    assert client.get("/admin/models").json()["models"]
    backend = app.state.snapshot.backends["my-model"]
    assert backend.upstream_name == "upstream-v1"
    assert backend.provider == "local"
    assert backend.reasoning["efforts"] == [
        {"id": "off", "wire": None}, {"id": "low", "wire": "low"},
        {"id": "high", "wire": "high"}]
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
    assert app.state.snapshot.backends["deep-model"].reasoning["format"] == "deepseek"


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
    assert model["reasoning"]["declared"] is True
    assert client.put("/admin/models/nope", json={"upstream": "x"}).status_code == 404


def test_put_model_rejects_unknown_provider(client, app):
    """A model whose provider does not exist drops out of the backend
    snapshot (the JOIN misses it) and every routed request 502s — so the
    write must be rejected and the model must stay routable."""
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


def test_delete_model_referenced_by_a_tree_is_409_then_free_model_ok(client):
    """Reference check now walks the SAVED routing trees (Task 11): both a
    route target and a direct natural-language model option block the delete,
    and every finding names its tree/node/edge (``via``)."""
    route_root = {"id": "root", "type": "route", "model": "qwen3-8b"}
    created = client.post("/admin/trees", json={
        "id": "ref-tree", "name": "Ref", "root": route_root})
    assert created.status_code == 201, created.text
    nl_root = {
        "id": "nl-root", "type": "nl_choice", "instructions": "choose",
        "options": [
            {"key": "fast", "model": "qwen3-8b"},
            {"key": "deep", "model": "qwen3-32b"},
        ],
        "otherwise": {"id": "fallback", "type": "route", "model": "qwen3-32b"},
    }
    created = client.post("/admin/trees", json={
        "id": "nl-ref", "name": "NL Ref", "root": nl_root})
    assert created.status_code == 201, created.text

    r = client.delete("/admin/models/qwen3-8b")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "路由树引用" in detail["message"]
    refs = detail["references"]
    assert {ref["tree_id"] for ref in refs} == {"ref-tree", "nl-ref"}
    for ref in refs:
        assert ref["tree_id"] and ref["node_id"] and ref["via"]
        assert "tree_revision" in ref
    assert "qwen3-8b" in {m["id"] for m in
                          client.get("/admin/models").json()["models"]}

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
def test_provider_key_tristate_and_headers(client, app):
    base = {"base_url": "http://127.0.0.1:9000"}
    r = client.put("/admin/providers/local", json={**base, "api_key": "sk-secret-1"})
    assert r.status_code == 200
    assert r.json()["provider"]["key_present"] is True
    assert r.json()["provider"]["key_source"] == "stored"
    assert "sk-secret-1" not in r.text
    assert app.state.snapshot.backends["qwen3-8b"].headers["Authorization"] == \
        "Bearer sk-secret-1"

    # omitted -> kept
    r = client.put("/admin/providers/local", json=base)
    assert r.json()["provider"]["key_present"] is True
    assert r.json()["provider"]["key_source"] == "stored"
    assert client.get("/admin/providers").text.count("sk-secret-1") == 0
    assert app.state.snapshot.backends["qwen3-8b"].headers["Authorization"] == \
        "Bearer sk-secret-1"

    # "" -> cleared
    r = client.put("/admin/providers/local", json={**base, "api_key": ""})
    provider = r.json()["provider"]
    assert provider["key_present"] is False
    assert provider["key_source"] == "none"
    assert "Authorization" not in app.state.snapshot.backends["qwen3-8b"].headers


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
    assert app.state.snapshot.backends["env-model"].headers["Authorization"] == \
        "Bearer env-secret"
    # a stored key wins over the environment (contract §5.3)
    client.put("/admin/providers/envprov", json={
        "base_url": "http://e/v1", "api_key": "stored-secret"})
    assert app.state.snapshot.backends["env-model"].headers["Authorization"] == \
        "Bearer stored-secret"
    # clearing the stored key falls back to the env
    client.put("/admin/providers/envprov", json={
        "base_url": "http://e/v1", "api_key": ""})
    assert app.state.snapshot.backends["env-model"].headers["Authorization"] == \
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
    assert app.state.snapshot.backends == {}
    client = TestClient(app)
    models = {m["id"]: m for m in client.get("/admin/models").json()["models"]}
    assert models["qwen3-8b"]["reasoning"]["declared"] is False
    assert models["qwen3-8b"]["reasoning"]["error"]


# --------------------------------------------------------------------------- #
# fix-wave 1 / I-1: model writes are judged against the PROSPECTIVE catalog
# --------------------------------------------------------------------------- #
def _reasoning_tree_root(level="high"):
    return {"id": "root", "type": "route", "model": "qwen3-8b",
            "reasoning": level}


def _declare_high(client):
    r = client.put("/admin/models/qwen3-8b", json={"profile": OPENAI_REASONING})
    assert r.status_code == 200, r.text


def _declare_low_only(client):
    """A declaration that no longer declares the tree's 'high' level."""
    return client.put("/admin/models/qwen3-8b", json={"profile": {
        "reasoning": {"efforts": {"off": None, "low": "low"},
                      "default": "low"}}})


def _activate_reasoning_tree(client, tree_id="reasoning-tree"):
    created = client.post("/admin/trees", json={
        "id": tree_id, "name": "Reasoning", "root": _reasoning_tree_root()})
    assert created.status_code == 201, created.text
    enabled = client.post(f"/admin/trees/{tree_id}/enable")
    assert enabled.status_code == 200, enabled.text
    return created.json()["tree"]


def test_model_write_removing_a_referenced_reasoning_level_is_422(client):
    """I-1: the health check must judge the write with the PROSPECTIVE rows.

    Removing a level the ACTIVE tree references is rejected instead of being
    accepted (which used to make the next startup refuse to boot).
    """
    _declare_high(client)
    _activate_reasoning_tree(client)
    before = {m["id"]: m["profile"] for m in
              client.get("/admin/models").json()["models"]}

    r = _declare_low_only(client)
    assert r.status_code == 422, r.text
    issues = r.json()["detail"]
    assert any(issue["code"] == "reasoning_unsupported"
               and issue["node_id"] == "root" for issue in issues), issues
    after = {m["id"]: m["profile"] for m in
             client.get("/admin/models").json()["models"]}
    assert after["qwen3-8b"] == before["qwen3-8b"]   # rejected write, DB intact


def test_model_write_ignores_reasoning_of_a_closed_tree(client):
    """Only the ACTIVE tree gates model writes: a closed draft does not, and
    disabling the active tree lifts the constraint again."""
    _declare_high(client)
    created = client.post("/admin/trees", json={
        "id": "draft-tree", "name": "Draft",
        "root": _reasoning_tree_root()})
    assert created.status_code == 201, created.text
    # No ACTIVE tree: the write cannot break the hot path or the next boot.
    assert _declare_low_only(client).status_code == 200

    # Same write against a formerly-active but now DISABLED tree is accepted.
    _declare_high(client)
    _activate_reasoning_tree(client, tree_id="disabled-tree")
    assert client.post("/admin/trees/disabled-tree/disable").status_code == 200
    assert _declare_low_only(client).status_code == 200


def test_model_write_keeping_the_referenced_reasoning_level_is_200(client):
    _declare_high(client)
    _activate_reasoning_tree(client)
    r = client.put("/admin/models/qwen3-8b", json={"profile": {
        "reasoning": {"efforts": {"off": None, "high": "high"},
                      "default": "high"}}})
    assert r.status_code == 200, r.text
    model = next(m for m in client.get("/admin/models").json()["models"]
                 if m["id"] == "qwen3-8b")
    assert model["reasoning"]["declared"] is True
    assert [entry["id"] for entry in model["reasoning"]["efforts"]] == [
        "off", "high"]


def test_rejected_model_write_keeps_the_next_startup_healthy(tmp_path):
    """The old bug: the write was accepted but the next boot refused.  After
    the 422 the SAME database must still boot with its active reasoning tree."""
    app = make_app(tmp_path)
    client = TestClient(app)
    _declare_high(client)
    _activate_reasoning_tree(client)
    assert _declare_low_only(client).status_code == 422

    restarted = make_app(tmp_path)           # create_app on the same DB file
    assert restarted.state.snapshot.tree_id == "reasoning-tree"


# --------------------------------------------------------------------------- #
# delete-model: the reference check and the delete share the publish lock
# --------------------------------------------------------------------------- #
def _instrument_delete_lock(app, client, monkeypatch):
    """Record whether ``model_references`` is called while the lock is held."""
    from router.api import routes_admin

    publisher = app.state.publisher
    real_locked = publisher.locked
    trace: list[str] = []
    state = {"inside": False}

    @contextmanager
    def recording_locked():
        state["inside"] = True
        trace.append("lock-enter")
        try:
            with real_locked():
                yield publisher
        finally:
            state["inside"] = False
            trace.append("lock-exit")

    monkeypatch.setattr(publisher, "locked", recording_locked)

    real_references = routes_admin.model_references

    def recording_references(trees, model_id):
        trace.append("check")
        assert state["inside"], "reference check must run inside the publish lock"
        return real_references(trees, model_id)

    monkeypatch.setattr(routes_admin, "model_references", recording_references)
    return trace


def test_delete_model_checks_references_inside_the_publish_lock(
        app, client, monkeypatch):
    created = client.post("/admin/trees", json={
        "id": "ref-tree", "name": "Ref",
        "root": {"id": "root", "type": "route", "model": "qwen3-8b"}})
    assert created.status_code == 201, created.text
    trace = _instrument_delete_lock(app, client, monkeypatch)   # after setup

    response = client.delete("/admin/models/qwen3-8b")

    assert response.status_code == 409
    assert trace == ["lock-enter", "check", "lock-exit"]


def test_delete_free_model_also_checks_inside_the_publish_lock(
        app, client, monkeypatch):
    trace = _instrument_delete_lock(app, client, monkeypatch)

    response = client.delete("/admin/models/qwen3-32b")

    assert response.status_code == 200, response.text
    assert trace[0] == "lock-enter"
    assert "check" in trace
    assert trace[-1] == "lock-exit"
