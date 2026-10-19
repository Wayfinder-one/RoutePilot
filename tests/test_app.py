from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings


def client(tmp_path):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"))))


def test_healthz(tmp_path):
    r = client(tmp_path).get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_console_mounted(tmp_path):
    r = client(tmp_path).get("/console/index.html")
    assert r.status_code in (200, 404)   # 404 until Task 18 adds the pages


def test_pipeline_seeded(tmp_path):
    r = client(tmp_path).get("/admin/pipeline")
    assert r.status_code == 200
    assert [st["type"] for st in r.json()["stations"]] == \
        ["gate", "gate", "split", "gate"]
