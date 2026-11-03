from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from router.routing.types import session_digest

@dataclass
class _Entry:
    model: str
    target_id: str
    tree_revision: str
    policy_path: str
    ttl_seconds: int
    expires_at: float


class SessionAffinity:
    def __init__(self, *, max_entries: int = 10000, clock=time.monotonic):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self.clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, session_id: str, *, tree_revision: str, policy_path: str, allowed_models: list[str], touch: bool = True) -> tuple[str, str] | None:
        if not session_id:
            return None
        key = session_digest(session_id)
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                if touch:
                    self._entries.pop(key, None)
                return None
            if entry.tree_revision != tree_revision or entry.policy_path != policy_path or entry.model not in allowed_models:
                if touch:
                    self._entries.pop(key, None)
                return None
            if touch:
                entry.expires_at = now + entry.ttl_seconds
                self._entries.move_to_end(key)
            return entry.model, entry.target_id

    def put(self, session_id: str, model: str, *, target_id: str, tree_revision: str, policy_path: str, ttl_seconds: int) -> None:
        if not session_id:
            return
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        key = session_digest(session_id)
        with self._lock:
            self._entries[key] = _Entry(
                model=model,
                target_id=target_id,
                tree_revision=tree_revision,
                policy_path=policy_path,
                ttl_seconds=ttl_seconds,
                expires_at=self.clock() + ttl_seconds,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
