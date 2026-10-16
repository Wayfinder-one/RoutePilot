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
