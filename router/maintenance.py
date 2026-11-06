"""后台维护任务：过期会话清理。

``MaintenanceWorker`` 每小时运行一次；数据库访问始终在工作线程里
（``asyncio.to_thread``），事件循环不会被全表清理阻塞。登录/鉴权热路径不做
全表清理 —— 单条过期会话仍由 ``SessionRepo.user_for_token`` 就地删除。
"""

from __future__ import annotations

import asyncio
import logging

from router.store.db import connect
from router.store.repositories import SessionRepo

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600.0


class MaintenanceWorker:
    def __init__(self, db_path: str,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS):
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
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
            try:
                await self.run_once()
            except Exception:
                logger.exception("expired session purge failed")

    async def run_once(self) -> None:
        await asyncio.to_thread(self._purge_expired)

    def _purge_expired(self) -> None:
        """Blocking purge — worker thread only."""
        conn = connect(self.db_path)
        try:
            SessionRepo(conn).purge_expired()
        finally:
            conn.close()
