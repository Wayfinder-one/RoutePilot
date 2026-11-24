import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from router.jev.service import JevService
from router.config import Settings
from router.control.validation import tree_issues
from router.routing.document import graph_document_bytes
from router.routing.graph.compiled import CompiledGraph, edge_order_key, compile_graph
from router.routing.graph.model import canonical_json, graph_from_dict
from router.routing.route_set import RouteSetInvariantError
from router.routing.tree import tree_from_dict
from router.routing.reloader import GraphReloadWorker
from router.routing.snapshot import (
    SnapshotPublisher,
    assemble_snapshot_set,
    build_snapshot_for_tree,
    load_snapshot_set,
)
from router.routing.types import NoActiveTreeError, NoDefaultTreeError
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ModelRepo, ProviderRepo
from router.store.routing_trees import GraphStorageError, RoutingTreeRepo
from tests.routing_fixtures import graph_dict, runtime_dict, tree_dict


def stored_catalog_runtime():
    """与 ``conn`` fixture 的目录行同端点的自包含运行配置。"""
    values = runtime_dict()
    values["backends"]["providers"][0]["base_url"] = "http://upstream.test/v1"
    values["backends"]["providers"][0]["key_env"] = ""
    values["backends"]["models"] = [
        {"id": "fast", "upstream": "fast", "provider": "local",
         "profile": {"capability": "fast 能力"}},
        {"id": "standard", "upstream": "standard", "provider": "local",
         "profile": {"capability": "standard 能力"}},
    ]
    values["jev"] = {
        "id": "default",
        "base_url": "http://jev.test",
        "model": "NeoHorse-Jev-4B",
        "key_env": "ROUTER_JEV_API_KEY",
        "timeout_seconds": 5.0,
        "instructions": runtime_dict()["jev"]["instructions"],
        "state_template": runtime_dict()["jev"]["state_template"],
    }
    return values


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
    JevServiceRepo(connection).save(
        JevService("default", "http://jev.test", "NeoHorse-Jev-4B",
                   key_env="ROUTER_JEV_API_KEY", timeout_seconds=5.0),
        api_key="jev-secret")
    yield connection
    connection.close()


@pytest.fixture
def repo(settings):
    repository = RoutingTreeRepo(settings.graph_dir)
    repository.initialize()
    raw = tree_dict(root=direct_graph("fast"))
    raw.update(stored_catalog_runtime())
    repository.create(tree_from_dict({**raw, "enabled": True, "default": True}))
    return repository


@pytest.fixture
def app(settings, conn, repo):
    instance = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot_set=None))
    instance.state.publisher = SnapshotPublisher(instance, repo)
    instance.state.publisher.refresh(conn)
    return instance


def test_snapshot_loads_canvas_config_and_revision_excludes_secrets(conn, repo):
    snapshot = load_snapshot_set(conn, repo).default_snapshot
    assert snapshot.tree.id == "main"
    # 端点匹配的已存密钥只在运行态 Backend 上使用，不进入快照内容。
    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer upstream-secret"
    assert snapshot.capabilities == {"fast": "fast 能力", "standard": "standard 能力"}
    assert snapshot.jev.model == "NeoHorse-Jev-4B"
    assert "upstream-secret" not in snapshot.content
    assert "jev-secret" not in snapshot.content
    assert snapshot.tree_revision == snapshot.tree.tree_revision


def portable_tree(values=None, tree_id='main', enabled=True, default=True,
                  departments=()):
    """自包含测试图：默认就是启用中的默认图（可改成草稿/部门图）。"""
    raw = tree_dict(tree_id, root=direct_graph("fast"), enabled=enabled,
                    default=default, departments=list(departments))
    raw.update(runtime_dict() if values is None else values)
    return tree_from_dict(raw)


