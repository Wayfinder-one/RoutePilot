"""熔断状态的跨副本同步 worker：内存状态 ↔ SQLite 共享 quarantine。

每次同步（一个 worker 线程里完成，事件循环只 ``await``）：

1. 读取共享表的全部行；
2. 把本副本 **变化过的** quarantine 状态批量写入（未变化的模型不重复写，
   避免两个副本计数不同时每轮互相覆盖）；
3. 本副本已经恢复（``success()``）的模型删除共享行；
4. 把其它副本拥有、本副本没有的 quarantine 导入内存（``import_state``，
   保留对方原始的 ``opened_at``，半开窗口从电路真正打开时算起）。

``start()`` 会先 await 一次同步再启动后台循环，因此 app 启动阶段就加载完共享
quarantine，之后才接收流量。默认间隔沿用拆分前 ``db_sync_seconds`` 的 30s。

可见性上界是 **双向** 的：推送与导入都搭同一轮 tick，所以"本副本打开 → 其它副本
可见"与"本副本恢复 → 其它副本解除"端到端各自最坏都要两轮，约 2×
``interval_seconds``。
"""

from __future__ import annotations

import asyncio
import logging
import time

from router.upstream.breaker import CircuitBreaker
from router.upstream.breaker_store import BreakerStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0


class BreakerSyncWorker:
    def __init__(self, breaker: CircuitBreaker, store: BreakerStore,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS):
        self.breaker = breaker
        self.store = store
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        # Local quarantine state last seen by the store (pushed or imported).
        # Comparing against this instead of the table keeps a replica that has
        # fewer failures from rewriting the row on every tick.
        self._written: dict[str, tuple[int, float]] = {}

    async def start(self) -> None:
        """Load the shared quarantine first, then poll in the background."""
        await self.sync_once()
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.sync_once()

    async def sync_once(self) -> None:
        try:
            await asyncio.to_thread(self._sync)
        except Exception:
            logger.exception("breaker quarantine sync failed")

    def _sync(self) -> None:
        """Blocking sync — worker thread only."""
        now_mono = time.monotonic()
        now_wall = time.time()
        shared = self.store.load_all()
        local = self.breaker.snapshot()

        for model, state in local.items():
            if self._written.get(model) == state:
                continue
            self.store.write_failure(
                model, state[0], now_wall - (now_mono - state[1]))
            self._written[model] = state

        # Models that recovered (``success()``) since the last tick. ``shared``
        # was read BEFORE these deletes, so its rows are stale precisely for
        # these models; re-importing them below would undo the recovery. That
        # is not hypothetical: a long streaming request calls ``success()``
        # after the circuit opened, and a re-imported fresh ``opened_at`` would
        # hard-block the model again for up to ``half_open_after``.
        cleared = {model for model in self._written if model not in local}
        for model in cleared:
            self.store.clear(model)
            self._written.pop(model, None)

        for model, (failures, opened_at) in shared.items():
            if model in local or model in cleared:
                continue                      # local state is already latched
            imported = self.breaker.import_state(model, failures, opened_at)
            if imported is not None:
                self._written[model] = imported
