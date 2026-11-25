import asyncio
from types import SimpleNamespace

from router.config import Settings
from router.routing.tree import tree_from_dict
from router.routing.reloader import GraphReloadWorker
from router.routing.snapshot import SnapshotPublisher
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
        "error_fallback": "target",
    }


def test_worker_background_loop_loads_changed_file(tmp_path):
    # 重载器只轮询图目录：不创建 SQLite，也不需要连接。
    settings = Settings(db_path=str(tmp_path / "control.db"), graph_dir=str(tmp_path / "routing-graph"))
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(
        root=direct_graph("fast"), enabled=True, default=True)))
    app = SimpleNamespace(state=SimpleNamespace(
        settings=settings, snapshot_set=None, jev_api_key=""))
    app.state.publisher = SnapshotPublisher(app, repo)
    app.state.publisher.refresh()

    updated = tree_from_dict(tree_dict(
        root=direct_graph("fast"), sticky={"enabled": True, "ttl_seconds": 60},
        enabled=True, default=True,
    ))
    repo.update(updated, base_document_sha256=repo.get("main").document_sha256)

    async def scenario():
        worker = GraphReloadWorker(app, poll_seconds=0.01)
        await worker.start()
        try:
            for _ in range(100):
                if app.state.snapshot_set.default_snapshot.tree.sticky.ttl_seconds == 60:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("background reload did not converge within one second")
        finally:
            await worker.close()

    asyncio.run(scenario())
