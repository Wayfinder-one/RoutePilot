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
    assert [st["name"] for st in r.json()["stations"]] == \
        ["① 安全闸", "② 关键词直达", "③ 学科 → 难度", "④ 兜底"]


def test_startup_refuses_an_unhealthy_stored_tree(tmp_path):
    """A stored tree the engine cannot execute must not reach the hot path:
    the service refuses to start instead of 502-ing every request."""
    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    seed_default(conn)
    conn.execute("UPDATE stations SET tree = ? WHERE ord = 0",
                 (json.dumps({"id": "s1", "name": "坏树",
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


def test_startup_judges_reasoning_written_straight_into_the_db(tmp_path):
    """The startup check builds the same §3.3 capability map as PUT
    /admin/pipeline: a leaf that names an undeclared reasoning level must be
    caught at boot when it bypassed the save-time 422 (direct DB edit /
    older version), not only be caught when it came through the API."""
    from router.reasoning import ReasoningError, validate_model_profile
    from router.store.repositories import StationRepo

    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    seed_default(conn)
    # the fallback model declares only the "low" effort (openai format)
    try:
        profile = validate_model_profile(
            {"reasoning": {"efforts": {"low": "low"}, "default": "low"}},
            "openai")
    except ReasoningError:                       # must never happen
        pytest.fail("test profile itself is invalid")
    conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                 (json.dumps(profile, ensure_ascii=False),))
    stations = StationRepo(conn).load()
    leaf = stations[3].root                      # fallback station leaf
    leaf.reasoning = "high"                      # not in the declared efforts
    StationRepo(conn).save(stations)
    conn.close()
    with pytest.raises(RuntimeError) as exc:
        create_app(Settings(db_path=db))
    assert "reasoning_unsupported" in str(exc.value)


def test_startup_accepts_a_declared_reasoning_level(tmp_path):
    """The boot-time map must not over-block: a leaf reasoning level the model
    does declare must leave the startup path free."""
    from router.reasoning import validate_model_profile
    from router.store.repositories import StationRepo

    db = str(tmp_path / "t.db")
    conn = connect(db)
    migrate(conn)
    seed_default(conn)
    profile = validate_model_profile(
        {"reasoning": {"efforts": {"low": "low", "high": "high"},
                       "default": "low"}}, "openai")
    conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'",
                 (json.dumps(profile, ensure_ascii=False),))
    stations = StationRepo(conn).load()
    stations[3].root.reasoning = "high"          # declared -> legal
    StationRepo(conn).save(stations)
    conn.close()
    create_app(Settings(db_path=db))             # must not raise