def test_snapshot_set_selects_department_then_default(conn):
    main = build_snapshot_for_tree(conn, portable_tree())
    finance_raw = tree_dict('finance', enabled=True, departments=['财务部'])
    finance_raw.update(stored_catalog_runtime())
    finance = build_snapshot_for_tree(conn, tree_from_dict(finance_raw))
    route_set = assemble_snapshot_set([main, finance], fingerprint='fp')
    assert route_set.select('财务部').snapshot is finance
    assert route_set.select('财务部').reason == 'department'
    assert route_set.select('未知部门').snapshot is main
    assert route_set.select('未知部门').reason == 'default'
    assert route_set.select('').snapshot is main


def test_snapshot_set_distinguishes_empty_and_missing_default(conn):
    empty = assemble_snapshot_set([], fingerprint='empty')
    with pytest.raises(NoActiveTreeError):
        empty.select('')

    valid = assemble_snapshot_set([build_snapshot_for_tree(conn, portable_tree())], fingerprint='valid')
    broken = replace(valid, default_tree_id=None)
    with pytest.raises(NoDefaultTreeError):
        broken.select('')

    raw = tree_dict('main', enabled=True, default=False)
    raw.update(stored_catalog_runtime())
    with pytest.raises(RouteSetInvariantError) as missing:
        assemble_snapshot_set(
            [build_snapshot_for_tree(conn, tree_from_dict(raw))], fingerprint='broken')
    assert missing.value.code == 'no_default_tree'


def test_snapshot_set_rejects_disabled_tree(conn):
    """路由集只装启用图：手工传入停用图快照必须被拒绝，绝不静默发布。

    ``assemble_snapshot_set()`` 的契约是「全部启用图」，此前只是隐含依赖调用
    方过滤；缺默认图/部门冲突有路由集不变量兜底，但一张停用图能混进集合让
    ``select()`` 选出它。
    """
    enabled = build_snapshot_for_tree(conn, portable_tree())
    disabled = build_snapshot_for_tree(
        conn, portable_tree(tree_id='draft', enabled=False, default=False))

    with pytest.raises(ValueError) as rejected:
        assemble_snapshot_set([enabled, disabled], fingerprint='fp')
    assert 'enabled' in str(rejected.value)


def test_snapshot_set_content_is_version_only_and_revision_hashes_it(conn):
    main = build_snapshot_for_tree(conn, portable_tree())
    finance_raw = tree_dict('finance', enabled=True, departments=['财务部'])
    finance_raw.update(stored_catalog_runtime())
    finance = build_snapshot_for_tree(conn, tree_from_dict(finance_raw))

    route_set = assemble_snapshot_set([finance, main], fingerprint='fp')

    document = {
        'default_tree_id': 'main',
        'department_tree_ids': {'财务部': 'finance'},
        'snapshots': {
            'finance': finance.snapshot_revision,
            'main': main.snapshot_revision,
        },
    }
    assert route_set.content == canonical_json(document)
    assert route_set.snapshot_revision == hashlib.sha256(
        route_set.content.encode('utf-8')).hexdigest()
    assert route_set.fingerprint == 'fp'
    # 路由集内容只含版本信息：密钥样本不得出现。
    assert 'upstream-secret' not in route_set.content
    assert 'jev-secret' not in route_set.content


def test_publisher_status_exposes_route_set_schema(conn, repo, app):
    status = app.state.publisher.status()

    assert set(status) == {
        'ready', 'stale', 'default_tree_id', 'enabled_tree_ids',
        'snapshot_revision', 'route_fingerprint', 'loaded_at', 'last_load_error',
    }
    assert status['ready'] is True
    assert status['stale'] is False
    assert status['default_tree_id'] == 'main'
    assert status['enabled_tree_ids'] == ['main']
    assert status['snapshot_revision'] == app.state.snapshot_set.snapshot_revision
    assert status['route_fingerprint'] == repo.route_fingerprint()
    assert status['last_load_error'] == ''


