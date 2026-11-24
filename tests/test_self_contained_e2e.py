"""空数据库端到端：只有一份自包含画布 JSON 也能完整运行。

覆盖：
1. 空 SQLite（无 provider/model/时段/JEV 行，仅图目录里一份自包含 JSON）启动快照；
2. ``/admin/try`` 的 backend URL、upstream、capability、JEV、窗口、时段全部来自 JSON；
3. ``/v1/chat/completions`` Auto 路由真实到达 JSON 指定的 mock 上游；
4. ``/admin/test/chat/completions`` 固定模型按 SQLite 目录解析（不读画布 JSON）后到达目录配置的上游；
5. 导出 bytes 在第二个空库实例恢复后结果一致；
6. 文件 / API envelope / tree / snapshot content 中均无密钥样本；
7. Task 11 最终验收：JSON 选择 ``jev-a`` + ``fast`` 后真实 mock JEV/模型调用成功、
   导出字节单独复制即可复现同一 ``tree_revision``/快照内容、数据库目录完全不同时
   Auto 仍只读 JSON 而固定模型直连只读数据库、目录写未确认时两幅画布与数据库行
   零改动、确认后只改受影响画布并发布新活动快照、全流程无密钥材料泄漏。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.service import JevService
from router.routing.document import graph_document_bytes
from router.routing.snapshot import build_snapshot_for_tree
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.repositories import SettingsRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
    default_tree,
    portable_tree_dict,
    test_settings,
)
from router.routing.tree import tree_from_dict

UPSTREAM_ORIGIN = "http://json-only.test"
JEV_ORIGIN = "http://jev-json.test"
ENV_SECRET = "sk-env-json-secret"
STORED_PROVIDER_SECRET = "sk-stored-provider-sample"
STORED_JEV_SECRET = "sk-stored-jev-sample"
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


def _acceptance_document(tree_id="portable"):
    """Task 11 验收画布：JEV 目录服务 id 为 ``jev-a``（ID 属于画布快照）。"""
    document = _portable_document(tree_id)
    document["jev"]["id"] = "jev-a"
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


def _install_document(settings, document, *, enable: bool = True) -> None:
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    payload = dict(document)
    if enable:
        payload.update({"enabled": True, "default": True})
    repo.create(tree_from_dict(payload))


def _instance(path, monkeypatch, documents=(), *, active_id: str | None = None):
    """空数据库实例：只装给定的自包含画布，mock JEV/上游。"""
    monkeypatch.setenv("JSON_PROVIDER_API_KEY", ENV_SECRET)
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    settings = test_settings(path)
    _empty_database(settings.db_path)
    if documents:
        repo = RoutingTreeRepo(settings.graph_dir)
        repo.initialize()
        active = active_id or documents[-1]["id"]
        for document in documents:
            payload = dict(document)
            is_active = document["id"] == active
            payload["enabled"] = is_active
            payload["default"] = is_active
            repo.create(tree_from_dict(payload))
    calls = {"jev": [], "upstream": []}
    app = create_app(settings, transport=_mock_transport(calls))
    return SimpleNamespace(
        settings=settings, app=app, client=TestClient(app), calls=calls)


def _instance_from_export(path, monkeypatch, tree_id: str, payload: bytes):
    """第二个空库实例：唯一输入是第一个实例导出的画布文件字节。"""
    monkeypatch.setenv("JSON_PROVIDER_API_KEY", ENV_SECRET)
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    settings = test_settings(path)
    _empty_database(settings.db_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    (repo.graphs / f"{tree_id}.json").write_bytes(payload)
    calls = {"jev": [], "upstream": []}
    app = create_app(settings, transport=_mock_transport(calls))
    return SimpleNamespace(
        settings=settings, app=app, client=TestClient(app), calls=calls)


def _unrelated_document(tree_id="other"):
    """第二幅画布：引用 backup 模型，与 fast 的目录变更无关（非活动画布）。"""
    document = _portable_document(tree_id)
    document["backends"]["models"][0].update(
        {"id": "backup", "upstream": "backup-upstream"})
    for node in document["root"]["nodes"]:
        if node.get("type") == "target":
            for slot in node["models"]:
                if slot["model"] == "fast":
                    slot["model"] = "backup"
    return document


def _seed_matching_catalog(settings) -> None:
    """目录行与画布声明一致：fast/backup 模型挂在 local provider 下。

    profile 必须与画布快照逐字段一致（含 target 引用的 high 推理档位），否则
    确认后的模型同步会用目录 profile 覆盖画布声明并触发启用级 422。
    """
    profile = json.dumps(
        _portable_document()["backends"]["models"][0]["profile"],
        ensure_ascii=False)
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
                "VALUES ('local', ?, 'JSON_PROVIDER_API_KEY', 'openai', '')",
                (UPSTREAM_ORIGIN + "/v1",))
            conn.execute(
                "INSERT INTO models (id, upstream, provider, profile) "
                "VALUES ('fast', 'json-upstream', 'local', ?)", (profile,))
            conn.execute(
                "INSERT INTO models (id, upstream, provider, profile) "
                "VALUES ('backup', 'backup-upstream', 'local', ?)", (profile,))
    finally:
        conn.close()


def _seed_secret_matching_catalog(settings, document=None) -> None:
    """目录里放端点匹配的已存密钥：provider local + 画布所选 JEV 服务。"""
    document = _portable_document() if document is None else document
    service = document["jev"]
    window = document["jev_window"]
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
            "VALUES ('local', ?, 'JSON_PROVIDER_API_KEY', 'openai', ?)",
            (UPSTREAM_ORIGIN + "/v1", STORED_PROVIDER_SECRET))
        conn.commit()
        JevServiceRepo(conn).save(JevService(
            id=service["id"],
            base_url=service["base_url"],
            model=service["model"],
            key_env=service["key_env"],
            timeout_seconds=service["timeout_seconds"],
            instructions=service["instructions"],
            state_template=service["state_template"],
            input_tokens=window["input_tokens"],
            context_tokens=window["context_tokens"],
        ), api_key=STORED_JEV_SECRET)
    finally:
        conn.close()


def _confirmation_from(response) -> dict:
    """409 影响预览 → 携带每幅受影响画布当前哈希的确认载荷。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required", response.text
    return {"trees": [
        {"id": item["id"], "document_sha256": item["document_sha256"]}
        for item in detail["affected_trees"]
    ]}


