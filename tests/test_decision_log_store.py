"""外围 20 条完整决策事件内存日志：容量、易失与回放来源。

Global Constraint 4/5/6：内核只输出事件；外围把通过校验的完整事件放进
``deque(maxlen=20)``，不写 SQLite、重启不恢复、第 21 条淘汰最旧事件；回放只读
同一条事件里的 ``graph``/``trace``/``request``，不依赖当前图或任何快照表。
"""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.decision_events import build_decision_event, replay_payload, summary_payload
from router.decision_log_store import DecisionLogStore
from router.routing.tree import tree_from_dict
from router.store.db import connect
from router.store.routing_trees import RoutingTreeRepo
from tests.decision_fixtures import decision_record, decision_snapshot, valid_event
from tests.routing_fixtures import test_settings, tree_dict


def _event(event_id, *, ts="2026-11-25T10:00:00+08:00", model="fast",
           sticky_hit=0, top=0.9):
    """One valid decision event with the list/overview facts varied."""
    event = valid_event(id=event_id)
    event["ts"] = ts
    event["decision"]["model"] = model
    event["decision"]["sticky_hit"] = sticky_hit
    event["jev"]["top"] = top
    return event


def _receive(client, event):
    return client.post("/internal/v1/decision-logs", json=event)


def test_store_keeps_only_twenty_complete_events():
    store = DecisionLogStore()
    for index in range(21):
        store.add(valid_event(id=f"e{index}"))
    assert [item["id"] for item in store.recent()] == [f"e{i}" for i in range(20, 0, -1)]
    assert store.get("e0") is None
    assert store.get("e1")["graph"]["id"] == "main"
    assert DecisionLogStore().recent() == []


def test_replay_uses_graph_inside_same_log_event(tmp_path):
    store = DecisionLogStore()
    event = valid_event(id="replay-me")
    event["graph"]["name"] = "日志里的旧图"
    store.add(event)
    replay = replay_payload(store.get("replay-me"))
    assert replay["tree"]["name"] == "日志里的旧图"
    assert replay["trace"] == event["trace"]
    assert replay["request"]["text"] == event["request"]["text"]


def test_store_rejects_invalid_events_and_keeps_existing_ones():
    store = DecisionLogStore()
    store.add(valid_event(id="kept"))
    broken = valid_event(id="broken")
    del broken["graph"]
    with pytest.raises(ValueError, match="graph"):
        store.add(broken)
    secret = valid_event(id="secret")
    secret["graph"]["jev"]["api_key"] = "must-not-enter-the-log"
    with pytest.raises(ValueError, match="secret"):
        store.add(secret)
    assert [item["id"] for item in store.recent()] == ["kept"]


def test_store_rejects_structurally_incomplete_events():
    """Design §7.2: required fields are validated before the deque insert."""
    store = DecisionLogStore()
    # 评审探针：只有 schema/type/graph={} 的事件此前会被当成“完整事件”收下。
    probe = {"schema_version": 1, "event_type": "routepilot.decision", "graph": {}}
    with pytest.raises(ValueError):
        store.add(probe)
    no_request = valid_event(id="incomplete")
    del no_request["request"]
    with pytest.raises(ValueError, match="request"):
        store.add(no_request)
    assert store.recent() == []


def test_receiver_rejects_incomplete_events_and_keeps_the_log_readable(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert _receive(client, _event("good-before-probe")).status_code == 200
    # 坏事件：422 且不进入 deque，已保留事件与读取端点都不受影响。
    probe = {"schema_version": 1, "event_type": "routepilot.decision", "graph": {}}
    assert _receive(client, probe).status_code == 422
    listed = client.get("/admin/requests/recent")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == ["good-before-probe"]
    assert client.get("/admin/requests/good-before-probe/replay").status_code == 200
    assert [event["id"] for event in app.state.decision_logs.recent()] == [
        "good-before-probe"]


def test_stored_event_is_not_mutated_by_a_later_outcome_refresh():
    """The memory event is a decision-time snapshot, not a live record alias.

    The transitional peripheral bridge stores the event before the execution
    result is appended to the same request record; the retained replay must keep
    the decision-time trace (the kernel emits no execution result at all).
    """
    record = decision_record("immutable")
    store = DecisionLogStore()
    store.add(build_decision_event(
        record, decision_snapshot(), department="", requested_model="auto"))
    before = list(store.get("immutable")["trace"])
    record["trace"].append({"event": "request_outcome", "phase": "outcome"})
    assert store.get("immutable")["trace"] == before


def test_a_broken_memory_log_cannot_break_a_decision_response():
    """Logging is best-effort: the memory log never affects the decision response."""
    from types import SimpleNamespace

    from router.api.routes_v1 import log_decision_event

    class BrokenStore:
        def add(self, event):
            raise RuntimeError("memory log is down")

    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(decision_logs=BrokenStore())))
    # 不抛异常即为通过：外层 run_chat_completion 的响应不会被日志故障变成 500。
    log_decision_event(
        request, decision_record("best-effort"), decision_snapshot(),
        department="", requested_model="auto")
    # 选图前失败（无图）也不生成/不写任何可回放事件。
    log_decision_event(
        request, decision_record("no-graph"), None,
        department="", requested_model="auto")


