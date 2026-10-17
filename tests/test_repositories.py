import pytest

from router.pipeline.model import Node
from router.store.db import connect, migrate
from router.store.repositories import (KeywordRepo, ModelRepo, PipelineRepo,
                                       SettingsRepo, seed_default)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    migrate(c)
    yield c
    c.close()


def test_seed_idempotent(conn):
    seed_default(conn)
    seed_default(conn)
    nodes = PipelineRepo(conn).load()
    assert [n.type for n in nodes] == ["gate", "gate", "score", "gate"]
    assert len(ModelRepo(conn).list()) == 5
    assert "医疗急症" in KeywordRepo(conn).groups()


def test_pipeline_save_bumps_version(conn):
    seed_default(conn)
    repo = PipelineRepo(conn)
    v0 = SettingsRepo(conn).get("pipeline_version", "0")
    nodes = repo.load()
    nodes[0].name = "改名"
    repo.save(nodes)
    assert SettingsRepo(conn).get("pipeline_version") != v0
    assert repo.load()[0].name == "改名"


def test_pipeline_roundtrip_types(conn):
    seed_default(conn)
    nodes = PipelineRepo(conn).load()
    score = nodes[2]
    assert isinstance(score, Node)
    assert score.branches[0].rows[0].weight == 0.5
    assert score.branches[0].min_score == 0.45
    gate = nodes[0]
    assert gate.branches[0].rows[0].conf_min == 0.45


def test_checkpoint_calibration_is_higher_and_healthy(conn):
    from router.pipeline.health import check_pipeline
    seed_default(conn, "checkpoint")
    nodes = PipelineRepo(conn).load()
    assert nodes[0].branches[0].rows[0].conf_min == 0.60
    assert nodes[2].branches[0].min_score == 0.55
    assert nodes[2].branches[1].min_score == 0.45
    known = {m["id"] for m in ModelRepo(conn).list()}
    assert check_pipeline(nodes, known) == []
