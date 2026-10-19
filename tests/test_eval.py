import asyncio

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings


def ok_upstream(request):
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def client(tmp_path, **kw):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"), **kw),
                                 transport=httpx.MockTransport(ok_upstream)))


def chat(c, text):
    return c.post("/v1/chat/completions",
                  json={"messages": [{"role": "user", "content": text}]})


def test_eval_labels(tmp_path):
    c = client(tmp_path)

def lower_floors(c, value=0.4):
    """The seeded pipeline defaults every condition to a 0.70 confidence floor;
    the heuristic test tagger caps confidence at 0.5, so routing tests lower the
    floors explicitly (they test routing, not calibration)."""
    pipe = c.get("/admin/pipeline").json()

    def walk(node):
        if node.get("conf_min") is not None:
            node["conf_min"] = value
        for child in node.get("children") or []:
            walk(child)

    for station in pipe["stations"]:
        if station.get("root"):
            walk(station["root"])
    assert c.put("/admin/pipeline", json=pipe).status_code == 200
    return c


    r = c.post("/admin/eval", json={"mode": "labels", "labels": [
        {"text": "今天天气怎么样", "expect_model": "qwen3-8b"},
        {"text": "我父亲病历上写着需要长期治疗，如何权衡方案？",
         "expect_model": "qwen3-8b-medical"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["matched"] == 2
    assert body["rate"] == 1.0
    assert body["matrix"]
    # persisted and returned by GET
    evals = c.get("/admin/evals").json()["evals"]
    assert evals and evals[0]["mode"] == "labels" and evals[0]["matched"] == 2


def test_eval_replay_from_logged_requests(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=httpx.MockTransport(ok_upstream))
    c = TestClient(app)
    chat(c, "今天天气怎么样")
    chat(c, "我父亲病历上写着需要长期治疗，如何权衡方案？")
    asyncio.run(app.state.requestlog.flush())          # persist requests+signals
    body = c.post("/admin/eval", json={"mode": "replay", "days": 7}).json()
    assert body["total"] == 2 and body["matched"] == 2
    assert body["mode"] == "replay"


def test_eval_replay_detects_drift(tmp_path):
    """A pipeline edit after the fact shows up as a replay mismatch."""
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=httpx.MockTransport(ok_upstream))
    c = TestClient(app)
    chat(c, "今天天气怎么样")                            # -> qwen3-8b
    asyncio.run(app.state.requestlog.flush())
    pipe = c.get("/admin/pipeline").json()
    pipe["stations"][3]["root"]["model"] = "qwen3-32b"        # change the fallback
    assert c.put("/admin/pipeline", json=pipe).status_code == 200
    body = c.post("/admin/eval", json={"mode": "replay", "days": 7}).json()
    assert body["total"] == 1 and body["matched"] == 0
    assert body["mismatches"][0]["got"] == "qwen3-32b"


def test_eval_bad_mode(tmp_path):
    assert client(tmp_path).post("/admin/eval", json={"mode": "nope"}).status_code == 422