def test_recent_is_newest_first_and_the_limit_is_bounded():
    store = DecisionLogStore()
    for index in range(5):
        store.add(valid_event(id=f"e{index}"))
    assert [item["id"] for item in store.recent(2)] == ["e4", "e3"]
    assert store.recent(0) == []
    assert store.recent(-1) == []
    assert len(store.recent(999)) == 5


def test_summary_projection_never_ships_the_embedded_graph():
    event = _event("summary-1")
    summary = summary_payload(event)
    assert summary["replay_url"] == "/console/routing.html?request=summary-1"
    assert summary["text"] == event["request"]["text"]
    assert summary["outcome_state"] == "completed"
    assert summary["model"] == "fast"
    assert summary["trace"] == event["trace"]
    assert "graph" not in summary and "root" not in summary and "canvas" not in summary


def test_replay_projection_is_the_complete_single_event():
    event = _event("replay-1")
    replay = replay_payload(event)
    assert set(replay) == {
        "request_id", "tree", "trace", "request", "decision", "answers", "node_labels"}
    assert replay["request_id"] == "replay-1"
    assert replay["tree"] == event["graph"]
    assert replay["request"] == event["request"]
    assert replay["decision"] == event["decision"]


def test_receiver_stores_the_event_without_admin_login(tmp_path):
    settings = test_settings(tmp_path, user_auth=True)
    client = TestClient(create_app(settings))
    # 管理面需要登录，但内核的日志接收口在 /admin 之外，必须能直接写入。
    assert client.get("/admin/requests/recent").status_code == 401
    event = _event("from-kernel")
    response = _receive(client, event)
    assert response.status_code == 200, response.text
    assert client.post("/admin/login", json={
        "email": "admin@test.com", "password": "1qaz@WSX"}).status_code == 200
    token = client.post("/admin/login", json={
        "email": "admin@test.com", "password": "1qaz@WSX"}).json()["token"]
    listed = client.get(
        "/admin/requests/recent", headers={"Authorization": f"Bearer {token}"}).json()
    assert [item["id"] for item in listed["requests"]] == ["from-kernel"]


