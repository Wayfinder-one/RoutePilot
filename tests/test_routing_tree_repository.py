import hashlib
import json
import threading
from dataclasses import replace

import pytest

from router.routing.tree import tree_behavior_dict, tree_from_dict, tree_revision_of
from router.routing.snapshot import build_snapshot
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphConflictError,
    GraphReadOnlyError,
    GraphStorageError,
    RoutingTreeRepo,
)
from tests.routing_fixtures import tree_dict


@pytest.fixture
def repo(tmp_path):
    repository = RoutingTreeRepo(tmp_path / "routing-graph")
    repository.initialize()
    return repository


def snapshot_of(tree):
    return build_snapshot(tree=tree, backends={}, time_periods=[], jev=None, capabilities={})


def test_create_stores_export_document_and_external_identity(repo):
    created = repo.create(tree_from_dict(tree_dict()))
    path = repo.root / "graphs" / "main.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["kind"] == "sr-routing-graph" and document["version"] == 4
    assert "id" not in document
    assert document["departments"] == [] and document["enabled"] is False
    assert document["default"] is False
    assert created.document_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert created.enabled is False and created.default is False


def test_initialize_only_creates_graphs_and_never_touches_legacy_pointer(tmp_path):
    """v4 仓储没有 current 指针：旧文件在盘上也不得被创建、读取或改写。"""
    root = tmp_path / "routing-graph"
    root.mkdir()
    legacy = root / "current"
    legacy.write_text("not-a-tree!!\n", encoding="utf-8")
    repo = RoutingTreeRepo(root)
    repo.initialize()
    assert repo.graphs.is_dir()
    assert legacy.read_text(encoding="utf-8") == "not-a-tree!!\n"
    assert repo.list() == [] and repo.enabled() == []
    assert repo.route_fingerprint()


def test_create_writes_given_lifecycle_verbatim(repo):
    """仓储只按给定 v4 文档落盘；新建图强制关闭由 GraphService 负责。"""
    created = repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    assert created.enabled is True and created.default is True
    document = json.loads((repo.root / "graphs" / "main.json").read_text(encoding="utf-8"))
    assert document["enabled"] is True and document["default"] is True


def test_route_fingerprint_ignores_draft_only_changes(repo):
    repo.create(tree_from_dict(tree_dict('main', enabled=True, default=True)))
    repo.create(tree_from_dict(tree_dict('draft')))
    before = repo.route_fingerprint()
    draft = repo.get('draft')
    raw = tree_dict('draft')
    raw['name'] = '只改草稿'
    repo.update(tree_from_dict(raw), base_document_sha256=draft.document_sha256)
    assert repo.route_fingerprint() == before
    main = repo.get('main')
    raw = tree_dict('main', enabled=True, default=True)
    raw['name'] = '改启用图'
    repo.update(tree_from_dict(raw), base_document_sha256=main.document_sha256)
    assert repo.route_fingerprint() != before


def test_route_fingerprint_tracks_lifecycle_and_default_switch(repo):
    main = repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    other = repo.create(tree_from_dict(tree_dict("other")))
    before = repo.route_fingerprint()

    # 文件内启停（GraphService 的写路径）：新增启用图改变路由集指纹。
    enabled_other = replace(other, enabled=True)
    repo.update(enabled_other, base_document_sha256=other.document_sha256)
    assert [tree.id for tree in repo.enabled()] == ["main", "other"]
    assert repo.route_fingerprint() != before

    # 默认图切换同样改变指纹（id/default 都进入指纹行）。
    with_default = repo.route_fingerprint()
    repo.update(replace(main, default=False),
                base_document_sha256=repo.get("main").document_sha256)
    repo.update(replace(repo.get("other"), default=True),
                base_document_sha256=repo.get("other").document_sha256)
    assert repo.route_fingerprint() != with_default


def test_duplicate_create_is_rejected(repo):
    repo.create(tree_from_dict(tree_dict()))
    with pytest.raises(GraphAlreadyExistsError):
        repo.create(tree_from_dict(tree_dict()))


