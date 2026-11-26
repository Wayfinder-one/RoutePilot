"""外围 20 条完整决策事件内存日志：容量、易失与回放来源。

Global Constraint 4/5/6：内核只输出事件；外围把通过校验的完整事件放进
``deque(maxlen=20)``，不写 SQLite、重启不恢复、第 21 条淘汰最旧事件；回放只读
同一条事件里的 ``graph``/``trace``/``request``，不依赖当前图或任何快照表。
"""

import sys
import threading
import time
from collections import deque

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.internal_app import create_internal_app, create_service_apps
from router.decision_events import build_decision_event, replay_payload, summary_payload
from router.decision_log_store import STORE_LIMIT, DecisionLogStore
from router.routing.tree import tree_from_dict
from router.store.db import connect
from router.store.routing_trees import RoutingTreeRepo
from tests.decision_fixtures import decision_record, decision_snapshot, valid_event
from tests.routing_fixtures import split_app_for, test_settings, tree_dict


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


def _service_apps(tmp_path, **overrides):
    """Public console app + internal receiver app sharing one decision-log store."""
    settings = test_settings(tmp_path, **overrides)
    return create_service_apps(settings)


def _clients(tmp_path, **overrides):
    public_app, internal_app = _service_apps(tmp_path, **overrides)
    return public_app, internal_app, TestClient(public_app), TestClient(internal_app)


def _internal_client_for(public_app):
    """Internal receiver app over the public app's store (for split_app fixtures)."""
    return TestClient(create_internal_app(public_app.state.decision_logs))


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
    public_app, _, public_client, internal_client = _clients(tmp_path)
    assert _receive(internal_client, _event("good-before-probe")).status_code == 200
    # 坏事件：422 且不进入 deque，已保留事件与读取端点都不受影响。
    probe = {"schema_version": 1, "event_type": "routepilot.decision", "graph": {}}
    assert _receive(internal_client, probe).status_code == 422
    listed = public_client.get("/admin/requests/recent")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == ["good-before-probe"]
    assert public_client.get(
        "/admin/requests/good-before-probe/replay").status_code == 200
    assert [event["id"] for event in public_app.state.decision_logs.recent()] == [
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
    _, _, public_client, internal_client = _clients(tmp_path, user_auth=True)
    # 管理面需要登录；接收口在独立内部监听上，内核投递时不带管理面会话。
    assert public_client.get("/admin/requests/recent").status_code == 401
    event = _event("from-kernel")
    response = _receive(internal_client, event)
    assert response.status_code == 200, response.text
    assert public_client.post("/admin/login", json={
        "email": "admin@test.com", "password": "1qaz@WSX"}).status_code == 200
    token = public_client.post("/admin/login", json={
        "email": "admin@test.com", "password": "1qaz@WSX"}).json()["token"]
    listed = public_client.get(
        "/admin/requests/recent", headers={"Authorization": f"Bearer {token}"}).json()
    assert [item["id"] for item in listed["requests"]] == ["from-kernel"]


def test_receiver_rejects_malformed_events_without_storing(tmp_path, monkeypatch):
    public_app, _, _, internal_client = _clients(tmp_path)
    assert internal_client.post(
        "/internal/v1/decision-logs", content=b"{not json",
        headers={"content-type": "application/json"}).status_code == 400
    assert internal_client.post(
        "/internal/v1/decision-logs", json=[]).status_code == 422
    assert internal_client.post(
        "/internal/v1/decision-logs", json={"event_type": "routepilot.other"}
    ).status_code == 422
    secret = _event("secret-event")
    secret["graph"]["jev"]["api_key"] = "must-not-enter-the-log"
    assert _receive(internal_client, secret).status_code == 422
    assert public_app.state.decision_logs.recent() == []
    # 请求大小上限：内核事件内嵌整图，但不能成为无界接收器。
    monkeypatch.setattr("router.api.routes_logs.MAX_DECISION_EVENT_BYTES", 32)
    assert _receive(internal_client, _event("too-large")).status_code == 413
    assert public_app.state.decision_logs.recent() == []


def test_recent_returns_summaries_with_presenter_fields(tmp_path):
    _, _, public_client, internal_client = _clients(tmp_path)
    for index in range(2):
        assert _receive(internal_client, _event(f"recent-{index}")).status_code == 200
    response = public_client.get("/admin/requests/recent")
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
    app = split_app_for(settings=settings)
    client = TestClient(app)
    internal_client = _internal_client_for(app)

    logged = _event("old-graph")
    logged["graph"]["name"] = "日志里的旧图"
    # 内核事件只投递到独立内部监听，绝不经过公开控制台监听。
    assert _receive(internal_client, logged).status_code == 200

    # 当前图后来被修改（经外围代理写入内核）：历史回放仍只读日志事件里的旧图。
    current = client.get("/admin/trees/main").json()["tree"]
    moved = client.put("/admin/trees/main", json={
        "name": "后来的当前图",
        "base_document_sha256": current["document_sha256"],
    })
    assert moved.status_code == 200, moved.text

    replay = client.get("/admin/requests/old-graph/replay")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    # 回放只读事件里的旧图，绝不读当前图或任何快照表。
    assert body["tree"]["name"] == "日志里的旧图"
    assert body["tree"] == logged["graph"]
    assert body["trace"] == logged["trace"]
    assert body["request"]["text"] == logged["request"]["text"]


def test_evicted_and_unknown_replays_are_404(tmp_path):
    _, _, public_client, internal_client = _clients(tmp_path)
    for index in range(21):
        assert _receive(internal_client, _event(f"e{index}")).status_code == 200
    assert public_client.get("/admin/requests/e0/replay").status_code == 404
    assert public_client.get("/admin/requests/e1/replay").status_code == 200
    missing = public_client.get("/admin/requests/never-existed/replay")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "unknown request"}