def test_embedded_runtime_drives_snapshot_instead_of_database(conn):
    snapshot = build_snapshot_for_tree(conn, portable_tree())

    assert set(snapshot.backends) == {"fast"}
    assert snapshot.backends["fast"].url == "http://json-only.test/v1/chat/completions"
    assert snapshot.backends["fast"].upstream_name == "json-upstream"
    assert snapshot.backends["fast"].headers == {}
    assert snapshot.capabilities == {"fast": "来自 JSON 的能力"}
    assert snapshot.jev.base_url == "http://jev-json.test"
    assert snapshot.jev.model == "json-jev"
    assert snapshot.jev.api_key == ""
    assert snapshot.jev_input_tokens == 1024
    assert snapshot.jev_context_tokens == 4096
    assert {period.name for period in snapshot.time_periods} == {"peak", "normal", "offpeak"}


def test_matching_embedded_endpoints_reuse_stored_keys_without_serializing_them(conn):
    values = runtime_dict()
    values["backends"]["providers"][0]["base_url"] = "http://upstream.test/v1/"
    values["jev"]["id"] = "default"
    values["jev"]["base_url"] = "http://jev.test/"
    values["jev"]["model"] = "NeoHorse-Jev-4B"

    snapshot = build_snapshot_for_tree(conn, portable_tree(values))

    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer upstream-secret"
    assert snapshot.jev.api_key == "jev-secret"
    assert "upstream-secret" not in snapshot.content
    assert "jev-secret" not in snapshot.content


def test_mismatched_embedded_endpoints_use_environment_keys_only(conn, monkeypatch):
    monkeypatch.setenv("JSON_PROVIDER_API_KEY", "json-provider-env-secret")
    monkeypatch.setenv("ROUTER_JEV_API_KEY", "json-jev-env-secret")

    snapshot = build_snapshot_for_tree(conn, portable_tree())

    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer json-provider-env-secret"
    assert snapshot.jev.api_key == "json-jev-env-secret"
    assert "upstream-secret" not in snapshot.content
    assert "jev-secret" not in snapshot.content


def test_jev_key_env_is_never_borrowed_from_another_service(conn, monkeypatch):
    """回归锁定：已删除的跨服务密钥回退不得复活。

    画布选中服务 B，而只有服务 A 的 ``JEV_A_API_KEY`` 在环境里；B 自己的
    ``JEV_B_API_KEY`` 未设置、数据库里 B 行的端点/模型又与画布不一致（已存密钥
    不可复用）。快照必须得不到任何 JEV 密钥——尤其不能借用 A 的环境变量或已存密钥。
    """
    monkeypatch.delenv("ROUTER_JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_B_API_KEY", raising=False)
    monkeypatch.setenv("JEV_A_API_KEY", "jev-a-env-secret")
    with conn:
        JevServiceRepo(conn).save(JevService(
            "jev-a", "http://jev-a.test", "jev-a-model",
            key_env="JEV_A_API_KEY", timeout_seconds=5.0),
            api_key="jev-a-stored-secret")
        JevServiceRepo(conn).save(JevService(
            "jev-b", "http://jev-b-stored.test", "jev-b-stored-model",
            key_env="JEV_B_API_KEY", timeout_seconds=5.0),
            api_key="jev-b-stored-secret")

    values = runtime_dict()
    values["jev"] = {
        "id": "jev-b",
        "base_url": "http://jev-b-canvas.test",
        "model": "jev-b-canvas-model",
        "key_env": "JEV_B_API_KEY",
        "timeout_seconds": 5.0,
        "instructions": runtime_dict()["jev"]["instructions"],
        "state_template": runtime_dict()["jev"]["state_template"],
    }

    snapshot = build_snapshot_for_tree(conn, portable_tree(values))

    # 画布按自己的内嵌快照运行（选中的是 B）。
    assert snapshot.tree.runtime.jev.id == "jev-b"
    assert snapshot.jev.base_url == "http://jev-b-canvas.test"
    assert snapshot.jev.model == "jev-b-canvas-model"
    # B 无可用密钥；A 的环境变量/已存密钥都不得附着到 B 上。
    assert snapshot.jev.api_key == ""
    assert "jev-a-env-secret" not in snapshot.content
    assert "jev-a-stored-secret" not in snapshot.content
    assert "jev-b-stored-secret" not in snapshot.content


