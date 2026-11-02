from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from router.jev.client import JevAnswer
from router.routing.graph.model import TargetNode


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


@dataclass(frozen=True)
class Candidate:
    """A target node activated by at least one true input slot."""

    target_id: str
    priority: int
    via_node: str
    via_port: str
    wave: int


@dataclass(frozen=True)
class WaveTrace:
    """One merged ``/v1/systemone`` decision call (spec §3.8)."""

    wave: int
    questions: tuple[str, ...]
    duration_ms: float
    failed: bool = False
    error: str | None = None


@dataclass
class RoutingDecision:
    model: str
    target: TargetNode
    reasoning: str | None
    policy_path: str
    trace: list[dict]
    sticky_hit: bool
    selection_reason: str
    session_hash: str | None
    lease: Any
    candidates: tuple[Candidate, ...]
    waves: tuple[WaveTrace, ...]
    answers: Mapping[str, JevAnswer]
    fallback_reason: str | None
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