def test_restart_clears_the_memory_decision_logs(tmp_path):
    settings = test_settings(tmp_path)
    public_app, internal_app = create_service_apps(settings)
    first_public = TestClient(public_app)
    first_internal = TestClient(internal_app)
    assert _receive(first_internal, _event("volatile")).status_code == 200
    assert first_public.get("/admin/requests/volatile/replay").status_code == 200
    # 同一目录、同一数据库重启：日志只存在内存，重启即清空，绝不从 SQLite 恢复。
    restarted = TestClient(create_app(settings))
    assert restarted.get("/admin/requests/recent").json() == {"requests": []}
    assert restarted.get("/admin/requests/volatile/replay").status_code == 404


def test_decision_log_store_has_no_sqlite_log_tables(tmp_path):
    """v7 迁移删除了三张旧日志表：内存事件没有任何 SQLite 落点。"""
    settings = test_settings(tmp_path)
    _, internal_app = create_service_apps(settings)
    internal_client = TestClient(internal_app)
    for index in range(20):
        assert _receive(internal_client, _event(f"mem-{index}")).status_code == 200
    conn = connect(settings.db_path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"request_logs", "graph_snapshots", "request_snapshots"} & tables
    finally:
        conn.close()


def test_overview_only_counts_the_retained_events(tmp_path):
    _, _, public_client, internal_client = _clients(tmp_path)
    for event in (
        _event("d1", ts="2026-11-24T10:00:00+08:00", model="fast", sticky_hit=1),
        _event("d2", ts="2026-11-25T10:00:00+08:00", model="fast"),
        _event("d3", ts="2026-11-25T11:00:00+08:00", model="strong", top=0.2),
    ):
        assert _receive(internal_client, event).status_code == 200
    overview = public_client.get("/admin/overview").json()
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
        assert _receive(internal_client, _event(f"roll-{index}")).status_code == 200
    retained = public_client.get("/admin/overview").json()
    assert retained["total"] == 20
    assert retained["daily"] == [{"d": "2026-11-25", "count": 20}]

    stats = public_client.get("/admin/requests/stats").json()
    assert stats["ring_size"] == 20
    assert stats["ring_limit"] == 20


def test_changes_still_reads_the_peripheral_repository(tmp_path):
    _, _, public_client, _ = _clients(tmp_path)
    assert public_client.get("/admin/changes").json() == {"changes": []}


