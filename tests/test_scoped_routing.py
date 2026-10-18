"""Hot-path wiring for pipeline-aware trimming.

The /v1 handler derives the question scope from the in-memory pipeline snapshot
and passes it to the tagger; /admin/try must stay a full-pack debugging tool.
A fake tagger (no torch) captures the ``TagRequest`` so these tests pin the
wiring without a GPU.
"""

import httpx

import router.api.app as app_module
from router.api.app import create_app
from router.config import Settings
from router.signals.tagger import TagResult


class FakeTagger:
    name = "jev"

    def __init__(self):
        self.requests = []

    async def tag(self, req):
        self.requests.append(req)
        return TagResult(signals={})       # fallback gate routes qwen3-8b

    async def aclose(self):
        pass


def _make_client(tmp_path, monkeypatch, **settings_kw):
    fake = FakeTagger()
    monkeypatch.setattr(app_module, "_make_tagger", lambda settings, kw: fake)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": "ok"}}]}))
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), **settings_kw),
                     transport=transport)
    from fastapi.testclient import TestClient
    return TestClient(app), fake


def _chat(client, text="hello"):
    return client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": text}]})


def test_hot_path_passes_the_pipeline_scope(tmp_path, monkeypatch):
    client, fake = _make_client(tmp_path, monkeypatch)
    assert _chat(client).status_code == 200
    scope = fake.requests[-1].scope
    assert scope is not None and scope.trimmed
    assert scope.signals == frozenset({"jailbreak", "pii", "domain",
                                       "complexity"})
    assert scope.pii_types == frozenset({"cn_id_card"})


def test_admin_try_keeps_the_full_pack(tmp_path, monkeypatch):
    client, fake = _make_client(tmp_path, monkeypatch)
    assert client.post("/admin/try", json={"text": "hi"}).status_code == 200
    assert fake.requests[-1].scope is None


def test_hot_pipeline_edit_changes_the_scope(tmp_path, monkeypatch):
    """/admin/pipeline hot edit: the next request scores the new questions."""
    client, fake = _make_client(tmp_path, monkeypatch)
    _chat(client)
    assert "fact_check" not in fake.requests[-1].scope.signals

    pipe = client.get("/admin/pipeline").json()
    pipe["nodes"].insert(len(pipe["nodes"]) - 1, {
        "id": "n-fc", "type": "gate", "name": "需核实",
        "branches": [{"id": "b-fc", "name": "fact", "action": "route",
                      "model": "qwen3-8b",
                      "rows": [{"signal": "fact_check", "value": "needs_check",
                                "conf_min": 0.5}]}]})
    assert client.put("/admin/pipeline", json=pipe).status_code == 200
    assert _chat(client).status_code == 200
    assert "fact_check" in fake.requests[-1].scope.signals

    back = client.get("/admin/pipeline").json()
    back["nodes"] = [n for n in back["nodes"] if n["id"] != "n-fc"]
    assert client.put("/admin/pipeline", json=back).status_code == 200
    assert _chat(client).status_code == 200
    assert "fact_check" not in fake.requests[-1].scope.signals


def test_trimming_can_be_disabled_with_the_setting(tmp_path, monkeypatch):
    client, fake = _make_client(tmp_path, monkeypatch, jev_trim=False)
    assert _chat(client).status_code == 200
    assert fake.requests[-1].scope is None


def test_rule_only_pipeline_needs_no_model_questions(tmp_path, monkeypatch):
    """A tree that only reads rule signals yields an empty (but trimmed) scope."""
    client, fake = _make_client(tmp_path, monkeypatch)
    nodes = [
        {"id": "g-kw", "type": "gate", "name": "关键词",
         "branches": [{"id": "b-kw", "name": "急诊", "action": "route",
                       "model": "qwen3-8b",
                       "rows": [{"signal": "keyword", "value": "医疗急症",
                                 "conf_min": 0.5}]}]},
        {"id": "g-fb", "type": "gate", "name": "兜底",
         "branches": [{"id": "b-fb", "name": "默认", "action": "route",
                       "model": "qwen3-8b", "rows": []}]},
    ]
    assert client.put("/admin/pipeline", json={"nodes": nodes}).status_code == 200
    assert _chat(client).status_code == 200
    scope = fake.requests[-1].scope
    assert scope is not None and scope.trimmed
    assert scope.signals == frozenset()
    assert scope.pii_types == frozenset()
