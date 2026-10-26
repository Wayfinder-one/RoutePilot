"""Async batched request logging + the in-memory calculation-log buffer.

Routing never waits on the database: handlers enqueue dicts, a background task
flushes them. Per-request detail (text, signal pack, decision trace) is NOT
persisted — it lives only in a bounded in-memory ring (RECENT_LIMIT) that the
console "calculation log" page and replay evaluation read from. The database
keeps two slim rows per request, both without text/signals:

* ``requests``  — the slim action row (decision/model/score/latency) plus the
  unified-tree provenance (``tree_id``/``terminal_node_id``, v19), written from
  the record enqueued BEFORE the upstream call; request text is never stored;
* ``request_ledger`` — the 2026-10-24 action—result ledger (JEV plan §3.1): the
  same record is mutated **in place** once the response is known and enqueued a
  second time, and the flush writes it with ``INSERT OR REPLACE`` on ``id``.
  A flush that lands between the two events leaves a half row
  (``outcome_state = 'incomplete'``); the next flush overwrites it. Only
  action/result/version metadata is stored — never the request text,
  signals/PII or the trace. v19 appends ``tree_id``/``tree_revision``/
  ``snapshot_revision``; the retired station/pipeline/capability columns stay
  declared (and NULL for new rows) so historical rows remain readable.

Backpressure and failures are counted, never silent (JEV P1-C §3): queue-full
drops of the half row / of the outcome update, ledger rows left in the
``incomplete`` terminal state, per-row ledger write failures and whole-batch
DB connection failures are exposed by :meth:`RequestLogger.stats`.

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
#: The station/pipeline/capability columns are kept for HISTORICAL rows only;
#: unified-tree records (v19) fill tree_id/tree_revision/snapshot_revision and
#: leave the retired ones NULL.
LEDGER_COLUMNS = (
    "id", "ts", "decision", "model", "station_id", "leaf_id", "score",
    "reasoning", "upstream_model", "upstream_status", "prompt_tokens",
    "completion_tokens", "total_tokens", "routing_ms", "upstream_ms",
    "ttft_ms", "total_ms", "jev_engine", "jev_model", "jev_window",
    "jev_temperature", "questions_sha256", "pipeline_version", "error_class",
    # v15 (JEV P1-B/C): provenance + terminal state + attribution
    "pipeline_revision", "questions_revision", "backends_revision",
    "outcome_state", "experiment_arm", "quality_score", "quality_evaluator",
    "usage_observed",
    # v16 (JEV P2-A/D): chunked long-input attribution
    "jev_chunk", "input_chunk_mode", "input_chunk_segments",
    "input_chunk_prompt_tokens",
    # v18 (2026-10-26): capability-routing pack revision (''
    # when no capability routing is configured)
    "capability_revision",
    # v19 (2026-10-26, unified routing tree): which tree/snapshot produced the
    # decision; NULL on rows from the retired station pipeline.
    "tree_id", "tree_revision", "snapshot_revision")

#: The eleven explicit ``requests`` target columns. ``text`` is always written
#: as an empty string: the request text is memory-only (design §4.4/privacy).
_SLIM_INSERT = (
    "INSERT OR REPLACE INTO requests (id, ts, text, decision, model, "
    "station_id, leaf_id, score, latency_ms, tree_id, terminal_node_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_LEDGER_INSERT = (
    "INSERT OR REPLACE INTO request_ledger ("
    + ", ".join(LEDGER_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in LEDGER_COLUMNS) + ")")


def slim_values(record: dict) -> tuple:
    """One ``requests`` row in :data:`_SLIM_INSERT` column order.

    The text column is unconditionally empty: detail lives in the memory ring
    only. ``station_id``/``leaf_id`` are read for backwards compatibility with
    older callers and stay NULL for unified-tree records.
    """
    return (
        record["id"], record["ts"], "", record["decision"],
        record.get("model"), record.get("station_id"), record.get("leaf_id"),
        record.get("score"), record.get("latency_ms"),
        record.get("tree_id"), record.get("terminal_node_id"))


def ledger_values(record: dict) -> tuple:
    """One ``request_ledger`` row: every column in :data:`LEDGER_COLUMNS` order.

    A key the record does not carry is written as NULL — the retired pipeline/
    capability/station columns are therefore NULL for new unified-tree records.
    """
    return tuple(record.get(column) for column in LEDGER_COLUMNS)


class RequestLogger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=FLUSH_QUEUE_LIMIT)
        self._task: asyncio.Task | None = None
        # complete records (text/signals/trace): newest last, never persisted
        self.recent: deque = deque(maxlen=RECENT_LIMIT)
        self._conf_buckets = [0] * CONF_BUCKETS
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        # ── drop counters (JEV P1-C §3): never lose rows silently ───────────
        #: half rows (``enqueue``) dropped because the flush queue was full
        self.dropped_rows = 0
        #: outcome updates (``enqueue_result``) dropped for the same reason
        self.dropped_updates = 0
        #: ledger rows whose last write still had outcome_state='incomplete'
        self.partial_rows = 0
        #: per-row DB write failures while flushing the batch
        self.ledger_write_failures = 0

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
            self.dropped_rows += 1                 # ring keeps it, DB row dropped

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
            self.dropped_updates += 1  # half row stays; ring still has the outcome

    def recent_requests(self, limit: int = RECENT_LIMIT) -> list[dict]:
        """Newest first, capped at the ring size (console log page)."""
        return list(reversed(self.recent))[:max(0, limit)]

    def replayable(self, since: str) -> list[dict]:
        """Route decisions in the ring (local date prefix >= ``since``) that a
        text-based replay can attempt.

        Returns exactly the inputs replay needs: the recorded original request
        (text/headers/n_messages), the recorded expectation (model) and the
        historical snapshot pointer (snapshot/tree revisions) plus the tagger
        evidence.  The retired replay reused the recorded ``signals``; that is
        deliberately NOT returned: a replay must re-tag the original text and
        never trust a stored signal pack.  A field that is absent from the ring
        record is returned as None so the caller can classify ``non_replayable``
        with a concrete reason instead of inventing an empty value.
        """
        out = []
        for r in self.recent:
            if r.get("decision") != "route" or not r.get("model"):
                continue
            if str(r.get("ts", ""))[:10] < since:
                continue
            out.append({
                "id": r["id"], "ts": r.get("ts", ""),
                "model": r.get("model"),
                "text": r.get("text"),
                "headers": r.get("headers"),
                "n_messages": r.get("n_messages"),
                "tree_id": r.get("tree_id"),
                "tree_revision": r.get("tree_revision"),
                "snapshot_revision": r.get("snapshot_revision"),
                "tagger_info": r.get("tagger_info"),
            })
        return out

    def confidence_histogram(self) -> list[dict]:
        return [{"lo": i / CONF_BUCKETS, "hi": (i + 1) / CONF_BUCKETS,
                 "count": self._conf_buckets[i]}
                for i in range(CONF_BUCKETS)]

    def stats(self) -> dict:
        """Ring/queue/ledger health for GET /admin/requests/stats (P1-C)."""
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
        try:
            conn = connect(self.settings.db_path)
        except sqlite3.Error:
            # The whole batch is lost: count it (the ring keeps the detail).
            self.ledger_write_failures += len(batch)
            raise
        # ids whose LAST write in this batch is still a half row (the same batch
        # often carries the half row followed by its outcome update; REPLACE
        # overwrites it, so only the rows left incomplete are counted).
        incomplete: set[str] = set()
        try:
            with conn:
                for r in batch:
                    # slim row only — text/signals/trace stay in the memory ring.
                    # The result-update event repeats the identical row (all its
                    # inputs are decision-time values), so the upsert is a no-op
                    # there and the original semantics are untouched.
                    try:
                        # Both rows come from pure row-value builders: the
                        # insert column list and the value tuple can never
                        # drift apart, and the privacy contract (no text/
                        # signals/trace) is enforced at the boundary.
                        conn.execute(_SLIM_INSERT, slim_values(r))
                        # action—result ledger: same id, first the half row
                        # (outcome_state='incomplete'), later the in-place
                        # outcome update replaces it. Only LEDGER_COLUMNS are
                        # selected, so no text/signals can ever leak into the
                        # ledger even though the ring record carries them.
                        conn.execute(_LEDGER_INSERT, ledger_values(r))
                    except sqlite3.Error:
                        # A single bad row must not drop the rest of the batch,
                        # and it must not be silent (P1-C: counter + log).
                        self.ledger_write_failures += 1
                        logger.exception("ledger row write failed id=%s",
                                         r.get("id"))
                        continue
                    if r.get("outcome_state") == "incomplete":
                        incomplete.add(r["id"])
                    else:
                        incomplete.discard(r["id"])
        finally:
            conn.close()
        self.partial_rows += len(incomplete)
