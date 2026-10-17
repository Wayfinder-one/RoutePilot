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

import os
import sqlite3
import threading
import time
from dataclasses import dataclass


@dataclass
class Backend:
    url: str
    headers: dict
    upstream_name: str


def _headers_for(key_env: str) -> dict:
    key = os.environ.get(key_env, "") if key_env else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def load_backends(conn: sqlite3.Connection) -> dict[str, Backend]:
    """Build the model_id -> Backend snapshot in one join.

    A model whose provider row is missing is simply absent from the snapshot;
    resolve_backend then raises KeyError and the /v1 handler answers 502.
    """
    rows = conn.execute(
        "SELECT m.id AS id, m.upstream AS upstream, "
        "       p.base_url AS base_url, p.key_env AS key_env "
        "FROM models m JOIN providers p ON m.provider = p.name")
    return {r["id"]: Backend(url=f"{r['base_url']}/chat/completions",
                             headers=_headers_for(r["key_env"]),
                             upstream_name=r["upstream"])
            for r in rows}


def resolve_backend(backends: dict[str, Backend], model_id: str) -> Backend:
    """Read the in-memory snapshot; unknown model raises KeyError."""
    if model_id not in backends:
        raise KeyError(model_id)
    return backends[model_id]


class CircuitBreaker:
    def __init__(self, threshold: int = 3, half_open_after: float = 300.0):
        self.threshold = threshold
        self.half_open_after = half_open_after
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_open(self, model: str) -> bool:
        with self._lock:
            if self._failures.get(model, 0) < self.threshold:
                return False
            return (time.monotonic() - self._opened_at.get(model, 0.0)
                    < self.half_open_after)

    def failure(self, model: str) -> None:
        with self._lock:
            self._failures[model] = self._failures.get(model, 0) + 1
            if self._failures[model] >= self.threshold:
                self._opened_at.setdefault(model, time.monotonic())

    def success(self, model: str) -> None:
        with self._lock:
            self._failures.pop(model, None)
            self._opened_at.pop(model, None)