def _canvas_files(settings) -> dict[str, bytes]:
    graphs = Path(settings.graph_dir, "graphs")
    return {path.name: path.read_bytes() for path in sorted(graphs.glob("*.json"))}


def _model_rows(settings) -> list[tuple]:
    conn = connect(settings.db_path)
    try:
        return conn.execute(
            "SELECT id, upstream, provider, profile FROM models ORDER BY id").fetchall()
    finally:
        conn.close()


def _audit_count(settings) -> int:
    conn = connect(settings.db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0])
    finally:
        conn.close()


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

    assert app.state.snapshot_set.default_snapshot.tree_id == "portable"
    backend = app.state.snapshot_set.default_snapshot.backends["fast"]
    assert backend.url == UPSTREAM_ORIGIN + "/v1/chat/completions"
    assert backend.upstream_name == "json-upstream"
    assert backend.provider == "local"
    assert app.state.snapshot_set.default_snapshot.capabilities["fast"] == "来自 JSON 的能力"
    assert app.state.snapshot_set.default_snapshot.jev.base_url == JEV_ORIGIN
    assert app.state.snapshot_set.default_snapshot.jev.model == "json-jev"
    assert (app.state.snapshot_set.default_snapshot.jev_input_tokens,
            app.state.snapshot_set.default_snapshot.jev_context_tokens) == (1024, 4096)
    assert [period.name for period in app.state.snapshot_set.default_snapshot.time_periods] == list(PERIODS)

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
    # 空数据库：画布声明的 provider/model/JEV 都不在目录里 → 全部是 hint 级差异提示，
    # 不阻塞执行；具体字段由 tests/test_runtime_consistency.py 精确覆盖。
    assert body["warnings"]
    assert {item["code"] for item in body["warnings"]} == {"runtime_mismatch"}
    assert all(item["level"] == "hint" and item["params"]["database"] is None
               for item in body["warnings"])
    # 时段来自 JSON：trace 的 time 事件列出活动时段，条件节点命中 peak。
    time_events = [item for item in body["trace"] if item["event"] == "time"]
    assert time_events and "peak" in time_events[0].get("active_periods", [])
    condition_events = [item for item in body["trace"] if item["event"] == "condition"]
    assert any("peak" in (item.get("matched") or []) for item in condition_events)
    # 窗口来自 JSON（input_tokens=1024）：超长输入被截断并记录在 trace。
    window_events = [item for item in body["trace"] if item["event"] == "window"]
    assert window_events and window_events[0]["truncated"] is True
    assert window_events[0]["tokens_after"] <= app.state.snapshot_set.default_snapshot.jev_input_tokens
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


