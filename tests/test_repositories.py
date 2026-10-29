import pytest

from router.routing.conditions import TimePeriod
from router.routing.model import tree_from_dict
from router.store.db import connect, migrate
from router.store.repositories import ModelRepo, TimePeriodRepo, model_references, seed_default
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import target_dict, tree_dict


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
    assert conn.execute("SELECT COUNT(*) FROM routing_trees").fetchone()[0] == 0


def test_time_periods_validate_and_roundtrip(conn):
    repo = TimePeriodRepo(conn)
    periods = [TimePeriod("夜间", (0, 1, 2, 3, 4, 5, 6), "18:00", "09:00").to_dict()]
    repo.save(periods)
    assert repo.load() == periods
    with pytest.raises(ValueError):
        repo.save([{"name": "坏", "days": [7], "start": "25:00", "end": "09:00"}])


def test_model_references_finds_every_target_in_saved_trees(conn):
    repo = RoutingTreeRepo(conn)
    raw = tree_dict(root=target_dict("pool", ("fast", "strong-a")))
    saved = repo.create(tree_from_dict(raw))
    refs = model_references(repo.list(), "strong-a")
    assert refs == [{"tree_id": "main", "tree_revision": saved.tree_revision, "node_id": "pool", "via": "target"}]
    assert model_references(repo.list(), "missing") == []
