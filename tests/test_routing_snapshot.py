"""RoutingSnapshot / SnapshotPublisher：不可变策略快照的装配、发布与恢复。

快照 content 是固定文档的 canonical JSON：策略行为文档（不含 name/layout）、
backends、时段、JEV 行为与窗口、capabilities；机密（上游 key、JEV key）绝不
入档。发布是纯内存替换：旧快照整体保持不可变，坏文件/读取失败只标记 stale，
恢复后自动清除。后台 worker 凭指纹决定是否重建。
"""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from router.config import Settings
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.routing.document import policy_document_bytes
from router.routing.policy import (
    PolicyResource,
    policy_from_dict,
    policy_revision_of,
)
from router.routing.policy.model import (
    canonical_json,
    policy_behavior_dict,
    policy_to_dict,
)
from router.routing.reloader import PolicyReloadWorker
from router.routing.snapshot import SnapshotPublisher, build_snapshot, load_snapshot
from router.store.db import connect, migrate
from router.store.jev_config import JevConfigRepo
from router.store.policies import PolicyRepo, PolicyStorageError
from router.store.repositories import ModelRepo, ProviderRepo
from router.upstream.catalog import Backend
from tests.routing_fixtures import policy_dict


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "control.db"), policy_dir=str(tmp_path / "routing-policy"))


@pytest.fixture
def conn(settings):
    connection = connect(settings.db_path)
    migrate(connection)
    ProviderRepo(connection).upsert("local", "http://upstream.test/v1", api_key="upstream-secret")
    for model in ("fast", "strong", "standard"):
        ModelRepo(connection).create(model, model, "local", json.dumps({"capability": f"{model} 能力"}))
    JevConfigRepo(connection).save(JevSettings("http://jev.test", "NeoHorse-Jev-4B", "jev-secret", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE))
    yield connection
    connection.close()


@pytest.fixture
def repo(settings):
    repository = PolicyRepo(settings.policy_dir)
    repository.initialize()
    repository.create("main", policy_from_dict(policy_dict()))
    repository.enable("main")
    return repository


@pytest.fixture
def app(settings, conn, repo):
    instance = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot=None))
    instance.state.publisher = SnapshotPublisher(instance, repo)
    instance.state.publisher.refresh(conn)
    return instance


def test_snapshot_loads_current_policy_and_content_excludes_secrets(conn, repo):
    snapshot = load_snapshot(conn, repo)
    assert snapshot.policy_id == "main"
    assert snapshot.policy.name == "测试策略"
    assert snapshot.policy_revision == policy_revision_of(snapshot.policy)
    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer upstream-secret"
    assert snapshot.capabilities == {"fast": "fast 能力", "strong": "strong 能力", "standard": "standard 能力"}
    assert snapshot.jev.model == "NeoHorse-Jev-4B"
    assert "upstream-secret" not in snapshot.content
    assert "jev-secret" not in snapshot.content
    document = json.loads(snapshot.content)
    assert document["policy_id"] == "main"
    assert document["policy"] == policy_behavior_dict(snapshot.policy)
    assert document["policy_revision"] == snapshot.policy_revision
    assert document["jev_window"] == {
        "context_tokens": snapshot.jev_context_tokens,
        "input_tokens": snapshot.jev_input_tokens,
    }


def test_policy_behavior_dict_is_the_document_without_name():
    policy = policy_from_dict(policy_dict())
    document = policy_behavior_dict(policy)
    assert document == {
        key: value for key, value in policy_to_dict(policy).items() if key != "name"
    }
    assert "name" not in document
    assert hashlib.sha256(
        canonical_json(document).encode("utf-8")
    ).hexdigest() == policy_revision_of(policy)


def test_published_policy_and_snapshot_are_immutable(conn, repo):
    snapshot = load_snapshot(conn, repo)
    with pytest.raises(AttributeError):
        snapshot.policy_revision = "changed"
    with pytest.raises(AttributeError):
        snapshot.policy.name = "改名"
    with pytest.raises(TypeError):
        snapshot.policy.questions["route"] = None
    with pytest.raises(TypeError):
        snapshot.backends["extra"] = snapshot.backends["fast"]


