"""Peripheral execution data plane: kernel decision → peripheral upstream call.

Task 8 contract (design §4.1 / §6.1):

* the peripheral resolves the kernel decision's **logical model** in its own
  SQLite execution catalog, keeps the peripheral Provider Key in the upstream
  headers and rewrites only the real ``model`` id;
* a catalog missing the logical model / naming another provider / pointing at
  another ``base_url`` is a 503 ``execution_catalog_mismatch`` with no upstream
  call and no substitute credential;
* the kernel itself performs no model-upstream call: the mock kernel transport
  in these tests fails the test if any ``/chat/completions`` request reaches it;
* fixed-model admin test chat stays peripheral-local and never calls the kernel.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.request_outcome import DiscardOutcome
from router.store.db import connect, migrate
from router.upstream.execution import ExecutionCatalogMismatch, backend_for_decision
from tests.decision_fixtures import decision_payload
from tests.routing_fixtures import (
    install_graph,
    recording_core_app,
    seed_execution_catalog,
    split_app,
    test_settings,
    tree_dict,
)

SECRET = "peripheral-secret"


def catalog_conn(tmp_path):
    """A migrated peripheral SQLite catalog: provider ``local`` + model ``fast``."""
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_execution_catalog(
        conn, key=SECRET,
        models=[("fast", "real-fast", "{}")],
    )
    return conn


# --------------------------------------------------------------------------- #
# backend_for_decision: catalog lookup + mismatch rejection
# --------------------------------------------------------------------------- #
def test_auto_backend_uses_peripheral_key_and_kernel_model(tmp_path):
    conn = catalog_conn(tmp_path)
    try:
        payload = decision_payload(
            logical_model="fast", model="real-fast",
            provider={"name": "local", "base_url": "http://upstream.test/v1",
                      "reasoning_format": "openai"})
        backend = backend_for_decision(payload, conn)
    finally:
        conn.close()
    assert backend.upstream_name == "real-fast"
    assert backend.url == "http://upstream.test/v1/chat/completions"
    assert backend.headers == {"Authorization": f"Bearer {SECRET}"}
    assert backend.provider == "local"


@pytest.mark.parametrize("override", [
    # catalog has no such logical model
    {"logical_model": "missing"},
    # catalog model belongs to another provider than the decision says
    {"provider": {"name": "gateway", "base_url": "http://upstream.test/v1",
                  "reasoning_format": "openai"}},
    # catalog provider points at a different upstream than the decision
    {"provider": {"name": "local", "base_url": "http://different.test/v1",
                  "reasoning_format": "openai"}},
])
def test_auto_backend_rejects_catalog_mismatch(tmp_path, override):
    conn = catalog_conn(tmp_path)
    try:
        payload = decision_payload(
            logical_model="fast", model="real-fast",
            provider={"name": "local", "base_url": "http://upstream.test/v1",
                      "reasoning_format": "openai"})
        payload.update(override)
        with pytest.raises(
                ExecutionCatalogMismatch, match="execution_catalog_mismatch"):
            backend_for_decision(payload, conn)
    finally:
        conn.close()


def test_auto_backend_accepts_a_trailing_slash_on_the_decision_base_url(tmp_path):
    conn = catalog_conn(tmp_path)
    try:
        payload = decision_payload(
            logical_model="fast", model="real-fast",
            provider={"name": "local", "base_url": "http://upstream.test/v1/",
                      "reasoning_format": "openai"})
        assert backend_for_decision(payload, conn).upstream_name == "real-fast"
    finally:
        conn.close()


def test_discard_outcome_never_stores_anything():
    outcome = DiscardOutcome()
    assert outcome.finish(outcome_state="completed", upstream_status=200) is True
    assert outcome.finish(outcome_state="upstream_error") is True
    assert not hasattr(outcome, "record")


# --------------------------------------------------------------------------- #
# Auto request end to end: real in-process kernel, mock peripheral upstream
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


def make_upstream(record):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        record.append({
            "url": str(request.url),
            "authorization": request.headers.get("authorization"),
            "model": body.get("model"),
        })
        return httpx.Response(200, json={
            "id": "cmpl-test", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
    return httpx.MockTransport(handler)


class _ForbiddenKernelTransport(httpx.AsyncBaseTransport):
    """Fails the test if the kernel performs any external (model upstream) call."""

    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(str(request.url))
        raise AssertionError(
            f"kernel made an external request: {request.method} {request.url}")


def graph_document(model="fast", upstream="real-fast"):
    """An enabled default graph whose logical model ``model`` maps to ``upstream``."""
    document = tree_dict("main", root=direct_graph(model),
                         enabled=True, default=True)
    document["backends"]["models"][0]["upstream"] = upstream
    return document


def _split(tmp_path, *, upstream_calls=None, kernel_transport=None):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_execution_catalog(conn, key=SECRET,
                           models=[("fast", "real-fast", "{}")])
    conn.close()
    calls = upstream_calls if upstream_calls is not None else []
    kernel, sink = recording_core_app(
        graph_dir=settings.graph_dir, transport=kernel_transport)
    install_graph(kernel, graph_document())
    peripheral = split_app(
        settings=settings, kernel=kernel, transport=make_upstream(calls))
    return peripheral, kernel, sink, calls


def test_auto_request_decides_in_kernel_and_executes_in_peripheral(tmp_path):
    kernel_transport = _ForbiddenKernelTransport()
    peripheral, kernel, sink, calls = _split(
        tmp_path, kernel_transport=kernel_transport)

    with TestClient(peripheral) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "ignored",
            "messages": [{"role": "user", "content": "你好"}],
        })

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    # 外围确实调用了模型上游：内核决策的真实模型 + 外围目录里的 Provider Key。
    assert [call["model"] for call in calls] == ["real-fast"]
    assert calls[0]["authorization"] == f"Bearer {SECRET}"
    assert calls[0]["url"] == "http://upstream.test/v1/chat/completions"
    # 内核从未调用模型上游（只做决策）。
    assert kernel_transport.requests == []
    # 决策事件由内核产生，响应头全部来自内核决策载荷。
    assert response.headers["x-sr-model"] == "real-fast"
    assert response.headers["x-sr-selection"] == "target"
    assert response.headers["x-sr-tree-id"] == "main"
    assert response.headers["x-sr-graph-selection"] == "default"
    assert len(sink.events) == 1
    assert sink.events[0]["decision"]["model"] == "fast"
    assert sink.events[0]["decision"]["upstream_model"] == "real-fast"


def test_auto_stream_completion_goes_through_the_peripheral_upstream(tmp_path):
    class SseBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SseBody())

    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_execution_catalog(conn, key=SECRET,
                           models=[("fast", "real-fast", "{}")])
    conn.close()
    kernel, _sink = recording_core_app(graph_dir=settings.graph_dir)
    install_graph(kernel, graph_document())
    peripheral = split_app(
        settings=settings, kernel=kernel, transport=httpx.MockTransport(handler))

    with TestClient(peripheral) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "流式"}], "stream": True,
        })

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"[DONE]" in response.content
    assert calls == ["real-fast"]
    assert response.headers["x-sr-model"] == "real-fast"


def test_auto_catalog_mismatch_is_503_without_any_upstream_call(tmp_path):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    # 目录 Provider 指向另一个上游：与内核决策的 provider.base_url 不一致。
    seed_execution_catalog(conn, key=SECRET, base_url="http://other.test/v1",
                           models=[("fast", "real-fast", "{}")])
    conn.close()
    calls = []
    kernel, sink = recording_core_app(graph_dir=settings.graph_dir)
    install_graph(kernel, graph_document())
    peripheral = split_app(
        settings=settings, kernel=kernel, transport=make_upstream(calls))

    with TestClient(peripheral) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "目录不一致"}],
        })

    assert response.status_code == 503
    assert response.json() == {
        "error": {"message": "execution_catalog_mismatch"}}
    assert calls == []
    # 内核已完成决策并发出事件；不一致只影响外围执行。
    assert len(sink.events) == 1


def test_auto_catalog_without_the_logical_model_is_503(tmp_path):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_execution_catalog(conn, key=SECRET, models=[("other-model", "other", "{}")])
    conn.close()
    calls = []
    kernel, _sink = recording_core_app(graph_dir=settings.graph_dir)
    install_graph(kernel, graph_document())
    peripheral = split_app(
        settings=settings, kernel=kernel, transport=make_upstream(calls))

    with TestClient(peripheral) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "缺模型"}],
        })

    assert response.status_code == 503
    assert response.json() == {
        "error": {"message": "execution_catalog_mismatch"}}
    assert calls == []


def test_kernel_decision_error_keeps_its_status_and_detail(tmp_path):
    """No current graph: the kernel's 503 ``no_active_tree`` reaches the client."""
    settings = test_settings(tmp_path)
    calls = []
    kernel, sink = recording_core_app(graph_dir=settings.graph_dir)
    # No graph installed: the kernel rejects the decision before selection.
    peripheral = split_app(
        settings=settings, kernel=kernel, transport=make_upstream(calls))

    with TestClient(peripheral) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "无图"}],
        })

    assert response.status_code == 503
    assert response.json() == {"error": {"message": "no_active_tree"}}
    assert calls == []
    assert sink.events == []


