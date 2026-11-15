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


def test_admin_test_rejects_unknown_model(tmp_path):
    app = composed_try_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "missing", "messages": [{"role": "user", "content": "hi"}],
        })
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "unknown_model"
