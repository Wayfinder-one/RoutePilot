"""空数据库端到端：只有一份自包含画布 JSON 也能完整运行（剩余计划 Task D2）。

覆盖：
1. 空 SQLite（无 provider/model/时段/JEV 行，仅图目录里一份自包含 JSON）启动快照；
2. ``/admin/try`` 的 backend URL、upstream、capability、JEV、窗口、时段全部来自 JSON；
3. ``/v1/chat/completions`` Auto 路由真实到达 JSON 指定的 mock 上游；
4. ``/admin/test/chat/completions`` 固定模型按 SQLite 目录解析（不读画布 JSON）后到达目录配置的上游；
5. 导出 bytes 在第二个空库实例恢复后结果一致；
6. 文件 / API envelope / tree / snapshot content 中均无密钥样本。
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.routing.document import graph_document_bytes
from router.routing.snapshot import build_snapshot_for_tree
from router.store.db import connect, migrate
from router.store.repositories import SettingsRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
    portable_tree_dict,
    test_settings,
)
from router.routing.tree import tree_from_dict

UPSTREAM_ORIGIN = "http://json-only.test"
JEV_ORIGIN = "http://jev-json.test"
ENV_SECRET = "sk-env-json-secret"
PERIODS = ("peak", "normal", "offpeak")


def self_contained_root():
    """start → 时段条件(peak) → 二选一问题 → 目标(fast)。

    条件节点用来证明时段来自 JSON；问题节点用来逼出 JEV 调用并暴露 profile
    的 capability 说明；目标引用 runtime 里的 fast 模型。
    """
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "cond-peak", "type": "condition", "source": "periods",
             "periods": ["peak"]},
            {"id": "q", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位。",
             "options": [
                 {"key": "off", "label": "轻量", "capability": "快速问答"},
                 {"key": "high", "label": "增强", "capability": "复杂分析"},
             ]},
            {"id": "fast-target", "type": "target", "priority": 1,
             "models": [{"model": "fast", "reasoning": "high"}]},
        ],
        "edges": [
            {"id": "e-start-q", "source": "start", "source_port": "out",
             "target": "q", "target_port": "in_1", "condition": None},
            {"id": "e-cond-q", "source": "cond-peak", "source_port": "out",
             "target": "q", "target_port": "in_2", "condition": None},
            {"id": "e-q-off", "source": "q", "source_port": "selected.off",
             "target": "fast-target", "target_port": "in_1", "condition": None},
            {"id": "e-q-high", "source": "q", "source_port": "selected.high",
             "target": "fast-target", "target_port": "in_2", "condition": None},
        ],
        "error_fallback": "fast-target",
    }


def _portable_document(tree_id="portable"):
    document = portable_tree_dict(tree_id, root=self_contained_root())
    # fixture 的 runtime 模型 id 是 fast；保证 root 引用的 reasoning 档位在 profile 内。
    document["backends"]["models"][0].setdefault("profile", {})
    return document


def _mock_transport(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else {}
        if url.startswith(JEV_ORIGIN):
            calls["jev"].append(body)
            answers = {}
            for node_id, question in (body.get("questions") or {}).items():
                criteria = question.get("criteria") or {}
                labels = list(criteria) or ["候选 A", "候选 B"]
                share = 1.0 / len(labels)
                answers[node_id] = {
                    "type": question.get("type"),
                    "probabilities": {label: share for label in labels},
                }
            return httpx.Response(200, json={
                "answers": answers,
                "usage": {"input_tokens": 7, "output_tokens": 0},
            })
        calls["upstream"].append({
            "url": url,
            "authorization": request.headers.get("authorization"),
            "body": body,
        })
        return httpx.Response(200, json={
            "id": "cmpl-json", "object": "chat.completion", "model": body.get("model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "来自 JSON 上游的回复"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
    return httpx.MockTransport(handler)


def _empty_database(path: str) -> None:
    """建表后清空目录并标记已种子：模拟“没有任何非密钥运行配置”的空库。"""
    conn = connect(path)
    try:
        migrate(conn)
        settings = SettingsRepo(conn)
        settings.set("catalog_seeded", "1")
        settings.set("time_periods_seeded", "1")
        conn.commit()
    finally:
        conn.close()


def _install_document(settings, document) -> None:
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(document))
    repo.enable(document["id"])


@pytest.fixture
def json_only(tmp_path, monkeypatch):
    """空库 + 只含一份自包含 JSON 的图目录 + mock JEV/上游。"""
    monkeypatch.setenv("JSON_PROVIDER_API_KEY", ENV_SECRET)
    settings = test_settings(tmp_path)
    _empty_database(settings.db_path)
    _install_document(settings, _portable_document())
    calls = {"jev": [], "upstream": []}
    app = create_app(settings, transport=_mock_transport(calls))
    return app, TestClient(app), settings, calls


def test_empty_database_snapshot_reads_everything_from_json(json_only):
    app, client, settings, _calls = json_only

    assert app.state.snapshot.tree_id == "portable"
    backend = app.state.snapshot.backends["fast"]
    assert backend.url == UPSTREAM_ORIGIN + "/v1/chat/completions"
    assert backend.upstream_name == "json-upstream"
    assert backend.provider == "local"
    assert app.state.snapshot.capabilities["fast"] == "来自 JSON 的能力"
    assert app.state.snapshot.jev.base_url == JEV_ORIGIN
    assert app.state.snapshot.jev.model == "json-jev"
    assert (app.state.snapshot.jev_input_tokens,
            app.state.snapshot.jev_context_tokens) == (1024, 4096)
    assert [period.name for period in app.state.snapshot.time_periods] == list(PERIODS)

    # 非密钥运行配置表仍然为空：JSON 是事实源，启动不物化数据库。
    conn = connect(settings.db_path)
    try:
        for table in ("providers", "models", "time_periods", "jev_services"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        conn.close()

    # 管理面目录 GET 只查 SQLite：空库就是空目录，不再投影活动画布 JSON。
    assert client.get("/admin/models").json()["models"] == []
    assert client.get("/admin/providers").json()["providers"] == []
    assert client.get("/admin/jev").json() == {"services": []}
    assert client.get("/admin/time-periods").json()["periods"] == []


def test_admin_try_uses_json_backends_jev_window_and_periods(json_only):
    app, client, _settings, calls = json_only

    long_text = "帮我分析这段复杂代码。" * 400
    response = client.post("/admin/try", json={
        "tree_id": "portable",
        "text": long_text,
        "now": "2026-11-20T10:00:00+08:00",   # 周五 10:00 → peak
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"]["model"] == "fast"
    assert body["outcome"]["target_id"] == "fast-target"
    assert body["warnings"] == []
    # 时段来自 JSON：trace 的 time 事件列出活动时段，条件节点命中 peak。
    time_events = [item for item in body["trace"] if item["event"] == "time"]
    assert time_events and "peak" in time_events[0].get("active_periods", [])
    condition_events = [item for item in body["trace"] if item["event"] == "condition"]
    assert any("peak" in (item.get("matched") or []) for item in condition_events)
    # 窗口来自 JSON（input_tokens=1024）：超长输入被截断并记录在 trace。
    window_events = [item for item in body["trace"] if item["event"] == "window"]
    assert window_events and window_events[0]["truncated"] is True
    assert window_events[0]["tokens_after"] <= app.state.snapshot.jev_input_tokens
    # JEV 调用来自 JSON 的 base_url / model / state_template，输入经过 JSON 窗口约束。
    assert calls["jev"], "graph question did not call the JSON-configured JEV"
    sent = calls["jev"][0]
    assert sent["model"] == "json-jev"
    assert sent["questions"], "JEV request carried no questions"
    assert "已截断" in sent["state"]
    assert sent["state"].startswith(DEFAULT_STATE_TEMPLATE.split("{request}")[0])


def test_auto_routing_reaches_the_json_mock_upstream(json_only):
    _app, client, _settings, calls = json_only

    response = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "帮我分析这段复杂代码"}],
    })

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "来自 JSON 上游的回复"
    assert calls["upstream"], "Auto routing never reached the JSON upstream"
    upstream = calls["upstream"][-1]
    assert upstream["url"] == UPSTREAM_ORIGIN + "/v1/chat/completions"
    assert upstream["body"]["model"] == "json-upstream"
    # provider 的 key_env 在运行时解析：旧密钥不来自 JSON 文件，但环境变量生效。
    assert upstream["authorization"] == "Bearer " + ENV_SECRET


def test_fixed_model_uses_db_catalog_instead_of_active_canvas(json_only):
    """固定模型直连是管理目录测试：读 SQLite 目录，不读活动画布快照。"""
    _app, client, settings, calls = json_only
    conn = connect(settings.db_path)
    with conn:
        conn.execute(
            "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
            "VALUES ('db', 'http://db.test/v1', '', 'openai', '')")
        conn.execute(
            "INSERT INTO models (id, upstream, provider, profile) "
            "VALUES ('fast', 'db-upstream', 'db', '{}')")
    conn.close()

    response = client.post("/admin/test/chat/completions", json={
        "model": "fast",
        "messages": [{"role": "user", "content": "固定模型直连"}],
    })

    assert response.status_code == 200, response.text
    assert calls["upstream"], "fixed model chat never reached the DB upstream"
    upstream = calls["upstream"][-1]
    assert upstream["body"]["model"] == "db-upstream"
    assert upstream["url"] == "http://db.test/v1/chat/completions"
    # 活动画布内嵌的 JSON 上游未被使用。
    assert calls["upstream"][-1]["url"] != UPSTREAM_ORIGIN + "/v1/chat/completions"


def _export_bytes(settings) -> bytes:
    path = settings.graph_dir.rstrip("/") + "/graphs/portable.json"
    with open(path, "rb") as handle:
        return handle.read()


def test_exported_json_restores_in_a_second_empty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("JSON_PROVIDER_API_KEY", ENV_SECRET)
    (tmp_path / "first").mkdir()
    first = test_settings(tmp_path / "first")
    _empty_database(first.db_path)
    _install_document(first, _portable_document())
    calls_one = {"jev": [], "upstream": []}
    app_one = create_app(first, transport=_mock_transport(calls_one))

    exported = _export_bytes(first)
    assert b"api_key" not in exported
    assert ENV_SECRET.encode() not in exported

    (tmp_path / "second").mkdir()
    second = test_settings(tmp_path / "second")
    _empty_database(second.db_path)
    repo = RoutingTreeRepo(second.graph_dir)
    repo.initialize()
    (repo.graphs / "portable.json").write_bytes(exported)
    (repo.current).write_text("portable\n", encoding="utf-8")
    calls_two = {"jev": [], "upstream": []}
    app_two = create_app(second, transport=_mock_transport(calls_two))

    assert app_two.state.snapshot.tree_revision == app_one.state.snapshot.tree_revision
    assert app_two.state.snapshot.content == app_one.state.snapshot.content
    assert app_two.state.snapshot.backends["fast"].url == \
        app_one.state.snapshot.backends["fast"].url
    assert app_two.state.snapshot.jev.model == app_one.state.snapshot.jev.model

    client_two = TestClient(app_two)
    response = client_two.post("/admin/try", json={
        "tree_id": "portable", "text": "帮我分析这段复杂代码",
        "now": "2026-11-20T10:00:00+08:00",
    })
    assert response.status_code == 200, response.text
    assert response.json()["outcome"]["model"] == "fast"
    assert response.json()["tree_revision"] == app_one.state.snapshot.tree_revision


def test_no_secret_leaks_into_files_api_or_snapshot(json_only):
    app, client, settings, _calls = json_only

    exported = _export_bytes(settings).decode("utf-8")
    for marker in ("api_key", "key_source", "key_present", ENV_SECRET):
        assert marker not in exported, marker

    # 目录里放一条只靠环境变量取密钥的 provider，验证派生状态与脱敏投影。
    conn = connect(settings.db_path)
    with conn:
        conn.execute(
            "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
            "VALUES ('env-provider', 'http://env.test/v1', 'JSON_PROVIDER_API_KEY', 'openai', '')")
    conn.close()
    tree_payload = client.get("/admin/trees").text
    provider_payload = client.get("/admin/providers").text
    jev_payload = client.get("/admin/jev").text
    for payload in (tree_payload, provider_payload, jev_payload):
        assert ENV_SECRET not in payload
        assert "api_key\":" not in payload
    provider = client.get("/admin/providers").json()["providers"][0]
    assert provider["name"] == "env-provider"
    assert provider["key_env"] == "JSON_PROVIDER_API_KEY"
    assert provider["key_source"] == "env"
    assert "api_key" not in provider

    assert ENV_SECRET not in app.state.snapshot.content
    assert "api_key" not in app.state.snapshot.content


def _create_payload(tree_id):
    """POST /admin/trees 的 DTO 不接受 enabled：从文档 fixture 里去掉。"""
    document = _portable_document(tree_id)
    document.pop("enabled", None)
    return document


def test_nested_profile_secrets_are_rejected_and_never_exported(json_only):
    """合法 profile 元数据照常保存；嵌套密钥经 POST/PUT/旧库物化都进不了 JSON。"""
    app, client, settings, _calls = json_only
    repo = app.state.graph_repo
    before = {path.name: path.read_bytes() for path in repo.graphs.glob("*.json")}

    # 合法元数据：不影响主流程，并原样写进文件。
    document = _create_payload("meta-tree")
    document["backends"]["models"][0]["profile"]["metadata"] = {
        "domains": ["json"], "tier": "small", "labels": [{"tier": "keep-me"}],
    }
    created = client.post("/admin/trees", json=document)
    assert created.status_code == 201, created.text
    stored = (repo.graphs / "meta-tree.json").read_text(encoding="utf-8")
    assert "keep-me" in stored

    # POST：大小写/连字符变体的嵌套密钥全部 422，不写文件。
    for key in ("api_key", "API-KEY", "authorization", "key_present", "key_source"):
        hostile = _create_payload("hostile")
        hostile["backends"]["models"][0]["profile"]["metadata"] = [{key: "review-secret"}]
        response = client.post("/admin/trees", json=hostile)
        assert response.status_code == 422, (key, response.text)
        assert "secret field is not allowed" in response.text
        assert "review-secret" not in response.text
        assert not (repo.graphs / "hostile.json").exists()

    # PUT：同一份文档拒绝后，活动图字节与响应都不含样本。
    current = client.get("/admin/trees/portable").json()["tree"]
    hostile_runtime = copy.deepcopy({
        field: current[field]
        for field in ("backends", "jev", "jev_window", "time_periods")
    })
    hostile_runtime["backends"]["models"][0]["profile"]["metadata"] = [
        {"authorization": "review-secret"}]
    updated = client.put("/admin/trees/portable", json={
        **hostile_runtime, "base_document_sha256": current["document_sha256"]})
    assert updated.status_code == 422, updated.text
    assert "secret field is not allowed" in updated.text
    assert "review-secret" not in updated.text
    assert (repo.graphs / "portable.json").read_bytes() == before["portable.json"]

    for path in repo.graphs.glob("*.json"):
        assert "review-secret" not in path.read_text(encoding="utf-8"), path.name
    assert ENV_SECRET not in app.state.snapshot.content


def test_graph_snapshot_does_not_change_when_db_catalog_changes(json_only):
    """已保存画布的快照是唯一运行事实源：目录行怎么改都不改变它。"""
    app, _client, settings, _calls = json_only
    before = app.state.snapshot.content
    tree = app.state.graph_repo.active()
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
                "VALUES ('other', 'http://other.test/v1', '', 'openai', '')")
            conn.execute(
                "INSERT INTO models (id, upstream, provider, profile) "
                "VALUES ('changed', 'changed', 'other', '{}')")
            conn.execute(
                "UPDATE jev_services SET base_url = 'http://db-changed.test'")
        after = build_snapshot_for_tree(conn, tree)
    finally:
        conn.close()
    assert after.content == before
    assert app.state.snapshot.content == before
