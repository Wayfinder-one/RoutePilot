import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.service import JevService
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.decision_fixtures import valid_event
from tests.routing_fixtures import (
    core_app,
    core_app_for,
    deliver_decision_events,
    kernel_state,
    recording_core_app,
    test_settings,
    tree_dict,
)


@pytest.fixture
def app(tmp_path):
    kernel = core_app(tmp_path)
    peripheral = create_app(
        test_settings(tmp_path), core_transport=core_app_for(tmp_path, app=kernel))
    peripheral.test_kernel = kernel
    return peripheral


@pytest.fixture
def client(app):
    return TestClient(app)


def _question_root():
    """start → q(choice) → direct：真实 /v1 请求会同时经过 JEV 与上游。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "q", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位",
             "options": [
                 {"key": "light", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
        ],
        "edges": [
            {"id": "e-start-q", "source": "start", "source_port": "out",
             "target": "q", "target_port": "in_1", "condition": None},
            {"id": "e-q-direct", "source": "q", "source_port": "selected.light",
             "target": "direct", "target_port": "in_1", "condition": None},
        ],
        "error_fallback": "direct",
    }


def test_overview_and_request_stats_use_current_log_contract(client):
    overview = client.get("/admin/overview").json()
    assert {"daily", "routes", "total", "sticky", "confidence", "confidence_since"} <= set(overview)
    assert "evals" not in overview
    # 概览只统计内存里保留的决策事件（零条时不伪造历史统计）。
    assert overview["total"] == 0
    assert overview["daily"] == []
    assert overview["routes"] == []
    stats = client.get("/admin/requests/stats").json()
    assert stats["ring_size"] == 0
    assert stats["ring_limit"] == 20


@pytest.mark.parametrize("path", [
    "/admin/signals", "/admin/signal-questions", "/admin/tagger",
    "/admin/keyword-groups", "/admin/eval", "/admin/evals",
    "/admin/settings",
])
def test_legacy_signal_and_eval_apis_are_gone(client, path):
    assert client.get(path).status_code == 404


def test_time_periods_roundtrip_is_catalog_only(client):
    """时段目录写只改数据库；零启用图的空路由集不投影目录。"""
    response = client.put("/admin/time-periods", json={"periods": [{"name": "值班", "days": [0, 1], "start": "22:00", "end": "06:00"}]})
    assert response.status_code == 200, response.text
    body = client.get("/admin/time-periods").json()
    assert body["periods"][0]["name"] == "值班"
    assert "now" in body
    assert kernel_state(client.app).snapshot_set.snapshots == {}


def test_request_recent_returns_memory_ring(client):
    body = client.get("/admin/requests/recent").json()
    assert body == {"requests": []}


def test_restart_never_restores_decision_logs_from_sqlite(tmp_path):
    """决策日志只存在内存：v6 旧 SQLite 账本行在 v7 迁移后既不进水台列表也不可回放。"""
    from tests.test_db import build_v6_database

    settings = test_settings(tmp_path)
    conn = build_v6_database(settings.db_path)
    conn.execute(
        "INSERT INTO request_logs (id, ts, decision, model) "
        "VALUES ('legacy-row', '2026-11-03T18:59:00+08:00', 'route', 'fast')")
    conn.commit()
    conn.close()

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/admin/requests/recent").json() == {"requests": []}
        assert client.get("/admin/requests/legacy-row/replay").status_code == 404
        assert client.get("/admin/requests/stats").json()["ring_size"] == 0
    conn = connect(settings.db_path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"request_logs", "graph_snapshots", "request_snapshots"} & tables
    finally:
        conn.close()


def _receive_event(client, event):
    return client.post("/internal/v1/decision-logs", json=event)


def test_recent_decision_log_links_replay_and_serves_the_events_own_graph(tmp_path):
    app = create_app(test_settings(tmp_path))
    event = valid_event(id="logged-event")
    event["graph"]["name"] = "日志里的旧图"
    with TestClient(app) as client:
        assert _receive_event(client, event).status_code == 200
        listed = client.get("/admin/requests/recent").json()["requests"][0]
        # 列表只给摘要与回放入口：不再有 snapshot_url / graph_snapshot_id。
        assert listed["id"] == "logged-event"
        assert listed["replay_url"] == "/console/routing.html?request=logged-event"
        assert "snapshot_url" not in listed
        assert "graph_snapshot_id" not in listed
        assert "graph" not in listed
        # presenter 字段仍由服务端生成，页面不需要旧日志适配器。
        assert listed["route_label"] == {"zh": "目标命中", "en": "Target selected"}
        assert [pair[0]["en"] for pair in listed["metrics"]] == [
            "Decision time", "Decision model time", "Waves"]
        assert [pair[0]["en"] for pair in listed["fields"]][:2] == [
            "Request ID", "Decision group"]

        replay = client.get("/admin/requests/logged-event/replay").json()
        assert replay["request_id"] == "logged-event"
        # tree/trace/request 全部来自同一条事件，不读当前图或快照表。
        assert replay["tree"]["name"] == "日志里的旧图"
        assert replay["trace"] == event["trace"]
        assert replay["request"]["text"] == event["request"]["text"]
        assert replay["decision"] == event["decision"]
        assert "graph_snapshot_id" not in replay
        assert "snapshot_revision" not in replay["request"]


def test_replay_url_encodes_decision_id(tmp_path):
    app = create_app(test_settings(tmp_path))
    event = valid_event(id="id with/space?")
    with TestClient(app) as client:
        assert _receive_event(client, event).status_code == 200
        listed = client.get("/admin/requests/recent").json()["requests"][0]
        assert listed["replay_url"] == "/console/routing.html?request=id%20with%2Fspace%3F"
        assert client.get("/admin/requests/id%20with%2Fspace%3F/replay").status_code == 200


def test_live_decision_events_keep_request_secrets_out_of_the_memory_log(tmp_path):
    """Upstream Authorization / caller key / raw session never enter the decision event."""
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        JevServiceRepo(conn).save(
            JevService("default", "http://jev.test", "NeoHorse-Jev-4B",
                       timeout_seconds=5.0))
    finally:
        conn.close()
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(
        root=_question_root(), enabled=True, default=True)))

    upstream_secret = "upstream-secret-value"
    session_secret = "raw-session-secret-value"
    caller_secret = "caller-secret-value"
    body_text = "正文机密-value"
    seen = {}

    def handler(request):
        if request.url.host == "jev.test":
            return httpx.Response(200, json={
                "answers": {"q": {"type": "choice", "probabilities": {
                    "轻量模型": 0.9, "增强模型": 0.1,
                }}},
                "usage": {"input_tokens": 10},
            })
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "model": "qwen3-8b",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

    # 拆分后：内核执行决策与 JEV（mock transport），外围执行模型上游并接收事件。
    # 内核决策事件先由 recording sink 捕获，再按真实 sink 的 POST 语义投递给外围。
    kernel, sink = recording_core_app(
        graph_dir=settings.graph_dir, transport=httpx.MockTransport(handler))
    app = create_app(
        settings, transport=httpx.MockTransport(handler),
        core_transport=core_app_for(graph_dir=settings.graph_dir, app=kernel))
    with TestClient(app) as client:
        provider_payload = {
            "base_url": "http://upstream.test/v1", "key_env": "",
            "api_key": upstream_secret,
        }
        preview = client.put("/admin/providers/local", json=provider_payload)
        assert preview.status_code == 409, preview.text
        confirmation = {"trees": [
            {"id": item["id"], "document_sha256": item["document_sha256"]}
            for item in preview.json()["detail"]["affected_trees"]
        ]}
        assert client.put("/admin/providers/local", json={
            **provider_payload, "impact_confirmation": confirmation,
        }).status_code == 200
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": body_text}]},
            headers={"x-sr-session-id": session_secret,
                     "authorization": f"Bearer {caller_secret}"},
        )
        assert response.status_code == 200, response.text
        # Provider Key 只在围外执行层解析：上游请求带目录已存密钥。
        assert seen.get("authorization") == f"Bearer {upstream_secret}"
        # 内核发出的完整事件不含任何用户/上游/JEV 密钥。
        assert len(sink.events) == 1
        kernel_event_text = json.dumps(sink.events[0], ensure_ascii=False)
        deliver_decision_events(app, sink)
        event = app.state.decision_logs.recent(1)[0]
        request_id = event["id"]
        replay = client.get(f"/admin/requests/{request_id}/replay")
        assert replay.status_code == 200, replay.text
        body = replay.json()
        # 决策事件完整保留请求正文与 trace；四类密钥都不在其中。
        assert body["request"]["text"] == body_text
        assert body["tree"]["root"]["type"] == "graph"
        assert any(event["event"] == "final" for event in body["trace"])
        assert isinstance(body["request"].get("session_hash"), str)
        for secret in (upstream_secret, session_secret, caller_secret):
            assert secret not in replay.text, f"memory decision log leaked {secret}"
            assert secret not in kernel_event_text, f"kernel event leaked {secret}"

    # 重启：内存日志清空，历史回放 404，不尝试从 SQLite 恢复。
    restarted = create_app(
        settings, transport=httpx.MockTransport(handler),
        core_transport=core_app_for(graph_dir=settings.graph_dir, app=kernel))
    with TestClient(restarted) as client:
        assert client.get(f"/admin/requests/{request_id}/replay").status_code == 404
        assert client.get("/admin/requests/recent").json() == {"requests": []}
