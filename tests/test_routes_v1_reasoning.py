import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.store.db import connect
from router.upstream.runtime_keys import tree_scope
from tests.routing_fixtures import runtime_from_catalog, test_settings, tree_dict

TEXT = "今天天气怎么样"


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: hi\n\n"


class StreamingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append({"body": json.loads(await request.aread())})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())


def harness(tmp_path, *, streaming=False):
    requests = []

    def handler(request):
        requests.append({"body": json.loads(request.content)})
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    transport = StreamingTransport() if streaming else httpx.MockTransport(handler)
    app = create_app(test_settings(tmp_path), transport=transport)
    return SimpleNamespace(app=app, client=TestClient(app), requests=transport.requests if streaming else requests)


def direct_graph(reasoning=None):
    """start → target（qwen3-8b）；直连图不激活问题节点。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": reasoning}]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def activate(h, level=None):
    """保存并启用一棵画布：运行配置从当前数据库目录显式物化。

    严格自包含语义下目录写不再回写画布，所以这里模拟「按当前目录刷新画布」
    的显式动作，而不是依赖已删除的全局同步。
    """
    raw = tree_dict("default-tree", root=direct_graph(level))
    conn = connect(h.app.state.settings.db_path)
    try:
        runtime = runtime_from_catalog(conn)
    finally:
        conn.close()
    body = {
        "id": raw["id"], "name": raw["name"], "root": raw["root"],
        "sticky": raw["sticky"], "timezone": raw["timezone"], **runtime,
    }
    client = h.client
    existing = client.get("/admin/trees/default-tree")
    if existing.status_code == 200:
        body["base_document_sha256"] = existing.json()["tree"]["document_sha256"]
        response = client.put("/admin/trees/default-tree", json=body)
    else:
        response = client.post("/admin/trees", json=body)
    assert response.status_code in (200, 201), response.text
    assert client.post("/admin/trees/default-tree/enable").status_code == 200


def _confirmation_from(response):
    """409 影响预览 → 携带当前画布哈希的确认载荷。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required", response.text
    return {"trees": [
        {"id": item["id"], "document_sha256": item["document_sha256"]}
        for item in detail["affected_trees"]
    ]}


def _put_with_confirmation(client, path, payload):
    """目录写两阶段：未确认的 409 预览用当前哈希确认重试。"""
    preview = client.put(path, json=payload)
    if preview.status_code != 409:
        return preview
    return client.put(path, json={
        **payload, "impact_confirmation": _confirmation_from(preview)})


def declare(client, reasoning, *, provider_format=None):
    if provider_format:
        response = _put_with_confirmation(client, "/admin/providers/local", {
            "base_url": "http://upstream.test/v1",
            "reasoning_format": provider_format,
        })
        assert response.status_code == 200, response.text
    response = _put_with_confirmation(
        client, "/admin/models/qwen3-8b",
        {"profile": {"reasoning": reasoning}})
    assert response.status_code == 200, response.text


def chat(client, **body):
    return client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": TEXT}], **body})


def test_null_reasoning_is_full_passthrough(tmp_path):
    h = harness(tmp_path)
    activate(h)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    fields = {"reasoning_effort": "client", "reasoning": {"effort": "client"}, "thinking": {"type": "enabled"}, "enable_thinking": True}
    response = chat(h.client, **fields)
    assert response.status_code == 200
    assert "x-sr-reasoning" not in response.headers
    sent = h.requests[-1]["body"]
    for key, value in fields.items():
        assert sent[key] == value


FORMAT_CASES = [
    ("openai", {"off": None, "high": "high"}, "high", {"reasoning_effort": "high"}),
    ("deepseek", {"off": None, "high": "high"}, "high", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("qwen", {"off": None, "on": None}, "on", {"enable_thinking": True}),
    ("qwen-chat-template", {"off": None, "on": None}, "on", {"chat_template_kwargs": {"keep": 1, "enable_thinking": True}}),
    ("openrouter", {"off": None, "high": "high"}, "high", {"reasoning": {"effort": "high"}}),
    ("zai", {"off": None, "high": "high"}, "high", {"thinking": {"type": "enabled", "clear_thinking": False}, "reasoning_effort": "high"}),
    ("together", {"off": None, "high": "high"}, "high", {"reasoning": {"enabled": True}, "reasoning_effort": "high"}),
    ("string-thinking", {"off": None, "high": "high"}, "high", {"thinking": "high"}),
    ("ant-ling", {"off": None, "high": "high"}, "high", {"reasoning": {"effort": "high"}}),
]


@pytest.mark.parametrize("fmt,efforts,level,expected", FORMAT_CASES)
def test_reasoning_injection_per_format(tmp_path, fmt, efforts, level, expected):
    h = harness(tmp_path)
    activate(h)
    declare(h.client, {"format": fmt, "efforts": efforts})
    activate(h, level)
    response = chat(h.client, reasoning_effort="client", thinking="client", enable_thinking=False, chat_template_kwargs={"keep": 1})
    assert response.status_code == 200, response.text
    assert response.headers["x-sr-reasoning"] == level
    sent = h.requests[-1]["body"]
    for key, value in expected.items():
        assert sent[key] == value, (fmt, key)
    record = h.client.get("/admin/requests/recent").json()["requests"][0]
    assert record["reasoning"] == level
    assert record["tree_id"] == "default-tree"


def test_provider_format_inheritance_and_model_override(tmp_path):
    h = harness(tmp_path)
    activate(h)
    declare(h.client, {"format": "qwen", "efforts": {"off": None, "on": None}}, provider_format="deepseek")
    activate(h, "on")
    assert chat(h.client).status_code == 200
    assert h.requests[-1]["body"]["enable_thinking"] is True


def test_streaming_request_injects_reasoning_and_releases(tmp_path):
    h = harness(tmp_path, streaming=True)
    activate(h)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    activate(h, "high")
    response = chat(h.client, stream=True)
    assert response.status_code == 200
    assert response.headers["x-sr-reasoning"] == "high"
    assert response.text == "data: hi\n\n"
    assert h.requests[-1]["body"]["reasoning_effort"] == "high"
    # 公开 /v1 是 Auto 路由：活动图的 tree.id 为 default-tree。
    assert h.app.state.balancer.inflight(
        "qwen3-8b", scope=tree_scope("default-tree")) == 0
