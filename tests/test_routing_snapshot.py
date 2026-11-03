import asyncio
import json
from types import SimpleNamespace

import pytest

from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevConfigRepo, JevSettings
from router.config import Settings
from router.routing.document import graph_document_bytes
from router.routing.model import tree_from_dict
from router.routing.reloader import GraphReloadWorker
from router.routing.snapshot import SnapshotPublisher, load_snapshot
from router.store.db import connect, migrate
from router.store.repositories import ModelRepo, ProviderRepo
from router.store.routing_trees import GraphStorageError, RoutingTreeRepo
from tests.routing_fixtures import tree_dict


def direct_graph(model):
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
            {"id": "target", "type": "target", "priority": 1,
             "position": {"x": 200, "y": 0},
             "models": [{"model": model, "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-target", "source": "start", "source_port": "out",
            "target": "target", "target_port": "in_1", "condition": None,
        }],
        "otherwise_target": "target", "error_fallback": "target",
    }


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "control.db"), graph_dir=str(tmp_path / "routing-graph"))


@pytest.fixture
def conn(settings):
    connection = connect(settings.db_path)
    migrate(connection)
    ProviderRepo(connection).upsert("local", "http://upstream.test/v1", api_key="upstream-secret")
    for model in ("fast", "standard"):
        ModelRepo(connection).create(model, model, "local", json.dumps({"capability": f"{model} 能力"}))
    JevConfigRepo(connection).save(JevSettings("http://jev.test", "NeoHorse-Jev-4B", "jev-secret", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE))
    yield connection
    connection.close()


@pytest.fixture
def repo(settings):
    repository = RoutingTreeRepo(settings.graph_dir)
    repository.initialize()
    repository.create(tree_from_dict(tree_dict(root=direct_graph("fast"))))
    repository.enable("main")
    return repository


@pytest.fixture
def app(settings, conn, repo):
    instance = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot=None))
    instance.state.publisher = SnapshotPublisher(instance, repo)
    instance.state.publisher.refresh(conn)
    return instance


def test_snapshot_loads_current_config_and_revision_excludes_secrets(conn, repo):
    snapshot = load_snapshot(conn, repo)
    assert snapshot.tree.id == "main"
    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer upstream-secret"
    assert snapshot.capabilities == {"fast": "fast 能力", "standard": "standard 能力"}
    assert snapshot.jev.model == "NeoHorse-Jev-4B"
    assert "upstream-secret" not in snapshot.content
    assert "jev-secret" not in snapshot.content
    assert snapshot.tree_revision == snapshot.tree.tree_revision
    assert conn.execute("SELECT COUNT(*) FROM config_revisions WHERE kind = 'snapshot'").fetchone()[0] == 0


def test_publisher_swaps_whole_snapshot_without_mutating_old_one(conn, repo):
    app = SimpleNamespace(state=SimpleNamespace())
    publisher = SnapshotPublisher(app, repo)
    first = publisher.refresh(conn)
    ModelRepo(conn).update("fast", upstream="new-upstream")
    second = publisher.refresh(conn)
    assert first.backends["fast"].upstream_name == "fast"
    assert second.backends["fast"].upstream_name == "new-upstream"
    assert first.snapshot_revision != second.snapshot_revision
    assert app.state.snapshot is second


def test_no_active_tree_is_a_valid_snapshot(tmp_path):
    settings = Settings(db_path=str(tmp_path / "control.db"), graph_dir=str(tmp_path / "routing-graph"))
    conn = connect(settings.db_path)
    migrate(conn)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    snapshot = load_snapshot(conn, repo)
    assert snapshot.tree is None
    assert snapshot.tree_id is None
    assert snapshot.backends == {}
    conn.close()


def test_bad_new_file_keeps_old_snapshot_and_recovers_after_repair(conn, repo, app):
    original = app.state.snapshot
    path = repo.root / "graphs" / "main.json"
    path.write_bytes(b"{broken")
    try:
        with pytest.raises(GraphStorageError):
            app.state.publisher.refresh(conn)
        assert app.state.snapshot is original
        assert app.state.publisher.status()["stale"] is True
    finally:
        path.write_bytes(graph_document_bytes(original.tree))
    app.state.publisher.refresh_if_changed(conn)
    assert app.state.publisher.status()["stale"] is False


@pytest.mark.parametrize(
    ("relative_path", "error_message"),
    [
        ("current", "current pointer is unavailable"),
        ("graphs/main.json", "active graph is unavailable: main"),
    ],
)
def test_first_fingerprint_read_failure_marks_stale_and_recovers(
    conn, repo, app, relative_path, error_message,
):
    original = app.state.snapshot
    path = repo.root / relative_path
    content = path.read_bytes()
    path.unlink()

    with pytest.raises(GraphStorageError, match=error_message):
        app.state.publisher.refresh_if_changed(conn)

    assert app.state.snapshot is original
    status = app.state.publisher.status()
    assert status["ready"] is True
    assert status["stale"] is True
    assert error_message in status["last_load_error"]

    path.write_bytes(content)
    app.state.publisher.refresh_if_changed(conn)
    recovered = app.state.publisher.status()
    assert recovered["stale"] is False
    assert recovered["last_load_error"] == ""
    assert recovered["document_sha256"] == status["document_sha256"]


def test_worker_polls_once_and_skips_unchanged_fingerprint(conn, repo, app, monkeypatch):
    calls = 0
    import router.routing.snapshot as snapshot_module
    original_load = snapshot_module.load_snapshot

    def counted_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "load_snapshot", counted_load)
    updated = tree_from_dict(tree_dict(
        root=direct_graph("fast"), sticky={"enabled": True, "ttl_seconds": 60},
    ))
    repo.update(updated, base_document_sha256=repo.get("main").document_sha256)
    worker = GraphReloadWorker(app, poll_seconds=0.01)
    asyncio.run(worker.poll_once())
    asyncio.run(worker.poll_once())
    assert calls == 1
    assert app.state.snapshot.tree.sticky.ttl_seconds == 60
    assert app.state.publisher.status()["document_sha256"] == repo.fingerprint()[1]


def test_worker_real_background_loop_loads_change(repo, app):
    updated = tree_from_dict(tree_dict(
        root=direct_graph("fast"), sticky={"enabled": True, "ttl_seconds": 60},
    ))
    repo.update(updated, base_document_sha256=repo.get("main").document_sha256)

    async def scenario():
        worker = GraphReloadWorker(app, poll_seconds=0.01)
        await worker.start()
        try:
            for _ in range(100):
                if app.state.snapshot.tree.sticky.ttl_seconds == 60:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("background reload did not converge within one second")
        finally:
            await worker.close()

    asyncio.run(scenario())
