import json

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.store.db import connect, migrate
from router.store.repositories import seed_default


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


def test_startup_refuses_an_unhealthy_stored_tree(tmp_path):
    """A stored tree the engine cannot execute must not reach the hot path:
    the service refuses to start instead of 502-ing every request."""
    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE stations SET tree = ? WHERE ord = 0",
                 (json.dumps({"id": "s1", "type": "gate", "name": "坏树",
                              "root": {"id": "r", "signal": "telepathy",
                                       "value": "yes", "conf_min": 0.5,
                                       "kind": "leaf", "action": "route",
                                       "model": "qwen3-8b", "weights": [],
                                       "children": []}}),))
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError) as exc:
        create_app(Settings(db_path=db))
    assert "unknown_signal" in str(exc.value)