def test_stale_or_missing_base_hash_cannot_overwrite(repo):
    created = repo.create(tree_from_dict(tree_dict()))
    before = (repo.root / "graphs" / "main.json").read_bytes()
    with pytest.raises(GraphConflictError) as stale:
        repo.update(tree_from_dict(tree_dict()), base_document_sha256="0" * 64)
    assert stale.value.actual_document_sha256 == created.document_sha256
    with pytest.raises(GraphConflictError):
        repo.update(tree_from_dict(tree_dict()), base_document_sha256="")
    assert (repo.root / "graphs" / "main.json").read_bytes() == before


def test_read_only_repo_cannot_write(repo):
    repo.create(tree_from_dict(tree_dict()))
    reader = RoutingTreeRepo(repo.root, writer=False)
    with pytest.raises(GraphReadOnlyError):
        reader.initialize()
    with pytest.raises(GraphReadOnlyError):
        reader.create(tree_from_dict(tree_dict("other")))
    with pytest.raises(GraphReadOnlyError):
        reader.update(tree_from_dict(tree_dict()), base_document_sha256="0" * 64)
    with pytest.raises(GraphReadOnlyError):
        reader.stage_updates([])
    # 只读仓储仍可扫描现有文档。
    assert reader.get("main") is not None
    assert [tree.id for tree in reader.list()] == ["main"]


def test_legacy_pointer_pointing_at_garbage_or_non_default_is_ignored(repo):
    """回归锁定：旧 ``current`` 即使指向垃圾 id 或非默认图，也不参与任何读取。"""
    repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    repo.create(tree_from_dict(tree_dict("other")))
    before = repo.route_fingerprint()
    (repo.root / "current").write_text("other\n", encoding="utf-8")

    assert repo.get("main").enabled is True and repo.get("main").default is True
    assert repo.get("other").enabled is False and repo.get("other").default is False
    assert [tree.id for tree in repo.enabled()] == ["main"]
    assert repo.route_fingerprint() == before

    (repo.root / "current").write_text("../escape\n", encoding="utf-8")
    assert [tree.id for tree in repo.enabled()] == ["main"]
    assert repo.route_fingerprint() == before


def test_atomic_replace_never_exposes_partial_json(repo):
    repo.create(tree_from_dict(tree_dict()))
    path = repo.root / "graphs" / "main.json"
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                document = json.loads(path.read_bytes())
                assert document["root"]["type"] == "graph"
            except BaseException as exc:
                errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(30):
            raw = tree_dict()
            raw["name"] = f"图-{index}"
            current = repo.get("main")
            repo.update(tree_from_dict(raw), base_document_sha256=current.document_sha256)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert errors == []


def test_layout_and_display_fields_survive_repository_roundtrip(repo):
    raw = tree_dict()
    raw["root"]["nodes"][0]["title"] = "请求入口"
    raw["canvas"]["start"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][-1]["title"] = "展示说明"
    created = repo.create(tree_from_dict(raw))
    loaded = repo.get("main")
    assert (loaded.root.nodes[0].position.x, loaded.root.nodes[0].position.y) == (321.0, 654.0)
    assert loaded.root.nodes[0].title == "请求入口"
    assert loaded.root.nodes[-1].title == "展示说明"
    assert loaded.tree_revision == created.tree_revision


def test_layout_only_update_keeps_revision_but_changes_file_hash(repo):
    raw = tree_dict()
    created = repo.create(tree_from_dict(raw))
    before = (repo.root / "graphs" / "main.json").read_bytes()
    raw["canvas"]["start"] = {"x": 40, "y": 80}
    raw["root"]["nodes"][0]["title"] = "入口改名"
    moved = repo.update(tree_from_dict(raw), base_document_sha256=created.document_sha256)

    assert moved.tree_revision == created.tree_revision
    assert tree_behavior_dict(moved) == tree_behavior_dict(created)
    assert moved.root.nodes[0].position.x == 40.0
    assert moved.root.nodes[0].title == "入口改名"
    assert snapshot_of(moved).snapshot_revision == snapshot_of(created).snapshot_revision
    assert (repo.root / "graphs" / "main.json").read_bytes() != before


