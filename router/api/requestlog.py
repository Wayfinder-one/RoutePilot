"""Async batched request logging + the in-memory calculation-log buffer.

Routing never waits on the database: handlers enqueue dicts, a background task
flushes them. Per-request detail (text, signal pack, decision trace) is NOT
persisted — it lives only in a bounded in-memory ring (RECENT_LIMIT) that the
console "calculation log" page and replay evaluation read from. The database
keeps one slim row per request (no text, no signals) so the overview's traffic
aggregates survive restarts.

Single-process only: `python -m router` runs one uvicorn process, so the ring is
the whole picture. Running multiple workers would split the ring per process and
the console page would show only a fraction of the traffic.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime

from router.config import Settings
from router.store.db import connect

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 2.0
RECENT_LIMIT = 20          # console promise: the last 20 requests, memory only
CONF_BUCKETS = 5           # overview confidence histogram resolution
#: Bounded flush queue (audit §5): a slow/stuck DB must not grow memory without
#: backpressure. When full, the slim DB row is dropped — the memory ring (the
#: console log page and replay) still holds the request.
FLUSH_QUEUE_LIMIT = 10000


class RequestLogger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=FLUSH_QUEUE_LIMIT)
        self._task: asyncio.Task | None = None
        # complete records (text/signals/trace): newest last, never persisted
        self.recent: deque = deque(maxlen=RECENT_LIMIT)
        self._conf_buckets = [0] * CONF_BUCKETS
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    # ── memory ring (hot path: synchronous, no I/O) ──────────────────────────
    def enqueue(self, record: dict) -> None:
        self.recent.append(record)
        for s in (record.get("signals") or {}).values():
            conf = s.get("confidence")
            if conf is not None and not s.get("is_rule"):
                i = min(CONF_BUCKETS - 1, int(float(conf) * CONF_BUCKETS))
                self._conf_buckets[i] += 1
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            pass                                   # ring keeps it, DB row dropped

    def recent_requests(self, limit: int = RECENT_LIMIT) -> list[dict]:
        """Newest first, capped at the ring size (console log page)."""
        return list(reversed(self.recent))[:max(0, limit)]

    def replayable(self, since: str) -> list[dict]:
        """Route decisions in the ring whose local date prefix >= ``since``."""
        out = []
        for r in self.recent:
            if r.get("decision") != "route" or not r.get("model"):
                continue
            if not (r.get("signals") or {}):
                continue
            if str(r.get("ts", ""))[:10] < since:
                continue
            out.append({"id": r["id"], "ts": r["ts"], "model": r["model"],
                        "signals": r["signals"]})
        return out

    def confidence_histogram(self) -> list[dict]:
        return [{"lo": i / CONF_BUCKETS, "hi": (i + 1) / CONF_BUCKETS,
                 "count": self._conf_buckets[i]}
                for i in range(CONF_BUCKETS)]

    # ── DB flush: slim rows only ─────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        # A failing DB must not turn shutdown into an exception: the batch is
        # already in the memory ring, so log it and let the service stop.
        try:
            await self.flush()
        except Exception:                     # noqa: BLE001 - never fail shutdown
            logger.exception("requestlog final flush failed; batch dropped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            # One DB error must not kill the flush task: a dead task means the
            # slim `requests` rows stop being written forever (the memory ring
            # still holds the detail, but the traffic aggregates die with it).
            try:
                await self.flush()
            except Exception:                 # noqa: BLE001 - keep the task alive
                logger.exception("requestlog flush failed; batch dropped")

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
                    # slim row only — text/signals/trace stay in the memory ring
                    conn.execute(
                        "INSERT OR REPLACE INTO requests (id, ts, text, decision, "
                        "model, station_id, leaf_id, score, latency_ms) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["id"], r["ts"], "", r["decision"], r.get("model"),
                         r.get("station_id"), r.get("leaf_id"), r.get("score"),
                         r.get("latency_ms")))
        finally:
            conn.close()
