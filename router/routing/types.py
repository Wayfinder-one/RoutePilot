from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from router.jev.client import JevAnswer
from router.routing.policy.model import Target


class NoActivePolicyError(RuntimeError):
    """快照没有已启用的路由策略。"""


class RoutingMiss(RuntimeError):
    pass


class NoAvailableBackend(RuntimeError):
    def __init__(self, message, *, selection=None):
        super().__init__(message)
        self.selection = selection or {}


@dataclass(frozen=True)
class RoutingRequest:
    text: str
    session_id: str = ""


@dataclass
class RoutingDecision:
    """一次路由的完整结果：命中规则（或兜底原因）+ 调度产物。

    ``matched_rule`` 是命中规则的名称，``matched_rule_index`` 是它在
    ``rules`` 数组里的 0 起序号（兜底时两者均为 None，具体原因在
    ``fallback_reason``：``jev_error``/``no_rule_matched``）；
    ``jev_duration_ms`` 与 ``jev_questions`` 是本次决策唯一一次 JEV 调用的证据。
    """

    model: str
    target: Target
    reasoning: str | None
    policy_path: str
    trace: list[dict]
    sticky_hit: bool
    selection_reason: str
    session_hash: str | None
    lease: Any
    answers: Mapping[str, JevAnswer]
    fallback_reason: str | None
    matched_rule: str | None
    matched_rule_index: int | None = None
    jev_duration_ms: float | None = None
    jev_questions: tuple[str, ...] = ()

    @property
    def target_id(self) -> str:
        return self.target.id

    def release(self) -> None:
        # ``Lease.release`` is the single idempotent release point.
        self.lease.release()


def session_digest(session_id: str) -> str | None:
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()
