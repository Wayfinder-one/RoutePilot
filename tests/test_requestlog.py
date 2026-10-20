import asyncio

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
           "decision": "route", "model": "qwen3-8b", "node_id": "n",
           "branch_id": "b", "score": None, "latency_ms": 12.0,
           "signals": {"domain": {"value": "other", "confidence": 0.4,
                                  "is_rule": False},
                       "language": {"value": "zh", "confidence": 1.0,
                                    "is_rule": True}}}
    rec.update(kw)
    return rec


def test_flush_writes_slim_rows_without_signals(tmp_path):
    """Detail (text/signals) must never reach the database."""
    log = make_log(tmp_path)
    log.enqueue(record(1))
    asyncio.run(log.flush())
    conn = connect(log.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert conn.execute("SELECT text FROM requests").fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM request_signals").fetchone()[0] == 0
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


def test_sessions_row_still_written(tmp_path):
    """Similarity / continuation read this next turn: it is not 'log' data."""
    log = make_log(tmp_path)
    log.enqueue(record(1, session_id="s1", vec=[0.1, 0.2],
                       session_model="qwen3-8b"))
    asyncio.run(log.flush())
    conn = connect(log.settings.db_path)
    row = conn.execute("SELECT last_vec, last_model FROM sessions "
                       "WHERE session_id='s1'").fetchone()
    assert row is not None and row["last_model"] == "qwen3-8b"
    conn.close()