def _export_bytes(settings, tree_id: str = "portable") -> bytes:
    path = settings.graph_dir.rstrip("/") + f"/graphs/{tree_id}.json"
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
    calls_two = {"jev": [], "upstream": []}
    app_two = create_app(second, transport=_mock_transport(calls_two))

    assert app_two.state.snapshot_set.default_snapshot.tree_revision == app_one.state.snapshot_set.default_snapshot.tree_revision
    assert app_two.state.snapshot_set.default_snapshot.content == app_one.state.snapshot_set.default_snapshot.content
    assert app_two.state.snapshot_set.default_snapshot.backends["fast"].url == \
        app_one.state.snapshot_set.default_snapshot.backends["fast"].url
    assert app_two.state.snapshot_set.default_snapshot.jev.model == app_one.state.snapshot_set.default_snapshot.jev.model

    client_two = TestClient(app_two)
    response = client_two.post("/admin/try", json={
        "tree_id": "portable", "text": "帮我分析这段复杂代码",
        "now": "2026-11-20T10:00:00+08:00",
    })
    assert response.status_code == 200, response.text
    assert response.json()["outcome"]["model"] == "fast"
    assert response.json()["tree_revision"] == app_one.state.snapshot_set.default_snapshot.tree_revision


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

    assert ENV_SECRET not in app.state.snapshot_set.default_snapshot.content
    assert "api_key" not in app.state.snapshot_set.default_snapshot.content


def _create_payload(tree_id):
    """POST /admin/trees 的 DTO 不接受 enabled/default：从文档 fixture 里去掉。"""
    document = _portable_document(tree_id)
    document.pop("enabled", None)
    document.pop("default", None)
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
    assert ENV_SECRET not in app.state.snapshot_set.default_snapshot.content


def test_graph_snapshot_does_not_change_when_db_catalog_changes(json_only):
    """已保存画布的快照是唯一运行事实源：目录行怎么改都不改变它。"""
    app, _client, settings, _calls = json_only
    before = app.state.snapshot_set.default_snapshot.content
    tree = default_tree(app.state.graph_repo)
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
    assert app.state.snapshot_set.default_snapshot.content == before


