from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from router.jev.client import JevAnswer
from router.routing.model import Target


class NoActiveTreeError(RuntimeError):
    pass


class RoutingMiss(RuntimeError):
    pass


class NoAvailableBackend(RuntimeError):
    pass


@dataclass(frozen=True)
class RoutingRequest:
    text: str
    session_id: str = ""


@dataclass
class RoutingDecision:
    model: str
    target: Target
    reasoning: str | None
    policy_path: str
    trace: list[dict]
    sticky_hit: bool
    selection_reason: str
    session_hash: str | None
    lease: Any
    jev_answer: JevAnswer | None = None
    fallback_reason: str | None = None
    _released: bool = field(default=False, init=False, repr=False)

    @property
    def target_id(self) -> str:
        return self.target.id

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.lease.release()


def session_digest(session_id: str) -> str | None:
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()
