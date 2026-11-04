import asyncio

from router.api.requestlog import LOG_COLUMNS, RECENT_LIMIT, RequestLogger
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


def test_hydrate_recent_restores_persisted_tail_without_live_detail(tmp_path):
    log = make_log(tmp_path)
    for i in range(RECENT_LIMIT + 2):
        log.enqueue(record(i, total_ms=100.0 + i))
    asyncio.run(log.flush())

    restored = RequestLogger(log.settings)
    conn = connect(log.settings.db_path)
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
    conn = connect(log.settings.db_path)
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
