"""Admin Test chat endpoint: Auto routing vs fixed direct model.

``POST /admin/test/chat/completions`` shares the real ``/v1/chat/completions``
executor.  Auto keeps the full routing path (graph + JEV + sticky); a fixed
model skips graph/JEV/sticky but still uses the published backend snapshot, the
LoadBalancer lease, the CircuitBreaker, the UpstreamProxy and the request log.
"""
import json

import httpx
from fastapi.testclient import TestClient

from tests.test_app import TryHandler, composed_try_app


class _SseBody(httpx.AsyncByteStream):
    """Minimal async SSE body: a buffered ``httpx.Response`` cannot be re-streamed."""

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: [DONE]\n\n"


def test_admin_auto_runs_graph_and_reports_selected_model(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好"}],
        })
    assert response.status_code == 200, response.text
    assert response.headers["x-sr-model"] == "qwen3-8b"
    assert response.headers["x-sr-selection"] == "target"
    assert handler.jev_paths == ["/v1/systemone"]


def test_admin_fixed_model_bypasses_jev_and_uses_selected_backend(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b",
            "messages": [{"role": "user", "content": "直接测试"}],
        })
    assert response.status_code == 200, response.text
    assert response.headers["x-sr-model"] == "qwen3-32b"
    assert response.headers["x-sr-selection"] == "direct"
    assert handler.jev_paths == []
    assert app.state.balancer.inflight("qwen3-32b") == 0


def test_admin_fixed_model_preserves_multimodal_content(tmp_path):
    seen = []

    def handler(request):
        if request.url.host == "jev.test":
            return httpx.Response(200, json={"answers": {}})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        })

    app = composed_try_app(tmp_path, handler)
    content = [
        {"type": "text", "text": "查看图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b", "messages": [{"role": "user", "content": content}],
        })
    assert response.status_code == 200
    assert seen[0]["model"] == "qwen3-32b"
    assert seen[0]["messages"][0]["content"] == content


def test_admin_fixed_model_open_breaker_is_503_without_upstream_call(tmp_path):
    """Open circuit for the fixed model: 503 and neither JEV nor upstream is hit."""
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "jev.test":
            return httpx.Response(200, json={"answers": {}})
        return httpx.Response(200, json={"choices": []})

    app = composed_try_app(tmp_path, handler)
    for _ in range(app.state.breaker.threshold):
        app.state.breaker.failure("qwen3-32b")
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b",
            "messages": [{"role": "user", "content": "直接测试"}],
        })
    assert response.status_code == 503, response.text
    assert response.json() == {"error": {"message": "no_available_backend"}}
    assert calls == []


def test_admin_fixed_model_logs_test_decision_and_direct_reason(tmp_path):
    """The shared request log stores fixed-mode rows as decision=test/direct."""
    app = composed_try_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b",
            "messages": [{"role": "user", "content": "记录"}],
        })
    assert response.status_code == 200, response.text
    rows = app.state.requestlog.recent_requests(1)
    assert len(rows) == 1
    record = rows[0]
    assert record["decision"] == "test"
    assert record["selection_reason"] == "direct"
    assert record["model"] == "qwen3-32b"
    # OutcomeRecorder refreshed the same row: the terminal (not the half) record wins.
    assert record["outcome_state"] == "completed"
    assert record["route_label"] == {"zh": "固定模型测试", "en": "Direct model test"}


def test_admin_fixed_model_streams_sse_and_releases_lease(tmp_path):
    """stream=true reuses the proxy tail: SSE relay, direct headers, lease freed."""

    def handler(request):
        if request.url.host == "jev.test":
            return httpx.Response(200, json={"answers": {}})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_SseBody())

    app = composed_try_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b",
            "messages": [{"role": "user", "content": "流式"}],
            "stream": True,
        })
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-sr-selection"] == "direct"
    assert response.headers["x-sr-model"] == "qwen3-32b"
    assert '"ok"' in response.text
    assert app.state.balancer.inflight("qwen3-32b") == 0


def test_admin_test_rejects_unknown_model(tmp_path):
    app = composed_try_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "missing", "messages": [{"role": "user", "content": "hi"}],
        })
    assert response.status_code == 404
    assert response.json() == {"error": {"message": "unknown_model"}}