def test_receiver_is_isolated_on_the_internal_listener(tmp_path):
    """I5: the kernel feed lives on its own app/listener, never on the console."""
    public_app, internal_app, public_client, internal_client = _clients(tmp_path)
    # 公开控制台监听上根本没有接收口：路径级隔离，而不是靠 401 挡。
    assert public_client.post(
        "/internal/v1/decision-logs", json=_event("host-injection")).status_code == 404
    assert public_client.get("/internal/v1/decision-logs").status_code == 404
    # 内部监听只有接收口和 /healthz：没有控制台、没有 /admin、没有 /v1。
    assert internal_client.get("/healthz").json() == {"ok": True}
    assert internal_client.get("/console/index.html").status_code == 404
    assert internal_client.get("/admin/requests/recent").status_code == 404
    assert internal_client.get("/v1/models").status_code == 404
    # 两个应用共享同一个内存 store：公开面仍能读内部接收的事件。
    assert public_app.state.decision_logs is internal_app.state.decision_logs
    assert _receive(internal_client, _event("from-kernel")).status_code == 200
    listed = public_client.get("/admin/requests/recent").json()["requests"]
    assert [item["id"] for item in listed] == ["from-kernel"]
    assert public_client.get("/admin/requests/from-kernel/replay").status_code == 200


# ---------------------------------------------------------------------------
# I4：add() 在事件循环接收端执行，recent()/get()/stats() 在 FastAPI 线程池执行；
# 第 21 条淘汰最旧事件时，读迭代可能撞上写淘汰而抛
# ``RuntimeError: deque mutated during iteration``。以下测试覆盖真实并发读写。
# ---------------------------------------------------------------------------


class _MutationWindowDeque(deque):
    """在迭代窗口中间打开一次真实可被并发 append 命中的空档。

    旧实现直接迭代 ``store._events``（无锁）：另一个线程在这个空档里 append
    并淘汰最旧事件，CPython 下一次 ``next()`` 就会抛
    ``RuntimeError: deque mutated during iteration``。新实现把整个读取投影放在
    store 锁内，写线程在窗口期间被挡住，因此迭代器不会失效。

    这是对私有 ``_events`` 的测试探针（生产代码无此子类）：它把旧竞态放大成
    确定性复现，因为 20 条小 deque 的纯 C 迭代窗口短到难以稳定命中。
    """

    def __init__(self, *, maxlen=None):
        super().__init__(maxlen=maxlen)
        self.mutation_window = threading.Event()

    def _window(self, iterator):
        first = next(iterator)  # 先取一条，再让写线程执行 append + 淘汰
        self.mutation_window.set()
        time.sleep(0.2)
        yield first
        yield from iterator  # 旧实现：这里抛 deque mutated during iteration

    def __iter__(self):
        return self._window(super().__iter__())

    def __reversed__(self):
        return self._window(super().__reversed__())


def _install_mutation_window(store):
    """把 store 的 deque 换成带窗口探针的同语义 deque（仍是 maxlen=20）。"""
    window = _MutationWindowDeque(maxlen=STORE_LIMIT)
    # 构造阶段用 C 层 extend，避免触发探针自身（探针只在读取投影时开门）。
    deque.extend(window, store._events)
    store._events = window
    return window


def _append_until(window, store, first, stop):
    assert window.mutation_window.wait(timeout=5.0), "reader never entered its window"
    for index in range(first, stop):
        store.add(valid_event(id=f"w{index:03d}"))


def _run_pair(reader, writer):
    reader_thread = threading.Thread(target=reader, name="store-reader")
    writer_thread = threading.Thread(target=writer, name="store-writer")
    reader_thread.start()
    writer_thread.start()
    reader_thread.join(10.0)
    writer_thread.join(10.0)
    assert not reader_thread.is_alive(), "reader deadlocked"
    assert not writer_thread.is_alive(), "writer deadlocked"


def test_recent_projection_is_synchronised_with_concurrent_appends():
    """旧实现：写线程在迭代窗口内 append/淘汰 → list(reversed()) 抛 RuntimeError。"""
    store = DecisionLogStore()
    for index in range(10):
        store.add(valid_event(id=f"e{index:03d}"))
    window = _install_mutation_window(store)
    errors, snapshots = [], []

    def reader():
        try:
            snapshots.append(store.recent())
        except BaseException as exc:  # noqa: BLE001 - 旧实现抛 RuntimeError
            errors.append(exc)

    _run_pair(
        reader,
        lambda: _append_until(window, store, first=10, stop=40),
    )
    assert errors == [], f"recent() raced with add(): {errors!r}"
    # 锁保护下的读取得到稳定快照：只含读取开始前保留的事件，且仍是 20 条上限内。
    assert [event["id"] for event in snapshots[0]] == [
        f"e{index:03d}" for index in range(9, -1, -1)]
    # 读锁释放后写入才生效：之后的读取看到最新 20 条（新→旧）。
    assert [event["id"] for event in store.recent()] == [
        f"w{index:03d}" for index in range(39, 19, -1)]


