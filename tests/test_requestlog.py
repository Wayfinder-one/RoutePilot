import asyncio

from router.api.requestlog import RequestLogger
from router.config import Settings
from router.store.db import connect, migrate


def test_flush_writes_rows(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    log = RequestLogger(settings)
    log.enqueue({"id": "r1", "ts": "2026-10-16T10:00:00", "text": "你好",
                 "decision": "route", "model": "qwen3-8b", "node_id": "n",
                 "branch_id": "b", "score": None, "latency_ms": 12.0,
                 "signals": {"domain": {"value": "other", "confidence": None,
                                        "is_rule": False}}})
    asyncio.run(log.flush())
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM request_signals").fetchone()[0] == 1
    conn.close()