# ────────────────────────────────────────────────────────────────────────��──
# Task 11 最终验收（计划 Step 1）：真实双实例、目录影响确认与全流程密钥扫描
# ────────────────────────────────────────────────────────────────────────��──
def test_acceptance_json_selects_jev_a_and_fast_and_calls_succeed(tmp_path, monkeypatch):
    """① 第一个空库实例只凭 JSON 选中 jev-a + fast，真实 mock JEV/模型调用成功。"""
    instance = _instance(tmp_path / "first", monkeypatch,
                         documents=[_acceptance_document()])
    app, client, calls = instance.app, instance.client, instance.calls
    snapshot = app.state.snapshot_set.default_snapshot

    tree = default_tree(app.state.graph_repo)
    assert tree.runtime.jev.id == "jev-a"
    assert [model["id"] for model in tree.runtime.backends.models] == ["fast"]
    assert (tree.runtime.jev_input_tokens, tree.runtime.jev_context_tokens) == (1024, 4096)
    assert snapshot.backends["fast"].url == UPSTREAM_ORIGIN + "/v1/chat/completions"
    assert snapshot.jev.base_url == JEV_ORIGIN and snapshot.jev.model == "json-jev"

    # 真实调用 1：/admin/try 的问题节点到达 JSON 里的 jev-a 端点。
    tried = client.post("/admin/try", json={
        "tree_id": "portable", "text": "帮我分析这段复杂代码",
        "now": "2026-11-20T10:00:00+08:00",
    })
    assert tried.status_code == 200, tried.text
    assert tried.json()["outcome"]["model"] == "fast"
    assert calls["jev"], "JEV 调用没有到达 JSON 声明的 jev-a 端点"
    assert calls["jev"][0]["model"] == "json-jev"

    # 真实调用 2：/v1 Auto 路由到达 JSON 声明的上游，密钥来自环境变量。
    answered = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "帮我分析这段复杂代码"}]})
    assert answered.status_code == 200, answered.text
    assert calls["upstream"], "Auto 路由没有到达 JSON 上游"
    assert calls["upstream"][-1]["url"] == UPSTREAM_ORIGIN + "/v1/chat/completions"
    assert calls["upstream"][-1]["body"]["model"] == "json-upstream"
    assert calls["upstream"][-1]["authorization"] == "Bearer " + ENV_SECRET


def test_acceptance_exported_bytes_alone_reproduce_the_snapshot(tmp_path, monkeypatch):
    """② 第二个空库实例只复制导出字节：tree_revision 与快照内容逐字节一致。"""
    first = _instance(tmp_path / "first", monkeypatch,
                      documents=[_acceptance_document()])
    exported = _export_bytes(first.settings)
    assert b"api_key" not in exported and ENV_SECRET.encode() not in exported
    second = _instance_from_export(
        tmp_path / "second", monkeypatch, "portable", exported)

    first_snapshot = first.app.state.snapshot_set.default_snapshot
    second_snapshot = second.app.state.snapshot_set.default_snapshot
    assert second_snapshot is not None
    assert second_snapshot.tree_revision == first_snapshot.tree_revision
    assert second_snapshot.content == first_snapshot.content
    assert second_snapshot.snapshot_revision == first_snapshot.snapshot_revision
    assert default_tree(second.app.state.graph_repo).runtime.jev.id == "jev-a"

    # 第二个实例的数据库目录确实为空：唯一输入是导出的画布字节。
    conn = connect(second.settings.db_path)
    try:
        for table in ("providers", "models", "time_periods", "jev_services"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()

    # 只凭字节复制出来的实例也能真实执行。
    answered = second.client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "帮我分析这段复杂代码"}]})
    assert answered.status_code == 200, answered.text
    assert second.calls["upstream"][-1]["url"] == UPSTREAM_ORIGIN + "/v1/chat/completions"


