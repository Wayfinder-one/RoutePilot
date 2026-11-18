"""多副本策略目录：writer 进程写文件，reader 进程凭指纹跟进并扛住坏文件。

两个进程共享同一 policy 目录与数据库：writer 侧 PolicyRepo 落 v4 bytes
（策略文档、current 指针、布局），reader 侧只读同一目录并发布内存快照。
布局写入绝不触发替换；坏文件期间 reader 继续用内存快照并标记 stale；
writer 关闭策略后 reader 发布空快照，重新启用后恢复。
"""
import json
from types import SimpleNamespace

import pytest

from router.config import Settings
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.routing.policy import policy_from_dict, policy_revision_of
from router.routing.snapshot import SnapshotPublisher
from router.store.db import connect, migrate
from router.store.jev_config import JevConfigRepo
from router.store.policies import PolicyRepo, PolicyStorageError
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


def _replicas(tmp_path):
    """writer/reader 两个“进程”：writer 可写，reader 只读同一 policy 目录。"""
    settings = Settings(db_path=str(tmp_path / "control.db"), policy_dir=str(tmp_path / "shared-policy"))
    conn = connect(settings.db_path)
    migrate(conn)
    _seed(conn)
    writer = PolicyRepo(settings.policy_dir)
    writer.initialize()
    reader = PolicyRepo(settings.policy_dir, writer=False)
    reader_app = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot=None))
    reader_app.state.publisher = SnapshotPublisher(reader_app, reader)
    return settings, conn, writer, reader, reader_app


def test_reader_follows_writer_files_across_replicas(tmp_path):
    _settings, conn, writer, _reader, reader_app = _replicas(tmp_path)
    try:
        writer.create("main", policy_from_dict(policy_dict()))
        writer.enable("main")
        initial = reader_app.state.publisher.refresh(conn)
        assert initial.policy_id == "main"
        assert initial.policy_revision == policy_revision_of(initial.policy)

        # 1. writer 改写策略行为 → reader 凭指纹换上新快照。
        updated = policy_from_dict(
            dict(policy_dict(), sticky={"enabled": True, "ttl_seconds": 300}))
        writer.update(
            "main", updated,
            base_document_sha256=writer.get("main").document_sha256,
        )
        reader_app.state.publisher.refresh_if_changed(conn)
        swapped = reader_app.state.snapshot
        assert swapped is not initial
        assert swapped.policy.sticky.ttl_seconds == 300
        assert swapped.policy_revision == policy_revision_of(updated)
        assert swapped.policy_revision != initial.policy_revision

        # 2. 布局是前端私有文档：写入不改变指纹，reader 不替换快照。
        writer.put_layout("main", {"panels": {"rules": [20, 40]}, "zoom": 0.8})
        reader_app.state.publisher.refresh_if_changed(conn)
        assert reader_app.state.snapshot is swapped

        # 3. 坏文件：reader 抛错并标记 stale，内存中仍用旧快照。
        path = writer.policies / "main.json"
        good = path.read_bytes()
        path.write_bytes(b"{broken")
        with pytest.raises(PolicyStorageError):
            reader_app.state.publisher.refresh_if_changed(conn)
        assert reader_app.state.snapshot is swapped
        status = reader_app.state.publisher.status()
        assert status["ready"] is True
        assert status["stale"] is True
        assert status["active_id"] == "main"

        # 4. 修复后 stale 清除；reader 继续用同一份内存快照。
        path.write_bytes(good)
        reader_app.state.publisher.refresh_if_changed(conn)
        recovered = reader_app.state.publisher.status()
        assert recovered["stale"] is False
        assert recovered["last_load_error"] == ""
        assert reader_app.state.snapshot is swapped

        # 5. writer 关闭策略 → reader 发布空快照；重新启用后恢复。
        writer.disable("main")
        reader_app.state.publisher.refresh_if_changed(conn)
        empty = reader_app.state.snapshot
        assert empty is not swapped
        assert empty.policy is None
        assert empty.policy_id is None
        assert empty.policy_revision == ""
        writer.enable("main")
        reader_app.state.publisher.refresh_if_changed(conn)
        restored = reader_app.state.snapshot
        assert restored.policy_id == "main"
        assert restored.policy_revision == swapped.policy_revision
    finally:
        conn.close()