def unreachable_kernel_transport():
    async def handler(request):
        raise httpx.ConnectError("kernel down", request=request)

    return httpx.MockTransport(handler)


def test_unreachable_kernel_is_503_core_unavailable(tmp_path):
    settings = test_settings(tmp_path)
    calls = []
    peripheral = split_app(
        settings=settings, transport=make_upstream(calls),
        core_transport=unreachable_kernel_transport())
    with TestClient(peripheral) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "内核不可达"}],
        })

    assert response.status_code == 503
    assert response.json() == {"error": {"message": "core_unavailable"}}
    assert calls == []


def test_fixed_admin_test_chat_never_calls_the_kernel(tmp_path):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_execution_catalog(conn, key=SECRET,
                           models=[("qwen3-32b", "qwen3-32b", "{}")])
    conn.close()
    calls = []
    forbidden = _ForbiddenKernelTransport()
    peripheral = split_app(
        settings=settings, transport=make_upstream(calls),
        core_transport=forbidden)

    with TestClient(peripheral) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b",
            "messages": [{"role": "user", "content": "固定模型直连"}],
        })

    assert response.status_code == 200, response.text
    assert forbidden.requests == []
    assert calls[0]["model"] == "qwen3-32b"
    assert response.headers["x-sr-model"] == "qwen3-32b"
    assert response.headers["x-sr-selection"] == "direct"
    assert "x-sr-graph-selection" not in response.headers
    assert "x-sr-tree-id" not in response.headers


def test_peripheral_readyz_does_not_depend_on_the_kernel(tmp_path):
    settings = test_settings(tmp_path)

    async def handler(request):
        raise httpx.ConnectError("kernel down", request=request)

    peripheral = split_app(
        settings=settings, core_transport=httpx.MockTransport(handler))
    with TestClient(peripheral) as client:
        assert client.get("/healthz").json() == {"ok": True}
        ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["database"] == "ok"
