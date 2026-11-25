"""决策事件输出 sink：``logging.type=routepilot`` 的 best-effort 异步 HTTP 输出。

内核只输出决策事件，不保存最近日志（Global Constraint 4）。本模块是唯一输出
实现：``routepilot`` 把事件 POST 到配置的外部日志接口，``disabled`` 表示显式
关闭并丢弃事件。语义固定为 best-effort：

* ``emit()`` 同步且永不阻塞：事件进入有界 ``asyncio.Queue(maxsize=1000)``；
  队列满只累加 ``dropped_events``，不等待、不抛异常、不影响决策响应。
* 后台 worker 一次 POST 一个完整 JSON 事件对象，超时取
  ``settings.timeout_ms / 1000``；连接错误或非 2xx 只累加 ``failed_events``，
  不重试、不落盘、不把异常抛回调用方。
* ``close()`` 先停止接收新事件，再排空当前批次（每批 POST 仍受同一超时
  预算约束），可重复调用。
* sink 不保留任何最近事件副本：最近 20 条只存在于外部日志服务的内存存储。

本模块属于内核：不读 SQLite、不依赖 FastAPI；HTTP 客户端由调用方注入。
"""
from __future__ import annotations

import asyncio

#: 队列中的关闭哨兵：close() 在所有已接收事件之后入队，worker 收到即退出。
_STOP = object()


class DecisionEventSink:
    """Bounded, non-blocking, best-effort decision event sink.

    ``type="disabled"`` 的 sink 不建队列、不启动 worker，``emit()`` 直接返回。
    Failed/dropped 只作为运行观测计数，不构成内核日志系统，也不回放事件。
    """

    QUEUE_LIMIT = 1000

    def __init__(self, settings, http):
        if settings.type not in {"routepilot", "disabled"}:
            raise ValueError(
                "logging.type must be routepilot or disabled, "
                f"got {settings.type!r}")
        self.type = settings.type
        self.url = settings.url
        self.timeout_seconds = settings.timeout_ms / 1000.0
        self._http = http
        self._queue = (
            asyncio.Queue(maxsize=self.QUEUE_LIMIT) if self.enabled else None)
        self._worker: asyncio.Task | None = None
        self._closed = False
        #: 运行观测计数：只增不减，不保留任何事件内容。
        self.dropped_events = 0
        self.failed_events = 0
        self.sent_events = 0

    @property
    def enabled(self) -> bool:
        return self.type == "routepilot"

    async def start(self) -> None:
        """Start the delivery worker (idempotent; disabled sinks stay inert)."""
        if not self.enabled or self._worker is not None or self._closed:
            return
        self._worker = asyncio.create_task(self._run(), name="decision-event-sink")

    def emit(self, event: dict) -> None:
        """Enqueue one event without ever blocking the decision path."""
        if not self.enabled:
            return
        if self._closed or self._queue is None:
            self.dropped_events += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1

    async def close(self) -> None:
        """Stop accepting events, then drain the batch already queued."""
        if not self.enabled:
            self._closed = True
            return
        if not self._closed:
            self._closed = True
            if self._worker is None:
                # close() 前没 start()：仍要按语义排空已接收的批次。
                self._worker = asyncio.create_task(
                    self._run(), name="decision-event-sink")
            await self._queue.put(_STOP)
        worker, self._worker = self._worker, None
        if worker is not None:
            await worker

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is _STOP:
                    return
                await self._post(event)
            finally:
                self._queue.task_done()

    async def _post(self, event: dict) -> None:
        try:
            response = await self._http.post(
                self.url, json=event, timeout=self.timeout_seconds)
        except Exception:
            # best-effort：任何传输失败都只计数，不重试、不向上抛。
            self.failed_events += 1
            return
        if response.status_code >= 300:
            self.failed_events += 1
        else:
            self.sent_events += 1


def create_decision_sink(settings, http):
    """Build the sink for the configured ``logging.type`` (routepilot/disabled)."""
    return DecisionEventSink(settings, http)