def test_jev_key_env_is_never_replaced_by_the_global_router_variable(conn, monkeypatch):
    """画布 ``jev.key_env`` 必须被原样使用，不得回退到全局 ``ROUTER_JEV_API_KEY``。

    回归锁定：把 ``resolve_jev_settings`` 里 ``os.environ.get(service.key_env)``
    改成硬编码 ``ROUTER_JEV_API_KEY`` 的突变目前会躲过整套测试；这里把
    ``CUSTOM_JEV_KEY`` 置空、全局变量设成明文，快照必须拿不到任何密钥。
    """
    monkeypatch.delenv("CUSTOM_JEV_KEY", raising=False)
    monkeypatch.setenv("ROUTER_JEV_API_KEY", "global-router-jev-secret")
    values = runtime_dict()
    values["jev"] = {
        "id": "custom-jev",
        "base_url": "http://custom-jev.test",
        "model": "custom-jev-model",
        "key_env": "CUSTOM_JEV_KEY",
        "timeout_seconds": 5.0,
        "instructions": runtime_dict()["jev"]["instructions"],
        "state_template": runtime_dict()["jev"]["state_template"],
    }

    snapshot = build_snapshot_for_tree(conn, portable_tree(values))

    assert snapshot.jev.base_url == "http://custom-jev.test"
    assert snapshot.jev.api_key == ""
    assert "global-router-jev-secret" not in snapshot.content


def test_embedded_empty_catalog_rejects_model_even_when_database_has_it(conn):
    values = runtime_dict()
    values["backends"]["models"] = []

    issues = tree_issues(conn, portable_tree(values))

    assert "unknown_model" in {issue.code for issue in issues}


def test_embedded_null_jev_rejects_questions_even_when_database_has_jev(conn):
    raw = tree_dict()
    raw.update(runtime_dict(jev=None))
    tree = tree_from_dict(raw)

    issues = tree_issues(conn, tree)

    assert "decision_service_unavailable" in {issue.code for issue in issues}


def test_graph_snapshot_does_not_change_when_db_catalog_changes(conn, repo):
    """严格自包含：数据库目录行怎么改都不改变活动画布快照。"""
    app = SimpleNamespace(state=SimpleNamespace())
    publisher = SnapshotPublisher(app, repo)
    first = publisher.refresh(conn)
    with conn:
        conn.execute("INSERT INTO providers VALUES ('other', 'http://other.test/v1', '', 'openai', '')")
        conn.execute("INSERT INTO models VALUES ('changed-model', 'changed', 'other', '{}')")
        conn.execute("UPDATE providers SET base_url = 'http://db-changed.test/v1' WHERE name = 'local'")
        conn.execute("UPDATE models SET upstream = 'changed' WHERE id = 'fast'")
        conn.execute("DELETE FROM jev_services")
        conn.execute("DELETE FROM time_periods")
    second = publisher.refresh(conn)
    assert second.content == first.content
    assert second.snapshot_revision == first.snapshot_revision
    assert second.default_snapshot.backends["fast"].upstream_name == "fast"
    assert app.state.snapshot_set is second


def test_load_snapshot_set_uses_default_enabled_tree_not_non_default(conn, repo):
    """非默认启用图也进路由集，但默认选图仍取默认图。"""
    raw = tree_dict("other", root=direct_graph("fast"))
    raw.update(stored_catalog_runtime())
    repo.create(tree_from_dict({**raw, "enabled": True, "default": False}))

    route_set = load_snapshot_set(conn, repo)

    assert sorted(route_set.snapshots) == ["main", "other"]
    assert route_set.default_tree_id == "main"
    assert route_set.default_snapshot.tree.id == "main"
    assert isinstance(route_set.snapshots, MappingProxyType)
    assert isinstance(route_set.department_tree_ids, MappingProxyType)


