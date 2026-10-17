from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings


def client(tmp_path):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"))))


def test_get_pipeline(tmp_path):
    body = client(tmp_path).get("/admin/pipeline").json()
    assert body["nodes"][2]["type"] == "score"
    assert body["nodes"][2]["branches"][0]["min_score"] == 0.45


def test_put_pipeline_roundtrip_and_snapshot_refresh(tmp_path):
    c = client(tmp_path)
    body = c.get("/admin/pipeline").json()
    body["nodes"][3]["branches"][0]["model"] = "qwen3-32b"
    r = c.put("/admin/pipeline", json=body)
    assert r.status_code == 200
    again = c.get("/admin/pipeline").json()
    assert again["nodes"][3]["branches"][0]["model"] == "qwen3-32b"


def test_put_pipeline_rejected_by_health(tmp_path):
    c = client(tmp_path)
    body = c.get("/admin/pipeline").json()
    body["nodes"] = body["nodes"][:1]          # drop the fallback node
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
