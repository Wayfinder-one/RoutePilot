"""外围决策日志：完整决策事件的 20 条易失内存存储。

内核只输出事件（Global Constraint 4）；外围接收端把通过校验的完整事件放进
``deque(maxlen=20)``（Global Constraint 5）：不写 SQLite、重启不恢复、第 21 条
淘汰最旧事件。回放由 :func:`router.decision_events.replay_payload` 读取同一条
事件里的 ``graph``/``trace``/``request``，因此历史回放不依赖当前图或任何快照表
（Global Constraint 6）。

并发约定：``add()`` 在事件循环的异步接收端执行，而 ``recent()``/``get()``/
``stats()`` 由 FastAPI 在同步路由的线程池里执行。第 21 条淘汰最旧事件时，
读线程的 ``deque`` 迭代会撞上写线程的 append/evict，CPython 抛
``RuntimeError: deque mutated during iteration``。因此所有读写都用同一把
``threading.RLock`` 串行化：每次读取在整个投影期间持锁，返回稳定快照；锁顺序
固定为“先校验、后加锁”，持锁时间只覆盖 deque 操作本身。

本模块属于外围：只依赖内核的事件校验函数，不访问 SQLite、FastAPI 或模型上游。
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

from router.decision_events import validate_decision_event

#: 与 Global Constraint 5 一致：只保留最近 20 条完整事件。
STORE_LIMIT = 20


class DecisionLogStore:
    """Recent decision events, newest last internally, newest first on read."""

    def __init__(self):
        self._events = deque(maxlen=STORE_LIMIT)
        #: 读写共用一把可重入锁：add() 与 recent()/get()/stats() 互为临界区。
        self._lock = threading.RLock()
        #: 内存日志的起始时刻（重启即重置）；``/overview`` 只统计保留的事件。
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def add(self, event):
        """Validate one complete event and retain it (the 21st evicts the oldest)."""
        validated = validate_decision_event(event)
        # 校验可能很慢（内嵌整图），放在临界区之外；只有 deque 变更需要持锁。
        with self._lock:
            self._events.append(validated)

    def recent(self, limit=20):
        """Newest-first complete events, at most ``limit`` of the retained ones."""
        # 先持锁拷贝快照，再在锁外做 reverse/slice：读取过程中 deque 不可能被写淘汰。
        with self._lock:
            snapshot = list(self._events)
        return list(reversed(snapshot))[:max(0, limit)]

    def get(self, event_id):
        """The retained event with ``event_id``, or ``None`` (evicted/unknown)."""
        with self._lock:
            for item in self._events:
                if item["id"] == event_id:
                    return item
        return None

    def stats(self):
        with self._lock:
            ring_size = len(self._events)
        return {
            "started_at": self.started_at,
            "ring_size": ring_size,
            "ring_limit": STORE_LIMIT,
        }
