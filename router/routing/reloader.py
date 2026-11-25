from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class GraphReloadWorker:
    def __init__(self, app, poll_seconds: float = 2.0):
        self.app = app
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def poll_once(self) -> None:
        # 发布器只轮询图目录，不打开 SQLite：重载器不再需要数据库连接。
        try:
            await asyncio.to_thread(self.app.state.publisher.refresh_if_changed)
        except Exception:
            logger.exception("graph snapshot reload failed")

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
