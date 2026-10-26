"""SnapshotPublisher: serialise config writes with their publication.

Task 9 Step 2 (2026-10-26).  The publisher is a *pipeline-layer* helper: tests
assemble a bare ``FastAPI()`` app and a real SQLite DB instead of going through
``create_app`` (which is mid-refactor until Task 9 Step 6).  ``install_snapshot``
alone is a single atomic assignment; the publisher's job is to make "mutation +
assembly + revision + publish" one locked critical section, so a concurrent
write can never leave the hot path on a snapshot the database no longer has.

Also pins the audit half of the same atomicity contract: ``ChangeRepo.record``
must JOIN an outer transaction (``_tx``) instead of committing it early, so a
rolled-back configuration write cannot leave an audit row behind.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI

from router.pipeline.snapshot import (
    SnapshotPublisher,
    canonical_json,
    sha256_text,
    snapshot_content,
    snapshot_from_content,
)
from router.pipeline.tree import Route
from router.store.db import connect, migrate
from router.store.repositories import ChangeRepo, write_transaction
from router.store.routing_trees import RoutingTreeRepo


class FakeRunner:
    """Minimal DecisionRunner evidence provider (``info`` only)."""

    def __init__(self, info=None):
        self._info = info or {"engine": "fake-decision", "revision": "fake-v1"}

    def info(self):
        return dict(self._info)


def make_database(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    migrate(conn)
    repo = RoutingTreeRepo(conn)
    repo.create("tree-a", "A", Route(id="root", model="qwen3-8b"))
    repo.create("tree-b", "B", Route(id="root", model="qwen3-32b"))
    conn.close()
    return path


def make_publisher(tmp_path, **kwargs):
    path = make_database(tmp_path)
    app = FastAPI()
    publisher = SnapshotPublisher(app, **kwargs)
    return app, publisher, path


def active_tree_id(conn) -> str | None:
    row = conn.execute(
        "SELECT id FROM routing_trees WHERE enabled = 1").fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------------------- #
# assembly + publish
# --------------------------------------------------------------------------- #
def test_refresh_publishes_assembled_snapshot_and_records_revision(tmp_path):
    app, publisher, path = make_publisher(tmp_path)
    conn = connect(path)
    try:
        RoutingTreeRepo(conn).enable("tree-a")
        snapshot = publisher.refresh(conn)

        # ONE assignment is the visible publication point
        assert app.state.snapshot is snapshot
        assert snapshot.tree_id == "tree-a"
        assert snapshot.tree_revision and snapshot.snapshot_revision
        assert snapshot.decision_revision == ""        # no runner installed

        # the append-only snapshot revision is resolvable and re-hashes
        row = conn.execute(
            "SELECT content FROM config_revisions WHERE kind = 'snapshot' "
            "AND sha256 = ?", (snapshot.snapshot_revision,)).fetchone()
        assert row is not None
        assert sha256_text(row["content"]) == snapshot.snapshot_revision
        assert snapshot_from_content(
            row["content"]).snapshot_revision == snapshot.snapshot_revision

        # refreshing an unchanged database state dedupes the revision row
        again = publisher.refresh(conn)
        assert again.snapshot_revision == snapshot.snapshot_revision
        assert conn.execute(
            "SELECT COUNT(*) FROM config_revisions WHERE kind = 'snapshot'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_refresh_without_active_tree_still_publishes(tmp_path):
    """No enabled tree is a legal state: the snapshot is published with
    ``tree=None`` (the /v1 hot path answers 503 before reading anything)."""
    path = make_database(tmp_path)
    app = FastAPI()
    publisher = SnapshotPublisher(app)
    conn = connect(path)
    try:
        snapshot = publisher.refresh(conn)
        assert app.state.snapshot.tree is None
        assert snapshot.tree_id is None
        assert snapshot.tree_revision == ""
        assert snapshot.snapshot_revision          # still a configuration hash
        assert conn.execute(
            "SELECT COUNT(*) FROM config_revisions WHERE kind = 'snapshot'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_runner_info_is_the_decision_evidence(tmp_path):
    """Uniform rule: ``runner.info() if runner is not None else {}``, read from
    ``app.state.runner`` first (the app factory installs it there)."""
    path = make_database(tmp_path)
    app = FastAPI()
    app.state.runner = FakeRunner({"engine": "fake", "revision": "v9"})
    publisher = SnapshotPublisher(app)               # no explicit runner
    conn = connect(path)
    try:
        snapshot = publisher.refresh(conn)
        expected = sha256_text(canonical_json(
            {"engine": "fake", "revision": "v9"}))
        assert snapshot.decision_revision == expected
        # the revision covers the decision evidence, not just the tree
        assert snapshot.snapshot_revision
    finally:
        conn.close()


def test_questions_override_is_frozen_hashed_and_replayable(tmp_path):
    app, publisher, path = make_publisher(tmp_path)
    conn = connect(path)
    try:
        cfg = {"signals": {"pii": {"types": {"cn_id_card": {"zh": "身份证号"}}}}}
        snapshot = publisher.refresh(conn, questions=cfg)
        assert snapshot.questions_revision == sha256_text(canonical_json(cfg))
        assert snapshot.questions["signals"]["pii"]["types"]["cn_id_card"][
            "zh"] == "身份证号"
        with pytest.raises(TypeError):               # MappingProxyType is read-only
            snapshot.questions["signals"] = {}
        with pytest.raises(TypeError):
            snapshot.questions["signals"]["pii"]["types"]["x"] = {}
        restored = snapshot_from_content(snapshot_content(snapshot))
        assert restored.snapshot_revision == snapshot.snapshot_revision
        assert restored.questions_revision == snapshot.questions_revision
    finally:
        conn.close()


def test_locked_is_reentrant(tmp_path):
    """``refresh`` takes the same RLock, so a caller holding ``locked()`` does
    not deadlock (this is exactly how the admin endpoints compose it)."""
    app, publisher, path = make_publisher(tmp_path)
    conn = connect(path)
    try:
        with publisher.locked():
            publisher.refresh(conn)
        with publisher.locked():
            with publisher.locked():
                publisher.refresh(conn)
        assert app.state.snapshot.tree_id is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# concurrency: why the lock must cover mutation + refresh
# --------------------------------------------------------------------------- #
def test_unlocked_write_can_publish_a_stale_snapshot(tmp_path, monkeypatch):
    """Characterization of the bug ``locked()`` prevents.

    Thread A assembles the snapshot with ``tree-a`` active and pauses before
    publishing; thread B then commits ``tree-b`` without taking the lock.
    A's publish is stale: the database says B, the hot path serves A.  This is
    why the admin endpoints wrap the *mutation* and the refresh in one
    ``publisher.locked()`` section.
    """
    app, publisher, path = make_publisher(tmp_path)
    import router.pipeline.snapshot as snapshot_module

    real_install = snapshot_module.install_snapshot
    assembled = threading.Event()
    release = threading.Event()

    def gated_install(target_app, snapshot):
        if snapshot.tree_id == "tree-a" and not assembled.is_set():
            assembled.set()
            assert release.wait(5)
        real_install(target_app, snapshot)

    monkeypatch.setattr(snapshot_module, "install_snapshot", gated_install)
    errors = []
    conn_a = connect(path)
    conn_b = connect(path)

    def writer_a():
        try:
            RoutingTreeRepo(conn_a).enable("tree-a")
            publisher.refresh(conn_a)        # assembles A, pauses before publish
        except BaseException as exc:         # noqa: BLE001 - reported below
            errors.append(exc)

    def writer_b():
        try:
            assert assembled.wait(5)
            RoutingTreeRepo(conn_b).enable("tree-b")   # DB switches to B
            release.set()
        except BaseException as exc:         # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer_a),
               threading.Thread(target=writer_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)

    check = connect(path)
    try:
        assert active_tree_id(check) == "tree-b"     # the database switched
    finally:
        check.close()
    conn_a.close()
    conn_b.close()
    assert app.state.snapshot.tree_id == "tree-a"    # ...but the app didn't


def test_locked_writers_always_publish_the_database_active_tree(tmp_path):
    """Two write threads, each doing enable+refresh inside ``locked()``: every
    publication corresponds to the database state it mutated, so the final
    snapshot always names the database's active tree (spec §12.6)."""
    app, publisher, path = make_publisher(tmp_path)
    barrier = threading.Barrier(2)
    errors = []

    def worker(tree_id):
        conn = connect(path)
        try:
            barrier.wait(timeout=5)
            for _ in range(10):
                with publisher.locked():
                    RoutingTreeRepo(conn).enable(tree_id)
                    publisher.refresh(conn)
        except BaseException as exc:         # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=("tree-a",)),
               threading.Thread(target=worker, args=("tree-b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)

    conn = connect(path)
    try:
        database_active = active_tree_id(conn)
    finally:
        conn.close()
    assert database_active in ("tree-a", "tree-b")
    assert app.state.snapshot.tree_id == database_active


# --------------------------------------------------------------------------- #
# audit atomicity: ChangeRepo.record joins the caller's transaction
# --------------------------------------------------------------------------- #
def test_change_record_joins_the_outer_transaction_and_rolls_back(tmp_path):
    path = make_database(tmp_path)
    conn = connect(path)
    try:
        repo = ChangeRepo(conn)
        with pytest.raises(RuntimeError):
            with write_transaction(conn):
                repo.record("2026-10-26T10:00:00", "model",
                            {"action": "update", "id": "qwen3-8b"})
                # visible inside the transaction ...
                assert conn.execute(
                    "SELECT COUNT(*) FROM changes").fetchone()[0] == 1
                raise RuntimeError("configuration write failed")
        # ... but gone after the rollback (no orphan audit row)
        assert conn.execute(
            "SELECT COUNT(*) FROM changes").fetchone()[0] == 0

        # standalone calls still commit on their own
        repo.record("2026-10-26T10:00:01", "model", {"action": "create"})
        assert conn.execute(
            "SELECT COUNT(*) FROM changes").fetchone()[0] == 1
    finally:
        conn.close()