def test_acceptance_auto_reads_json_while_fixed_model_reads_db(tmp_path, monkeypatch):
    """③ 第二个实例数据库目录完全不同：Auto 仍走 JSON，固定模型直连走数据库。"""
    first = _instance(tmp_path / "first", monkeypatch,
                      documents=[_acceptance_document()])
    exported = _export_bytes(first.settings)
    second = _instance_from_export(
        tmp_path / "second", monkeypatch, "portable", exported)

    conn = connect(second.settings.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
                "VALUES ('db-only', 'http://db-only.test/v1', '', 'openai', '')")
            conn.execute(
                "INSERT INTO models (id, upstream, provider, profile) "
                "VALUES ('fast', 'db-upstream', 'db-only', '{}')")
        JevServiceRepo(conn).save(JevService(
            id="db-jev",
            base_url="http://db-jev.test",
            model="db-jev-model",
            key_env="",
            timeout_seconds=10.0,
            instructions=DEFAULT_INSTRUCTIONS,
            state_template=DEFAULT_STATE_TEMPLATE,
            input_tokens=2048,
            context_tokens=8192,
        ))
    finally:
        conn.close()

    # Auto：执行配置只来自 JSON，数据库目录不同的 JEV/模型不参与。
    auto = second.client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "帮我分析这段复杂代码"}]})
    assert auto.status_code == 200, auto.text
    assert second.calls["upstream"][-1]["url"] == UPSTREAM_ORIGIN + "/v1/chat/completions"
    assert second.calls["upstream"][-1]["body"]["model"] == "json-upstream"
    assert second.calls["jev"], "Auto 没有使用 JSON 声明的 jev-a"
    assert second.calls["jev"][0]["model"] == "json-jev"

    # 固定模型直连：管理目录测试，读 SQLite 目录而非活动画布快照。
    fixed = second.client.post("/admin/test/chat/completions", json={
        "model": "fast",
        "messages": [{"role": "user", "content": "固定模型直连"}],
    })
    assert fixed.status_code == 200, fixed.text
    assert second.calls["upstream"][-1]["url"] == "http://db-only.test/v1/chat/completions"
    assert second.calls["upstream"][-1]["body"]["model"] == "db-upstream"


def _impact_instance(tmp_path, monkeypatch):
    """两幅自包含画布（portable 为活动画布，other 引用 backup）+ 匹配的目录行。"""
    instance = _instance(
        tmp_path / "impact", monkeypatch,
        documents=[_portable_document(), _unrelated_document()],
        active_id="portable")
    _seed_matching_catalog(instance.settings)
    return instance


def test_acceptance_unconfirmed_catalog_change_changes_nothing(tmp_path, monkeypatch):
    """④ 目录修改未确认：两幅画布字节、数据库行、审计与快照全部不变。"""
    instance = _impact_instance(tmp_path, monkeypatch)
    app, client = instance.app, instance.client
    before_files = _canvas_files(instance.settings)
    before_rows = _model_rows(instance.settings)
    before_audit = _audit_count(instance.settings)
    before_tree_revision = app.state.snapshot_set.default_snapshot.tree_revision
    before_snapshot_revision = app.state.snapshot_set.snapshot_revision
    assert set(before_files) == {"portable.json", "other.json"}

    response = client.put("/admin/models/fast", json={"upstream": "changed-upstream"})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    assert [item["id"] for item in detail["affected_trees"]] == ["portable"]
    assert _canvas_files(instance.settings) == before_files
    assert _model_rows(instance.settings) == before_rows
    assert _audit_count(instance.settings) == before_audit
    assert app.state.snapshot_set.default_snapshot.tree_revision == before_tree_revision
    assert app.state.snapshot_set.snapshot_revision == before_snapshot_revision


