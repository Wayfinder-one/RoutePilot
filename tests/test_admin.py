import json

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.store.db import connect


def client(tmp_path):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"))))


def test_get_pipeline(tmp_path):
    body = client(tmp_path).get("/admin/pipeline").json()
    assert [s["name"] for s in body["stations"]] == \
        ["① 安全闸", "② 关键词直达", "③ 学科 → 难度", "④ 兜底"]
    subject = body["stations"][2]
    assert subject["root"]["kind"] == "fork"
    health = subject["root"]["children"][0]
    assert health["signal"] == "domain" and health["value"] == "health"
    assert [c["value"] for c in health["children"]] == ["hard", "easy"]


def test_put_pipeline_roundtrip_and_snapshot_refresh(tmp_path):
    c = client(tmp_path)
    body = c.get("/admin/pipeline").json()
    body["stations"][3]["root"]["model"] = "qwen3-32b"
    r = c.put("/admin/pipeline", json=body)
    assert r.status_code == 200
    again = c.get("/admin/pipeline").json()
    assert again["stations"][3]["root"]["model"] == "qwen3-32b"


def test_put_pipeline_noop_does_not_bump_version_or_audit(tmp_path):
    """Re-saving an unchanged pipeline (console smoke check, editor no-op) must
    not bump pipeline_version or append an audit row (2026-10-17 review N3)."""
    c = client(tmp_path)
    before = c.get("/admin/pipeline").json()
    changes_before = len(c.get("/admin/changes").json()["changes"])
    r = c.put("/admin/pipeline", json=before)
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["version"] == before["version"]
    after = c.get("/admin/pipeline").json()
    assert after["version"] == before["version"]
    assert len(c.get("/admin/changes").json()["changes"]) == changes_before

    # a real edit still bumps the version and writes one audit row
    after["stations"][3]["root"]["model"] = "qwen3-32b"
    r = c.put("/admin/pipeline", json=after)
    assert r.json()["changed"] is True
    newer = c.get("/admin/pipeline").json()
    assert int(newer["version"]) == int(before["version"]) + 1
    assert len(c.get("/admin/changes").json()["changes"]) == changes_before + 1


def test_put_pipeline_rejected_by_health(tmp_path):
    c = client(tmp_path)
    body = c.get("/admin/pipeline").json()
    body["stations"] = body["stations"][:1]    # drop the fallback station
    r = c.put("/admin/pipeline", json=body)
    assert r.status_code == 422
    assert any(i["code"] == "missing_fallback" for i in r.json()["detail"])


def test_signals_endpoint_includes_keyword_groups(tmp_path):
    body = client(tmp_path).get("/admin/signals").json()
    assert len(body["signals"]) == 15
    assert "医疗急症" in body["keyword_groups"]


def test_overview(tmp_path):
    body = client(tmp_path).get("/admin/overview").json()
    assert {"daily", "routes", "blocked", "total"} <= set(body)


