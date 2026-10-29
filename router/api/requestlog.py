from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from datetime import datetime

from router.config import Settings
from router.store.db import connect

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 2.0
RECENT_LIMIT = 20
CONF_BUCKETS = 5
FLUSH_QUEUE_LIMIT = 10000

LOG_COLUMNS = (
    "id", "ts", "decision", "model", "target_id", "tree_id", "tree_revision",
    "snapshot_revision", "policy_path", "selection_reason", "sticky_hit",
    "session_hash", "jev_model", "jev_choice", "jev_top", "jev_margin",
    "jev_ms", "reasoning", "routing_ms", "upstream_model", "upstream_status",
    "upstream_ms", "ttft_ms", "total_ms", "prompt_tokens", "completion_tokens",
    "total_tokens", "outcome_state", "error_class",
)

_LOG_INSERT = (
    "INSERT OR REPLACE INTO request_logs (" + ", ".join(LOG_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in LOG_COLUMNS) + ")"
)


def log_values(record: dict) -> tuple:
    return tuple(record.get(column) for column in LOG_COLUMNS)


class RequestLogger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=FLUSH_QUEUE_LIMIT)
        self._task: asyncio.Task | None = None
        self.recent: deque = deque(maxlen=RECENT_LIMIT)
        self._conf_buckets = [0] * CONF_BUCKETS
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.dropped_rows = 0
        self.dropped_updates = 0
        self.partial_rows = 0
        self.ledger_write_failures = 0

    def enqueue(self, record: dict) -> None:
        self.recent.append(record)
        top = record.get("jev_top")
        if isinstance(top, (int, float)) and not isinstance(top, bool):
            index = min(CONF_BUCKETS - 1, max(0, int(float(top) * CONF_BUCKETS)))
            self._conf_buckets[index] += 1
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_rows += 1

    def enqueue_result(self, record: dict) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_updates += 1

    def recent_requests(self, limit: int = RECENT_LIMIT) -> list[dict]:
        return list(reversed(self.recent))[:max(0, limit)]

    def confidence_histogram(self) -> list[dict]:
        return [
            {"lo": index / CONF_BUCKETS, "hi": (index + 1) / CONF_BUCKETS, "count": count}
            for index, count in enumerate(self._conf_buckets)
        ]

    def stats(self) -> dict:
        return {
            "started_at": self.started_at,
            "ring_size": len(self.recent),
            "ring_limit": RECENT_LIMIT,
            "queue_size": self._queue.qsize(),
            "queue_limit": FLUSH_QUEUE_LIMIT,
            "dropped_rows": self.dropped_rows,
            "dropped_updates": self.dropped_updates,
            "partial_rows": self.partial_rows,
            "ledger_write_failures": self.ledger_write_failures,
        }

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self.flush()
        except Exception:
            logger.exception("requestlog final flush failed; batch dropped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await self.flush()
            except Exception:
                logger.exception("requestlog flush failed; batch dropped")

    async def flush(self) -> None:
        batch = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if not batch:
            return
        try:
            conn = connect(self.settings.db_path)
        except sqlite3.Error:
            self.ledger_write_failures += len(batch)
            raise
        incomplete: set[str] = set()
        try:
            with conn:
                for record in batch:
                    try:
                        conn.execute(_LOG_INSERT, log_values(record))
                    except sqlite3.Error:
                        self.ledger_write_failures += 1
                        logger.exception("request log write failed id=%s", record.get("id"))
                        continue
                    if record.get("outcome_state") == "incomplete":
                        incomplete.add(record["id"])
                    else:
                        incomplete.discard(record["id"])
        finally:
            conn.close()
        self.partial_rows += len(incomplete)
