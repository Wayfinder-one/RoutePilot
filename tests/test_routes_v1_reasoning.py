"""热路径（§6）：推理注入 / 完全透传 / 竞态降级 / 响应头 / ring 记录。

上游用 httpx.MockTransport 捕获真实请求体，覆盖 9 种 format 的注入与 off 语义。
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings

TEXT = "今天天气怎么样"     # no seeded condition matches -> fallback routes to qwen3-8b


def make_app(tmp_path, handler=None):
    seen = {"requests": []}

    def capture(request):
        seen["requests"].append({
            "url": str(request.url),
            "body": json.loads(request.content or b"{}"),
            "headers": dict(request.headers)})
        return handler(request)

    transport = httpx.MockTransport(capture)
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=transport)
    return app, TestClient(app), seen


class _Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: hi\n\n"


class _StreamingTransport(httpx.AsyncBaseTransport):
    """Async transport so a mock upstream can really stream an SSE body."""

    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        content = await request.aread()
        self.requests.append({"url": str(request.url),
                              "body": json.loads(content or b"{}")})
        return httpx.Response(200,
                              headers={"content-type": "text/event-stream"},
                              stream=_Chunks())


def ok_upstream(request):
    return httpx.Response(200, json={"choices": [
        {"message": {"role": "assistant", "content": "hi"}}]})


def declare(client, reasoning, *, provider_format=None, model="qwen3-8b"):
    if provider_format is not None:
        r = client.put("/admin/providers/local", json={
            "base_url": "http://127.0.0.1:9000",
            "reasoning_format": provider_format})
        assert r.status_code == 200, r.text
    r = client.put(f"/admin/models/{model}", json={"profile": {"reasoning": reasoning}})
    assert r.status_code == 200, r.text


def set_leaf_reasoning(client, level, station_index=-1):
    pipe = client.get("/admin/pipeline").json()
    pipe["stations"][station_index]["root"]["reasoning"] = level
    r = client.put("/admin/pipeline", json=pipe)
    assert r.status_code == 200, r.text


def chat(client, **body):
    return client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": TEXT}],
                             **body})


# --------------------------------------------------------------------------- #
# null（默认）= 完全透传：不注入、不剥离
# --------------------------------------------------------------------------- #
def test_null_reasoning_is_full_passthrough(tmp_path):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    client_reasoning = {
        "reasoning_effort": "client-effort",
        "reasoning": {"effort": "client-effort"},
        "thinking": {"type": "enabled"},
        "enable_thinking": True,
        "chat_template_kwargs": {"enable_thinking": True, "keep": 1},
    }
    r = chat(client, **client_reasoning)
    assert r.status_code == 200
    assert r.headers["x-sr-model"] == "qwen3-8b"
    assert "x-sr-reasoning" not in r.headers
    assert "x-sr-reasoning-warning" not in r.headers
    sent = seen["requests"][-1]["body"]
    for key, value in client_reasoning.items():
        assert sent[key] == value, key      # untouched: not stripped, not injected
    ring = client.get("/admin/requests/recent").json()["requests"][0]
    assert ring["reasoning"] is None
    assert ring["reasoning_warning"] is None


# --------------------------------------------------------------------------- #
# 9 种 format 注入（非 off）
# --------------------------------------------------------------------------- #
FORMAT_CASES = [
    ("openai", {"off": None, "high": "high"}, "high",
     {"reasoning_effort": "high"}),
    ("deepseek", {"off": None, "high": "high"}, "high",
     {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("qwen", {"off": None, "on": None}, "on", {"enable_thinking": True}),
    ("qwen-chat-template", {"off": None, "on": None}, "on",
     {"chat_template_kwargs": {"keep": 1, "enable_thinking": True}}),
    ("openrouter", {"off": None, "high": "high"}, "high",
     {"reasoning": {"effort": "high"}}),
    ("zai", {"off": None, "high": "high"}, "high",
     {"thinking": {"type": "enabled", "clear_thinking": False},
      "reasoning_effort": "high"}),
    ("together", {"off": None, "high": "high"}, "high",
     {"reasoning": {"enabled": True}, "reasoning_effort": "high"}),
    ("string-thinking", {"off": None, "high": "high"}, "high",
     {"thinking": "high"}),
    ("ant-ling", {"off": None, "high": "high"}, "high",
     {"reasoning": {"effort": "high"}}),
]


@pytest.mark.parametrize("fmt,efforts,level,expected", FORMAT_CASES)
def test_injection_per_format(tmp_path, fmt, efforts, level, expected):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    declare(client, {"format": fmt, "efforts": efforts})
    set_leaf_reasoning(client, level)
    client_junk = {
        "reasoning_effort": "client",
        "reasoning": {"effort": "client"},
        "thinking": "client",
        "enable_thinking": False,
        "chat_template_kwargs": {"keep": 1, "enable_thinking": False},
    }
    r = chat(client, **client_junk)
    assert r.status_code == 200, r.text
    assert r.headers["x-sr-reasoning"] == level
    assert "x-sr-reasoning-warning" not in r.headers
    sent = seen["requests"][-1]["body"]
    assert sent["model"] == "qwen3-8b"
    for key, value in expected.items():
        assert sent[key] == value, (fmt, key)
    # every client field not re-injected by the format was stripped
    for key in ("reasoning_effort", "reasoning", "thinking", "enable_thinking"):
        if key not in expected:
            assert key not in sent, (fmt, key)
    if fmt != "qwen-chat-template":
        assert sent["chat_template_kwargs"] == {"keep": 1}
    ring = client.get("/admin/requests/recent").json()["requests"][0]
    assert ring["reasoning"] == level
    assert ring["reasoning_warning"] is None


OFF_CASES = [
    ("openai", {"off": None, "high": "high"}, {}),
    ("deepseek", {"off": None, "high": "high"},
     {"thinking": {"type": "disabled"}}),
    ("qwen", {"off": None, "on": None}, {"enable_thinking": False}),
    ("qwen-chat-template", {"off": None, "on": None},
     {"chat_template_kwargs": {"enable_thinking": False}}),
    ("openrouter", {"off": None, "high": "high"},
     {"reasoning": {"effort": "none"}}),
    ("zai", {"off": None, "high": "high"},
     {"thinking": {"type": "disabled"}}),
    ("together", {"off": None, "high": "high"},
     {"reasoning": {"enabled": False}}),
    ("string-thinking", {"off": None, "high": "high"}, {"thinking": "none"}),
    ("ant-ling", {"off": None, "high": "high"}, {}),
]


@pytest.mark.parametrize("fmt,efforts,expected", OFF_CASES)
def test_off_level_per_format(tmp_path, fmt, efforts, expected):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    declare(client, {"format": fmt, "efforts": efforts})
    set_leaf_reasoning(client, "off")
    r = chat(client, reasoning_effort="client", thinking="client",
             enable_thinking=True)
    assert r.status_code == 200, r.text
    assert r.headers["x-sr-reasoning"] == "off"
    sent = seen["requests"][-1]["body"]
    for key, value in expected.items():
        assert sent[key] == value, (fmt, key)
    for key in ("reasoning_effort", "reasoning", "thinking", "enable_thinking"):
        if key not in expected:
            assert key not in sent, (fmt, key)


def test_model_level_format_wins_over_provider(tmp_path):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    # provider says deepseek, the model overrides to qwen
    declare(client, {"format": "qwen", "efforts": {"off": None, "on": None}},
            provider_format="deepseek")
    set_leaf_reasoning(client, "on")
    assert chat(client).status_code == 200
    sent = seen["requests"][-1]["body"]
    assert sent["enable_thinking"] is True
    assert "thinking" not in sent


def test_model_without_format_inherits_provider_format(tmp_path):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    declare(client, {"efforts": {"off": None, "high": None}},
            provider_format="deepseek")
    set_leaf_reasoning(client, "high")
    assert chat(client).status_code == 200
    sent = seen["requests"][-1]["body"]
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "high"       # null wire -> level id


# --------------------------------------------------------------------------- #
# 竞态降级：配置落后于声明 -> 透传 + warning，请求不失败
# --------------------------------------------------------------------------- #
def test_level_removed_from_declaration_degrades_to_passthrough(tmp_path):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    set_leaf_reasoning(client, "high")
    # concurrent admin edit: the model now only declares low, the tree still says high
    assert client.put("/admin/models/qwen3-8b", json={"profile": {
        "reasoning": {"efforts": {"off": None, "low": "low"}}}}).status_code == 200
    r = chat(client, reasoning_effort="client-effort")
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning-warning"] == "unsupported"
    assert "x-sr-reasoning" not in r.headers
    sent = seen["requests"][-1]["body"]
    assert sent["reasoning_effort"] == "client-effort"   # passthrough, not stripped
    ring = client.get("/admin/requests/recent").json()["requests"][0]
    assert ring["reasoning"] == "high"
    assert ring["reasoning_warning"] == "unsupported"


def test_capability_removed_degrades_to_passthrough(tmp_path):
    _app, client, seen = make_app(tmp_path, ok_upstream)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    set_leaf_reasoning(client, "high")
    # explicitly unsupported now (e.g. provider switch / profile edit)
    assert client.put("/admin/models/qwen3-8b",
                      json={"profile": {"reasoning": False}}).status_code == 200
    r = chat(client, thinking={"type": "enabled"})
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning-warning"] == "unsupported"
    sent = seen["requests"][-1]["body"]
    assert sent["thinking"] == {"type": "enabled"}        # untouched
    ring = client.get("/admin/requests/recent").json()["requests"][0]
    assert ring["reasoning_warning"] == "unsupported"


def test_model_without_any_declaration_and_leaf_level(tmp_path):
    """A stale tree selecting a level for an undeclared model must not 5xx."""
    _app, client, _seen = make_app(tmp_path, ok_upstream)
    # write the tree directly into the DB, bypassing health check validation
    from router.pipeline.snapshot import install_snapshot, load_snapshot
    from router.store.db import connect
    from router.store.repositories import StationRepo

    pipe = client.get("/admin/pipeline").json()
    station = pipe["stations"][-1]
    conn = connect(client.app.state.settings.db_path)
    try:
        stations = StationRepo(conn).load()
        stations[-1].root.reasoning = "high"
        StationRepo(conn).save(stations)
        # P1-B: the hot path reads the immutable snapshot, so publish the tree
        # the test wrote straight into the DB (bypassing admin validation).
        install_snapshot(
            client.app,
            load_snapshot(conn, jev_trim=client.app.state.settings.jev_trim))
    finally:
        conn.close()
    r = chat(client)
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning-warning"] == "unsupported"
    assert station["root"]["reasoning"] is None     # untouched GET payload


# --------------------------------------------------------------------------- #
# 流式请求同样注入 + 响应头
# --------------------------------------------------------------------------- #
def test_streaming_request_injects_reasoning(tmp_path):
    transport = _StreamingTransport()
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=transport)
    client = TestClient(app)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    set_leaf_reasoning(client, "high")
    r = chat(client, stream=True)
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning"] == "high"
    assert r.text == "data: hi\n\n"
    sent = transport.requests[-1]["body"]
    assert sent["reasoning_effort"] == "high"
    assert sent["stream"] is True