def test_snapshot_backend_mutable_leaves_are_detached_from_live_catalog():
    headers = {"Authorization": "Bearer live-secret"}
    reasoning = {"declared": True, "supported": True, "format": "openai",
                "efforts": [{"id": "high", "wire": "high"}]}
    backend = Backend(url="http://u", headers=headers, upstream_name="live",
                      provider="local", reasoning=reasoning)
    snapshot = build_snapshot(
        resource=None, backends={"live": backend}, time_periods=[],
        jev=None, capabilities={},
    )
    headers["Authorization"] = "Bearer changed"
    reasoning["efforts"][0]["id"] = "low"
    assert snapshot.backends["live"].headers["Authorization"] == "Bearer live-secret"
    assert snapshot.backends["live"].reasoning["efforts"][0]["id"] == "high"


def test_publisher_swaps_whole_snapshot_without_mutating_old_one(conn, repo):
    app = SimpleNamespace(state=SimpleNamespace())
    publisher = SnapshotPublisher(app, repo)
    first = publisher.refresh(conn)
    ModelRepo(conn).update("fast", upstream="new-upstream")
    second = publisher.refresh(conn)
    assert first.backends["fast"].upstream_name == "fast"
    assert second.backends["fast"].upstream_name == "new-upstream"
    assert first.snapshot_revision != second.snapshot_revision
    assert first.policy_revision == second.policy_revision
    assert app.state.snapshot is second


def test_no_active_policy_is_a_valid_snapshot(tmp_path):
    settings = Settings(db_path=str(tmp_path / "control.db"), policy_dir=str(tmp_path / "routing-policy"))
    conn = connect(settings.db_path)
    migrate(conn)
    repo = PolicyRepo(settings.policy_dir)
    repo.initialize()
    snapshot = load_snapshot(conn, repo)
    assert snapshot.policy is None
    assert snapshot.policy_id is None
    assert snapshot.policy_revision == ""
    assert snapshot.backends == {}
    conn.close()


def test_invalid_active_policy_is_rejected_before_publication(conn, repo, app):
    original = app.state.snapshot
    raw = policy_dict()
    raw["rules"] = [{"name": "坏规则", "when": {"answer": "missing.value", "gte": 5}, "target": "safe"}]
    broken = PolicyResource(
        id="main", policy=policy_from_dict(raw), enabled=True,
        policy_revision=policy_revision_of(policy_from_dict(raw)),
    )
    path = repo.policies / "main.json"
    path.write_bytes(policy_document_bytes(broken))

    with pytest.raises(PolicyStorageError, match="invalid active policy: unknown_answer_path"):
        app.state.publisher.refresh(conn)
    assert app.state.snapshot is original


def test_bad_new_file_keeps_old_snapshot_and_recovers_after_repair(conn, repo, app):
    original = app.state.snapshot
    path = repo.policies / "main.json"
    good = path.read_bytes()
    path.write_bytes(b"{broken")
    try:
        with pytest.raises(PolicyStorageError):
            app.state.publisher.refresh(conn)
        assert app.state.snapshot is original
        assert app.state.publisher.status()["stale"] is True
    finally:
        path.write_bytes(good)
    app.state.publisher.refresh_if_changed(conn)
    assert app.state.publisher.status()["stale"] is False


@pytest.mark.parametrize(
    ("relative_path", "error_message"),
    [
        ("current", "current pointer is unavailable"),
        ("policies/main.json", "active policy is unavailable: main"),
    ],
)
def test_first_fingerprint_read_failure_marks_stale_and_recovers(
    conn, repo, app, relative_path, error_message,
):
    original = app.state.snapshot
    path = repo.root / relative_path
    content = path.read_bytes()
    path.unlink()

    with pytest.raises(PolicyStorageError, match=error_message):
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
    updated = policy_from_dict(
        dict(policy_dict(), sticky={"enabled": True, "ttl_seconds": 60}))
    repo.update("main", updated, base_document_sha256=repo.get("main").document_sha256)
    worker = PolicyReloadWorker(app, poll_seconds=0.01)
    asyncio.run(worker.poll_once())
    asyncio.run(worker.poll_once())
    assert calls == 1
    assert app.state.snapshot.policy.sticky.ttl_seconds == 60
    assert app.state.publisher.status()["document_sha256"] == repo.fingerprint()[1]


def test_worker_real_background_loop_loads_change(conn, repo, app):
    updated = policy_from_dict(
        dict(policy_dict(), sticky={"enabled": True, "ttl_seconds": 60}))
    repo.update("main", updated, base_document_sha256=repo.get("main").document_sha256)

    async def scenario():
        worker = PolicyReloadWorker(app, poll_seconds=0.01)
        await worker.start()
        try:
            for _ in range(100):
                if app.state.snapshot.policy.sticky.ttl_seconds == 60:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("background reload did not converge within one second")
        finally:
            await worker.close()

    asyncio.run(scenario())