def test_receiver_rejects_malformed_events_without_storing(tmp_path, monkeypatch):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.post(
        "/internal/v1/decision-logs", content=b"{not json",
        headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/internal/v1/decision-logs", json=[]).status_code == 422
    assert client.post(
        "/internal/v1/decision-logs", json={"event_type": "routepilot.other"}
    ).status_code == 422
    secret = _event("secret-event")
    secret["graph"]["jev"]["api_key"] = "must-not-enter-the-log"
    assert _receive(client, secret).status_code == 422
    assert app.state.decision_logs.recent() == []
    # 请求大小上限：内核事件内嵌整图，但不能成为无界接收器。
    monkeypatch.setattr("router.api.routes_logs.MAX_DECISION_EVENT_BYTES", 32)
    assert _receive(client, _event("too-large")).status_code == 413
    assert app.state.decision_logs.recent() == []


def test_recent_returns_summaries_with_presenter_fields(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    for index in range(2):
        assert _receive(client, _event(f"recent-{index}")).status_code == 200
    response = client.get("/admin/requests/recent")
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["requests"]] == ["recent-1", "recent-0"]
    first = body["requests"][0]
    assert first["replay_url"] == "/console/routing.html?request=recent-1"
    assert "graph" not in first and "root" not in first
    assert '"canvas"' not in response.text
    # 列表仍带页面需要的展示字段（presenter 生成，不在事件里存两份）。
    assert first["route_label"] == {"zh": "目标命中", "en": "Target selected"}
    assert [pair[0]["en"] for pair in first["metrics"]] == [
        "Decision time", "Decision model time", "Waves"]
    assert [pair[0]["en"] for pair in first["fields"]][:2] == [
        "Request ID", "Decision group"]


def test_replay_serves_the_events_own_graph_after_the_current_graph_moved(tmp_path):
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    app = create_app(settings)
    client = TestClient(app)

    logged = _event("old-graph")
    logged["graph"]["name"] = "日志里的旧图"
    assert _receive(client, logged).status_code == 200

    current = repo.get("main")
    repo.update(
        replace(current, name="后来的当前图"),
        base_document_sha256=current.document_sha256,
    )
    assert client.get("/admin/trees/main").json()["tree"]["name"] == "后来的当前图"

    replay = client.get("/admin/requests/old-graph/replay")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    # 回放只读事件里的旧图，绝不读当前图或任何快照表。
    assert body["tree"]["name"] == "日志里的旧图"
    assert body["tree"] == logged["graph"]
    assert body["trace"] == logged["trace"]
    assert body["request"]["text"] == logged["request"]["text"]


def test_evicted_and_unknown_replays_are_404(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    for index in range(21):
        assert _receive(client, _event(f"e{index}")).status_code == 200
    assert client.get("/admin/requests/e0/replay").status_code == 404
    assert client.get("/admin/requests/e1/replay").status_code == 200
    missing = client.get("/admin/requests/never-existed/replay")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "unknown request"}


def test_restart_clears_the_memory_decision_logs(tmp_path):
    settings = test_settings(tmp_path)
    first = TestClient(create_app(settings))
    assert _receive(first, _event("volatile")).status_code == 200
    assert first.get("/admin/requests/volatile/replay").status_code == 200
    # 同一目录、同一数据库重启：日志只存在内存，重启即清空，绝不从 SQLite 恢复。
    restarted = TestClient(create_app(settings))
    assert restarted.get("/admin/requests/recent").json() == {"requests": []}
    assert restarted.get("/admin/requests/volatile/replay").status_code == 404


def test_memory_logs_never_write_the_legacy_sqlite_log_tables(tmp_path):
    settings = test_settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    for index in range(20):
        assert _receive(client, _event(f"mem-{index}")).status_code == 200
    conn = connect(settings.db_path)
    try:
        for table in ("request_logs", "graph_snapshots", "request_snapshots"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"decision log event wrote the legacy table {table}"
    finally:
        conn.close()


def test_overview_only_counts_the_retained_events(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    for event in (
        _event("d1", ts="2026-11-24T10:00:00+08:00", model="fast", sticky_hit=1),
        _event("d2", ts="2026-11-25T10:00:00+08:00", model="fast"),
        _event("d3", ts="2026-11-25T11:00:00+08:00", model="strong", top=0.2),
    ):
        assert _receive(client, event).status_code == 200
    overview = client.get("/admin/overview").json()
    assert overview["total"] == 3
    assert overview["sticky"] == 1
    assert overview["daily"] == [
        {"d": "2026-11-24", "count": 1}, {"d": "2026-11-25", "count": 2}]
    assert overview["routes"] == [
        {"model": "fast", "count": 2}, {"model": "strong", "count": 1}]
    assert "confidence_since" in overview
    assert sum(bucket["count"] for bucket in overview["confidence"]) == 3
    # 第 21 条淘汰最旧事件后，统计只覆盖当前保留的 20 条。
    for index in range(19):
        assert _receive(client, _event(f"roll-{index}")).status_code == 200
    retained = client.get("/admin/overview").json()
    assert retained["total"] == 20
    assert retained["daily"] == [{"d": "2026-11-25", "count": 20}]

    stats = client.get("/admin/requests/stats").json()
    assert stats["ring_size"] == 20
    assert stats["ring_limit"] == 20


def test_changes_still_reads_the_peripheral_repository(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/admin/changes").json() == {"changes": []}
