"""Upstream resolution (models join providers) and the circuit breaker.

Hot-path discipline (design doc section 4.4): routing must NOT touch the
database. The app builds a `model_id -> Backend` snapshot at startup and
rebuilds it wholesale whenever admin writes a model or a provider; the /v1
handler only reads that in-memory snapshot.

The breaker keeps per-model failure counts in process memory: three
consecutive failures open the circuit; after half_open_after seconds one
probe request is allowed through. Deliberately process-local for M1 —
cross-replica sharing is an M4 concern (design doc section 5.2).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

from router.reasoning import normalize_reasoning


@dataclass
class Backend:
    url: str
    headers: dict
    upstream_name: str
    #: 归一化后的推理能力（§3.2）；None = 未声明/明确不支持 → 热路径透传。
    reasoning: dict | None = None
    provider: str = ""


def _headers_for(key_env: str, api_key: str = "") -> dict:
    # Stored key wins over the environment (contract §5.3); empty both = no auth.
    key = api_key or (os.environ.get(key_env, "") if key_env else "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _reasoning_capability(profile_text: str | None,
                          provider_format: str) -> dict | None:
    """Parse profile JSON + normalize; unusable/illegal declarations -> None.

    Hot path does zero parsing: this runs at startup and on every admin write
    that swaps the snapshot. A corrupt/illegal declaration must never prevent
    the service from starting (old databases), so fall back to "undeclared".
    """
    if not profile_text:
        return None
    try:
        profile = json.loads(profile_text)
    except (TypeError, ValueError):
        return None
    normalized = normalize_reasoning(profile, provider_format)
    if not normalized or not normalized.get("declared") \
            or not normalized.get("supported"):
        return None
    return normalized


def load_backends(conn: sqlite3.Connection) -> dict[str, Backend]:
    """Build the model_id -> Backend snapshot in one join.

    A model whose provider row is missing is simply absent from the snapshot;
    resolve_backend then raises KeyError and the /v1 handler answers 502.
    """
    rows = conn.execute(
        "SELECT m.id AS id, m.upstream AS upstream, m.provider AS provider, "
        "       m.profile AS profile, p.base_url AS base_url, "
        "       p.key_env AS key_env, p.api_key AS api_key, "
        "       p.reasoning_format AS reasoning_format "
        "FROM models m JOIN providers p ON m.provider = p.name")
    return {r["id"]: Backend(
        url=f"{r['base_url']}/chat/completions",
        headers=_headers_for(r["key_env"], r["api_key"] or ""),
        upstream_name=r["upstream"],
        reasoning=_reasoning_capability(r["profile"], r["reasoning_format"]),
        provider=r["provider"])
        for r in rows}


def resolve_backend(backends: dict[str, Backend], model_id: str) -> Backend:
    """Read the in-memory snapshot; unknown model raises KeyError."""
    if model_id not in backends:
        raise KeyError(model_id)
    return backends[model_id]


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
        self._lock = threading.Lock()

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
                return (time.monotonic() - self._opened_at.get(model, 0.0)
                        < self.half_open_after)
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
            # A DB row that already outlived half_open_after returns False
            # below: this replica probes immediately (audit F4 — not "never
            # releases early", just conservative while the row is fresh).
            self._failures[model] = row[0]
            self._opened_at[model] = time.monotonic() - (time.time() - opened_at)
            return (time.time() - opened_at) < self.half_open_after

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
            if self.db_path and had_state:
                self._db_write(model, 0, None)

