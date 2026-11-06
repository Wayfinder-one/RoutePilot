"""SQLite 上的跨副本熔断 quarantine 存储（``breaker`` 表）。

所有方法都是 **阻塞的同步调用**，由 ``BreakerSyncWorker`` 通过
``asyncio.to_thread`` 在 worker 线程里执行（app 启动引导也在事件循环开始服务
流量之前完成）。熔断器本身不导入本模块，路由热路径也永远不会走到这里。

故障纪律：读取失败返回空表（等价于"共享 quarantine 暂不可见"），写入失败静默
丢弃 —— breaker 必须永不抛错、也永不阻塞请求。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


class BreakerStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)

    def load_all(self) -> dict[str, tuple[int, float | None]]:
        try:
            conn = self._connect()
        except sqlite3.Error:
            logger.warning("breaker quarantine unavailable", exc_info=True)
            return {}
        try:
            rows = conn.execute(
                "SELECT model, failures, opened_at FROM breaker").fetchall()
        except sqlite3.Error:
            logger.warning("breaker quarantine read failed", exc_info=True)
            return {}
        finally:
            conn.close()
        return {row[0]: (int(row[1]), row[2]) for row in rows}

    def write_failure(self, model: str, failures: int,
                      opened_at: float | None) -> None:
        try:
            conn = self._connect()
        except sqlite3.Error:
            logger.warning("breaker quarantine unavailable", exc_info=True)
            return
        try:
            with conn:
                if failures <= 0:
                    conn.execute("DELETE FROM breaker WHERE model = ?", (model,))
                else:
                    conn.execute(
                        "INSERT INTO breaker VALUES (?, ?, ?) "
                        "ON CONFLICT(model) DO UPDATE SET failures = excluded.failures, "
                        "opened_at = excluded.opened_at",
                        (model, failures, opened_at))
        except sqlite3.Error:
            logger.warning("breaker quarantine write failed model=%s", model,
                           exc_info=True)
        finally:
            conn.close()

    def clear(self, model: str) -> None:
        self.write_failure(model, 0, None)
