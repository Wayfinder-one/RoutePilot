"""请求日志账本：事件循环只入队/排空，SQLite 写入始终在 worker 线程。

热路径纪律（design 4.4）：

* ``enqueue()`` / ``enqueue_result()`` 只碰内存（ring + ``asyncio.Queue``），
  永不触库，也永不让请求等待磁盘。
* ``flush()`` 在事件循环里一次性排空队列，把每条可变 record 投影成不可变的
  数据库行值（``log_values()`` 的元组），再交给
  ``await asyncio.to_thread(self._write_rows, batch)`` 在默认 executor 里写库。
* ``_write_rows()`` 是同步函数，位于线程边界另一侧：只接收 ``list[tuple]``，
  不读 ``asyncio.Queue``、不碰可变 record、不修改任何共享统计，返回失败行数。
  失败计数与 ``partial_rows`` 只在事件循环中更新（单线程，无锁）。

请求日志仍是 best-effort 运行账本：数据库不可用时该批被丢弃并计入
``stats()`` 的 ``ledger_write_failures``，但绝不阻塞请求事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from datetime import datetime

from router.request_outcome import OUTCOME_INCOMPLETE
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
    "total_tokens", "outcome_state", "error_class", "waves",
)

_LOG_INSERT = (
    "INSERT OR REPLACE INTO request_logs (" + ", ".join(LOG_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in LOG_COLUMNS) + ")"
)

_ID = LOG_COLUMNS.index("id")
_OUTCOME_STATE = LOG_COLUMNS.index("outcome_state")
_INCOMPLETE = OUTCOME_INCOMPLETE


def log_values(record: dict) -> tuple:
    """Project a mutable record onto the immutable SQLite whitelist row."""
    return tuple(record.get(column) for column in LOG_COLUMNS)


def pending_rows(rows: list[tuple]) -> int:
    """How many rows in this ordered batch end on the ``incomplete`` state.

    Same semantics as the pre-refactor in-transaction set: a result row for the
    same id supersedes the half row queued before it. Pure function over the
    drained tuples, so it can run in the event loop after the worker write.
    """
    pending: set[str] = set()
    for values in rows:
        if values[_OUTCOME_STATE] == _INCOMPLETE:
            pending.add(values[_ID])
        else:
            pending.discard(values[_ID])
    return len(pending)


class RequestLogger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=FLUSH_QUEUE_LIMIT)
        self._task: asyncio.Task | None = None
        self.recent: deque = deque(maxlen=RECENT_LIMIT)
        self._conf_buckets = [0] * CONF_BUCKETS
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.dropped_rows = 0
        self.dropped_updates = 0
        self.partial_rows = 0
        self.ledger_write_failures = 0

    # ── 热路径：只碰内存 ────────────────────────────────────────────────────
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

    def hydrate_recent(self, conn: sqlite3.Connection) -> None:
        """Warm the ring with the persisted tail after a process restart.

        SQLite intentionally stores only the ledger whitelist. Restored rows
        therefore have no request text, trace, answers, node_labels or
        probabilities; the console renders those details as absent until a new
        live request arrives.  Only raw ledger dicts are kept here; display
        facts (``route_label``/``metrics``/``fields``) are attached at the API
        outlet via ``present_record()``.
        """
        if self.recent:
            return
        rows = conn.execute(
            "SELECT * FROM request_logs ORDER BY ts DESC, rowid DESC LIMIT ?",
            (RECENT_LIMIT,),
        ).fetchall()
        for row in reversed(rows):
            record = {column: row[column] for column in LOG_COLUMNS}
            total_ms = record.get("total_ms")
            record["latency_ms"] = (
                total_ms if total_ms is not None else record.get("routing_ms")
            )
            self.recent.append(record)

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

    # ── 生命周期：排空在事件循环，写库在线程 ────────────────────────────────
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

    def _drain_rows(self) -> list[tuple]:
        """Drain the queue into immutable row tuples (event loop only)."""
        rows: list[tuple] = []
        while not self._queue.empty():
            rows.append(log_values(self._queue.get_nowait()))
        return rows

    async def flush(self) -> None:
        batch = self._drain_rows()
        if not batch:
            return
        try:
            failures = await asyncio.to_thread(self._write_rows, batch)
        except Exception:
            self.ledger_write_failures += len(batch)
            raise
        self.ledger_write_failures += failures
        self.partial_rows += pending_rows(batch)

    def _write_rows(self, rows: list[tuple]) -> int:
        """Blocking batch insert — worker thread only. Returns failed-row count.

        Must stay free of asyncio.Queue reads, record mutation and shared
        counters; the only shared object it touches is its own db_path string.
        """
        failed = 0
        conn = connect(self.db_path)
        try:
            with conn:
                for values in rows:
                    try:
                        conn.execute(_LOG_INSERT, values)
                    except sqlite3.Error:
                        failed += 1
                        logger.exception("request log write failed id=%s", values[_ID])
        finally:
            conn.close()
        return failed
