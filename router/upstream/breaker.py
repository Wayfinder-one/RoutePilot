"""按模型的熔断器（进程内失败计数；db_path 给出时跨副本共享隔离状态）。

存储职责的拆分属于后续任务；本模块当前只承载从旧 API 模块原样搬过来
的实现，保证应用仍可启动。
"""

from __future__ import annotations

import sqlite3
import threading
import time


class CircuitBreaker:
    """Per-model circuit breaker.

    Process-local failure counts; when ``db_path`` is given (M4) the open state
    is also persisted to the ``breaker`` table so a second replica that has no
    local state still sees the quarantine. The first time a replica observes a
    DB quarantine it imports the DB's failure count and the DB's **original**
    ``opened_at`` (translated onto its own monotonic clock), so the half-open
    window is measured from when the circuit actually opened — not from when
    this replica noticed it.

    Consequence for the "conservative" framing: it is conservative only while
    the DB quarantine is fresh. If the persisted row is older than
    ``half_open_after``, the latching replica immediately allows the probe
    (``is_open`` returns False), i.e. cross-replica quarantine does not by
    itself preserve the full window (second-round audit F4).

    Half-open is a TRUE single-probe state: once the window has elapsed,
    ``is_open`` admits exactly one probe per model (tracked in ``_probe_at``)
    and keeps returning True for every concurrent request until the probe
    concludes — ``success()``/``failure()`` clear the marker, and a failed
    probe also re-arms ``opened_at`` so the circuit closes again. A probe that
    never reports back would otherwise lock the circuit forever, so after
    ``probe_timeout`` (slightly above the 60s upstream request timeout) the
    next caller becomes a replacement probe.

    DB discipline (2026-10-21 audit P0 2.2 / P2 4.3):

    * ``failure()`` re-arms the clock on EVERY failure at/above the threshold
      (``self._opened_at[model] = time.monotonic()``) — a failed half-open
      probe must close the circuit again. The previous ``setdefault`` kept the
      stale open time, so after one half-open window the circuit never blocked
      again (with or without a DB row).
    * DB sync is low-rate: ``is_open`` reads the quarantine row at most once
      per ``db_sync_seconds`` per model, and ``success()`` writes only when
      this replica actually held failure state. Healthy requests therefore
      open zero DB connections in steady state (design 4.4 exception list:
      this rare low-rate breaker sync).
    """

    #: Minimum interval between two DB reads for the same model (P2 4.3).
    db_sync_seconds: float = 30.0

    #: Max lifetime of a half-open probe; slightly above the 60s upstream
    #: request timeout so a wedged probe cannot lock the circuit forever.
    probe_timeout: float = 65.0

    def __init__(self, threshold: int = 3, half_open_after: float = 300.0,
                 db_path: str | None = None,
                 db_sync_seconds: float | None = None):
        self.threshold = threshold
        self.half_open_after = half_open_after
        self.db_path = db_path
        if db_sync_seconds is not None:
            self.db_sync_seconds = db_sync_seconds
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._db_read_at: dict[str, float] = {}   # last quarantine check
        self._probe_at: dict[str, float] = {}     # in-flight half-open probe
        self._lock = threading.Lock()

    def _admit_probe(self, model: str, now: float) -> bool:
        """Half-open gate: True if THIS request may proceed as the probe."""
        probe_at = self._probe_at.get(model)
        if probe_at is not None and now - probe_at < self.probe_timeout:
            return False
        self._probe_at[model] = now
        return True

    # ── DB helpers (M4, only used when db_path is set) ──────────────────────
    def _db_read(self, model: str):
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0,
                                   check_same_thread=False)
            try:
                row = conn.execute(
                    "SELECT failures, opened_at FROM breaker WHERE model = ?",
                    (model,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        return row

    def _db_write(self, model: str, failures: int, opened_at: float | None) -> None:
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0,
                                   check_same_thread=False)
            try:
                with conn:
                    if failures <= 0:
                        conn.execute("DELETE FROM breaker WHERE model = ?", (model,))
                    else:
                        conn.execute(
                            "INSERT INTO breaker VALUES (?, ?, ?) "
                            "ON CONFLICT(model) DO UPDATE SET failures = excluded.failures, "
                            "opened_at = excluded.opened_at",
                            (model, failures, opened_at))
            finally:
                conn.close()
        except sqlite3.Error:
            pass                                   # breaker must never raise

    def is_open(self, model: str) -> bool:
        with self._lock:
            if self._failures.get(model, 0) >= self.threshold:
                now = time.monotonic()
                if now - self._opened_at.get(model, 0.0) < self.half_open_after:
                    return True
                # Window elapsed: admit one probe, keep blocking the rest so a
                # recovering upstream is not flooded the instant it wakes up.
                return not self._admit_probe(model, now)
            if not self.db_path:
                return False
            # Rate-limited sync (P2 4.3): the local latch and the last check
            # cover the window; skip the DB while it is fresh.
            last_check = self._db_read_at.get(model)
            now = time.monotonic()
            if last_check is not None and now - last_check < self.db_sync_seconds:
                return False
            self._db_read_at[model] = now
            row = self._db_read(model)
            if not row or row[0] < self.threshold:
                return False
            opened_at = row[1] or time.time()
            # Latch the DB failure count and its ORIGINAL open time locally
            # (time.time -> time.monotonic, preserving the elapsed window).
            # A DB row that already outlived half_open_after falls into the
            # probe gate below: this replica probes immediately (audit F4 —
            # not "never releases early", just conservative while the row is
            # fresh).
            self._failures[model] = row[0]
            self._opened_at[model] = now - (time.time() - opened_at)
            if (time.time() - opened_at) < self.half_open_after:
                return True
            return not self._admit_probe(model, now)

    def failure(self, model: str) -> None:
        with self._lock:
            count = self._failures.get(model, 0) + 1
            self._failures[model] = count
            if count >= self.threshold:
                # Re-arm from NOW on every failure at/above the threshold: the
                # first crossing opens the circuit, and a failed half-open
                # probe closes it again. setdefault() here kept the ORIGINAL
                # open time, so after one half_open window is_open stayed
                # False forever and the circuit never blocked again (P0 2.2).
                self._opened_at[model] = time.monotonic()
                self._probe_at.pop(model, None)
                if self.db_path:
                    self._db_write(model, count, time.time())

    def success(self, model: str) -> None:
        with self._lock:
            # Only a replica that held failure state can have written a DB row
            # (or latched one); skip the DELETE otherwise (P2 4.3: healthy
            # requests must not open the database at all).
            had_state = model in self._failures or model in self._opened_at
            self._failures.pop(model, None)
            self._opened_at.pop(model, None)
            self._probe_at.pop(model, None)
            if self.db_path and had_state:
                self._db_write(model, 0, None)
