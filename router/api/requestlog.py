"""Async batched request logging. Routing never waits on the database:
handlers enqueue dicts, a background task flushes every FLUSH_SECONDS.
Signals are stored per request so replay evaluation (M3) can rebuild packs.
"""

from __future__ import annotations

import asyncio
import json

from router.config import Settings
from router.store.db import connect

FLUSH_SECONDS = 2.0


class RequestLogger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def enqueue(self, record: dict) -> None:
        self._queue.put_nowait(record)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        await self.flush()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            await self.flush()

    async def flush(self) -> None:
        batch = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if not batch:
            return
        conn = connect(self.settings.db_path)
        try:
            with conn:
                for r in batch:
                    conn.execute(
                        "INSERT OR REPLACE INTO requests VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["id"], r["ts"], r["text"][:500], r["decision"],
                         r.get("model"), r.get("node_id"), r.get("branch_id"),
                         r.get("score"), r.get("latency_ms")))
                    for key, s in (r.get("signals") or {}).items():
                        conn.execute(
                            "INSERT INTO request_signals VALUES (?, ?, ?, ?, ?)",
                            (r["id"], key,
                             json.dumps(s.get("value"), ensure_ascii=False),
                             s.get("confidence"), int(bool(s.get("is_rule")))))
                    # M2: the embedding that goes with this turn, for the next
                    # turn's similarity/reask. Written here (async) so the hot
                    # path only ever reads the previous vector.
                    sid, vec = r.get("session_id"), r.get("vec")
                    if sid and vec:
                        conn.execute(
                            "INSERT INTO sessions VALUES (?, ?, ?) "
                            "ON CONFLICT(session_id) DO UPDATE SET "
                            "last_vec = excluded.last_vec, updated = excluded.updated",
                            (sid, json.dumps(vec), r["ts"]))
        finally:
            conn.close()
