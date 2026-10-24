"""Async batched request logging + the in-memory calculation-log buffer.

Routing never waits on the database: handlers enqueue dicts, a background task
flushes them. Per-request detail (text, signal pack, decision trace) is NOT
persisted — it lives only in a bounded in-memory ring (RECENT_LIMIT) that the
console "calculation log" page and replay evaluation read from. The database
keeps two slim rows per request, both without text/signals:

* ``requests``  — the original 9-column action row (decision/model/score/
  latency), written from the record enqueued BEFORE the upstream call;
* ``request_ledger`` — the 2026-10-24 action—result ledger (JEV plan §3.1): the
  same record is mutated **in place** once the response is known and enqueued a
  second time, and the flush writes it with ``INSERT OR REPLACE`` on ``id``.
  A flush that lands between the two events leaves the half row (outcome
  columns NULL); the next flush overwrites it. Only action/result/version
  metadata is stored — never the request text, signals/PII or the trace.

Single-process only: `python -m router` runs one uvicorn process, so the ring is
the whole picture. Running multiple workers would split the ring per process and
the console page would show only a fraction of the traffic.
"""

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
RECENT_LIMIT = 20          # console promise: the last 20 requests, memory only
CONF_BUCKETS = 5           # overview confidence histogram resolution
#: Bounded flush queue (audit §5): a slow/stuck DB must not grow memory without
#: backpressure. When full, the slim DB row is dropped — the memory ring (the
#: console log page and replay) still holds the request.
FLUSH_QUEUE_LIMIT = 10000

#: request_ledger columns (migration 14). The ledger deliberately holds no
#: text/signals/trace — only the action, the outcome and the version metadata.
LEDGER_COLUMNS = (
    "id", "ts", "decision", "model", "station_id", "leaf_id", "score",
    "reasoning", "upstream_model", "upstream_status", "prompt_tokens",
    "completion_tokens", "total_tokens", "routing_ms", "upstream_ms",
    "ttft_ms", "total_ms", "jev_engine", "jev_model", "jev_window",
    "jev_temperature", "questions_sha256", "pipeline_version", "error_class")

_SLIM_INSERT = (
    "INSERT OR REPLACE INTO requests (id, ts, text, decision, model, "
    "station_id, leaf_id, score, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")

_LEDGER_INSERT = (
    "INSERT OR REPLACE INTO request_ledger ("
    + ", ".join(LEDGER_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in LEDGER_COLUMNS) + ")")


def _load_pipeline_version(db_path: str) -> int | None:
    """Read ``settings.pipeline_version`` once, when the logger is built.

    ``RequestLogger`` is constructed during ``create_app``, so this is the app
    startup read the ledger plan asks for: the hot path never opens the
    database (design 4.4) — every ledger row copies this cached value. A
    database without the settings row (unit tests migrate but do not seed) or
    with a non-numeric value yields NULL instead of failing the boot.

    A runtime pipeline edit does not wait for a restart: ``PUT
    /admin/pipeline`` (the only bumping write path) hot-syncs this cache right
    after it saves — see ``routes_admin._sync_ledger_pipeline_version`` — so
    the request path still never queries the database.
    """
    try:
        conn = connect(db_path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           ("pipeline_version",)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


class RequestLogger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=FLUSH_QUEUE_LIMIT)
        self._task: asyncio.Task | None = None
        # complete records (text/signals/trace): newest last, never persisted
        self.recent: deque = deque(maxlen=RECENT_LIMIT)
        self._conf_buckets = [0] * CONF_BUCKETS
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        #: Cached settings.pipeline_version (see _load_pipeline_version).
        self.pipeline_version = _load_pipeline_version(settings.db_path)

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

    def enqueue_result(self, record: dict) -> None:
        """Queue the outcome update for an already-logged record.

        ``routes_v1`` fills the outcome fields into the SAME dict the ring
        holds (the deque keeps the reference) and calls this once the response
        is known — after the handler for buffered responses, at stream end for
        SSE. The flush overwrites the half row by primary key.

        It must NOT go through :meth:`enqueue`: the request is already in the
        ring and its model signals were already counted; appending here would
        duplicate the console entry and double-count the confidence histogram.
        """
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            pass                      # half row stays; ring still has the outcome

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
                    # slim row only — text/signals/trace stay in the memory ring.
                    # The result-update event repeats the identical row (all its
                    # inputs are decision-time values), so the upsert is a no-op
                    # there and the original semantics are untouched.
                    conn.execute(
                        _SLIM_INSERT,
                        (r["id"], r["ts"], "", r["decision"], r.get("model"),
                         r.get("station_id"), r.get("leaf_id"), r.get("score"),
                         r.get("latency_ms")))
                    # action—result ledger: same id, first the half row (outcome
                    # columns NULL), later the in-place outcome update replaces
                    # it. Only LEDGER_COLUMNS are selected, so no text/signals
                    # can ever leak into the ledger even though the ring record
                    # carries them.
                    conn.execute(_LEDGER_INSERT,
                                 tuple(r.get(c) for c in LEDGER_COLUMNS))
        finally:
            conn.close()
