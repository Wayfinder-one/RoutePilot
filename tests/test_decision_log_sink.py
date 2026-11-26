"""Decision event sink: bounded async queue, best-effort delivery, no replay copies."""
import asyncio
import json
import time

import httpx
import pytest

import router.decision_log_sink as decision_log_sink
from router.core_config import LogSinkSettings
from router.decision_log_sink import DecisionEventSink, create_decision_sink
from tests.decision_fixtures import async_test, valid_event


def sink_settings(**overrides):
    values = {"type": "routepilot", "url": "http://console/logs", "timeout_ms": 1000}
    values.update(overrides)
    return LogSinkSettings(**values)


def event_handler(sent, *, status_code=204):
    async def handler(request):
        sent.append(request)
        return httpx.Response(status_code)
    return handler


@async_test
async def test_sink_failure_does_not_block_or_replay_event():
    sent = []
    async def handler(request):
        sent.append(request)
        raise httpx.ConnectError("down", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = create_decision_sink(
        LogSinkSettings(type="routepilot", url="http://console/logs", timeout_ms=1), client)
    await sink.start()
    sink.emit(valid_event())
    await sink.close()
    assert sink.dropped_events == 0
    assert sink.failed_events == 1
    assert len(sent) == 1
    assert not hasattr(sink, "recent_events")
    await client.aclose()


@async_test
async def test_sink_posts_one_complete_json_object_per_event_in_order():
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(), client)
    await sink.start()
    sink.emit(valid_event("e1"))
    sink.emit(valid_event("e2"))
    await sink.close()
    assert sink.dropped_events == 0
    assert sink.failed_events == 0
    payloads = [json.loads(request.content) for request in sent]
    assert [payload["id"] for payload in payloads] == ["e1", "e2"]
    assert payloads[0]["event_type"] == "routepilot.decision"
    assert payloads[0]["graph"]["id"] == "main"
    assert sent[0].url == "http://console/logs"
    assert not hasattr(sink, "recent_events")
    await client.aclose()


@async_test
async def test_disabled_sink_emit_returns_immediately_and_never_posts():
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(LogSinkSettings(type="disabled"), client)
    await sink.start()
    assert sink.emit(valid_event()) is None
    await sink.close()
    assert sent == []
    assert sink.dropped_events == 0
    assert sink.failed_events == 0
    await client.aclose()


@async_test
async def test_queue_is_bounded_and_full_queue_counts_dropped_events():
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(), client)
    assert isinstance(sink, DecisionEventSink)
    # 没有 start() 的 worker 消费，第 1001 次 emit 必须立即丢弃而不是阻塞。
    for index in range(DecisionEventSink.QUEUE_LIMIT + 1):
        sink.emit(valid_event(f"e{index}"))
    assert sink.dropped_events == 1
    await sink.close()
    assert len(sent) == DecisionEventSink.QUEUE_LIMIT
    assert sink.sent_events == DecisionEventSink.QUEUE_LIMIT
    assert sink.failed_events == 0
    await client.aclose()


@async_test
async def test_close_stops_accepting_new_events():
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(), client)
    await sink.start()
    sink.emit(valid_event("e1"))
    await sink.close()
    sink.emit(valid_event("e2"))
    await client.aclose()
    assert len(sent) == 1
    assert sink.dropped_events == 1


@async_test
async def test_upstream_error_status_only_counts_a_failure():
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        event_handler(sent, status_code=503)))
    sink = create_decision_sink(sink_settings(), client)
    await sink.start()
    sink.emit(valid_event())
    await sink.close()
    await client.aclose()
    assert sink.failed_events == 1
    assert sink.sent_events == 0
    assert sink.dropped_events == 0


@async_test
async def test_close_is_idempotent_and_drains_within_the_same_timeout_budget():
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(timeout_ms=1), client)
    await sink.start()
    sink.emit(valid_event("e1"))
    await sink.close()
    await sink.close()
    await client.aclose()
    assert len(sent) == 1
    assert sink.dropped_events == 0
    assert sink.failed_events == 0


def test_unknown_sink_type_is_rejected_instead_of_silently_disabled():
    with pytest.raises(ValueError, match="logging.type"):
        create_decision_sink(LogSinkSettings(type="victorialogs"), None)


# ---------------------------------------------------------------------------
# I8：队列只限条数不限字节 + close() 逐条等待无整体 deadline。
# 以下测试覆盖字节上限、单条事件上限与整体关闭 deadline（RED 于旧实现）。
# ---------------------------------------------------------------------------


def payload_size(event):
    """与 sink.emit() 相同的字节口径：JSON 编码后的 UTF-8 字节数。"""
    return len(json.dumps(event, ensure_ascii=False).encode())