def test_update_writes_document_verbatim_including_lifecycle(repo):
    created = repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    raw = tree_dict("main", enabled=True, default=True, sticky={"enabled": False, "ttl_seconds": 60})
    raw["root"]["nodes"][1]["instructions"] = "重新选择能力"
    updated = repo.update(tree_from_dict(raw),
                          base_document_sha256=repo.get("main").document_sha256)
    assert updated.enabled is True and updated.default is True
    assert updated.root.nodes[1].instructions == "重新选择能力"
    assert updated.sticky.enabled is False
    assert updated.tree_revision != created.tree_revision

    # 仓储不强制生命周期：启用状图也可以按给定文档写成关闭态（GraphService 负责语义）。
    raw = tree_dict("main")
    downgraded = repo.update(tree_from_dict(raw),
                             base_document_sha256=updated.document_sha256)
    assert downgraded.enabled is False and downgraded.default is False


def test_question_option_order_changes_revision(repo):
    raw = tree_dict()
    first = repo.create(tree_from_dict(raw))
    raw["root"]["nodes"][1]["options"].reverse()
    second = repo.update(tree_from_dict(raw), base_document_sha256=first.document_sha256)
    assert second.tree_revision != first.tree_revision
    assert [option.key for option in second.root.nodes[1].options] == ["strong", "easy"]


def test_copy_preserves_layout_and_display(repo):
    raw = tree_dict("source")
    raw["root"]["nodes"][0]["title"] = "请求入口"
    raw["canvas"]["start"] = {"x": 40, "y": 60}
    raw["root"]["nodes"][-1]["title"] = "展示说明"
    source = repo.create(tree_from_dict(raw))

    copied = repo.copy("source", "copy", "副本")

    assert copied.enabled is False
    assert copied.name == "副本"
    assert copied.tree_revision == source.tree_revision
    assert copied.root.nodes[0].title == "请求入口"
    assert (copied.root.nodes[0].position.x, copied.root.nodes[0].position.y) == (40.0, 60.0)
    assert copied.root.nodes[-1].title == "展示说明"


def test_copy_of_enabled_default_forces_lifecycle_off(repo):
    source = repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    copied = repo.copy("main", "copy", "副本")
    assert copied.enabled is False and copied.default is False
    assert copied.tree_revision == source.tree_revision
    assert [tree.id for tree in repo.enabled()] == ["main"]


def test_stage_updates_orders_default_last_and_commits(repo):
    main = repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    other = repo.create(tree_from_dict(tree_dict("other")))
    updates = [
        (replace(main, name="默认图改名"), main.document_sha256),
        (replace(other, enabled=True), other.document_sha256),
    ]
    batch = repo.stage_updates(updates)
    try:
        assert [entry.path.stem for entry in batch.entries] == ["other", "main"]
        batch.commit()
    finally:
        batch.discard()

    assert repo.get("main").name == "默认图改名"
    assert repo.get("main").enabled is True and repo.get("main").default is True
    assert repo.get("other").enabled is True and repo.get("other").default is False


def test_stage_updates_rejects_stale_base_and_cleans_staged_files(repo):
    other = repo.create(tree_from_dict(tree_dict("other")))
    before = (repo.root / "graphs" / "other.json").read_bytes()
    with pytest.raises(GraphConflictError) as stale:
        repo.stage_updates([(replace(other, enabled=True), "0" * 64)])
    assert stale.value.actual_document_sha256 == other.document_sha256
    assert (repo.root / "graphs" / "other.json").read_bytes() == before
    assert list(repo.graphs.glob(".*")) == []


def test_stage_updates_rollback_restores_committed_files(repo):
    main = repo.create(tree_from_dict(tree_dict("main", enabled=True, default=True)))
    other = repo.create(tree_from_dict(tree_dict("other")))
    before_main = (repo.root / "graphs" / "main.json").read_bytes()
    before_other = (repo.root / "graphs" / "other.json").read_bytes()
    batch = repo.stage_updates([
        (replace(other, enabled=True), other.document_sha256),
        (replace(main, name="改名"), main.document_sha256),
    ])
    batch.commit()
    assert (repo.root / "graphs" / "main.json").read_bytes() != before_main
    batch.rollback()
    assert (repo.root / "graphs" / "main.json").read_bytes() == before_main
    assert (repo.root / "graphs" / "other.json").read_bytes() == before_other


def test_invalid_document_raises_storage_error(repo):
    repo.create(tree_from_dict(tree_dict()))
    path = repo.root / "graphs" / "main.json"
    path.write_bytes(b"{broken")
    with pytest.raises(GraphStorageError):
        repo.get("main")
    with pytest.raises(GraphStorageError):
        repo.enabled()
