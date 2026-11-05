import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.upstream import load_backends
from router.jev.config import JevConfigRepo
from router.routing.conditions import TimePeriod
from router.routing.snapshot import _capability, build_snapshot, read_jev_window
from router.store.db import connect
from router.store.repositories import ModelRepo, TimePeriodRepo
from router.store.routing_trees import GraphStorageError
from tests.routing_fixtures import test_settings, tree_dict

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
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
            {"id": "direct", "type": "target", "priority": 1,
             "position": {"x": 200, "y": 0},
             "models": [{"model": "qwen3-8b", "reasoning": reasoning}]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def activate(client, level=None):
    raw = tree_dict("default-tree", root=direct_graph(level))
    body = {"id": raw["id"], "name": raw["name"], "root": raw["root"], "sticky": raw["sticky"], "timezone": raw["timezone"]}
    existing = client.get("/admin/trees/default-tree")
    if existing.status_code == 200:
        body["base_document_sha256"] = existing.json()["tree"]["document_sha256"]
        response = client.put("/admin/trees/default-tree", json=body)
    else:
        response = client.post("/admin/trees", json=body)
    assert response.status_code in (200, 201), response.text
    assert client.post("/admin/trees/default-tree/enable").status_code == 200


def declare(client, reasoning, *, provider_format=None):
    if provider_format:
        response = client.put("/admin/providers/local", json={"base_url": "http://upstream.test/v1", "reasoning_format": provider_format})
        assert response.status_code == 200, response.text
    response = client.put("/admin/models/qwen3-8b", json={"profile": {"reasoning": reasoning}})
    assert response.status_code == 200, response.text


def chat(client, **body):
    return client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": TEXT}], **body})


def test_null_reasoning_is_full_passthrough(tmp_path):
    h = harness(tmp_path)
    activate(h.client)
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
    activate(h.client)
    declare(h.client, {"format": fmt, "efforts": efforts})
    activate(h.client, level)
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
    activate(h.client)
    declare(h.client, {"format": "qwen", "efforts": {"off": None, "on": None}}, provider_format="deepseek")
    activate(h.client, "on")
    assert chat(h.client).status_code == 200
    assert h.requests[-1]["body"]["enable_thinking"] is True


def test_stale_reasoning_level_degrades_to_passthrough_with_warning(tmp_path):
    h = harness(tmp_path)
    activate(h.client)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    activate(h.client, "high")
    conn = connect(h.app.state.settings.db_path)
    with conn:
        conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'", (json.dumps({"reasoning": {"efforts": {"off": None, "low": "low"}}}),))
    stale_tree = h.app.state.snapshot.tree
    with pytest.raises(GraphStorageError):
        h.app.state.publisher.refresh(conn)
    assert h.app.state.snapshot.tree is stale_tree
    input_tokens, context_tokens = read_jev_window(conn)
    h.app.state.snapshot = build_snapshot(
        tree=stale_tree,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(row) for row in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: _capability(row) for row in ModelRepo(conn).list()},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
    )
    conn.close()
    response = chat(h.client, reasoning_effort="client")
    assert response.status_code == 200
    assert response.headers["x-sr-reasoning-warning"] == "unsupported"
    assert h.requests[-1]["body"]["reasoning_effort"] == "client"


def test_streaming_request_injects_reasoning_and_releases(tmp_path):
    h = harness(tmp_path, streaming=True)
    activate(h.client)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    activate(h.client, "high")
    response = chat(h.client, stream=True)
    assert response.status_code == 200
    assert response.headers["x-sr-reasoning"] == "high"
    assert response.text == "data: hi\n\n"
    assert h.requests[-1]["body"]["reasoning_effort"] == "high"
    assert h.app.state.balancer.inflight("qwen3-8b") == 0
