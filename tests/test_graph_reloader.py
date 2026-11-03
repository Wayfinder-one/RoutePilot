import asyncio
import json
from types import SimpleNamespace

from router.config import Settings
from router.routing.model import tree_from_dict
from router.routing.reloader import GraphReloadWorker
from router.routing.snapshot import SnapshotPublisher
from router.store.db import connect, migrate
from router.store.repositories import ModelRepo, ProviderRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import tree_dict


def direct_graph(model):
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "target", "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-target", "source": "start", "source_port": "out",
            "target": "target", "target_port": "in_1", "condition": None,
        }],
        "otherwise_target": "target", "error_fallback": "target",
    }


def test_worker_background_loop_loads_changed_file(tmp_path):
    settings = Settings(db_path=str(tmp_path / "control.db"), graph_dir=str(tmp_path / "routing-graph"))
    conn = connect(settings.db_path)
    migrate(conn)
    ProviderRepo(conn).upsert("local", "http://upstream.test/v1")
    ModelRepo(conn).create("fast", "fast", "local", json.dumps({"capability": "快速"}))
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(root=direct_graph("fast"))))
    repo.enable("main")
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot=None))
    app.state.publisher = SnapshotPublisher(app, repo)
    app.state.publisher.refresh(conn)

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
            conn.close()

    asyncio.run(scenario())
