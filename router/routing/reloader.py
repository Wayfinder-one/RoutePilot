from __future__ import annotations

import asyncio
import logging

from router.store.db import connect

logger = logging.getLogger(__name__)


class PolicyReloadWorker:
    """定期复核策略目录指纹：仅指纹变化时重建并替换内存快照。

    指纹只覆盖策略文件与 current 指针（``PolicyRepo.fingerprint``），布局写入
    不触发快照替换；轮询失败仅记录日志，绝不中断服务或替换旧快照。
    """

    def __init__(self, app, poll_seconds: float = 2.0):
        self.app = app
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def poll_once(self) -> None:
        def refresh():
            conn = connect(self.app.state.settings.db_path)
            try:
                self.app.state.publisher.refresh_if_changed(conn)
            finally:
                conn.close()

        try:
            await asyncio.to_thread(refresh)
        except Exception:
            logger.exception("policy snapshot reload failed")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            await self.poll_once()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
