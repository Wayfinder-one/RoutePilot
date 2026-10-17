"""M4 tests: optional session continuation (design 3.7)."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings


def ok_upstream(request):
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def make(tmp_path, sessions=True):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), sessions=sessions),
                     transport=httpx.MockTransport(ok_upstream))
    return app, TestClient(app)


def chat(c, text, **headers):
    return c.post("/v1/chat/completions",
                  json={"messages": [{"role": "user", "content": text}]},
                  headers=headers)


MEDICAL = "我父亲病历上写着需要长期治疗，如何权衡方案？"


def test_sessions_off_no_state_header(tmp_path):
    _, c = make(tmp_path, sessions=False)
    r = chat(c, "今天天气怎么样", **{"x-sr-session": "s1"})
    assert "x-sr-session-state" not in r.headers


def test_sticky_overrides_pipeline(tmp_path):
    app, c = make(tmp_path)
    r1 = chat(c, MEDICAL, **{"x-sr-session": "s1"})
    assert r1.headers["x-sr-session-state"] == "new"
    assert r1.headers["x-sr-model"] == "qwen3-8b-medical"
    asyncio.run(app.state.requestlog.flush())
    # a text the pipeline would send to the fallback, but the session sticks
    r2 = chat(c, "今天天气怎么样", **{"x-sr-session": "s1"})
    assert r2.headers["x-sr-session-state"] == "sticky"
    assert r2.headers["x-sr-model"] == "qwen3-8b-medical"


def test_wrong_answer_escalates_a_tier(tmp_path):
    app, c = make(tmp_path)
    r1 = chat(c, "今天天气怎么样", **{"x-sr-session": "s2"})
    assert r1.headers["x-sr-model"] == "qwen3-8b"          # small tier
    asyncio.run(app.state.requestlog.flush())
    r2 = chat(c, "再问一次", **{"x-sr-session": "s2",
                             "x-sr-feedback": "wrong_answer"})
    assert r2.headers["x-sr-session-state"] == "unlocked"
    assert r2.headers["x-sr-model"] == "deepseek-v3"       # one tier up (large)


def test_a_new_session_does_not_stick(tmp_path):
    app, c = make(tmp_path)
    chat(c, MEDICAL, **{"x-sr-session": "a"})
    asyncio.run(app.state.requestlog.flush())
    r = chat(c, "今天天气怎么样", **{"x-sr-session": "b"})   # different session
    assert r.headers["x-sr-session-state"] == "new"
    assert r.headers["x-sr-model"] == "qwen3-8b"