def test_put_model_updates_and_swaps_snapshot(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db")))
    assert app.state.backends["qwen3-8b"].upstream_name == "qwen3-8b"
    c = TestClient(app)
    r = c.put("/admin/models/qwen3-8b", json={"upstream": "real-model"})
    assert r.status_code == 200
    assert r.json()["model"]["upstream"] == "real-model"
    # in-memory backend snapshot was swapped wholesale (design 4.4)
    assert app.state.backends["qwen3-8b"].upstream_name == "real-model"


def test_put_model_unknown_is_404(tmp_path):
    r = client(tmp_path).put("/admin/models/nope", json={"upstream": "x"})
    assert r.status_code == 404


def test_put_model_accepts_dict_profile(tmp_path):
    c = client(tmp_path)
    r = c.put("/admin/models/qwen3-8b", json={"profile": {"domains": ["math"]}})
    assert r.status_code == 200
    assert r.json()["model"]["profile"] == '{"domains": ["math"]}'


def test_put_provider_upserts_and_swaps_snapshot(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db")))
    c = TestClient(app)
    r = c.put("/admin/providers/local",
              json={"base_url": "http://example.internal:9/v1", "key_env": ""})
    assert r.status_code == 200
    provs = c.get("/admin/providers").json()["providers"]
    assert any(p["name"] == "local"
               and p["base_url"] == "http://example.internal:9/v1" for p in provs)
    assert app.state.backends["qwen3-8b"].url == \
        "http://example.internal:9/v1/chat/completions"


def test_settings_roundtrip_and_reserved(tmp_path):
    c = client(tmp_path)
    r = c.put("/admin/settings", json={"default_note": "hello", "n": 3})
    assert r.status_code == 200
    assert r.json()["settings"]["default_note"] == "hello"
    assert r.json()["settings"]["n"] == "3"
    assert c.put("/admin/settings",
                 json={"pipeline_version": "9"}).status_code == 422
    assert c.put("/admin/settings", json={"_x": 1}).status_code == 422
    assert c.put("/admin/settings", json={"bad": {}}).status_code == 422


def test_changes_audit_trail(tmp_path):
    c = client(tmp_path)
    pipe = c.get("/admin/pipeline").json()
    pipe["stations"][3]["root"]["model"] = "qwen3-32b"        # a real change
    c.put("/admin/pipeline", json=pipe)
    c.put("/admin/models/qwen3-8b", json={"upstream": "real"})
    c.put("/admin/providers/local", json={"base_url": "http://x:1/v1"})
    c.put("/admin/settings", json={"note": "n"})
    changes = c.get("/admin/changes").json()["changes"]
    kinds = {ch["kind"] for ch in changes}
    assert {"pipeline", "model", "provider", "settings"} <= kinds
    # newest first
    assert changes[0]["kind"] == "settings"


def test_overview_has_confidence_and_evals(tmp_path):
    body = client(tmp_path).get("/admin/overview").json()
    assert {"daily", "routes", "blocked", "total", "confidence", "evals",
            "confidence_since"} <= set(body)
    assert len(body["confidence"]) == 5
    assert body["confidence_since"]
    assert body["evals"] == []


def test_confidence_histogram_counts_model_signals_only(tmp_path):
    """Rule signals (c == 1) must not swamp the confidence dashboard, and the
    histogram is counted in memory: no request_signals rows exist any more."""
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=httpx.MockTransport(
                         lambda r: httpx.Response(200, json={"choices": []})))
    c = TestClient(app)
    c.post("/v1/chat/completions",
           json={"messages": [{"role": "user", "content": "今天天气怎么样"}]})
    body = c.get("/admin/overview").json()
    counted = sum(b["count"] for b in body["confidence"])
    rec = app.state.requestlog.recent_requests(1)[0]
    model_signals = [s for s in rec["signals"].values()
                     if s["confidence"] is not None and not s["is_rule"]]
    rule_signals = [s for s in rec["signals"].values() if s["is_rule"]]
    assert rule_signals                       # the request did carry rule signals
    assert counted == len(model_signals)



def test_try_reports_vec_dim(tmp_path):
    body = client(tmp_path).post("/admin/try", json={"text": "你好"}).json()
    assert body["vec_dim"] == 0            # heuristic has no embedding


def questions_client(tmp_path):
    q = tmp_path / "questions.json"
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"),
                                          signal_questions=str(q)))), q


def test_signal_questions_roundtrip_and_audit(tmp_path):
    c, q = questions_client(tmp_path)
    body = c.get("/admin/signal-questions").json()
    assert body["config"]["signals"]["pii"]["types"]["cn_id_card"]["zh"] == "身份证号"
    assert body["meta"]["questions_path"] == str(q)
    cfg = body["config"]
    cfg["signals"]["domain"]["instructions"] = ["请判断这段文本的学科门类。"]
    changes_before = len(c.get("/admin/changes").json()["changes"])
    r = c.put("/admin/signal-questions", json={"config": cfg})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    saved = json.loads(q.read_text(encoding="utf-8"))
    assert saved["signals"]["domain"]["instructions"] == ["请判断这段文本的学科门类。"]
    assert len(c.get("/admin/changes").json()["changes"]) == changes_before + 1
    kinds = [ch["kind"] for ch in c.get("/admin/changes").json()["changes"]]
    assert kinds[0] == "signal_questions"


def test_signal_questions_rejects_invalid_config(tmp_path):
    c, q = questions_client(tmp_path)
    cfg = c.get("/admin/signal-questions").json()["config"]
    cfg["signals"].pop("pii")
    r = c.put("/admin/signal-questions", json={"config": cfg})
    assert r.status_code == 422
    assert not q.exists()                      # nothing written on validation failure


