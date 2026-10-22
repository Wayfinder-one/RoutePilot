import asyncio
import logging
import sqlite3

from router.api.requestlog import RECENT_LIMIT, RequestLogger
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
           "signals": {"domain": {"value": "other", "confidence": 0.4,
                                  "is_rule": False},
                       "language": {"value": "zh", "confidence": 1.0,
                                    "is_rule": True}}}
    rec.update(kw)
    return rec


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
