"""Real two-service standalone lifecycle: peripheral ↔ kernel over ASGI HTTP.

Task 8 acceptance (design §4.1 / §10.2).  Both applications are created with
their production factories and run their production lifespans in one event loop;
the peripheral's ``CoreClient`` reaches the kernel through an ASGI transport and
the kernel's real decision-event sink posts back into the peripheral receiver
through an ASGI transport as well.  No decision/routing code is stubbed: only the
model upstream and the JEV service are replaced by HTTP mocks.

Covered:
* ``/admin/trees`` create/enable through the peripheral proxy → graph is written
  by the kernel and a decision immediately uses it;
* standalone ``/v1/chat/completions`` auto → kernel decision → peripheral
  upstream with the rewritten real model and the peripheral Provider Key
  (JSON and SSE); the kernel never calls the model upstream;
* the kernel posts one complete event to ``/internal/v1/decision-logs``;
  ``/admin/requests/recent`` and ``/admin/requests/{id}/replay`` read that event;
* the 21st event evicts the oldest (and its replay becomes 404);
* editing / deleting the current graph never changes an old replay;
* a fresh peripheral app starts with an empty memory log (restart);
* a failing event sink never breaks a decision.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

from router.api.app import create_app
from router.api.core_app import create_core_app
from router.core_config import CoreSettings, LogSinkSettings
from router.store.db import connect, migrate
from tests.decision_fixtures import async_test
from tests.routing_fixtures import (
    DEFAULT_PROVIDER_BASE_URL,
    remove_graph,
    seed_execution_catalog,
    test_settings,
    tree_dict,
)

PROVIDER_SECRET = "peripheral-secret"
UPSTREAM_ORIGIN = "http://upstream.test"
PERIPHERAL_ORIGIN = "http://peripheral.test"
EVENT_SINK_HOST = "peripheral.internal"


# --------------------------------------------------------------------------- #
# graph + upstream fixtures
# --------------------------------------------------------------------------- #
def direct_graph(model="fast"):
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "target", "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-target", "source": "start", "source_port": "out",
            "target": "target", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "target",
    }


def tree_create_payload(tree_id="main", *, model="fast", upstream="real-fast"):
    """A self-contained enabled-graph-create body (lifecycle excludes enabled)."""
    document = tree_dict(tree_id, root=direct_graph(model))
    document["backends"]["models"][0]["upstream"] = upstream
    document.pop("enabled", None)
    document.pop("default", None)
    return document


def seed_catalog(settings):
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        seed_execution_catalog(
            conn, key=PROVIDER_SECRET, base_url=UPSTREAM_ORIGIN + "/v1",
            models=[("fast", "real-fast", "{}")])
    finally:
        conn.close()


JSON_COMPLETION = {
    "id": "cmpl-e2e", "object": "chat.completion", "model": None,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "e2e-ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

SSE_BODY = (
    b'data: {"choices":[{"delta":{"content":"e2e-sse"}}]}\n\n'
    b"data: [DONE]\n\n"
)


def upstream_transport(calls):
    """Mock model upstream: records every execution request the peripheral makes."""

    class _Sse(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield SSE_BODY

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append({
            "url": str(request.url),
            "authorization": request.headers.get("authorization"),
            "model": body.get("model"),
        })
        if body.get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_Sse())
        return httpx.Response(200, json={**JSON_COMPLETION, "model": body.get("model")})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# the two-service stack
# --------------------------------------------------------------------------- #
def _kernel_http(upstream_calls, fail_sink):
    """Kernel's real HTTP client: JEV/upstream are forbidden, sink → peripheral."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == EVENT_SINK_HOST:
            if fail_sink:
                raise httpx.ConnectError("peripheral log sink is down", request=request)
            return await httpx.ASGITransport(app=_STACK.peripheral).handle_async_request(request)
        raise AssertionError(
            f"kernel made an external (non-sink) request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


class _StackHolder:
    peripheral = None


_STACK = _StackHolder()


@asynccontextmanager
async def two_service_stack(tmp_path, *, sink_fails=False):
    """Kernel + peripheral with both production lifespans running in one loop."""
    settings = test_settings(tmp_path)
    seed_catalog(settings)
    calls: list[dict] = []
    kernel = create_core_app(
        CoreSettings(
            graph_dir=settings.graph_dir,
            logging=LogSinkSettings(
                type="routepilot",
                url=f"http://{EVENT_SINK_HOST}/internal/v1/decision-logs",
                timeout_ms=2000,
            ),
        ),
        transport=_kernel_http(calls, sink_fails),
    )
    peripheral = create_app(
        settings, transport=upstream_transport(calls),
        core_transport=httpx.ASGITransport(app=kernel))
    _STACK.peripheral = peripheral
    try:
        async with kernel.router.lifespan_context(kernel):
            async with peripheral.router.lifespan_context(peripheral):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=peripheral),
                        base_url=PERIPHERAL_ORIGIN) as client:
                    yield SimpleNamespace(
                        settings=settings, kernel=kernel,
                        peripheral=peripheral, client=client, calls=calls)
    finally:
        _STACK.peripheral = None


