import asyncio

from router.api.requestlog import RECENT_LIMIT, RequestLogger
from router.config import Settings
from router.store.db import connect, migrate


def make_log(tmp_path):
    settings = Settings(db_path=str(tmp_path / "v3.db"))
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    return RequestLogger(settings)


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
    conn = connect(log.settings.db_path)
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


def test_queue_full_is_counted_and_ring_survives(tmp_path, monkeypatch):
    monkeypatch.setattr("router.api.requestlog.FLUSH_QUEUE_LIMIT", 1)
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
