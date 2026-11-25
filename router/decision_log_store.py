"""外围决策日志：完整决策事件的 20 条易失内存存储。

内核只输出事件（Global Constraint 4）；外围接收端把通过校验的完整事件放进
``deque(maxlen=20)``（Global Constraint 5）：不写 SQLite、重启不恢复、第 21 条
淘汰最旧事件。回放由 :func:`router.decision_events.replay_payload` 读取同一条
事件里的 ``graph``/``trace``/``request``，因此历史回放不依赖当前图或任何快照表
（Global Constraint 6）。

本模块属于外围：只依赖内核的事件校验函数，不访问 SQLite、FastAPI 或模型上游。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from router.decision_events import validate_decision_event

#: 与 Global Constraint 5 一致：只保留最近 20 条完整事件。
STORE_LIMIT = 20


class DecisionLogStore:
    """Recent decision events, newest last internally, newest first on read."""

    def __init__(self):
        self._events = deque(maxlen=STORE_LIMIT)
        #: 内存日志的起始时刻（重启即重置）；``/overview`` 只统计保留的事件。
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def add(self, event):
        """Validate one complete event and retain it (the 21st evicts the oldest)."""
        self._events.append(validate_decision_event(event))

    def recent(self, limit=20):
        """Newest-first complete events, at most ``limit`` of the retained ones."""
        return list(reversed(self._events))[:max(0, limit)]

    def get(self, event_id):
        """The retained event with ``event_id``, or ``None`` (evicted/unknown)."""
        return next((item for item in self._events if item["id"] == event_id), None)

    def stats(self):
        return {
            "started_at": self.started_at,
            "ring_size": len(self._events),
            "ring_limit": STORE_LIMIT,
        }
