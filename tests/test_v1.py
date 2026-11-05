import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from router.api.requestlog import RequestLogger
from router.api.routes_v1 import router
from router.api.upstream import Backend, CircuitBreaker
from router.config import Settings
from router.jev.client import JevClient
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.routing.affinity import SessionAffinity
from router.routing.conditions import TimePeriod
from router.routing.engine import RoutingEngine
from router.routing.types import RoutingMiss
from router.routing.model import tree_from_dict
from router.routing.scheduler import LoadBalancer
from router.routing.snapshot import build_snapshot
from router.store.db import connect, migrate
from tests.routing_fixtures import target_dict, tree_dict


CAPABILITIES = {"fast": "简单请求", "strong-a": "复杂请求", "strong-b": "复杂请求", "standard": "通用请求"}
JEV_SETTINGS = JevSettings("http://jev.test", "NeoHorse-Jev-4B", "", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE)


def make_app(tmp_path, handler, *, raw_tree=None, active=True):
    path = str(tmp_path / "v3.db")
    conn = connect(path)
    migrate(conn)
    conn.close()
    app = FastAPI()
    app.state.settings = Settings(db_path=path)
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.breaker = CircuitBreaker()
    app.state.balancer = LoadBalancer(app.state.breaker)
    app.state.affinity = SessionAffinity()
    app.state.engine = RoutingEngine(JevClient(app.state.http), app.state.affinity, app.state.balancer)
    app.state.requestlog = RequestLogger(app.state.settings)
    backends = {model: Backend(url=f"http://{model}.test/v1/chat/completions", headers={}, upstream_name=model) for model in CAPABILITIES}
    tree = tree_from_dict(raw_tree or tree_dict()) if active else None
    app.state.snapshot = build_snapshot(tree=tree, backends=backends, time_periods=[TimePeriod("夜间", (0, 1, 2, 3, 4, 5, 6), "18:00", "09:00")], jev=JEV_SETTINGS, capabilities=CAPABILITIES)
    app.include_router(router, prefix="/v1")
    return app


def payload(text="请求", *, stream=False):
    return {"model": "ignored", "messages": [{"role": "user", "content": text}], "stream": stream}


def test_concurrent_sessions_use_least_inflight_then_stick_to_their_models(tmp_path):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        jev_calls = []
        upstream_calls = []
        blocked = [True]

        async def handler(request):
            if request.url.host == "jev.test":
                jev_calls.append(request)
                return httpx.Response(200, json={"answers": {"route": {"probabilities": {"轻量模型": 0.1, "增强模型": 0.9}}}, "usage": {"input_tokens": 20}})
            model = json.loads(request.content)["model"]
            upstream_calls.append(model)
            if model == "strong-a" and blocked[0]:
                blocked[0] = False
                started.set()
                await release.wait()
            return httpx.Response(200, json={"model": model, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        app = make_app(tmp_path, handler)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router.test") as client:
            first_task = asyncio.create_task(client.post("/v1/chat/completions", json=payload("first"), headers={"x-sr-session-id": "first-session"}))
            await asyncio.wait_for(started.wait(), 1)
            second = await client.post("/v1/chat/completions", json=payload("second"), headers={"x-sr-session-id": "second-session"})
            third = await client.post("/v1/chat/completions", json=payload("third"), headers={"x-sr-session-id": "second-session"})
            release.set()
            first = await first_task
        assert first.status_code == second.status_code == third.status_code == 200
        assert upstream_calls == ["strong-a", "strong-b", "strong-b"]
        assert len(jev_calls) == 2
        assert app.state.balancer.inflight("strong-a") == 0
        assert app.state.balancer.inflight("strong-b") == 0
        await app.state.requestlog.flush()
        conn = connect(app.state.settings.db_path)
        stored = [dict(row) for row in conn.execute("SELECT * FROM request_logs")]
        conn.close()
        assert len(stored) == 3
        third_record = app.state.requestlog.recent_requests(1)[0]
        assert third_record["model"] == "strong-b"
        assert third_record["sticky_hit"] == 1
        assert third_record["selection_reason"] == "sticky"
        assert third_record["outcome_state"] == "completed"
        assert third_record["route_label"]["en"] == "Sticky route"
        assert third_record["trace"][-1]["event"] == "request_outcome"
        assert third_record["trace"][-1]["tone"] == "hit"
        assert [item[0]["en"] for item in third_record["metrics"]] == ["Upstream", "Routing", "Total"]

    asyncio.run(scenario())


def test_jev_failure_uses_configured_fallback(tmp_path):
    async def handler(request):
        if request.url.host == "jev.test":
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"model": json.loads(request.content)["model"], "choices": []})

    app = make_app(tmp_path, handler)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 200
        assert response.json()["model"] == "standard"
        record = app.state.requestlog.recent_requests(1)[0]
        assert record["selection_reason"] == "fallback"
        assert record["fallback_reason"] == "jev_error"
        assert app.state.balancer.inflight("standard") == 0

    asyncio.run(scenario())