def test_no_active_tree_is_a_valid_empty_snapshot_set(tmp_path):
    settings = Settings(db_path=str(tmp_path / "control.db"), graph_dir=str(tmp_path / "routing-graph"))
    conn = connect(settings.db_path)
    migrate(conn)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    route_set = load_snapshot_set(conn, repo)
    assert route_set.snapshots == {}
    assert route_set.default_tree_id is None
    assert route_set.default_snapshot is None
    assert route_set.department_tree_ids == {}
    assert route_set.snapshot_revision  # content hash of the empty route set
    conn.close()


def test_bad_new_file_keeps_old_snapshot_set_and_recovers_after_repair(conn, repo, app):
    original = app.state.snapshot_set
    path = repo.root / "graphs" / "main.json"
    path.write_bytes(b"{broken")
    try:
        with pytest.raises(GraphStorageError):
            app.state.publisher.refresh(conn)
        assert app.state.snapshot_set is original
        assert app.state.publisher.status()["stale"] is True
    finally:
        path.write_bytes(graph_document_bytes(original.default_snapshot.tree))
    app.state.publisher.refresh_if_changed(conn)
    assert app.state.publisher.status()["stale"] is False


def test_corrupt_graph_file_marks_stale_and_recovers(conn, repo, app):
    """路由集扫描读失败：保留旧快照并报告 stale，修复后恢复。"""
    original = app.state.snapshot_set
    path = repo.root / "graphs" / "main.json"
    content = path.read_bytes()
    path.write_bytes(b"{broken")

    with pytest.raises(GraphStorageError, match="invalid graph document: main"):
        app.state.publisher.refresh_if_changed(conn)

    assert app.state.snapshot_set is original
    status = app.state.publisher.status()
    assert status["ready"] is True
    assert status["stale"] is True
    assert "invalid graph document: main" in status["last_load_error"]

    path.write_bytes(content)
    app.state.publisher.refresh_if_changed(conn)
    recovered = app.state.publisher.status()
    assert recovered["stale"] is False
    assert recovered["last_load_error"] == ""
    assert recovered["snapshot_revision"] == status["snapshot_revision"]


def test_invalid_route_set_marks_stale_and_recovers(conn, repo, app):
    """集合不变量失败（有启用图但没有默认图）在装配边界转成存储错误。"""
    original = app.state.snapshot_set
    path = repo.root / "graphs" / "main.json"
    content = path.read_bytes()
    raw = tree_dict(root=direct_graph("fast"), enabled=True, default=False)
    raw.update(stored_catalog_runtime())
    path.write_bytes(graph_document_bytes(tree_from_dict(raw)))

    with pytest.raises(GraphStorageError, match="invalid route set: no_default_tree"):
        app.state.publisher.refresh_if_changed(conn)

    assert app.state.snapshot_set is original
    status = app.state.publisher.status()
    assert status["ready"] is True
    assert status["stale"] is True
    assert "no_default_tree" in status["last_load_error"]

    path.write_bytes(content)
    app.state.publisher.refresh_if_changed(conn)
    recovered = app.state.publisher.status()
    assert recovered["stale"] is False
    assert recovered["last_load_error"] == ""
    assert recovered["snapshot_revision"] == status["snapshot_revision"]


def test_publisher_prepare_maps_invalid_route_set_to_storage_error(conn, repo, app):
    """装配边界回归：``prepare()`` 必须把不变量损坏转成存储错误（503 路径）。

    ``assemble_snapshot_set()`` 保持纯函数语义（继续抛 ``RouteSetInvariantError``），
    由发布器/加载器在边界统一映射为 ``GraphStorageError``，损坏的路由集不会
    把 GraphService 的 update/enable/disable 打成 500。
    """
    broken = portable_tree(enabled=True, default=False)

    with pytest.raises(GraphStorageError, match="invalid route set: no_default_tree"):
        app.state.publisher.prepare(conn, [broken], "")

    with pytest.raises(RouteSetInvariantError) as invariant:
        assemble_snapshot_set([build_snapshot_for_tree(conn, broken)], fingerprint="")
    assert invariant.value.code == "no_default_tree"


