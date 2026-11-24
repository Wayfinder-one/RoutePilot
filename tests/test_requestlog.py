import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from router.api.request_presenter import present_record
from router.request_logging import LOG_COLUMNS, RECENT_LIMIT, RequestLogger
from router.config import Settings
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from tests.routing_fixtures import GRAPH_CANVAS, tree_dict


def make_log(tmp_path):
    settings = Settings(db_path=str(tmp_path / "v3.db"))
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    return RequestLogger(settings.db_path)


def record(i, **overrides):
    value = {
        "id": f"r{i}", "ts": "2026-10-29T10:00:00+08:00", "text": "秘密请求文本",
        "decision": "route", "model": "strong-a", "target_id": "strong-target",
        "tree_id": "main", "tree_revision": "t" * 64, "snapshot_revision": "s" * 64,
        "policy_path": "root/choose", "selection_reason": "jev", "sticky_hit": 0,
        "session_hash": "h" * 64, "jev_model": "NeoHorse-Jev-4B", "jev_choice": "strong",
        "jev_top": 0.8, "jev_margin": 0.6, "jev_ms": 12.0, "routing_ms": 15.0,
        "trace": [{"event": "jev_choice", "probabilities": {"fast": 0.2, "strong": 0.8}}],
        "outcome_state": "incomplete",
    }
    value.update(overrides)
    return value