def test_get_projection_is_synchronised_with_concurrent_appends():
    """旧实现的 get() 用生成器 next(...) 逐条迭代，最容易被写淘汰打中。"""
    store = DecisionLogStore()
    for index in range(10):
        store.add(valid_event(id=f"e{index:03d}"))
    window = _install_mutation_window(store)
    errors, results = [], []

    def reader():
        try:
            # 未知 ID 迫使 get() 走完整个 deque，正好跨过窗口里的淘汰。
            results.append(store.get("missing-id"))
        except BaseException as exc:  # noqa: BLE001 - 旧实现抛 RuntimeError
            errors.append(exc)

    _run_pair(
        reader,
        lambda: _append_until(window, store, first=10, stop=40),
    )
    assert errors == [], f"get() raced with add(): {errors!r}"
    assert results == [None]


def test_concurrent_readers_and_writer_never_raise_or_tear_snapshots():
    """真实多线程压力：4 个读线程持续 recent()/get()/stats()，1 个写线程追加淘汰。

    旧实现没有锁，读者会撞上 ``deque mutated during iteration``；修好后每个
    recent() 都是稳定快照：≤20 条、严格新→旧、且是连续的一段保留窗口。
    """
    store = DecisionLogStore()
    for index in range(STORE_LIMIT):
        store.add(valid_event(id=f"{index:06d}"))

    errors: list[BaseException] = []
    violations: list[str] = []
    stop = threading.Event()
    reader_count = 4
    writer_total = 400
    barrier = threading.Barrier(reader_count + 1)
    previous_switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 放大线程切换，让旧竞态在压力下稳定暴露
    try:
        def reader():
            try:
                barrier.wait(timeout=10.0)
            except threading.BrokenBarrierError:
                return
            while not stop.is_set():
                try:
                    events = store.recent()
                    stats = store.stats()
                    store.get(f"{0:06d}")
                    store.get("missing-id")
                except BaseException as exc:  # noqa: BLE001 - 旧实现抛 RuntimeError
                    errors.append(exc)
                    return
                ids = [event["id"] for event in events]
                if len(ids) > STORE_LIMIT:
                    violations.append(f"recent() returned {len(ids)} events")
                expected = [] if not ids else [
                    f"{number:06d}"
                    for number in range(int(ids[0]), int(ids[0]) - len(ids), -1)]
                if ids != expected:
                    violations.append(f"torn snapshot: {ids[:3]}...{ids[-3:]}")
                if stats["ring_size"] != STORE_LIMIT or stats["ring_limit"] != STORE_LIMIT:
                    violations.append(f"inconsistent stats: {stats}")

        def writer():
            try:
                barrier.wait(timeout=10.0)
            except threading.BrokenBarrierError:
                return
            try:
                for index in range(STORE_LIMIT, STORE_LIMIT + writer_total):
                    store.add(valid_event(id=f"{index:06d}"))
            finally:
                stop.set()

        threads = [threading.Thread(target=reader, name=f"store-reader-{index}")
                   for index in range(reader_count)]
        writer_thread = threading.Thread(target=writer, name="store-writer")
        for thread in threads:
            thread.start()
        writer_thread.start()
        for thread in threads:
            thread.join(30.0)
        writer_thread.join(30.0)
        assert all(not thread.is_alive() for thread in threads), "readers deadlocked"
        assert not writer_thread.is_alive(), "writer deadlocked"
    finally:
        stop.set()
        sys.setswitchinterval(previous_switch)

    assert errors == [], f"concurrent read/write raised: {errors!r}"
    assert violations == [], f"inconsistent snapshots: {violations[:5]!r}"
    assert store.stats()["ring_size"] == STORE_LIMIT