async def wait_for_events(peripheral, count, *, timeout=5.0):
    """Wait until the kernel's real sink has delivered ``count`` events."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(peripheral.state.decision_logs.recent(20)) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"decision events did not arrive: wanted {count}, got "
        f"{len(peripheral.state.decision_logs.recent(20))}")


async def create_and_enable(client, tree_id="main", **overrides):
    response = await client.post(
        "/admin/trees", json=tree_create_payload(tree_id, **overrides))
    assert response.status_code == 201, response.text
    enabled = await client.post(f"/admin/trees/{tree_id}/enable")
    assert enabled.status_code == 200, enabled.text
    return enabled.json()


async def auto_completion(client, text="你好", *, stream=False):
    return await client.post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": text}],
        "stream": stream,
    })


# --------------------------------------------------------------------------- #
# the real lifecycle
# --------------------------------------------------------------------------- #
@async_test
async def test_peripheral_proxy_decision_execution_and_event_replay(tmp_path):
    async with two_service_stack(tmp_path) as stack:
        client = stack.client
        # 1. 图管理经外围代理写入内核，随后启用。
        created = await create_and_enable(client)
        assert created["trees"][0]["id"] == "main"
        assert (Path(stack.settings.graph_dir) / "graphs" / "main.json").is_file()
        listed = await client.get("/admin/trees")
        assert [tree["id"] for tree in listed.json()["trees"]] == ["main"]

        # 2. standalone JSON 数据面：内核决策 → 外围真实调用模型上游。
        response = await auto_completion(client)
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "e2e-ok"
        assert response.headers["x-sr-model"] == "real-fast"
        assert response.headers["x-sr-selection"] == "target"
        assert response.headers["x-sr-tree-id"] == "main"
        assert response.headers["x-sr-graph-selection"] == "default"
        assert [call["model"] for call in stack.calls] == ["real-fast"]
        assert stack.calls[0]["authorization"] == f"Bearer {PROVIDER_SECRET}"
        assert stack.calls[0]["url"] == UPSTREAM_ORIGIN + "/v1/chat/completions"

        # 3. 内核把完整事件投递到外围接收口，最近列表与回放都来自这条事件。
        await wait_for_events(stack.peripheral, 1)
        recent = (await client.get("/admin/requests/recent")).json()["requests"]
        assert len(recent) == 1
        decision_id = recent[0]["id"]
        assert recent[0]["model"] == "fast"
        assert recent[0]["upstream_model"] == "real-fast"
        assert recent[0]["replay_url"] == (
            f"/console/routing.html?request={decision_id}")
        replay = await client.get(f"/admin/requests/{decision_id}/replay")
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert replay_body["request_id"] == decision_id
        assert replay_body["request"]["text"] == "你好"
        assert replay_body["tree"]["id"] == "main"
        assert any(event["event"] == "final" for event in replay_body["trace"])
        assert "graph_snapshot_id" not in replay_body
        assert "snapshot_revision" not in replay_body["request"]
        old_tree = replay_body["tree"]

        # 4. SSE 同样由外围上游返回，并产生第二条事件。
        streamed = await auto_completion(client, "流式", stream=True)
        assert streamed.status_code == 200, streamed.text
        assert streamed.headers["content-type"].startswith("text/event-stream")
        assert b"e2e-sse" in streamed.content
        assert b"[DONE]" in streamed.content
        assert [call["model"] for call in stack.calls] == ["real-fast", "real-fast"]
        await wait_for_events(stack.peripheral, 2)

        # 5. 修改当前图：历史回放仍是事件内嵌的旧图。
        current = (await client.get("/admin/trees/main")).json()["tree"]
        updated = await client.put("/admin/trees/main", json={
            "name": "改过的图", "base_document_sha256": current["document_sha256"],
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["tree"]["name"] == "改过的图"
        after_edit = (await client.get(
            f"/admin/requests/{decision_id}/replay")).json()
        assert after_edit["tree"] == old_tree

        # 6. 删除当前图：内存中的历史回放仍可读，新请求按内核语义 503。
        remove_graph(stack.kernel, "main")
        after_delete = (await client.get(
            f"/admin/requests/{decision_id}/replay")).json()
        assert after_delete["tree"] == old_tree
        no_graph = await auto_completion(client, "没有图")
        assert no_graph.status_code == 503
        assert no_graph.json() == {"error": {"message": "no_active_tree"}}

        # 7. 外围重启（同一内核）：内存日志为空，旧回放 404。
        restarted = create_app(
            stack.settings, transport=upstream_transport(stack.calls),
            core_transport=httpx.ASGITransport(app=stack.kernel))
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=restarted),
                    base_url=PERIPHERAL_ORIGIN) as restarted_client:
                assert (await restarted_client.get(
                    "/admin/requests/recent")).json() == {"requests": []}
                assert (await restarted_client.get(
                    f"/admin/requests/{decision_id}/replay")).status_code == 404


@async_test
async def test_twenty_first_event_evicts_the_oldest(tmp_path):
    async with two_service_stack(tmp_path) as stack:
        client = stack.client
        await create_and_enable(client)
        await auto_completion(client, "第一条")
        await wait_for_events(stack.peripheral, 1)
        first_id = (await client.get(
            "/admin/requests/recent")).json()["requests"][0]["id"]

        for index in range(20):
            response = await auto_completion(client, f"第 {index + 2} 条")
            assert response.status_code == 200, response.text
        await wait_for_events(stack.peripheral, 20)

        recent = (await client.get("/admin/requests/recent")).json()["requests"]
        assert len(recent) == 20
        assert first_id not in [item["id"] for item in recent]
        assert (await client.get(
            f"/admin/requests/{first_id}/replay")).status_code == 404
        assert len(stack.peripheral.state.decision_logs.recent(20)) == 20


@async_test
async def test_failing_event_sink_does_not_break_a_decision(tmp_path):
    async with two_service_stack(tmp_path, sink_fails=True) as stack:
        client = stack.client
        await create_and_enable(client)
        response = await auto_completion(client, "日志服务不可达")
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "e2e-ok"
        assert [call["model"] for call in stack.calls] == ["real-fast"]
        # best-effort：决策正常，外围内存日志保持为空。
        assert stack.peripheral.state.decision_logs.recent(20) == []
        assert (await client.get(
            "/admin/requests/recent")).json() == {"requests": []}
