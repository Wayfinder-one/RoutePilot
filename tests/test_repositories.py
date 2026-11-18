import pytest

from router.routing.conditions import TimePeriod
from router.routing.policy import policy_from_dict
from router.store.db import connect, migrate
from router.store.policies import PolicyRepo
from router.store.repositories import ModelRepo, TimePeriodRepo, model_references, seed_default
from tests.routing_fixtures import policy_dict


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "v3.db"))
    migrate(connection)
    yield connection
    connection.close()


def test_seed_is_idempotent_and_creates_no_routing_tree(conn):
    seed_default(conn)
    seed_default(conn)
    assert len(ModelRepo(conn).list()) == 5
    assert len(TimePeriodRepo(conn).load()) == 3
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "routing_trees" not in tables


def test_time_periods_validate_and_roundtrip(conn):
    repo = TimePeriodRepo(conn)
    periods = [TimePeriod("夜间", (0, 1, 2, 3, 4, 5, 6), "18:00", "09:00").to_dict()]
    repo.save(periods)
    assert repo.load() == periods
    with pytest.raises(ValueError):
        repo.save([{"name": "坏", "days": [7], "start": "25:00", "end": "09:00"}])


def test_model_references_finds_every_target_in_saved_policies(conn, tmp_path):
    repo = PolicyRepo(tmp_path / "routing-policy")
    repo.initialize()
    raw = policy_dict()
    raw["targets"]["pool"] = {"models": [{"model": "strong-a"}]}
    saved = repo.create("main", policy_from_dict(raw))
    refs = model_references(repo.list(), "strong-a")
    assert refs == [{
        "policy_id": "main",
        "policy_revision": saved.policy_revision,
        "target_id": "pool",
        "via": "target",
    }]
    assert model_references(repo.list(), "missing") == []