def rows(log):
    conn = connect(log.db_path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM request_logs ORDER BY rowid")]
    finally:
        conn.close()


def test_half_row_is_replaced_by_outcome_without_persisting_detail(tmp_path):
    log = make_log(tmp_path)
    item = record(1)
    log.enqueue(item)
    asyncio.run(log.flush())
    assert len(rows(log)) == 1
    assert rows(log)[0]["outcome_state"] == "incomplete"
    item.update({"outcome_state": "completed", "upstream_status": 200, "total_ms": 30.0, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    log.enqueue_result(item)
    asyncio.run(log.flush())
    stored = rows(log)
    assert len(stored) == 1
    assert stored[0]["outcome_state"] == "completed"
    assert stored[0]["total_tokens"] == 15
    assert stored[0]["jev_top"] == 0.8
    assert "text" not in stored[0]
    assert "trace" not in stored[0]
    assert log.recent_requests(1)[0]["text"] == "秘密请求文本"
    assert log.partial_rows == 1


def test_ring_is_bounded_and_result_does_not_duplicate_confidence(tmp_path):
    log = make_log(tmp_path)
    for i in range(RECENT_LIMIT + 2):
        log.enqueue(record(i))
    assert len(log.recent) == RECENT_LIMIT
    assert log.recent_requests(1)[0]["id"] == f"r{RECENT_LIMIT + 1}"
    assert sum(bucket["count"] for bucket in log.confidence_histogram()) == RECENT_LIMIT + 2
    before = log.confidence_histogram()
    log.enqueue_result(record(99))
    assert log.confidence_histogram() == before


def test_hydrate_recent_restores_persisted_tail_without_live_detail(tmp_path):
    log = make_log(tmp_path)
    for i in range(RECENT_LIMIT + 2):
        log.enqueue(record(i, total_ms=100.0 + i))
    asyncio.run(log.flush())

    restored = RequestLogger(log.db_path)
    conn = connect(log.db_path)
    try:
        restored.hydrate_recent(conn)
        restored.hydrate_recent(conn)
    finally:
        conn.close()

    recent = restored.recent_requests(RECENT_LIMIT)
    assert [item["id"] for item in recent] == [
        f"r{i}" for i in range(RECENT_LIMIT + 1, 1, -1)
    ]
    assert recent[0]["latency_ms"] == 100.0 + RECENT_LIMIT + 1
    assert recent[0]["model"] == "strong-a"
    assert "text" not in recent[0]
    assert "trace" not in recent[0]
    assert "jev_probabilities" not in recent[0]
    # Restored rows use the same presenter as live rows (no client-side adapter).
    presented = present_record(recent[0])
    assert presented["route_label"] == {"zh": "目标命中", "en": "Target selected"}
    assert [pair[0]["en"] for pair in presented["metrics"]] == [
        "Decision time", "Decision model time", "Waves"]
    assert presented["fields"][0][0] == {"zh": "请求 ID", "en": "Request ID"}


def test_hydrate_presents_otherwise_selection_reason(tmp_path):
    log = make_log(tmp_path)
    log.enqueue(record(1, selection_reason="otherwise", outcome_state="completed",
                       total_ms=5.0))
    asyncio.run(log.flush())

    restored = RequestLogger(log.db_path)
    conn = connect(log.db_path)
    try:
        restored.hydrate_recent(conn)
    finally:
        conn.close()

    assert present_record(restored.recent_requests(1)[0])["route_label"] == {
        "zh": "默认目标兜底", "en": "Default target fallback"}


def test_queue_full_is_counted_and_ring_survives(tmp_path, monkeypatch):
    monkeypatch.setattr("router.request_logging.FLUSH_QUEUE_LIMIT", 1)
    log = make_log(tmp_path)
    log.enqueue(record(1))
    log.enqueue(record(2))
    assert log.dropped_rows == 1
    assert len(log.recent) == 2
    asyncio.run(log.flush())
    assert len(rows(log)) == 1


def test_stats_exposes_queue_and_drop_health(tmp_path):
    log = make_log(tmp_path)
    log.enqueue(record(1))
    stats = log.stats()
    assert stats["ring_size"] == 1
    assert stats["queue_size"] == 1
    assert stats["dropped_rows"] == 0
    assert stats["ring_limit"] == RECENT_LIMIT


def test_waves_persists_but_answers_text_and_trace_stay_in_memory(tmp_path):
    log = make_log(tmp_path)
    answers = {
        "capability": {
            "type": "choice",
            "choice": "strong",
            "probabilities": {"fast": 0.2, "strong": 0.8},
        },
    }
    item = record(1, waves=2, answers=answers, fallback_reason="error_fallback")
    log.enqueue(item)
    asyncio.run(log.flush())
    stored = rows(log)[0]
    assert stored["waves"] == 2
    for forbidden in (
        "text", "trace", "answers", "jev_probabilities", "fallback_reason",
        "session_id", "latency_ms", "reasoning_warning", "node_labels",
    ):
        assert forbidden not in stored
    assert log.recent_requests(1)[0]["answers"] == answers
    assert log.recent_requests(1)[0]["trace"] == item["trace"]


def test_log_columns_are_the_sqlite_whitelist(tmp_path):
    log = make_log(tmp_path)
    conn = connect(log.db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
    finally:
        conn.close()
    assert set(LOG_COLUMNS) == columns
    assert "waves" in LOG_COLUMNS
    assert not {"answers", "text", "trace", "session_id", "jev_probabilities"} & set(LOG_COLUMNS)


def test_routing_error_uses_existing_ledger_columns(tmp_path):
    log = make_log(tmp_path)
    item = record(1, outcome_state="routing_error", error_class="no_available_backend", model=None)
    log.enqueue(item)
    asyncio.run(log.flush())

    stored = rows(log)[0]
    assert stored["outcome_state"] == "routing_error"
    assert stored["error_class"] == "no_available_backend"
    assert stored["model"] is None
    assert "trace" not in stored
    assert "node_labels" not in stored


def test_flush_does_not_block_event_loop(tmp_path, monkeypatch):
    """Step 1 (RED before the refactor): the SQLite write must happen off the
    event loop.  A 50ms write must leave the loop free to run a 5ms ticker."""
    log = make_log(tmp_path)
    worker_threads = []

    async def run():
        real_write = log._write_rows

        def slow_write(rows, snapshots, report=None):
            worker_threads.append(threading.current_thread())
            time.sleep(0.05)
            return real_write(rows, snapshots, report)

        monkeypatch.setattr(log, "_write_rows", slow_write)
        log.enqueue(record(1))
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        task = asyncio.create_task(tick())
        try:
            await log.flush()
            assert ticks >= 3
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    # The blocking write ran on a worker thread, not on the event-loop thread.
    assert worker_threads and worker_threads[0] is not threading.current_thread()
    assert len(rows(log)) == 1


def test_shared_graph_snapshot_is_stored_once_and_trace_keeps_final_event(tmp_path):
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    first, second = record(1), record(2)
    log.enqueue(first, snapshot=snapshot)
    log.enqueue(second, snapshot=snapshot)
    asyncio.run(log.flush())

    conn = connect(log.db_path)
    try:
        assert first["graph_snapshot_id"] == second["graph_snapshot_id"]
        assert conn.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM request_snapshots").fetchone()[0] == 2
        stored_graph = json.loads(
            conn.execute("SELECT graph FROM graph_snapshots").fetchone()[0])
        assert stored_graph["canvas"]["start"] == {"x": 0, "y": 0}
        assert conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0] == 2
        # 账本本身仍然不存正文与 trace。
        columns = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
        assert not {"text", "trace", "answers"} & columns
    finally:
        conn.close()

    first["trace"].append({"event": "request_outcome", "outcome_state": "completed"})
    log.enqueue_result(first)
    asyncio.run(log.flush())

    conn = connect(log.db_path)
    try:
        stored_trace = json.loads(conn.execute(
            "SELECT trace FROM request_snapshots WHERE request_id='r1'"
        ).fetchone()[0])
        assert stored_trace[-1]["event"] == "request_outcome"
        assert conn.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_close_waits_for_the_inflight_batch_before_the_final_flush(tmp_path):
    """close() must not let an older batch land after the final result row.

    The old lifecycle cancelled the worker task but never joined the thread
    already inside ``_write_rows``; that stale batch could then overwrite the
    fresh outcome and trace written by the final flush.
    """
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    real_write = log._write_rows
    started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    active = []
    overlapped = []

    def delayed_write(rows, snapshots=(), report=None):
        active.append(1)
        if len(active) > 1:
            overlapped.append(True)
        try:
            if not started.is_set():
                started.set()
                assert release.wait(timeout=5)
            else:
                second_started.set()
            return real_write(rows, snapshots, report)
        finally:
            active.pop()

    async def scenario():
        item = record(1, outcome_state="incomplete")
        log.enqueue(item, snapshot=snapshot)
        log._write_rows = delayed_write
        log._task = asyncio.create_task(log.flush())
        assert await asyncio.to_thread(started.wait, 5)
        item["outcome_state"] = "completed"
        item["trace"].append({"event": "request_outcome", "outcome_state": "completed"})
        log.enqueue_result(item)
        closing = asyncio.create_task(log.close())
        # The old implementation cancels the worker and immediately runs the
        # final flush; only release after that bounded window so a stale batch
        # that starts later would deterministically win the race.
        await asyncio.to_thread(second_started.wait, 0.5)
        release.set()
        await closing

    asyncio.run(scenario())
    assert overlapped == []
    assert active == []
    stored = rows(log)
    assert len(stored) == 1
    assert stored[0]["outcome_state"] == "completed"
    conn = connect(log.db_path)
    try:
        trace = json.loads(conn.execute(
            "SELECT trace FROM request_snapshots WHERE request_id='r1'"
        ).fetchone()[0])
    finally:
        conn.close()
    assert trace[-1]["event"] == "request_outcome"


def test_close_wakes_real_periodic_worker_without_cancelling_it(tmp_path, monkeypatch):
    """A worker parked in ``_wake.wait()`` must leave via the wake event, not cancel.

    The observation of ``_wake.wait()`` proves the worker really reached its
    sleep point; otherwise the test could pass merely because close() ran before
    the periodic worker started.
    """
    log = make_log(tmp_path)
    log.enqueue(record(1))
    monkeypatch.setattr("router.request_logging.FLUSH_SECONDS", 60.0)

    async def scenario():
        waiting = asyncio.Event()
        real_wait = log._wake.wait()

        async def observed_wait():
            waiting.set()
            return await real_wait

        monkeypatch.setattr(log._wake, "wait", observed_wait)
        await log.start()
        await asyncio.wait_for(waiting.wait(), timeout=1)
        await asyncio.wait_for(log.close(), timeout=1)
        assert log._task.done()
        assert not log._task.cancelled()

    asyncio.run(scenario())
    assert rows(log)[0]["id"] == "r1"


def test_snapshot_for_reads_pending_records_beyond_the_ring(tmp_path):
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    for index in range(RECENT_LIMIT + 1):
        log.enqueue(record(index, text=f"文本{index}"), snapshot=snapshot)

    # r0 has fallen out of the display ring but is still queued for its first flush.
    assert log.recent_requests(RECENT_LIMIT)[-1]["id"] != "r0"
    live = log.snapshot_for("r0")
    assert live is not None
    assert live["request"]["id"] == "r0"
    assert live["request"]["text"] == "文本0"
    assert live["graph_snapshot_id"] == log.recent_requests(1)[0]["graph_snapshot_id"]
    assert log.snapshot_for("missing") is None


def test_pending_snapshot_index_is_bounded_and_counts_evictions(tmp_path, monkeypatch):
    monkeypatch.setattr("router.request_logging.FLUSH_QUEUE_LIMIT", 2)
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    for index in range(5):
        log.enqueue(record(index), snapshot=snapshot)

    stats = log.stats()
    assert stats["pending_snapshots"] == 2
    assert stats["snapshot_pending_evictions"] == 3
    assert stats["dropped_rows"] == 3


def test_snapshot_write_failure_rolls_back_the_pair_and_retries(tmp_path):
    log = make_log(tmp_path)
    first_tree = tree_from_dict(tree_dict())
    canvas = {node_id: dict(coords) for node_id, coords in GRAPH_CANVAS.items()}
    canvas["start"] = {"x": 9, "y": 9}
    second_tree = tree_from_dict(tree_dict(canvas=canvas))
    first_snapshot = SimpleNamespace(tree=first_tree, snapshot_revision="s" * 64)
    second_snapshot = SimpleNamespace(tree=second_tree, snapshot_revision="s" * 64)

    log.enqueue(record(1), snapshot=first_snapshot)
    asyncio.run(log.flush())

    conn = connect(log.db_path)
    try:
        conn.execute(
            "CREATE TRIGGER fail_request_snapshot "
            "BEFORE INSERT ON request_snapshots WHEN NEW.request_id = 'r2' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
    finally:
        conn.close()

    bad = record(2)
    log.enqueue(bad, snapshot=second_snapshot)
    asyncio.run(log.flush())

    stats = log.stats()
    assert stats["snapshot_write_failures"] == 1
    conn = connect(log.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM request_snapshots WHERE request_id = 'r1'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM request_snapshots WHERE request_id = 'r2'").fetchone()[0] == 0
    finally:
        conn.close()

    # The failed pair stays pending: memory reads still serve it and a later
    # flush retries the same immutable projection.
    assert log.snapshot_for("r2")["trace"] == bad["trace"]
    conn = connect(log.db_path)
    try:
        conn.execute("DROP TRIGGER fail_request_snapshot")
        conn.commit()
    finally:
        conn.close()
    asyncio.run(log.flush())
    conn = connect(log.db_path)
    try:
        assert conn.execute(
            "SELECT graph_snapshot_id FROM request_snapshots WHERE request_id = 'r2'"
        ).fetchone()[0] == bad["graph_snapshot_id"]
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_snapshots WHERE snapshot_id = ?",
            (bad["graph_snapshot_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()
    assert log.stats()["snapshot_write_failures"] == 1
    assert log.stats()["pending_snapshots"] == 0


def test_flush_does_not_retire_new_pending_capture_added_during_write(tmp_path):
    """Only the object a flush actually wrote may be retired from pending.

    A capture registered for the same id while the older batch is still in the
    worker thread must survive that batch's retirement, and the next flush must
    persist the new layout.
    """
    log = make_log(tmp_path)
    first_tree = tree_from_dict(tree_dict())
    canvas = {key: dict(value) for key, value in GRAPH_CANVAS.items()}
    canvas["start"] = {"x": 9, "y": 9}
    second_tree = tree_from_dict(tree_dict(canvas=canvas))
    first, second = record(1), record(1)
    log.enqueue(first, snapshot=SimpleNamespace(tree=first_tree, snapshot_revision="s1"))
    real_write = log._write_rows
    started, release = threading.Event(), threading.Event()

    def delayed_write(rows, snapshots=(), report=None):
        started.set()
        assert release.wait(timeout=5)
        return real_write(rows, snapshots, report)

    async def scenario():
        log._write_rows = delayed_write
        flushing = asyncio.create_task(log.flush())
        assert await asyncio.to_thread(started.wait, 5)
        log.enqueue(second, snapshot=SimpleNamespace(tree=second_tree, snapshot_revision="s2"))
        release.set()
        await flushing

    asyncio.run(scenario())
    assert first["graph_snapshot_id"] != second["graph_snapshot_id"]
    assert log._pending_snapshots["r1"] is second
    assert log.snapshot_for("r1")["graph_snapshot_id"] == second["graph_snapshot_id"]
    asyncio.run(log.flush())
    assert "r1" not in log._pending_snapshots


def test_worker_failure_counts_snapshot_on_pending_only_retry(tmp_path):
    """A database-level worker failure has no per-row report but still counts.

    The first flush carries one ledger row and one request snapshot; the second
    flush only retries the pending snapshot.  Each failed attempt is cumulative
    and a later successful retry must not decrease the historical count.
    """
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    log.enqueue(record(1), snapshot=snapshot)
    real_write = log._write_rows

    def fail_write(rows, snapshots=(), report=None):
        raise sqlite3.OperationalError("injected database failure")

    log._write_rows = fail_write
    with pytest.raises(sqlite3.OperationalError, match="injected database failure"):
        asyncio.run(log.flush())

    # 首次 flush 同时有一行账本和一份请求快照。
    assert log.ledger_write_failures == 1
    assert log.snapshot_write_failures == 1
    assert log.stats()["pending_snapshots"] == 1
    assert log.stats()["queue_size"] == 0

    # 队列已排空；第二次仅重试 pending 快照，不能重复计账本失败。
    with pytest.raises(sqlite3.OperationalError, match="injected database failure"):
        asyncio.run(log.flush())

    assert log.ledger_write_failures == 1
    assert log.snapshot_write_failures == 2
    assert log.stats()["pending_snapshots"] == 1
    assert log.stats()["queue_size"] == 0

    # 恢复真实 writer 后 pending 仍能写入；成功不会抹去历史失败计数。
    log._write_rows = real_write
    asyncio.run(log.flush())
    assert log.stats()["pending_snapshots"] == 0
    assert log.snapshot_write_failures == 2
    assert log.ledger_write_failures == 1
    conn = connect(log.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM request_snapshots WHERE request_id = 'r1'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_snapshots"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_result_that_misses_a_full_queue_is_merged_on_the_next_flush(tmp_path, monkeypatch):
    monkeypatch.setattr("router.request_logging.FLUSH_QUEUE_LIMIT", 1)
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    item = record(1)
    log.enqueue(item, snapshot=snapshot)
    asyncio.run(log.flush())

    # Occupy the single queue slot with another request so the final result for
    # r1 cannot be enqueued immediately.
    log.enqueue(record(2))
    item.update({"outcome_state": "completed", "total_ms": 30.0})
    item["trace"].append({"event": "request_outcome", "outcome_state": "completed"})
    log.enqueue_result(item)
    assert log.dropped_updates == 1

    asyncio.run(log.flush())
    stored = {row["id"]: row for row in rows(log)}
    assert sorted(stored) == ["r1", "r2"]
    assert stored["r1"]["outcome_state"] == "completed"
    conn = connect(log.db_path)
    try:
        trace = json.loads(conn.execute(
            "SELECT trace FROM request_snapshots WHERE request_id='r1'"
        ).fetchone()[0])
    finally:
        conn.close()
    assert trace[-1]["event"] == "request_outcome"


def test_result_index_eviction_is_bounded_and_counted(tmp_path, monkeypatch):
    """Best-effort boundary: the oldest merged result is dropped and counted."""
    monkeypatch.setattr("router.request_logging.FLUSH_QUEUE_LIMIT", 1)
    log = make_log(tmp_path)
    log.enqueue_result(record(1, outcome_state="completed"))
    log.enqueue_result(record(2, outcome_state="completed"))
    assert list(log._latest_results) == ["r2"]
    assert log.stats()["evicted_updates"] == 1
    assert log.stats()["dropped_updates"] == 1


def test_graph_snapshot_id_hashes_identity_behavior_and_layout_only(tmp_path):
    log = make_log(tmp_path)
    base = tree_from_dict(tree_dict())
    variant = replace(
        base,
        enabled=True,
        default=True,
        tree_revision="a" * 64,
        updated_at="2026-11-21T00:00:00+08:00",
        document_sha256="b" * 64,
    )

    def snapshot_id(index, tree):
        item = record(index)
        log.enqueue(item, snapshot=SimpleNamespace(tree=tree, snapshot_revision="s" * 64))
        return item["graph_snapshot_id"]

    canonical = snapshot_id(1, base)
    # Enabled/default/revision/updated_at/document hash are display metadata,
    # not identity: lifecycle must not split graph dedup.
    assert snapshot_id(2, variant) == canonical

    canvas = {node_id: dict(coords) for node_id, coords in GRAPH_CANVAS.items()}
    canvas["start"] = {"x": 9, "y": 9}
    assert snapshot_id(3, tree_from_dict(tree_dict(canvas=canvas))) != canonical
    assert snapshot_id(4, replace(base, timezone="UTC")) != canonical

    asyncio.run(log.flush())
    conn = connect(log.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()[0] == 3
    finally:
        conn.close()


def test_snapshot_for_serves_the_live_record_and_unknown_ids_miss(tmp_path):
    log = make_log(tmp_path)
    tree = tree_from_dict(tree_dict())
    snapshot = SimpleNamespace(tree=tree, snapshot_revision="s" * 64)
    item = record(1)
    log.enqueue(item, snapshot=snapshot)
    asyncio.run(log.flush())

    live = log.snapshot_for("r1")
    assert live["request_id"] == "r1"
    assert live["graph_snapshot_id"] == item["graph_snapshot_id"]
    assert live["tree"]["canvas"]["start"] == {"x": 0, "y": 0}
    assert live["trace"] == item["trace"]
    assert live["request"]["text"] == "秘密请求文本"
    assert "_graph_snapshot" not in live["request"]
    assert log.snapshot_for("missing") is None
    # 无图请求不产生快照索引。
    plain = record(2)
    log.enqueue(plain)
    assert "graph_snapshot_id" not in plain
    assert log.snapshot_for("r2") is None
