"""按模型的熔断器：纯内存状态机，不含任何数据库或文件 I/O。

跨副本的共享 quarantine 由两个独立组件承载：

* ``router.upstream.breaker_store.BreakerStore`` —— SQLite ``breaker`` 表；
* ``router.upstream.breaker_worker.BreakerSyncWorker`` —— 周期把本地状态推到
  store，并把其它副本的 quarantine 导入内存。

因此本模块不导入 ``sqlite3``，``is_open()`` 在状态锁内只做算术，永不等待磁盘；
路由热路径（``LoadBalancer.available``）只访问内存状态。

语义：

* ``failure()`` 在计数达到阈值后的 **每一次** 失败都重新计时
  （``self._opened_at[model] = time.monotonic()``）：半开探针失败必须重新关上
  电路。
* ``success()`` 清空该模型的失败计数、开启时间与在途探针标记。
* 半开是**严格单探针**：窗口结束后 ``is_open`` 只放行一个探针
  （记录在 ``_probe_at``），并发请求继续被阻断，直到 ``success()`` /
  ``failure()`` 结束探针；探针若一直不回报，``probe_timeout``（略高于 60s
  上游超时）后由下一个调用者接替。
* 跨副本导入（``import_state``）：导入对方记录的失败计数与 **原始**
  ``opened_at``（按本进程单调钟平移），半开窗口从电路真正打开的时刻计算，
  而不是从本副本发现它的时刻；早于 ``half_open_after`` 的陈旧行导入后立即
  放行探针（自愈）。
"""

from __future__ import annotations

import threading
import time


class CircuitBreaker:
    """Per-model circuit breaker; process-local failure counts only."""

    #: Max lifetime of a half-open probe; slightly above the 60s upstream
    #: request timeout so a wedged probe cannot lock the circuit forever.
    probe_timeout: float = 65.0

    def __init__(self, threshold: int = 3, half_open_after: float = 300.0):
        self.threshold = threshold
        self.half_open_after = half_open_after
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._probe_at: dict[str, float] = {}     # in-flight half-open probe
        self._lock = threading.Lock()

    def _admit_probe(self, model: str, now: float) -> bool:
        """Half-open gate: True if THIS request may proceed as the probe."""
        probe_at = self._probe_at.get(model)
        if probe_at is not None and now - probe_at < self.probe_timeout:
            return False
        self._probe_at[model] = now
        return True

    def is_open(self, model: str) -> bool:
        with self._lock:
            if self._failures.get(model, 0) >= self.threshold:
                now = time.monotonic()
                if now - self._opened_at.get(model, 0.0) < self.half_open_after:
                    return True
                # Window elapsed: admit one probe, keep blocking the rest so a
                # recovering upstream is not flooded the instant it wakes up.
                return not self._admit_probe(model, now)
            return False

    def failure(self, model: str) -> None:
        with self._lock:
            count = self._failures.get(model, 0) + 1
            self._failures[model] = count
            if count >= self.threshold:
                # Re-arm from NOW on every failure at/above the threshold: the
                # first crossing opens the circuit, and a failed half-open
                # probe closes it again, so the timer must restart for the
                # next half_open window.
                self._opened_at[model] = time.monotonic()
                self._probe_at.pop(model, None)

    def success(self, model: str) -> None:
        with self._lock:
            self._failures.pop(model, None)
            self._opened_at.pop(model, None)
            self._probe_at.pop(model, None)

    # ── 同步接口：只由 BreakerSyncWorker 在线程中调用 ────────────────────────
    def snapshot(self) -> dict[str, tuple[int, float]]:
        """Quarantine-eligible state: model -> (failures, opened_at monotonic).

        Only models at/above the threshold can be quarantined. The sync worker
        compares this mapping with what it last pushed and deletes the shared
        rows of models that disappeared from it (a recovered circuit). The
        timestamp stays on this process's monotonic clock; the worker
        translates it to wall-clock epoch when it writes.
        """
        with self._lock:
            return {
                model: (count, self._opened_at[model])
                for model, count in self._failures.items()
                if count >= self.threshold and model in self._opened_at
            }

    def import_state(self, model: str, failures: int,
                     opened_at: float | None) -> tuple[int, float] | None:
        """Latch another replica's quarantine without resetting its clock.

        ``opened_at`` is wall-clock epoch (the ``breaker`` table's format);
        it is translated onto the local monotonic clock preserving the elapsed
        window. Sub-threshold rows are not a quarantine and are ignored, and a
        local latch that is already stronger is never regressed. Returns the
        latched ``(failures, opened_at)`` for the caller's sync bookkeeping, or
        ``None`` when the state was ignored.
        """
        if failures < self.threshold:
            return None
        with self._lock:
            if self._failures.get(model, 0) >= failures:
                return None                      # never regress a newer local latch
            now = time.monotonic()
            wall = time.time() if opened_at is None else opened_at
            self._failures[model] = failures
            self._opened_at[model] = now - (time.time() - wall)
            return failures, self._opened_at[model]
