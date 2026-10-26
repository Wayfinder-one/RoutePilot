"""热路径（§6）：推理注入 / 完全透传 / 竞态降级 / 响应头 / ring 记录。

Migrated from the four-station pipeline to the unified routing tree:

* the old ``set_leaf_reasoning`` helper edited the fallback Station leaf through
  ``GET/PUT /admin/pipeline``; the unified tree keeps that leaf as the root
  branch's explicit ``otherwise`` route, so the helper now saves the ACTIVE tree
  with that route's ``reasoning`` field set (saving the active tree republishes
  the immutable snapshot);
* the routing fixture is the helper's ``default_signal_tree`` (equivalent to the
  old seeded pipeline: safety block -> keyword -> subject -> fallback) instead
  of a globally seeded pipeline with lowered confidence floors.  ``TEXT`` has no
  matching condition, so it reaches the fallback route (qwen3-8b) exactly like
  before;
* upstream body capture, the response headers (``x-sr-model`` /
  ``x-sr-reasoning`` / ``x-sr-reasoning-warning``) and the ring record contract
  are unchanged (design §8.1: the upstream forwarding boundary is stable).
"""

from __future__ import annotations

import json

import httpx
import pytest

from router.pipeline.snapshot import install_snapshot  # noqa: F401  (seam doc)
from router.pipeline.tree import tree_from_dict, tree_revision_of
from router.store.db import connect
from tests.unified_tree_helpers import (
    activate_default_tree,
    default_signal_tree,
    make_app,
    ok_upstream,
    set_active_fallback_reasoning,
)

TEXT = "今天天气怎么样"     # no condition matches -> fallback route -> qwen3-8b


def make_harness(tmp_path, *, transport=None, handler=None):
    """App + client + captured upstream requests (see unified_tree_helpers)."""
    return make_app(tmp_path,
                    handler if handler is not None else ok_upstream,
                    transport=transport)


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


def declare(client, reasoning, *, provider_format=None, model="qwen3-8b"):
    if provider_format is not None:
        r = client.put("/admin/providers/local", json={
            "base_url": "http://127.0.0.1:9000",
            "reasoning_format": provider_format})
        assert r.status_code == 200, r.text
    r = client.put(f"/admin/models/{model}",
                   json={"profile": {"reasoning": reasoning}})
    assert r.status_code == 200, r.text


def set_leaf_reasoning(client, level):
    """Set the fallback ("otherwise") route's reasoning level on the active tree.

    The unified-tree replacement of the old Station-leaf helper: the active tree
    is saved back through ``PUT /admin/trees/{id}``, which republishes the
    snapshot with the new route reasoning.
    """
    set_active_fallback_reasoning(client, level)


def insert_active_tree(app, root, tree_id="stale-tree"):
    """Publish an ACTIVE tree straight from the DB, bypassing /admin validation.

    Reproduces a stale/manual configuration (older version, a direct DB edit):
    the enable-time health check would reject it today, so the hot path itself
    must degrade gracefully.  This is the equivalent of the old test's
    "concurrent admin edit" seam now that admin writes are health-checked.
    """
    tree = tree_from_dict({"id": tree_id, "name": "Stale tree",
                           "enabled": True, "root": root})
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, "
            "updated_at) VALUES (?, ?, 1, ?, ?, ?)",
            (tree.id, tree.name, json.dumps(root, ensure_ascii=False),
             tree_revision_of(tree.root), "2026-10-26T00:00:00+08:00"))
        conn.commit()
        app.state.publisher.refresh(conn)
    finally:
        conn.close()
    assert app.state.snapshot.tree_id == tree_id


def rewrite_model_profile(app, model, profile):
    """Direct DB edit of the model catalog + publish (bypasses write validation)."""
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("UPDATE models SET profile=? WHERE id=?",
                     (json.dumps(profile), model))
        conn.commit()
        app.state.publisher.refresh(conn)
    finally:
        conn.close()


def chat(client, **body):
    return client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": TEXT}],
                             **body})


def ring_record(client):
    """Newest in-memory ring record (the console/replay view of the request)."""
    return client.get("/admin/requests/recent").json()["requests"][0]


