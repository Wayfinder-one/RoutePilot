import asyncio
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
    assert [s["type"] for s in body["stations"]] == ["gate", "gate", "split", "gate"]
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
    assert len(body["signals"]) == 17
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
    assert {"daily", "routes", "blocked", "total", "confidence", "evals"} <= set(body)
    assert len(body["confidence"]) == 5
    assert body["evals"] == []


def test_confidence_histogram_counts_model_signals_only(tmp_path):
    """Rule signals (c == 1) must not swamp the confidence dashboard."""
    app = create_app(Settings(db_path=str(tmp_path / "t.db")),
                     transport=httpx.MockTransport(
                         lambda r: httpx.Response(200, json={"choices": []})))
    c = TestClient(app)
    c.post("/v1/chat/completions",
           json={"messages": [{"role": "user", "content": "今天天气怎么样"}]})
    asyncio.run(app.state.requestlog.flush())
    body = c.get("/admin/overview").json()
    counted = sum(b["count"] for b in body["confidence"])
    conn = connect(str(tmp_path / "t.db"))
    model_signals = conn.execute(
        "SELECT COUNT(*) FROM request_signals "
        "WHERE confidence IS NOT NULL AND is_rule = 0").fetchone()[0]
    rule_signals = conn.execute(
        "SELECT COUNT(*) FROM request_signals WHERE is_rule = 1").fetchone()[0]
    conn.close()
    assert rule_signals > 0                    # the request did carry rule signals
    assert counted == model_signals



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
