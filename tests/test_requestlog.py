import asyncio
import logging
import sqlite3

from router.api.requestlog import LEDGER_COLUMNS, RECENT_LIMIT, RequestLogger
from router.config import Settings
from router.store.db import connect, migrate


def make_log(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    return RequestLogger(settings)


def record(i, **kw):
    """One logged request; the ring keeps `signals` (detail is memory-only)."""
    rec = {"id": f"r{i}", "ts": "2026-10-16T10:00:00", "text": "你好",
           "decision": "route", "model": "qwen3-8b", "station_id": "s",
           "leaf_id": "n", "score": None, "latency_ms": 12.0,
           "pipeline_revision": "p" * 64, "backends_revision": "b" * 64,
           "questions_revision": None, "pipeline_version": 1,
           "experiment_arm": None, "quality_score": None,
           "quality_evaluator": None, "usage_observed": 0,
           "outcome_state": "incomplete",
           "signals": {"domain": {"value": "other", "confidence": 0.4,
                                  "is_rule": False},
                       "language": {"value": "zh", "confidence": 1.0,
                                    "is_rule": True}}}
    rec.update(kw)
    return rec


def outcome(**kw):
    """The fields routes_v1 fills in after the response is known (§3.1/P1-C)."""
    fields = {"upstream_model": "upstream-qwen3-8b", "upstream_status": 200,
              "upstream_ms": 5.0, "ttft_ms": None, "total_ms": 20.0,
              "error_class": None, "prompt_tokens": 11,
              "completion_tokens": 7, "total_tokens": 18,
              "outcome_state": "completed", "usage_observed": 1}
    fields.update(kw)
    return fields


def ledger_rows(log):
    conn = connect(log.settings.db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM request_ledger ORDER BY rowid")]
    finally:
        conn.close()


def test_flush_writes_slim_rows_without_signals(tmp_path):
    """Detail (text/signals) must never reach the database."""
    log = make_log(tmp_path)
    log.enqueue(record(1, tree_path='{"spoiler": true}'))
    asyncio.run(log.flush())
    conn = connect(log.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    row = conn.execute("SELECT text, decision, model, station_id, leaf_id "
                        "FROM requests").fetchone()
    assert row["text"] == ""
    assert row["decision"] == "route" and row["model"] == "qwen3-8b"
    assert row["station_id"] == "s" and row["leaf_id"] == "n"
    columns = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
    assert "tree_path" not in columns      # v12 dropped the dead column
    assert "node_id" not in columns and "branch_id" not in columns
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "request_signals" not in tables   # dropped (v10): never written
    conn.close()


def test_memory_ring_keeps_newest_20_only(tmp_path):
    log = make_log(tmp_path)
    for i in range(RECENT_LIMIT + 5):
        log.enqueue(record(i))
    assert len(log.recent) == RECENT_LIMIT
    newest = log.recent_requests(RECENT_LIMIT)
    assert newest[0]["id"] == f"r{RECENT_LIMIT + 4}"      # newest first
    assert newest[-1]["id"] == f"r5"                       # oldest kept
    assert len(log.recent_requests(50)) == RECENT_LIMIT   # limit is a cap


def test_confidence_histogram_counts_model_signals_only(tmp_path):
    log = make_log(tmp_path)
    log.enqueue(record(1))
    assert sum(b["count"] for b in log.confidence_histogram()) == 1  # rule excluded


def test_replayable_only_route_decisions(tmp_path):
    log = make_log(tmp_path)
    log.enqueue(record(1))
    log.enqueue(record(2, decision="block", model=None))
    assert [r["id"] for r in log.replayable("2026-01-01")] == ["r1"]
    assert log.replayable("2027-01-01") == []


def test_flush_queue_is_bounded_and_never_raises(tmp_path, monkeypatch):
    """2026-10-21 audit §5: the flush queue is bounded; when full the slim DB
    row is dropped but the memory ring keeps the request (hot path never
    blocks on a slow/stuck database)."""
    import router.api.requestlog as rl

    monkeypatch.setattr(rl, "FLUSH_QUEUE_LIMIT", 2)
    log = make_log(tmp_path)
    for i in range(5):
        log.enqueue(record(i))               # must not raise when full
    assert log._queue.qsize() == 2
    assert len(log.recent) == 5
    asyncio.run(log.flush())
    conn = connect(log.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
    conn.close()


def test_flush_task_survives_a_db_error(tmp_path, monkeypatch, caplog):
    """One DB error must not kill the flush loop: a dead task means the slim
    `requests` rows stop being written for the rest of the process' life."""
    import router.api.requestlog as rl

    monkeypatch.setattr(rl, "FLUSH_SECONDS", 0.05)
    log = make_log(tmp_path)
    real_connect = rl.connect
    state = {"fail": True}

    def flaky_connect(path):
        if state["fail"]:
            state["fail"] = False
            raise sqlite3.OperationalError("database is locked")
        return real_connect(path)

    monkeypatch.setattr(rl, "connect", flaky_connect)

    async def scenario():
        await log.start()
        log.enqueue(record(1))
        await asyncio.sleep(0.2)              # the failing round
        assert not log._task.done()           # the task survived the error
        log.enqueue(record(2))
        await asyncio.sleep(0.2)              # the next round writes again
        await log.close()

    with caplog.at_level(logging.ERROR, logger="router.api.requestlog"):
        asyncio.run(scenario())
    assert "flush failed" in caplog.text

    conn = connect(log.settings.db_path)
    rows = [r["id"] for r in conn.execute("SELECT id FROM requests")]
    conn.close()
    assert rows == ["r2"]                     # the dropped batch, then the next one


# ── action—result ledger (JEV plan §3.1) ────────────────────────────────────
def test_flush_writes_half_row_then_replaces_it_with_the_outcome(tmp_path):
    """The flush may land between the decision and the response (2s period):
    the ledger row exists as a half row (outcome columns NULL) and the later
    result event overwrites it by id — never a duplicate row."""
    log = make_log(tmp_path)
    rec = record(1)
    log.enqueue(rec)
    asyncio.run(log.flush())

    rows = ledger_rows(log)
    assert len(rows) == 1
    half = rows[0]
    assert half["decision"] == "route" and half["model"] == "qwen3-8b"
    assert half["routing_ms"] is None      # outcome-only columns are still NULL
    assert half["upstream_status"] is None and half["error_class"] is None
    assert half["prompt_tokens"] is None and half["total_ms"] is None
    assert half["outcome_state"] == "incomplete"   # no terminal state yet
    assert half["usage_observed"] == 0
    assert half["pipeline_revision"] == "p" * 64
    assert log.partial_rows == 1                   # one half row flushed

    rec.update(outcome())
    log.enqueue_result(rec)
    asyncio.run(log.flush())

    rows = ledger_rows(log)
    assert len(rows) == 1                  # same id, INSERT OR REPLACE
    row = rows[0]
    assert row["upstream_model"] == "upstream-qwen3-8b"
    assert row["upstream_status"] == 200 and row["error_class"] is None
    assert (row["prompt_tokens"], row["completion_tokens"],
            row["total_tokens"]) == (11, 7, 18)
    assert row["upstream_ms"] == 5.0 and row["total_ms"] == 20.0
    assert row["outcome_state"] == "completed" and row["usage_observed"] == 1
    # The slim requests row keeps its original semantics (one row, no detail).
    conn = connect(log.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    conn.close()


def test_result_update_does_not_duplicate_ring_or_histogram(tmp_path):
    """The outcome event only reaches the flush queue: the request is already
    in the ring and its model signals are already counted exactly once."""
    log = make_log(tmp_path)
    rec = record(1)
    log.enqueue(rec)
    before = [b["count"] for b in log.confidence_histogram()]
    rec.update(outcome())
    log.enqueue_result(rec)
    assert list(log.recent) == [rec]                 # not appended twice
    assert [b["count"] for b in log.confidence_histogram()] == before
    assert log._queue.qsize() == 2                   # half row + update
    asyncio.run(log.flush())
    assert len(ledger_rows(log)) == 1


def test_ledger_holds_no_text_signals_or_trace(tmp_path):
    """Privacy constraint survives the ledger: only action/result/version
    columns are written; the request text, signals and trace stay memory-only."""
    log = make_log(tmp_path)
    rec = record(1, text="我的身份证号是 110101199001011234",
                 trace=[{"spoiler": True}])
    log.enqueue(rec)
    rec.update(outcome())
    log.enqueue_result(rec)
    asyncio.run(log.flush())

    conn = connect(log.settings.db_path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(request_ledger)")]
    conn.close()
    assert columns == list(LEDGER_COLUMNS)
    assert not {"text", "signals", "trace"} & set(columns)
    row = ledger_rows(log)[0]
    dumped = " ".join(str(v) for v in row.values() if v is not None)
    assert "110101199001011234" not in dumped
    assert "你好" not in dumped
    assert "spoiler" not in dumped


def test_partial_rows_counts_only_rows_left_incomplete(tmp_path):
    """A batch that carries a half row AND its outcome update is not partial:
    only rows whose last write in the batch is still 'incomplete' count."""
    log = make_log(tmp_path)
    rec = record(1)
    log.enqueue(rec)
    rec.update(outcome())                       # same batch: half + completed
    log.enqueue_result(rec)
    asyncio.run(log.flush())
    assert log.partial_rows == 0
    assert ledger_rows(log)[0]["outcome_state"] == "completed"

    log.enqueue(record(2))                      # a lone half row
    asyncio.run(log.flush())
    assert log.partial_rows == 1
    assert ledger_rows(log)[1]["outcome_state"] == "incomplete"


def test_drop_counters_are_never_silent(tmp_path, monkeypatch):
    """P1-C §3: queue-full drops of the half row and of the outcome update are
    counted and exposed (the ring still holds the request detail)."""
    import router.api.requestlog as rl

    monkeypatch.setattr(rl, "FLUSH_QUEUE_LIMIT", 1)
    log = make_log(tmp_path)
    rec = record(1)
    log.enqueue(rec)                            # fills the queue
    log.enqueue(record(2))                      # dropped half row
    rec.update(outcome())
    log.enqueue_result(rec)                     # dropped outcome update
    assert log.dropped_rows == 1
    assert log.dropped_updates == 1
    assert len(log.recent) == 2                 # detail is still in the ring

    stats = log.stats()
    assert {"started_at", "ring_size", "ring_limit", "queue_size",
            "queue_limit", "dropped_rows", "dropped_updates", "partial_rows",
            "ledger_write_failures"} <= set(stats)
    assert stats["dropped_rows"] == 1 and stats["dropped_updates"] == 1
    assert stats["queue_size"] == 1 and stats["ring_size"] == 2


def test_ledger_write_failure_is_counted_and_does_not_drop_the_batch(
        tmp_path, monkeypatch, caplog):
    """P1-C §3: one bad ledger row must not silently lose the whole batch —
    it is counted (and logged), the remaining rows still commit."""
    import router.api.requestlog as rl

    monkeypatch.setattr(rl, "_LEDGER_INSERT",
                        "INSERT INTO table_that_does_not_exist VALUES (1)")
    log = make_log(tmp_path)
    for i in range(3):
        log.enqueue(record(i))
    with caplog.at_level(logging.ERROR, logger="router.api.requestlog"):
        asyncio.run(log.flush())

    assert log.ledger_write_failures == 3
    assert "ledger row write failed" in caplog.text
    conn = connect(log.settings.db_path)
    # The slim requests rows are written before the ledger statement, so a
    # ledger failure does not lose the traffic aggregate either.
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3
    conn.close()
    assert log.stats()["ledger_write_failures"] == 3