@async_test
async def test_oversize_single_event_is_dropped_without_queueing(monkeypatch):
    monkeypatch.setattr(decision_log_sink, "EVENT_BYTES_LIMIT", 4096)
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(), client)
    assert sink.event_bytes_limit == 4096

    oversized = valid_event("oversized")
    oversized["graph"]["name"] = "x" * 8192
    size = payload_size(oversized)
    assert size > sink.event_bytes_limit
    sink.emit(oversized)
    # 单条超大事件根本不入队，只计丢弃与丢弃字节。
    assert sink.dropped_events == 1
    assert sink.dropped_oversize == 1
    assert sink.dropped_bytes == size
    assert sink.queued_events == 0
    assert sink.queued_bytes == 0

    # 边界内事件照常入队：丢弃只针对单条超大事件。
    normal = valid_event("normal")
    sink.emit(normal)
    assert sink.queued_events == 1
    assert sink.queued_bytes == payload_size(normal)

    await sink.close()
    assert [json.loads(request.content)["id"] for request in sent] == ["normal"]
    assert sink.sent_events == 1
    assert sink.queued_bytes == 0
    await client.aclose()


@async_test
async def test_queue_byte_budget_drops_events_instead_of_growing_memory(monkeypatch):
    size = payload_size(valid_event("e000"))
    # 字节预算只够 3 条：第 4 条起必须立即丢弃，不能只靠 1000 条计数上限。
    monkeypatch.setattr(decision_log_sink, "QUEUE_BYTES_LIMIT", 3 * size)
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(), client)
    assert sink.queue_bytes_limit == 3 * size

    for index in range(10):
        sink.emit(valid_event(f"e{index:03d}"))

    assert sink.queued_events == 3
    assert sink.queued_bytes == 3 * size
    assert sink.queued_bytes <= sink.queue_bytes_limit
    assert sink.queued_events < DecisionEventSink.QUEUE_LIMIT
    assert sink.dropped_events == 7
    assert sink.dropped_bytes == 7 * size
    assert sink.failed_events == 0

    await sink.close()
    assert len(sent) == 3
    assert sink.sent_events == 3
    # 出队时同步释放字节计数。
    assert sink.queued_events == 0
    assert sink.queued_bytes == 0
    await client.aclose()


@async_test
async def test_queued_bytes_is_released_when_the_worker_dequeues(monkeypatch):
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(event_handler(sent)))
    sink = create_decision_sink(sink_settings(), client)
    await sink.start()
    sink.emit(valid_event("q1"))
    assert sink.queued_bytes > 0
    for _ in range(200):
        if sink.sent_events == 1:
            break
        await asyncio.sleep(0.005)
    assert sink.sent_events == 1
    assert sink.queued_events == 0
    assert sink.queued_bytes == 0
    await sink.close()
    await client.aclose()


@async_test
async def test_close_has_one_bounded_deadline_for_hanging_posts(monkeypatch):
    """每条 POST 都挂死时，close() 必须在整体 deadline 内返回且不抛异常。"""
    release = asyncio.Event()
    received = []
    started = asyncio.Event()

    async def hanging_handler(request):
        received.append(request)
        started.set()
        await release.wait()  # 模拟上游一直不响应（MockTransport 不施加 per-POST 超时）
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(hanging_handler))
    monkeypatch.setattr(decision_log_sink, "CLOSE_TIMEOUT_SECONDS", 0.2)
    sink = create_decision_sink(sink_settings(timeout_ms=10_000), client)
    assert sink.close_timeout_seconds == 0.2

    await sink.start()
    for index in range(3):
        sink.emit(valid_event(f"h{index}"))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    began = time.monotonic()
    await sink.close()  # 旧实现会一直等悬挂的 POST（10s per-POST）
    elapsed = time.monotonic() - began

    assert elapsed >= 0.15, f"close() returned before its deadline: {elapsed:.3f}s"
    assert elapsed < 2.0, f"close() exceeded its deadline: {elapsed:.3f}s"
    # 正在 POST 的一条被取消 → failed；队列里剩余两条 → dropped。
    assert sink.failed_events == 1
    assert sink.dropped_events == 2
    assert sink.sent_events == 0
    assert sink.queued_events == 0
    assert sink.queued_bytes == 0
    assert sink._worker is None
    leftovers = [
        task for task in asyncio.all_tasks()
        if task.get_name() == "decision-event-sink" and not task.done()]
    assert leftovers == []

    release.set()
    await sink.close()  # 幂等：不再有 worker，也不再改动计数
    assert sink.dropped_events == 2
    assert sink.failed_events == 1
    await client.aclose()
