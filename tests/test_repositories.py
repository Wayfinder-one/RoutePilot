import pytest

from router.pipeline.model import Station
from router.store.db import connect, migrate
from router.store.repositories import (KeywordRepo, ModelRepo, SettingsRepo,
                                       StationRepo, default_pipeline,
                                       seed_default)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    migrate(c)
    yield c
    c.close()


def test_seed_idempotent(conn):
    seed_default(conn)
    seed_default(conn)
    stations = StationRepo(conn).load()
    assert [s.type for s in stations] == ["gate", "gate", "split", "gate"]
    assert len(ModelRepo(conn).list()) == 5
    assert "医疗急症" in KeywordRepo(conn).groups()


def test_station_save_bumps_version(conn):
    seed_default(conn)
    repo = StationRepo(conn)
    v0 = SettingsRepo(conn).get("pipeline_version", "0")
    stations = repo.load()
    stations[0].name = "改名"
    repo.save(stations)
    assert SettingsRepo(conn).get("pipeline_version") != v0
    assert repo.load()[0].name == "改名"


def test_station_roundtrip_tree(conn):
    seed_default(conn)
    stations = StationRepo(conn).load()
    subject = stations[2]
    assert subject.type == "split"
    assert subject.root.signal is None and subject.root.kind == "fork"
    health = subject.root.children[0]
    assert health.signal == "domain" and health.value == "health"
    assert health.conf_min == 0.40      # heuristic calibration
    leaves = {leaf.value: leaf for leaf in health.children}
    assert leaves["hard"].model == "qwen3-8b-medical"
    assert leaves["easy"].model == "qwen3-0.6b-medical"
    fallback = stations[-1]
    assert fallback.root.signal is None and fallback.root.action == "route"
    assert fallback.root.model == "qwen3-8b"


def test_checkpoint_calibration_is_higher_and_healthy(conn):
    from router.pipeline.health import check_pipeline
    seed_default(conn, "checkpoint")
    stations = StationRepo(conn).load()
    safety = stations[0]
    assert safety.root.children[0].conf_min == 0.99   # jailbreak (checkpoint)
    assert check_pipeline(stations, {m["id"] for m in ModelRepo(conn).list()}) == []


def test_default_pipeline_types():
    stations = default_pipeline("jev")
    assert len(stations) == 4
    assert isinstance(stations[0], Station)
    assert stations[0].root.kind == "fork"
