import json
from types import SimpleNamespace

from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevConfigRepo, JevSettings
from router.routing.model import tree_from_dict
from router.routing.snapshot import SnapshotPublisher, load_snapshot
from router.store.db import connect, migrate
from router.store.repositories import ModelRepo, ProviderRepo, TimePeriodRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import target_dict, tree_dict


def prepare_db(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    ProviderRepo(conn).upsert("local", "http://upstream.test/v1", api_key="upstream-secret")
    for model in ("fast", "standard"):
        ModelRepo(conn).create(model, model, "local", json.dumps({"capability": f"{model} 能力"}))
    TimePeriodRepo(conn).save([{"name": "白天", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"}])
    JevConfigRepo(conn).save(JevSettings("http://jev.test", "NeoHorse-Jev-4B", "jev-secret", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE))
    repo = RoutingTreeRepo(conn)
    repo.create(tree_from_dict(tree_dict(root=target_dict("target", ("fast",)))))
    repo.enable("main")
    return conn


def test_snapshot_loads_current_config_and_revision_excludes_secrets(tmp_path):
    conn = prepare_db(tmp_path)
    snapshot = load_snapshot(conn)
    assert snapshot.tree.id == "main"
    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer upstream-secret"
    assert snapshot.capabilities == {"fast": "fast 能力", "standard": "standard 能力"}
    assert snapshot.jev.model == "NeoHorse-Jev-4B"
    stored = conn.execute("SELECT content FROM config_revisions WHERE kind = 'snapshot'").fetchone()
    assert stored is not None
    assert "upstream-secret" not in stored[0]
    assert "jev-secret" not in stored[0]
    assert snapshot.tree_revision == snapshot.tree.tree_revision
    conn.close()
    check = connect(str(tmp_path / "v3.db"))
    assert check.execute("SELECT COUNT(*) FROM config_revisions WHERE kind = 'snapshot'").fetchone()[0] == 1
    check.close()


def test_publisher_swaps_whole_snapshot_without_mutating_old_one(tmp_path):
    conn = prepare_db(tmp_path)
    app = SimpleNamespace(state=SimpleNamespace())
    publisher = SnapshotPublisher(app)
    first = publisher.refresh(conn)
    ModelRepo(conn).update("fast", upstream="new-upstream")
    second = publisher.refresh(conn)
    assert first.backends["fast"].upstream_name == "fast"
    assert second.backends["fast"].upstream_name == "new-upstream"
    assert first.snapshot_revision != second.snapshot_revision
    assert app.state.snapshot is second
    conn.close()


def test_no_active_tree_is_a_valid_snapshot(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    snapshot = load_snapshot(conn)
    assert snapshot.tree is None
    assert snapshot.tree_id is None
    assert snapshot.backends == {}
    conn.close()