def test_publisher_publish_stamps_route_set_fingerprint(conn, repo, app):
    """发布的路由集指纹必须等于发布时使用的指纹。"""
    prepared = app.state.publisher.prepare(conn, [portable_tree()], "")

    published = app.state.publisher.publish(
        replace(prepared, fingerprint=repo.route_fingerprint()))

    assert published.fingerprint == repo.route_fingerprint()
    assert app.state.snapshot_set.fingerprint == repo.route_fingerprint()
    assert app.state.publisher.status()["route_fingerprint"] == repo.route_fingerprint()


def test_worker_polls_once_and_skips_unchanged_fingerprint(conn, repo, app, monkeypatch):
    calls = 0
    import router.routing.snapshot as snapshot_module
    original_load = snapshot_module.load_snapshot_set

    def counted_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "load_snapshot_set", counted_load)
    updated = tree_from_dict(tree_dict(
        root=direct_graph("fast"), sticky={"enabled": True, "ttl_seconds": 60},
        enabled=True, default=True,
    ))
    repo.update(updated, base_document_sha256=repo.get("main").document_sha256)
    worker = GraphReloadWorker(app, poll_seconds=0.01)
    asyncio.run(worker.poll_once())
    asyncio.run(worker.poll_once())
    assert calls == 1
    assert app.state.snapshot_set.default_snapshot.tree.sticky.ttl_seconds == 60
    status = app.state.publisher.status()
    assert status["default_tree_id"] == "main"
    assert status["enabled_tree_ids"] == ["main"]
    assert status["snapshot_revision"] == app.state.snapshot_set.snapshot_revision


def test_worker_real_background_loop_loads_change(repo, app):
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


def test_compile_graph_sorts_immutable_topology():
    raw = graph_dict()
    raw["nodes"].reverse()
    raw["edges"].reverse()
    root = graph_from_dict(raw)
    compiled = compile_graph(root)

    assert isinstance(compiled, CompiledGraph)
    assert compiled.root is root
    assert compiled.node_order == ("q", "safe", "start")
    assert set(compiled.nodes) == set(compiled.node_order)
    assert compiled.edges == tuple(sorted(root.edges, key=edge_order_key))
    assert compiled.incoming["q"] == tuple(edge for edge in compiled.edges if edge.target == "q")
    assert [edge.id for edge in compiled.incoming["q"]] == ["e-start-q"]
    assert compiled.incoming["start"] == ()
    assert set(compiled.ports["q"]) == {
        "selected.easy", "selected.strong", "prob.easy", "prob.strong", "p_top", "margin",
    }
    assert compiled.ports["safe"] == {}
    for value in (compiled.nodes, compiled.incoming, compiled.ports):
        assert isinstance(value, MappingProxyType)
    with pytest.raises(TypeError):
        compiled.nodes["extra"] = None


def test_published_snapshot_compiles_after_enable_validation(conn, repo):
    snapshot = load_snapshot_set(conn, repo).default_snapshot
    compiled = snapshot.compiled
    assert isinstance(compiled, CompiledGraph)
    assert compiled.root is snapshot.tree.root
    assert compiled.node_order == ("start", "target")
    assert compiled.edges == tuple(sorted(snapshot.tree.root.edges, key=edge_order_key))
    assert compiled.incoming["target"] == compiled.edges
    assert set(compiled.ports["target"]) == set()


def test_invalid_active_graph_is_rejected_before_compilation(conn, repo, app):
    original = app.state.snapshot_set
    raw = tree_dict(enabled=True, default=True)
    raw["root"]["nodes"].append(copy.deepcopy(raw["root"]["nodes"][1]))  # duplicate node id q
    path = repo.root / "graphs" / "main.json"
    path.write_bytes(graph_document_bytes(tree_from_dict(raw)))

    with pytest.raises(GraphStorageError, match="duplicate_node_id"):
        app.state.publisher.refresh(conn)
    assert app.state.snapshot_set is original
