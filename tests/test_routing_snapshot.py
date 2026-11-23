import asyncio
import copy
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from router.jev.service import JevService
from router.config import Settings
from router.control.validation import tree_issues
from router.routing.document import graph_document_bytes
from router.routing.graph.compiled import CompiledGraph, edge_order_key, compile_graph
from router.routing.graph.model import graph_from_dict
from router.routing.tree import tree_from_dict
from router.routing.reloader import GraphReloadWorker
from router.routing.snapshot import SnapshotPublisher, build_snapshot_for_tree, load_snapshot
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
    repository.create(tree_from_dict(raw))
    repository.enable("main")
    return repository


@pytest.fixture
def app(settings, conn, repo):
    instance = SimpleNamespace(state=SimpleNamespace(settings=settings, snapshot=None))
    instance.state.publisher = SnapshotPublisher(instance, repo)
    instance.state.publisher.refresh(conn)
    return instance


def test_snapshot_loads_canvas_config_and_revision_excludes_secrets(conn, repo):
    snapshot = load_snapshot(conn, repo)
    assert snapshot.tree.id == "main"
    # 端点匹配的已存密钥只在运行态 Backend 上使用，不进入快照内容。
    assert snapshot.backends["fast"].headers["Authorization"] == "Bearer upstream-secret"
    assert snapshot.capabilities == {"fast": "fast 能力", "standard": "standard 能力"}
    assert snapshot.jev.model == "NeoHorse-Jev-4B"
    assert "upstream-secret" not in snapshot.content
    assert "jev-secret" not in snapshot.content
    assert snapshot.tree_revision == snapshot.tree.tree_revision


def portable_tree(values=None):
    raw = tree_dict(root=direct_graph("fast"))
    raw.update(runtime_dict() if values is None else values)
    return tree_from_dict(raw)


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
    assert second.backends["fast"].upstream_name == "fast"
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
    assert snapshot.jev is None
    assert snapshot.time_periods == ()
    assert snapshot.capabilities == {}
    assert snapshot.jev_input_tokens > 0 and snapshot.jev_context_tokens > 0
    assert snapshot.compiled is None  # no active graph → nothing to compile
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
    snapshot = load_snapshot(conn, repo)
    compiled = snapshot.compiled
    assert isinstance(compiled, CompiledGraph)
    assert compiled.root is snapshot.tree.root
    assert compiled.node_order == ("start", "target")
    assert compiled.edges == tuple(sorted(snapshot.tree.root.edges, key=edge_order_key))
    assert compiled.incoming["target"] == compiled.edges
    assert set(compiled.ports["target"]) == set()


def test_invalid_active_graph_is_rejected_before_compilation(conn, repo, app):
    original = app.state.snapshot
    raw = tree_dict()
    raw["root"]["nodes"].append(copy.deepcopy(raw["root"]["nodes"][1]))  # duplicate node id q
    path = repo.root / "graphs" / "main.json"
    path.write_bytes(graph_document_bytes(tree_from_dict(raw)))

    with pytest.raises(GraphStorageError, match="duplicate_node_id"):
        app.state.publisher.refresh(conn)
    assert app.state.snapshot is original
