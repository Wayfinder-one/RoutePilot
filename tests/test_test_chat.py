"""Admin Test chat endpoint: Auto routing vs fixed direct model.

``POST /admin/test/chat/completions`` shares the real ``/v1/chat/completions``
executor.  Auto keeps the full routing path (graph + JEV + sticky); a fixed
model skips graph/JEV/sticky and resolves through the SQLite catalog
(``load_backends``) instead of the active canvas snapshot, but still uses the
LoadBalancer lease, the CircuitBreaker, the UpstreamProxy and the request log.

管理端测试对话凭可信 ``x-sr-department`` Header 验证部门选图（公开 ``/v1`` 仍然
拒绝该 Header）：百分号编码的 ``财务部`` 命中部门图，缺失/空 Header 回退默认图，
固定模型直连完全跳过路由集选择。
"""
import json
from dataclasses import replace
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import test_settings, tree_dict
from tests.test_app import (
    JEV_ENV,
    TryHandler,
    composed_try_app,
    direct_graph,
    prepare_active_tree,
)


class _SseBody(httpx.AsyncByteStream):
    """Minimal async SSE body: a buffered ``httpx.Response`` cannot be re-streamed."""

    async def __aiter__(self):
        yield 'data: {"choices":[{"delta":{"content":"第一段"}}]}\n\n'.encode()
        yield 'data: {"choices":[{"delta":{"content":"第二段"}}]}\n\n'.encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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
    assert "第一段" in response.text
    assert "第二段" in response.text
    assert app.state.balancer.inflight("qwen3-32b") == 0


def test_admin_test_rejects_unknown_model(tmp_path):
    app = composed_try_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "missing", "messages": [{"role": "user", "content": "hi"}],
        })
    assert response.status_code == 404
    assert response.json() == {"error": {"message": "unknown_model"}}


def test_admin_fixed_model_works_without_an_active_snapshot(tmp_path):
    """固定模型直连是目录测试：画布文件损坏、快照不可用也能工作。"""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    # 启用文档损坏：启动路由集构造失败，app.state.snapshot_set 保持 None。
    (repo.graphs / "main.json").write_bytes(b"{broken")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        })

    app = create_app(settings, transport=httpx.MockTransport(handler))
    assert app.state.snapshot_set is None
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-8b",
            "messages": [{"role": "user", "content": "无快照直连"}],
        })

    assert response.status_code == 200, response.text
    assert response.headers["x-sr-model"] == "qwen3-8b"
    assert response.headers["x-sr-selection"] == "direct"
    assert seen and seen[-1].url.path.endswith("/chat/completions")


# ── 管理端测试对话：可信部门 Header 选图 ──────────────────────────────

FINANCE_DEPARTMENT = quote("财务部", safe="")


def department_app(tmp_path, handler):
    """默认图 main(fast) 由启动装配；部门图 finance(strong) 走真实管理 API。"""
    settings = prepare_active_tree(tmp_path, tree_dict(
        root=direct_graph(("fast",)),
        sticky={"enabled": False, "ttl_seconds": 1800},
    ))
    return create_app(replace(settings, **JEV_ENV), transport=httpx.MockTransport(handler))


def create_direct_model_tree(client, tree_id, model):
    raw = tree_dict(tree_id, root=direct_graph((model,)))
    payload = {key: value for key, value in raw.items()
               if key not in {"enabled", "default"}}
    response = client.post("/admin/trees", json=payload)
    assert response.status_code == 201, response.text
    assert client.post(f"/admin/trees/{tree_id}/enable").status_code == 200


def set_departments(client, tree_id, departments):
    current = client.get(f"/admin/trees/{tree_id}").json()["tree"]
    response = client.put(f"/admin/trees/{tree_id}/departments", json={
        "departments": departments,
        "base_document_sha256": current["document_sha256"],
    })
    assert response.status_code == 200, response.text


def test_admin_auto_chat_routes_department_header_and_logs_selection(tmp_path):
    """Auto 测试对话按 Header 选部门图，缺失 Header 回退默认图，账本如实记录。"""
    app = department_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        create_direct_model_tree(client, "finance", "strong")
        set_departments(client, "finance", ["财务部"])

        default = client.post("/admin/test/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "默认请求"}],
        })
        department = client.post("/admin/test/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "部门请求"}],
        }, headers={"x-sr-department": FINANCE_DEPARTMENT})

        recent = client.get("/admin/requests/recent").json()["requests"]

    assert default.status_code == 200, default.text
    assert default.headers["x-sr-model"] == "fast"
    assert default.headers["x-sr-tree-id"] == "main"
    assert default.headers["x-sr-graph-selection"] == "default"
    assert department.status_code == 200, department.text
    assert department.headers["x-sr-model"] == "strong"
    assert department.headers["x-sr-tree-id"] == "finance"
    assert department.headers["x-sr-graph-selection"] == "department"
    assert department.headers["x-sr-selection"] == "target"
    # 最新请求在前：先默认图后部门图，账本记录每张图自己的模型。
    assert [row["tree_id"] for row in recent[:2]] == ["finance", "main"]
    assert recent[0]["model"] == "strong"
    assert recent[1]["model"] == "fast"


def test_admin_auto_chat_empty_department_header_uses_default_graph(tmp_path):
    app = department_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        create_direct_model_tree(client, "finance", "strong")
        set_departments(client, "finance", ["财务部"])
        response = client.post("/admin/test/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "空部门"}],
        }, headers={"x-sr-department": ""})

    assert response.status_code == 200, response.text
    assert response.headers["x-sr-model"] == "fast"
    assert response.headers["x-sr-tree-id"] == "main"
    assert response.headers["x-sr-graph-selection"] == "default"


def test_admin_chat_rejects_malformed_department_header(tmp_path):
    """可信 Header 只在校验通过后参与选图：畸形百分号编码仍是 400。"""
    app = department_app(tmp_path, TryHandler())
    with TestClient(app) as client:
        response = client.post("/admin/test/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "坏 Header"}],
        }, headers={"x-sr-department": "%zz"})

    assert response.status_code == 400, response.text
    assert response.json() == {"error": {"message": "invalid_department"}}


def test_admin_fixed_model_chat_ignores_department_and_bypasses_jev(tmp_path):
    """固定模型直连是目录测试：可信部门 Header 不参与选图/JEV/响应头。"""
    handler = TryHandler()
    app = department_app(tmp_path, handler)
    with TestClient(app) as client:
        create_direct_model_tree(client, "finance", "strong")
        set_departments(client, "finance", ["财务部"])
        response = client.post("/admin/test/chat/completions", json={
            "model": "qwen3-32b",
            "messages": [{"role": "user", "content": "直连测试"}],
        }, headers={"x-sr-department": FINANCE_DEPARTMENT})
        rows = app.state.requestlog.recent_requests(1)

    assert response.status_code == 200, response.text
    assert response.headers["x-sr-model"] == "qwen3-32b"
    assert response.headers["x-sr-selection"] == "direct"
    assert "x-sr-graph-selection" not in response.headers
    assert "x-sr-tree-id" not in response.headers
    assert handler.jev_paths == []
    assert rows[0]["decision"] == "test"
    assert rows[0]["selection_reason"] == "direct"
