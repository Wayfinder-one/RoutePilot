"""Decision event sink: bounded async queue, best-effort delivery, no replay copies."""
import json

import httpx
import pytest

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