def test_signal_questions_without_signals_object_is_422(tmp_path):
    """A config without a 'signals' object must be a clean 422 (the old code
    raised NameError there and the admin PUT answered 500)."""
    c, q = questions_client(tmp_path)
    r = c.put("/admin/signal-questions", json={"config": {"foo": 1}})
    assert r.status_code == 422
    assert "signals" in str(r.json()["detail"])
    assert not q.exists()


def test_signal_questions_rolls_the_memory_back_when_the_write_fails(tmp_path,
                                                                     monkeypatch):
    """The in-memory config is applied first; a failed disk write must roll it
    back, so memory and disk stay in the same state. The PUT carries a
    *modified* config so a rollback to the wrong object cannot pass the
    assertion (2026-10-22 review)."""
    import pathlib

    app = create_app(Settings(db_path=str(tmp_path / "t.db"),
                              signal_questions=str(tmp_path / "questions.json")))
    c = TestClient(app)
    original = c.get("/admin/signal-questions").json()["config"]
    app.state.tagger.questions = original
    modified = json.loads(json.dumps(original))
    modified["signals"]["pii"]["true"] = "是的，包含{zh}"
    applied = []

    def fake_set(cfg):
        app.state.tagger.questions = cfg
        applied.append(cfg)

    app.state.tagger.set_questions = fake_set

    def boom(self, *args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    r = c.put("/admin/signal-questions", json={"config": modified})
    assert r.status_code == 500
    assert applied == [modified, original]      # applied, then rolled back
    assert app.state.tagger.questions == original
    assert not (tmp_path / "questions.json").exists()   # nothing written


def test_signal_questions_write_is_atomic_and_leaves_no_tmp(tmp_path,
                                                            monkeypatch):
    """The config file lands via named tmp + os.replace: a crashed half-write
    must never become the working config, and a failed save leaves no .tmp
    litter (2026-10-22 review)."""
    import pathlib

    app = create_app(Settings(db_path=str(tmp_path / "t.db"),
                               signal_questions=str(tmp_path / "questions.json")))
    c = TestClient(app)
    cfg = c.get("/admin/signal-questions").json()["config"]
    cfg["signals"]["pii"]["true"] = "是的，包含{zh}"
    target = tmp_path / "questions.json"
    r = c.put("/admin/signal-questions", json={"config": cfg})
    assert r.status_code == 200
    assert json.loads(target.read_text(encoding="utf-8"))["signals"]["pii"] \
        ["true"] == "是的，包含{zh}"
    assert not list(tmp_path.glob("*.tmp"))     # renamed away, no litter

    def boom(self, *args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    r = c.put("/admin/signal-questions", json={"config": cfg})
    assert r.status_code == 500
    assert not list(tmp_path.glob("*.tmp"))     # cleaned up on failure
    assert json.loads(target.read_text(encoding="utf-8"))["signals"]["pii"] \
        ["true"] == "是的，包含{zh}"             # config file untouched


def test_put_settings_persists_without_the_audit_call(tmp_path, monkeypatch):
    """A setting must not depend on a later, unrelated write to be persisted
    (the whole write is one transaction now)."""
    import router.api.routes_admin as routes_admin

    monkeypatch.setattr(routes_admin.ChangeRepo, "record",
                        lambda self, ts, kind, detail: None)
    c = client(tmp_path)
    assert c.put("/admin/settings", json={"foo": "bar"}).status_code == 200
    conn = connect(str(tmp_path / "t.db"))
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'foo'").fetchone()
    finally:
        conn.close()
    assert row is not None and row["value"] == "bar"


def test_tagger_meta_and_recent_requests(tmp_path):
    c = client(tmp_path)
    tagger = c.get("/admin/tagger").json()["tagger"]
    assert tagger["name"] == "heuristic"
    assert tagger["questions_path"].endswith("signal_questions.json")
    assert c.get("/admin/requests/recent").json() == {"requests": []}
    assert c.get("/admin/requests/recent?limit=1000").status_code == 200


def test_time_periods_roundtrip(tmp_path):
    c = client(tmp_path)
    resp = c.get("/admin/time-periods").json()
    assert "now" in resp
    assert [p["name"] for p in resp["periods"]] == ["白天", "夜间", "周末"]

    custom = [{"name": "夜班", "days": [0, 1, 2, 3, 4, 5, 6],
               "start": "22:00", "end": "06:00"}]
    assert c.put("/admin/time-periods", json={"periods": custom}).status_code == 200
    assert c.get("/admin/time-periods").json()["periods"] == custom
    # persisted for a fresh client
    assert client(tmp_path).get("/admin/time-periods").json()["periods"] == custom

    for bad in ({"periods": [{"name": "", "days": [1], "start": "08:00", "end": "09:00"}]},
                {"periods": [{"name": "x", "days": [7], "start": "08:00", "end": "09:00"}]},
                {"periods": [{"name": "x", "days": [1], "start": "8点", "end": "09:00"}]},
                {"periods": [{"name": "dup", "days": [1], "start": "08:00", "end": "09:00"},
                             {"name": "dup", "days": [2], "start": "08:00", "end": "09:00"}]}):
        assert c.put("/admin/time-periods", json=bad).status_code == 422


def test_emptied_time_periods_survive_restart(tmp_path):
    """PUT [] is a deliberate delete; the next boot must not resurrect the
    seeded defaults — seeding is marker-driven, not table-state-driven."""
    c = client(tmp_path)
    assert c.put("/admin/time-periods", json={"periods": []}).status_code == 200
    assert client(tmp_path).get("/admin/time-periods").json()["periods"] == []


def test_signal_questions_unreadable_config_falls_back_with_warning(tmp_path):
    """A broken/missing configured question file silently served the built-in
    defaults: the console edited one config while the running tagger used
    another. The fallback must be visible in the response (2026-10-22)."""
    app = create_app(Settings(db_path=str(tmp_path / "t.db"),
                               signal_questions=str(tmp_path / "gone.json")))
    c = TestClient(app)
    body = c.get("/admin/signal-questions").json()
    assert body["config"]["signals"]          # defaults are served ...
    assert "unreadable" in body["warning"]    # ... and the fallback is loud
    assert "gone.json" in body["warning"]


def test_signal_questions_no_warning_when_the_file_reads(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"),
                               signal_questions=str(tmp_path / "questions.json")))
    c = TestClient(app)
    cfg = c.get("/admin/signal-questions").json()["config"]   # defaults
    assert c.put("/admin/signal-questions",
                 json={"config": cfg}).status_code == 200     # writes the file
    assert "warning" not in c.get("/admin/signal-questions").json()


def test_recent_requests_carry_trace(tmp_path):
    """The console renders the decision trace: it must ride on the record."""
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=httpx.MockTransport(
                         lambda r: httpx.Response(200, json={"choices": []})))
    c = TestClient(app)
    c.post("/v1/chat/completions",
           json={"messages": [{"role": "user", "content": "今天天气怎么样"}]})
    recent = c.get("/admin/requests/recent").json()["requests"]
    assert recent, "ring should hold the request"
    rec = recent[0]
    assert isinstance(rec["trace"], list) and rec["trace"]
    stations = [t["result"] for t in rec["trace"]]
    assert "hit" in stations                      # the walker stopped on a hit
    hit = next(t for t in rec["trace"] if t["result"] == "hit")
    assert hit["path"] is not None                # path/board keys exist


def test_block_decision_also_records_trace(tmp_path):
    """Blocks are logged with their trace too: moving enqueue after the block
    return would silently drop them from the console."""
    c = client(tmp_path)
    pipe = c.get("/admin/pipeline").json()
    pipe["stations"][0]["root"]["children"].append(
        {"id": "n1z", "signal": "language", "value": "zh", "conf_min": None,
         "kind": "leaf", "action": "block", "model": None, "min_score": None,
         "weights": [], "children": []})
    assert c.put("/admin/pipeline", json=pipe).status_code == 200
    r = c.post("/v1/chat/completions",
               json={"messages": [{"role": "user", "content": "今天天气怎么样"}]})
    assert r.status_code == 403
    rec = c.get("/admin/requests/recent").json()["requests"][0]
    assert rec["decision"] == "block"
    assert isinstance(rec["trace"], list) and rec["trace"]
