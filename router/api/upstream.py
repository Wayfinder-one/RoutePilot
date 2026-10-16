"""Upstream resolution (models join providers) and the circuit breaker.

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


def resolve_backend(conn: sqlite3.Connection, model_id: str) -> Backend:
    model = conn.execute("SELECT * FROM models WHERE id = ?",
                         (model_id,)).fetchone()
    if model is None:
        raise KeyError(model_id)
    provider = conn.execute("SELECT * FROM providers WHERE name = ?",
                            (model["provider"],)).fetchone()
    if provider is None:
        raise KeyError(model["provider"])
    key = os.environ.get(provider["key_env"], "") if provider["key_env"] else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return Backend(url=f"{provider['base_url']}/chat/completions",
                   headers=headers, upstream_name=model["upstream"])


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