def test_no_active_tree_fails_before_any_http_call(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    app = make_app(tmp_path, handler, active=False)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_active_tree"
        assert calls == []
        records = app.state.requestlog.recent_requests()
        assert len(records) == 1
        assert records[0]["outcome_state"] == "routing_error"
        assert records[0]["trace"][-1]["event"] == "routing_error"
        assert records[0]["trace"][-1]["facts"][0][1] == "no_active_tree"
        await app.state.requestlog.flush()
        conn = connect(app.state.settings.db_path)
        saved = conn.execute("SELECT outcome_state, error_class FROM request_logs").fetchone()
        conn.close()
        assert tuple(saved) == ("routing_error", "no_active_tree")

    asyncio.run(scenario())


def test_routing_failure_retains_partial_trace_without_exposing_exception(tmp_path):
    app = make_app(tmp_path, lambda request: httpx.Response(500))

    class FailingEngine:
        async def route(self, snapshot, routing_request, *, trace_sink):
            trace_sink.append({"event": "branch_selected", "phase": "condition",
                               "label": {"zh": "条件", "en": "Condition"},
                               "cell": "A", "summary": "A", "facts": [], "tone": "hit"})
            raise RoutingMiss("private internal detail")

    app.state.engine = FailingEngine()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", json=payload("secret request"))
        assert response.status_code == 503
        record = app.state.requestlog.recent_requests(1)[0]
        assert record["outcome_state"] == "routing_error"
        assert [event["event"] for event in record["trace"]] == ["branch_selected", "routing_error"]
        assert "private internal detail" not in str(record)
        assert record["trace"][-1]["tone"] == "bad"

    asyncio.run(scenario())


def test_stream_completion_releases_lease_and_records_usage(tmp_path):
    async def handler(request):
        if request.url.host == "jev.test":
            raise AssertionError("direct target must skip JEV")
        body = b'data: {"choices":[]}\n\ndata: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\ndata: [DONE]\n\n'

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

    app = make_app(tmp_path, handler, raw_tree=tree_dict(root=target_dict("direct", ("standard",))))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", json=payload(stream=True))
        assert response.status_code == 200
        assert b"[DONE]" in response.content
        assert app.state.balancer.inflight("standard") == 0
        record = app.state.requestlog.recent_requests(1)[0]
        assert record["outcome_state"] == "completed"
        assert record["total_tokens"] == 3
        assert len(app.state.requestlog.recent_requests()) == 1
        assert [event["event"] for event in record["trace"]].count("request_outcome") == 1

    asyncio.run(scenario())


def test_upstream_5xx_maps_to_502_and_releases_lease(tmp_path):
    app = make_app(tmp_path, lambda request: httpx.Response(503, json={"error": "down"}), raw_tree=tree_dict(root=target_dict("direct", ("standard",))))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 502
        assert app.state.balancer.inflight("standard") == 0
        assert app.state.requestlog.recent_requests(1)[0]["outcome_state"] == "upstream_error"
        event = app.state.requestlog.recent_requests(1)[0]["trace"][-1]
        assert event["event"] == "request_outcome"
        assert event["tone"] == "bad"

    asyncio.run(scenario())


def test_cancellation_during_upstream_records_terminal_state_and_releases(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        app = make_app(tmp_path, handler, raw_tree=tree_dict(root=target_dict("direct", ("standard",))))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            task = asyncio.create_task(client.post("/v1/chat/completions", json=payload()))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert app.state.balancer.inflight("standard") == 0
        assert app.state.requestlog.recent_requests(1)[0]["outcome_state"] == "cancelled"

    asyncio.run(scenario())


def test_stream_disconnect_releases_lease(tmp_path):
    from router.api.routes_v1 import _relay

    class Response:
        status_code = 200

        def __init__(self):
            self.closed = False

        async def aiter_raw(self):
            yield b"data: first\n\n"
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    class Outcome:
        def finish(self, **kwargs):
            self.kwargs = kwargs

    async def scenario():
        response = Response()
        outcome = Outcome()
        released = []
        backend = Backend(url="", headers={}, upstream_name="standard")
        generator = _relay(response, outcome=outcome, request=None, backend=backend,
                           upstream_started=0.0, upstream_ms=1.0,
                           on_release=lambda: released.append(True))
        assert await generator.__anext__() == b"data: first\n\n"
        await generator.aclose()
        assert response.closed is True
        assert released == [True]
        assert outcome.kwargs["outcome_state"] == "client_disconnected"

    asyncio.run(scenario())


def test_invalid_json_is_a_client_error(tmp_path):
    app = make_app(tmp_path, lambda request: httpx.Response(500))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/chat/completions", content="{", headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "invalid_json"

    asyncio.run(scenario())
