"""请求日志账本：事件循环只入队/排空，SQLite 写入始终在 worker 线程。

热路径纪律（design 4.4）：

* ``enqueue(record, snapshot=...)`` / ``enqueue_result()`` 只碰内存
  （ring + ``asyncio.Queue``），永不触库，也永不让请求等待磁盘。图快照在
  ``enqueue()`` 时捕获到记录的私有字段 ``_graph_snapshot``（见
  ``router.request_snapshots``）。
* ``flush()`` 在事件循环里一次性排空队列，把每条可变 record 投影成不可变的
  数据库行值（``log_values()`` 的元组）以及快照的两行值
  （``snapshot_values()``），再交给
  ``await asyncio.to_thread(self._write_rows, batch, snapshots)`` 在默认 executor 里写库。
* ``_write_rows()`` 是同步函数，位于线程边界另一侧：只接收排空后的
  ``list[tuple]``（账本行）与 ``snapshots``（图/trace 两行值），不读
  ``asyncio.Queue``、不碰可变 record、不修改任何共享统计，返回失败行数。
  失败计数与 ``partial_rows`` 只在事件循环中更新（单线程，无锁）。
* ``snapshot_for()`` 是日志详情端点读取整图的唯一内存入口；按 recent → pending
  → miss 查找，未命中时由调用方回退到 SQLite。``pending`` 索引覆盖「已入队但
  还没 flush」的快照；它不读 ``asyncio.Queue`` 私有字段，并有界（与队列上限
  相同，超限按最旧项淘汰并计入 ``stats()`` 的可观测计数）。
* 可靠性语义：``enqueue_result()`` 的最终结果登记在 ``_latest_results``；队列满
  只影响「立即入队」，仍在索引中的最新投影会被下一次可写 flush 合并且不丢，但
  超过 ``FLUSH_QUEUE_LIMIT`` 时最旧的更新被淘汰并计入 ``evicted_updates``——
  best-effort 不承诺所有结果必达。图行和 trace 行按请求用 SAVEPOINT 原子写入，
  失败只影响该请求的附加事实，已落库的旧快照保持原样，pending 条目留给后续 flush
  重试。``snapshot_write_failures`` 有两种计入路径：单请求 SAVEPOINT 失败由
  ``_write_rows()`` 的 ``failed_snapshots`` 逐请求计数、同批其他请求继续；worker
  或数据库级异常没有逐行 report，本批 ``snapshots`` 的每个请求快照各计一次失败
  尝试，pending 保留且原异常继续抛出。该计数是**累计的按请求失败尝试次数**（不是
  去重后的 request ID 数），重试再次失败继续累计，成功重试不回减历史失败。
* 生命周期只有一个写者：周期 flush、手动 flush 与关闭时的 final flush 都串行在
  ``_flush_lock`` 下；``close()`` 先让 worker 正常退出（不 cancel 正在执行的批次），
  再执行 final flush。因此任何时刻最多一个批次调用 ``_write_rows()``，且
  ``close()`` 返回后不会遗留写线程。

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
from router.request_snapshots import capture_snapshot, memory_snapshot, snapshot_values
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

_GRAPH_INSERT = (
    "INSERT OR IGNORE INTO graph_snapshots (snapshot_id, tree_id, snapshot_revision, graph, created_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
_REQUEST_SNAPSHOT_INSERT = (
    "INSERT OR REPLACE INTO request_snapshots (request_id, graph_snapshot_id, trace, created_at) "
    "VALUES (?, ?, ?, ?)"
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
        self._flush_lock = asyncio.Lock()
        self._closing = False
        self._wake = asyncio.Event()
        self.recent: deque = deque(maxlen=RECENT_LIMIT)
        #: request_id → 尚未成功落库的快照记录（有界，见 ``_remember_pending``）。
        self._pending_snapshots: dict[str, dict] = {}
        #: request_id → 最新的最终结果记录；队列满也不能丢。
        self._latest_results: dict[str, dict] = {}
        self._conf_buckets = [0] * CONF_BUCKETS
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.dropped_rows = 0
        self.dropped_updates = 0
        self.evicted_updates = 0
        self.partial_rows = 0
        self.ledger_write_failures = 0
        #: 累计的按请求快照写入失败尝试次数；成功重试不回减（不是去重 ID 数）。
        self.snapshot_write_failures = 0
        self.snapshot_pending_evictions = 0

    # ── 热路径：只碰内存 ────────────────────────────────────────────────────
    def enqueue(self, record: dict, *, snapshot=None) -> None:
        if snapshot is not None:
            capture_snapshot(record, snapshot)
        self.recent.append(record)
        top = record.get("jev_top")
        if isinstance(top, (int, float)) and not isinstance(top, bool):
            index = min(CONF_BUCKETS - 1, max(0, int(float(top) * CONF_BUCKETS)))
            self._conf_buckets[index] += 1
        self._remember_pending(record)
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_rows += 1

    def _remember_pending(self, record: dict) -> None:
        """Index a captured snapshot until its rows are confirmed written."""
        request_id = record.get("id")
        if not isinstance(request_id, str) or not request_id:
            return
        if "_graph_snapshot" not in record:
            return
        # Re-insert so eviction always drops the oldest *capture*.
        if request_id in self._pending_snapshots:
            del self._pending_snapshots[request_id]
        self._pending_snapshots[request_id] = record
        while len(self._pending_snapshots) > FLUSH_QUEUE_LIMIT:
            self._pending_snapshots.pop(next(iter(self._pending_snapshots)))
            self.snapshot_pending_evictions += 1

    def enqueue_result(self, record: dict) -> None:
        request_id = record.get("id")
        if isinstance(request_id, str) and request_id:
            # The latest projection must survive a full queue: the periodic
            # flush merges it into the next batch even if this enqueue drops.
            if request_id in self._latest_results:
                del self._latest_results[request_id]
            self._latest_results[request_id] = record
            while len(self._latest_results) > FLUSH_QUEUE_LIMIT:
                self._latest_results.pop(next(iter(self._latest_results)))
                self.evicted_updates += 1
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_updates += 1

    def recent_requests(self, limit: int = RECENT_LIMIT) -> list[dict]:
        return list(reversed(self.recent))[:max(0, limit)]

    def snapshot_for(self, request_id: str) -> dict | None:
        """Live snapshot payload: memory ring first, then pending captures."""
        if not request_id:
            return None
        for record in reversed(self.recent):
            if record.get("id") != request_id:
                continue
            if "_graph_snapshot" in record:
                return memory_snapshot(record)
            return None
        pending = self._pending_snapshots.get(request_id)
        if pending is not None:
            return memory_snapshot(pending)
        return None

    def hydrate_recent(self, conn: sqlite3.Connection) -> None:
        """Warm the ring with the persisted tail after a process restart.

        SQLite intentionally stores only the ledger whitelist. Restored rows
        therefore have no request text, trace, answers, node_labels or
        probabilities; the console renders those details as absent until a new
        live request arrives. The snapshot index (``graph_snapshot_id``) is
        joined in so restored rows keep their "view snapshot" link; the graph
        itself is still read by the snapshot endpoint, never by this list.  Only raw ledger dicts are kept here; display
        facts (``route_label``/``metrics``/``fields``) are attached at the API
        outlet via ``present_record()``.
        """
        if self.recent:
            return
        rows = conn.execute(
            "SELECT * FROM request_logs ORDER BY ts DESC, rowid DESC LIMIT ?",
            (RECENT_LIMIT,),
        ).fetchall()
        restored = []
        for row in reversed(rows):
            record = {column: row[column] for column in LOG_COLUMNS}
            total_ms = record.get("total_ms")
            record["latency_ms"] = (
                total_ms if total_ms is not None else record.get("routing_ms")
            )
            restored.append(record)
        # 快照索引不在账本里：重启后仍要让列表显示「查看图快照」链接。
        ids = [record["id"] for record in restored if record.get("id")]
        if ids:
            try:
                snapshot_ids = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT request_id, graph_snapshot_id FROM request_snapshots "
                        "WHERE request_id IN (" + ", ".join("?" for _ in ids) + ")",
                        ids,
                    )
                }
            except sqlite3.Error:
                snapshot_ids = {}
            for record in restored:
                snapshot_id = snapshot_ids.get(record["id"])
                if snapshot_id:
                    record["graph_snapshot_id"] = snapshot_id
        self.recent.extend(restored)

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
            "pending_snapshots": len(self._pending_snapshots),
            "dropped_rows": self.dropped_rows,
            "dropped_updates": self.dropped_updates,
            "evicted_updates": self.evicted_updates,
            "partial_rows": self.partial_rows,
            "ledger_write_failures": self.ledger_write_failures,
            "snapshot_write_failures": self.snapshot_write_failures,
            "snapshot_pending_evictions": self.snapshot_pending_evictions,
        }

    # ── 生命周期：排空在事件循环，写库在线程 ────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        """Stop the periodic worker, wait for its current batch, then drain.

        The worker task is never cancelled while it may be inside
        ``_write_rows()``: cancelling only detaches the coroutine, leaving the
        thread free to finish an older batch after the final flush and overwrite
        the newer rows.  The wake event makes a sleeping worker exit promptly;
        the flush lock keeps the final drain behind any in-flight batch.
        """
        self._closing = True
        self._wake.set()
        task = self._task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                # Only swallow an externally cancelled worker; a cancelled
                # close() itself must stay cancelled.
                if not task.cancelled():
                    raise
        try:
            await self.flush()
        except Exception:
            logger.exception("requestlog final flush failed; batch dropped")

    async def _loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.wait_for(self._wake.wait(), FLUSH_SECONDS)
            except asyncio.TimeoutError:
                pass
            else:
                self._wake.clear()
                if self._closing:
                    break
            try:
                await self.flush()
            except Exception:
                logger.exception("requestlog flush failed; batch dropped")

    def _drain(self) -> tuple[list[tuple], list[tuple[tuple, tuple]], dict, dict]:
        """Drain the queue and merge pending/result indexes (event loop only).

        Returns ``(rows, snapshots, merged_results, projected_records)``:

        * ``rows`` keeps queue order and appends the latest result projection
          for every request id, so ``INSERT OR REPLACE`` lets the final outcome
          win even when its ``enqueue_result()`` was dropped by a full queue;
        * ``snapshots`` is deduplicated per request id (last projection wins)
          and includes still-pending captures so a failed pair is retried;
        * ``merged_results`` and ``projected_records`` map request id to the
          exact record objects projected, so the flush can remove only what it
          actually wrote without deleting a newer capture added mid-write.
        """
        rows: list[tuple] = []
        snapshots: dict[str, tuple[tuple, tuple]] = {}
        projected: dict[str, dict] = {}

        def project(record: dict) -> None:
            values = snapshot_values(record)
            if values is None:
                return
            request_id = values[1][0]
            snapshots[request_id] = values
            projected[request_id] = record

        while not self._queue.empty():
            record = self._queue.get_nowait()
            rows.append(log_values(record))
            project(record)

        merged = dict(self._latest_results)
        for record in merged.values():
            rows.append(log_values(record))
            project(record)

        # A failed pair stays pending until a later flush confirms the write.
        for record in self._pending_snapshots.values():
            request_id = record.get("id")
            if isinstance(request_id, str) and request_id in snapshots:
                continue
            project(record)

        return rows, list(snapshots.values()), merged, projected

    async def flush(self) -> None:
        # One writer at a time: periodic, manual and final flushes all queue
        # behind the same lock, so an older batch can never land after a newer
        # projection of the same request id.
        async with self._flush_lock:
            batch, snapshots, merged, projected = self._drain()
            if not batch and not snapshots:
                return
            report = {"failed_rows": [], "failed_snapshots": [], "written_snapshots": []}
            try:
                failures = await asyncio.to_thread(
                    self._write_rows, batch, snapshots, report)
            except Exception:
                # No per-row report exists when the worker itself fails: the
                # whole batch failed to reach the database.  Count every
                # snapshot attempt here; pending entries stay indexed for the
                # next flush, and the original exception keeps propagating.
                self.ledger_write_failures += len(batch)
                self.snapshot_write_failures += len(snapshots)
                raise
            self.ledger_write_failures += failures
            self.partial_rows += pending_rows(batch)
            self.snapshot_write_failures += len(report.get("failed_snapshots", ()))
            for request_id in report.get("written_snapshots", ()):
                record = projected.get(request_id)
                if record is not None and self._pending_snapshots.get(request_id) is record:
                    self._pending_snapshots.pop(request_id, None)
            failed_rows = set(report.get("failed_rows", ()))
            for request_id, record in merged.items():
                if request_id in failed_rows:
                    continue
                if self._latest_results.get(request_id) is record:
                    self._latest_results.pop(request_id, None)

    def _write_rows(self, rows: list[tuple], snapshots=(), report=None) -> int:
        """Blocking batch insert — worker thread only. Returns failed-row count.

        ``snapshots`` holds ``(graph_row, request_row)`` pairs in queue order;
        the graph insert is ``INSERT OR IGNORE`` (content-addressed) while the
        request trace is ``INSERT OR REPLACE`` so the last row for an id wins.
        Each pair runs inside a SAVEPOINT: when the trace row fails the graph
        row is rolled back too, so a request never points at an uncommitted or
        half-updated snapshot.  Other requests in the same batch continue.

        ``report`` is an optional out-dict filled with the request ids written
        or failed, so the event loop can retire exactly those pending indexes.
        Must stay free of asyncio.Queue reads, record mutation and shared
        counters; the only shared object it touches is its own db_path string.
        """
        failed = 0
        failed_rows: list[str] = []
        failed_snapshots: list[str] = []
        written_snapshots: list[str] = []
        conn = connect(self.db_path)
        try:
            with conn:
                for graph_row, request_row in snapshots:
                    request_id = request_row[0]
                    conn.execute("SAVEPOINT request_snapshot")
                    try:
                        conn.execute(_GRAPH_INSERT, graph_row)
                        conn.execute(_REQUEST_SNAPSHOT_INSERT, request_row)
                    except sqlite3.Error:
                        conn.execute("ROLLBACK TO request_snapshot")
                        conn.execute("RELEASE request_snapshot")
                        failed_snapshots.append(request_id)
                        # 快照是附加事实，写失败不能拖垮同批的账本行。
                        logger.exception("request snapshot write failed id=%s", request_id)
                    else:
                        conn.execute("RELEASE request_snapshot")
                        written_snapshots.append(request_id)
                for values in rows:
                    try:
                        conn.execute(_LOG_INSERT, values)
                    except sqlite3.Error:
                        failed += 1
                        failed_rows.append(values[_ID])
                        logger.exception("request log write failed id=%s", values[_ID])
        finally:
            conn.close()
        if report is not None:
            report["failed_rows"] = failed_rows
            report["failed_snapshots"] = failed_snapshots
            report["written_snapshots"] = written_snapshots
        return failed
