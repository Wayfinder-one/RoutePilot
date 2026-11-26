"""决策事件输出 sink：``logging.type=routepilot`` 的 best-effort 异步 HTTP 输出。

内核只输出决策事件，不保存最近日志（Global Constraint 4）。本模块是唯一输出
实现：``routepilot`` 把事件 POST 到配置的外部日志接口，``disabled`` 表示显式
关闭并丢弃事件。语义固定为 best-effort：

* ``emit()`` 同步且永不阻塞、永不抛异常：事件进入有界 ``asyncio.Queue``。队列
  同时受条数（``QUEUE_LIMIT``）与字节（``QUEUE_BYTES_LIMIT``）约束，单条事件
  另受 ``EVENT_BYTES_LIMIT`` 约束（事件内嵌整图，只限条数会放大成 GiB 级内存）。
  任何放不下的情况都只累加丢弃计数，不等待、不影响决策响应。
* 后台 worker 一次 POST 一个完整 JSON 事件对象，超时取
  ``settings.timeout_ms / 1000``；连接错误或非 2xx 只累加 ``failed_events``，
  不重试、不落盘、不把异常抛回调用方。
* ``close()`` 先停止接收新事件，再排空当前批次；整个关闭过程只有 **一个整体
  deadline**（``CLOSE_TIMEOUT_SECONDS``），因为 per-POST 超时是逐条预算，逐条
  等待会让关闭时间随队列长度线性放大（1000 条 × 1s ≈ 16 分钟）。到期后取消
  worker、把仍在队列里的事件计为 dropped，并正常返回；``close()`` 可重复调用。
* sink 不保留任何最近事件副本：最近 20 条只存在于外部日志服务的内存存储。

本模块属于内核：不读 SQLite、不依赖 FastAPI；HTTP 客户端由调用方注入。
"""
from __future__ import annotations

import asyncio
import json

#: 队列中的关闭哨兵：close() 在所有已接收事件之后入队，worker 收到即退出。
_STOP = object()

#: 队列内存的字节预算：事件内嵌整图，只限 1000 条仍可能占用数 GiB。
QUEUE_BYTES_LIMIT = 8 * 1024 * 1024
#: 单条事件字节上限：超过即整条丢弃，绝不入队。
EVENT_BYTES_LIMIT = 1 * 1024 * 1024
#: 关闭时排空队列的整体 deadline（不是每条 POST 的超时，避免关停时间线性放大）。
CLOSE_TIMEOUT_SECONDS = 5.0


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
        #: 上限只决定队列/关闭的硬边界，测试可替换为小值做确定性验证。
        self.queue_bytes_limit = QUEUE_BYTES_LIMIT
        self.event_bytes_limit = EVENT_BYTES_LIMIT
        self.close_timeout_seconds = CLOSE_TIMEOUT_SECONDS
        #: 当前仍在队列里的 payload 字节数（入队加、出队减）。
        self._queued_bytes = 0
        #: 运行观测计数：只增不减，不保留任何事件内容。
        self.dropped_events = 0
        self.dropped_bytes = 0
        self.dropped_oversize = 0
        self.dropped_invalid = 0
        self.failed_events = 0
        self.sent_events = 0

    @property
    def enabled(self) -> bool:
        return self.type == "routepilot"

    @property
    def queued_bytes(self) -> int:
        """Bytes currently sitting in the queue (0 for a disabled sink)."""
        return self._queued_bytes

    @property
    def queued_events(self) -> int:
        """Events currently sitting in the queue (0 for a disabled sink)."""
        return self._queue.qsize() if self._queue is not None else 0

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
        # 字节口径只算一次：入队与丢弃计数复用同一个序列化结果。
        try:
            size = len(json.dumps(event, ensure_ascii=False).encode())
        except Exception:
            # 不可序列化的事件同样只是 best-effort 丢弃，绝不影响决策响应。
            self.dropped_events += 1
            self.dropped_invalid += 1
            return
        if size > self.event_bytes_limit:
            self.dropped_events += 1
            self.dropped_oversize += 1
            self.dropped_bytes += size
            return
        if self._queued_bytes + size > self.queue_bytes_limit:
            self.dropped_events += 1
            self.dropped_bytes += size
            return
        try:
            self._queue.put_nowait((event, size))
        except asyncio.QueueFull:
            self.dropped_events += 1
            self.dropped_bytes += size
            return
        self._queued_bytes += size

    async def close(self) -> None:
        """Stop accepting events, then drain the batch already queued.

        整个排空过程共用一个 ``close_timeout_seconds`` deadline；到期后取消
        worker、把仍在队列中的事件计为 dropped，不向调用方抛异常。
        """
        if not self.enabled:
            self._closed = True
            return
        if self._closed:
            return
        self._closed = True
        queue, worker = self._queue, self._worker
        if queue is None:
            return
        if worker is None:
            # close() 前没 start()：仍要按语义排空已接收的批次。
            worker = asyncio.create_task(self._run(), name="decision-event-sink")
            self._worker = worker
        try:
            await asyncio.wait_for(
                self._drain_and_stop(queue, worker),
                timeout=self.close_timeout_seconds)
        except asyncio.TimeoutError:
            await self._abort(worker)
        finally:
            self._worker = None

    async def _drain_and_stop(self, queue, worker) -> None:
        try:
            queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            # 队列满时等待 worker 腾出一格；整体等待仍受 close() 的 deadline 约束。
            await queue.put(_STOP)
        await worker

    async def _abort(self, worker) -> None:
        """Cancel the worker and account every event still queued as dropped."""
        worker.cancel()
        # return_exceptions 只吸收 worker 自身的 CancelledError，不吞外层取消。
        await asyncio.gather(worker, return_exceptions=True)
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is _STOP:
                    continue
                _event, size = item
                self._queued_bytes -= size
                self.dropped_events += 1
                self.dropped_bytes += size
            finally:
                self._queue.task_done()
        self._queued_bytes = 0

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                event, size = item
                # 出队即释放字节配额，使配额反映“队列里还在排队的 payload”。
                self._queued_bytes -= size
                try:
                    await self._post(event)
                except asyncio.CancelledError:
                    # 关闭 deadline 触发：这一条没发出去，按失败计数后继续传播取消。
                    self.failed_events += 1
                    raise
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