def test_acceptance_confirmed_catalog_change_syncs_only_affected_canvas(tmp_path, monkeypatch):
    """⑤ 确认后只有受影响画布改变，活动实例发布新快照。"""
    instance = _impact_instance(tmp_path, monkeypatch)
    app, client = instance.app, instance.client
    before_files = _canvas_files(instance.settings)
    before_tree_revision = app.state.snapshot_set.default_snapshot.tree_revision
    before_snapshot_revision = app.state.snapshot_set.snapshot_revision

    preview = client.put("/admin/models/fast", json={"upstream": "changed-upstream"})
    confirmation = _confirmation_from(preview)
    confirmed = client.put("/admin/models/fast", json={
        "upstream": "changed-upstream", "impact_confirmation": confirmation})

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["synced_trees"] == ["portable"]
    after_files = _canvas_files(instance.settings)
    assert after_files["portable.json"] != before_files["portable.json"]
    assert after_files["other.json"] == before_files["other.json"]
    rows = {row[0]: row for row in _model_rows(instance.settings)}
    assert rows["fast"][1] == "changed-upstream"
    assert rows["backup"][1] == "backup-upstream"

    snapshot = app.state.snapshot_set.default_snapshot
    assert snapshot.tree_revision != before_tree_revision
    assert snapshot.snapshot_revision != before_snapshot_revision
    assert snapshot.backends["fast"].upstream_name == "changed-upstream"
    assert "changed-upstream" in snapshot.content
    # 非活动画布未被同步：仍只声明 backup。
    other = app.state.graph_repo.get("other")
    assert other.runtime.backends.models[0]["id"] == "backup"


def test_acceptance_whole_flow_never_leaks_secret_material(tmp_path, monkeypatch):
    """⑥ 全流程（try + Auto + 目录读取）不把密钥材料写进文件/API/快照内容。"""
    monkeypatch.setenv("JSON_PROVIDER_API_KEY", ENV_SECRET)
    base = tmp_path / "secrets"
    base.mkdir()
    settings = test_settings(base)
    _empty_database(settings.db_path)
    _install_document(settings, _portable_document())
    # 端点匹配的已存密钥：provider local（与画布 base_url/key_env 匹配）与 jev-a。
    _seed_secret_matching_catalog(settings)
    calls = {"jev": [], "upstream": []}
    app = create_app(settings, transport=_mock_transport(calls))
    client = TestClient(app)

    # 密钥只解析进内存快照（headers / JevSettings），且已存密钥优先于环境变量。
    assert STORED_PROVIDER_SECRET in \
        app.state.snapshot_set.default_snapshot.backends["fast"].headers["Authorization"]
    assert app.state.snapshot_set.default_snapshot.jev.api_key == STORED_JEV_SECRET

    tried = client.post("/admin/try", json={
        "tree_id": "portable", "text": "帮我分析这段复杂代码"})
    assert tried.status_code == 200, tried.text
    answered = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "帮我分析这段复杂代码"}]})
    assert answered.status_code == 200, answered.text
    assert calls["upstream"][-1]["authorization"] == "Bearer " + STORED_PROVIDER_SECRET

    secrets = (ENV_SECRET, STORED_PROVIDER_SECRET, STORED_JEV_SECRET)
    derived = ("api_key", "authorization", "key_present", "key_source")

    # 1) 图目录的所有文件（画布 JSON + current）不含密钥或派生密钥字段。
    for path in sorted(Path(settings.graph_dir).rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for secret in secrets:
            assert secret not in text, path
        for name in derived:
            assert name not in text, (path, name)

    # 2) 图 API / 检查路由的响应不含密钥或派生密钥字段。
    for payload in (client.get("/admin/trees").text,
                    client.get("/admin/trees/portable").text,
                    tried.text):
        for secret in secrets:
            assert secret not in payload
        for name in derived:
            assert name not in payload

    # 3) 目录 API 不回显密钥材料（key_present/key_source 是允许的派生状态）。
    for path in ("/admin/models", "/admin/providers", "/admin/jev",
                 "/admin/changes", "/admin/requests/recent"):
        payload = client.get(path).text
        for secret in secrets:
            assert secret not in payload, path
        assert '"api_key"' not in payload, path
        assert "authorization" not in payload.lower(), path

    # 4) 已发布快照内容不含密钥材料。
    for secret in secrets:
        assert secret not in app.state.snapshot_set.default_snapshot.content
    for name in derived:
        assert name not in app.state.snapshot_set.default_snapshot.content
