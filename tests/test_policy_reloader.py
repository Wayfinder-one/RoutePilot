"""PolicyReloadWorker：旧进程只在策略指纹变化后替换快照。

两进程文件变化场景（写 v4 bytes）：writer 侧 PolicyRepo 写策略文档与布局；
旧进程（reader）持有已发布快照，凭 PolicyRepo.fingerprint（策略文件 + current
指针）决定是否重建。布局是前端私有文档，写入不改变指纹，绝不触发快照替换。
"""
import asyncio
import json
from types import SimpleNamespace

from router.config import Settings
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.routing.policy import policy_from_dict
from router.routing.reloader import PolicyReloadWorker
from router.routing.snapshot import SnapshotPublisher
from router.store.db import connect, migrate
from router.store.jev_config import JevConfigRepo
from router.store.policies import PolicyRepo
from router.store.repositories import ModelRepo, ProviderRepo
from tests.routing_fixtures import policy_dict


def _seed(conn):
    ProviderRepo(conn).upsert("local", "http://upstream.test/v1")
    for model in ("fast", "strong"):
        ModelRepo(conn).create(model, model, "local", json.dumps({"capability": f"{model} 能力"}))
    JevConfigRepo(conn).save(JevSettings(
        "http://jev.test", "NeoHorse-Jev-4B", "jev-secret", 5,
        DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE,
    ))


def test_worker_swaps_snapshot_only_after_fingerprint_change(tmp_path):
    settings = Settings(db_path=str(tmp_path / "control.db"), policy_dir=str(tmp_path / "routing-policy"))
    conn = connect(settings.db_path)
    migrate(conn)
    _seed(conn)
    writer = PolicyRepo(settings.policy_dir)
    writer.initialize()
    writer.create("main", policy_from_dict(policy_dict()))
    writer.enable("main")

    # 旧进程：只读仓库 + 启动时发布一次，此后仅凭指纹变化决定是否替换。
    reader = PolicyRepo(settings.policy_dir, writer=False)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot=None))
    app.state.publisher = SnapshotPublisher(app, reader)
    initial = app.state.publisher.refresh(conn)

    async def scenario():
        worker = PolicyReloadWorker(app, poll_seconds=0.01)
        await worker.start()
        try:
            # 未变化：两次轮询都不替换。
            await worker.poll_once()
            await worker.poll_once()
            assert app.state.snapshot is initial

            # 布局写入不改变指纹：旧进程不替换快照。
            writer.put_layout("main", {"panels": {"rules": [10, 40]}, "zoom": 0.8})
            await worker.poll_once()
            assert app.state.snapshot is initial

            # 策略文档变化（v4 bytes）→ 指纹变化 → 后台循环换上新快照。
            updated = policy_from_dict(
                dict(policy_dict(), sticky={"enabled": True, "ttl_seconds": 60}))
            writer.update(
                "main", updated,
                base_document_sha256=writer.get("main").document_sha256,
            )
            for _ in range(100):
                if app.state.snapshot is not initial:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("background reload did not converge within one second")
        finally:
            await worker.close()
            conn.close()

    asyncio.run(scenario())

    swapped = app.state.snapshot
    assert swapped.policy.sticky.ttl_seconds == 60
    assert swapped.snapshot_revision != initial.snapshot_revision
    status = app.state.publisher.status()
    assert status["stale"] is False
    assert status["active_id"] == "main"
    assert status["document_sha256"] == reader.fingerprint()[1]