# --------------------------------------------------------------------------- #
# null（默认）= 完全透传：不注入、不剥离
# --------------------------------------------------------------------------- #
def test_null_reasoning_is_full_passthrough(tmp_path):
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)              # fallback reasoning stays null
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
    sent = h.requests[-1]["body"]
    for key, value in client_reasoning.items():
        assert sent[key] == value, key      # untouched: not stripped, not injected
    ring = ring_record(client)
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
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)
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
    sent = h.requests[-1]["body"]
    assert sent["model"] == "qwen3-8b"
    for key, value in expected.items():
        assert sent[key] == value, (fmt, key)
    # every client field not re-injected by the format was stripped
    for key in ("reasoning_effort", "reasoning", "thinking", "enable_thinking"):
        if key not in expected:
            assert key not in sent, (fmt, key)
    if fmt != "qwen-chat-template":
        assert sent["chat_template_kwargs"] == {"keep": 1}
    ring = ring_record(client)
    assert ring["reasoning"] == level
    assert ring["reasoning_warning"] is None
    # the record names the unified-tree provenance the decision used
    assert ring["tree_id"] == "default-tree"
    assert ring["tree_revision"] == h.app.state.snapshot.tree_revision


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
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)
    declare(client, {"format": fmt, "efforts": efforts})
    set_leaf_reasoning(client, "off")
    r = chat(client, reasoning_effort="client", thinking="client",
             enable_thinking=True)
    assert r.status_code == 200, r.text
    assert r.headers["x-sr-reasoning"] == "off"
    sent = h.requests[-1]["body"]
    for key, value in expected.items():
        assert sent[key] == value, (fmt, key)
    for key in ("reasoning_effort", "reasoning", "thinking", "enable_thinking"):
        if key not in expected:
            assert key not in sent, (fmt, key)


def test_model_level_format_wins_over_provider(tmp_path):
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)
    # provider says deepseek, the model overrides to qwen
    declare(client, {"format": "qwen", "efforts": {"off": None, "on": None}},
            provider_format="deepseek")
    set_leaf_reasoning(client, "on")
    assert chat(client).status_code == 200
    sent = h.requests[-1]["body"]
    assert sent["enable_thinking"] is True
    assert "thinking" not in sent


def test_model_without_format_inherits_provider_format(tmp_path):
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)
    declare(client, {"efforts": {"off": None, "high": None}},
            provider_format="deepseek")
    set_leaf_reasoning(client, "high")
    assert chat(client).status_code == 200
    sent = h.requests[-1]["body"]
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "high"       # null wire -> level id


# --------------------------------------------------------------------------- #
# 竞态降级：配置落后于声明 -> 透传 + warning，请求不失败
# --------------------------------------------------------------------------- #
def test_level_removed_from_declaration_degrades_to_passthrough(tmp_path):
    """Behavior preserved: a tree that selects a level the CURRENT model catalog
    no longer declares degrades to passthrough + warning, it never fails the
    user's request.  The stale configuration is published directly from the DB
    (the /admin model write now health-checks the active tree and would reject
    the inconsistent catalog)."""
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    set_leaf_reasoning(client, "high")
    # the catalog now only declares low while the active tree still says high
    rewrite_model_profile(h.app, "qwen3-8b",
                          {"reasoning": {"efforts": {"off": None, "low": "low"}}})
    r = chat(client, reasoning_effort="client-effort")
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning-warning"] == "unsupported"
    assert "x-sr-reasoning" not in r.headers
    sent = h.requests[-1]["body"]
    assert sent["reasoning_effort"] == "client-effort"   # passthrough, not stripped
    ring = ring_record(client)
    assert ring["reasoning"] == "high"
    assert ring["reasoning_warning"] == "unsupported"


def test_capability_removed_degrades_to_passthrough(tmp_path):
    """Behavior preserved: an explicitly unsupported declaration (False) also
    degrades to passthrough + warning instead of a 5xx."""
    h = make_harness(tmp_path)
    client = h.client
    activate_default_tree(client)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    set_leaf_reasoning(client, "high")
    rewrite_model_profile(h.app, "qwen3-8b", {"reasoning": False})
    r = chat(client, thinking={"type": "enabled"})
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning-warning"] == "unsupported"
    sent = h.requests[-1]["body"]
    assert sent["thinking"] == {"type": "enabled"}        # untouched
    ring = ring_record(client)
    assert ring["reasoning_warning"] == "unsupported"


def test_model_without_any_declaration_and_leaf_level(tmp_path):
    """A stale tree selecting a level for an undeclared model must not 5xx."""
    h = make_harness(tmp_path)
    client = h.client
    root = default_signal_tree()
    root["otherwise"]["reasoning"] = "high"
    insert_active_tree(h.app, root, tree_id="bypass-tree")

    r = chat(client)
    assert r.status_code == 200
    assert r.headers["x-sr-model"] == "qwen3-8b"
    assert r.headers["x-sr-reasoning-warning"] == "unsupported"
    assert "x-sr-reasoning" not in r.headers
    ring = ring_record(client)
    assert ring["reasoning"] == "high"
    assert ring["reasoning_warning"] == "unsupported"


# --------------------------------------------------------------------------- #
# 流式请求同样注入 + 响应头
# --------------------------------------------------------------------------- #
def test_streaming_request_injects_reasoning(tmp_path):
    transport = _StreamingTransport()
    h = make_harness(tmp_path, transport=transport)
    client = h.client
    activate_default_tree(client)
    declare(client, {"efforts": {"off": None, "high": "high"}})
    set_leaf_reasoning(client, "high")
    r = chat(client, stream=True)
    assert r.status_code == 200
    assert r.headers["x-sr-reasoning"] == "high"
    assert r.text == "data: hi\n\n"
    sent = transport.requests[-1]["body"]
    assert sent["reasoning_effort"] == "high"
    assert sent["stream"] is True
