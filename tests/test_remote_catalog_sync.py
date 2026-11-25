"""Task 7：跨服务目录同步与原子补偿。

两条边界在本文件直接验证：

* 外围 ``RemoteCatalogSyncService``：SQLite 写事务内先 recheck/db_write，再调用
  内核条件批次；内核已提交而 SQLite 任一步失败时，按候选倒序、原图内容与新基线
  哈希调用反向批次恢复；补偿自身失败必须显式抛 ``CatalogSyncDiverged``（503
  ``catalog_sync_diverged``），绝不返回成功。内核批次失败发生在 SQLite 提交之前
  时数据库必须整体回滚（内核未被改写）。
* 内核 ``POST /internal/v1/graph-batches``：一个发布锁 + 一个 ``GraphBatch`` 内
  完成基线复核 → 候选严格校验 → 结果启用集合校验 → 提交 → 发布；任一过期基线
  整批拒绝且不落盘，非法候选 422 且不落盘，成功响应带每份新 ``document_sha256``
  供外围补偿使用。

测试 fake 与生产异常共用同一契约（``CoreBatchError``/``CatalogSyncDiverged``）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.core_app import create_core_app
from router.control.errors import ValidationFailed
from router.control.remote_catalog_sync import (
    CatalogSyncDiverged,
    CoreBatchError,
    RemoteCatalogSyncService,
)
from router.core_client import CoreClient
from router.core_config import CoreSettings
from router.routing.tree import RoutingTree, tree_from_dict, tree_to_dict
from router.store.db import connect
from router.store.routing_trees import GraphConflictError
from tests.decision_fixtures import async_test
from tests.routing_fixtures import tree_dict

OLD_A_SHA = "a" * 64
OLD_B_SHA = "b" * 64
NEW_A_SHA = "1" * 64
NEW_B_SHA = "2" * 64


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
def tree(tree_id: str, sha: str, **overrides) -> RoutingTree:
    return replace(tree_from_dict(tree_dict(tree_id, **overrides)), document_sha256=sha)


def batch_reply(*trees: RoutingTree) -> dict:
    return {"trees": [tree_to_dict(item) for item in trees]}


class FakeCoreClient:
    """最小内核客户端桩：``list_graphs`` + ``replace_graph_batch`` 同一契约。"""

    def __init__(self, graphs=(), batch_results=()):
        self.graphs = list(graphs)
        self.batch_results = list(batch_results)
        self.calls: list[dict] = []

    async def list_graphs(self):
        self.calls.append({"method": "list_graphs"})
        return list(self.graphs)

    async def replace_graph_batch(self, writes):
        self.calls.append({"method": "replace_graph_batch", "writes": writes})
        result = self.batch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "catalog.db"))
    connection.execute("CREATE TABLE catalog_writes (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT)")
    connection.commit()
    yield connection
    connection.close()


def _db_rows(conn, table: str) -> list:
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def write_catalog_row(conn):
    conn.execute("INSERT INTO catalog_writes (id) VALUES ('written')")


def record_audit(conn):
    conn.execute("INSERT INTO audit (kind) VALUES ('model')")


def write_then_fail_commit(conn):
    """模拟「内核已提交后 SQLite 提交失败」：写审计行后抛错，事务必须整体回滚。"""
    conn.execute("INSERT INTO audit (kind) VALUES ('failed-commit')")
    raise sqlite3.Error("commit failed")


def _candidate_service(conn, client):
    return RemoteCatalogSyncService(conn, client)


# --------------------------------------------------------------------------- #
# 外围提交：SQLite 失败 → 内核反向批次恢复
# --------------------------------------------------------------------------- #
@async_test
async def test_sqlite_failure_restores_all_core_graphs_in_reverse_order(conn):
    old_a = tree("a", OLD_A_SHA)
    old_b = tree("b", OLD_B_SHA)
    new_a = replace(old_a, name="new-a", document_sha256=NEW_A_SHA)
    new_b = replace(old_b, name="new-b", document_sha256=NEW_B_SHA)
    client = FakeCoreClient(
        graphs=[old_b, old_a],  # 内核顺序无关：服务按候选顺序写入、倒序补偿
        batch_results=[batch_reply(new_a, new_b), batch_reply(old_b, old_a)],
    )

    with pytest.raises(sqlite3.Error, match="commit failed"):
        await _candidate_service(conn, client).commit(
            db_write=write_catalog_row,
            candidates=[old_a, old_b],
            audit=write_then_fail_commit,
            recheck=None,
        )

    # 第一次是候选批次；第二次是倒序补偿批次。
    assert [call["method"] for call in client.calls] == [
        "list_graphs", "replace_graph_batch", "replace_graph_batch"]
    first, restore = client.calls[1]["writes"], client.calls[2]["writes"]
    assert [(entry["tree"].id, entry["base_document_sha256"]) for entry in first] == [
        ("a", OLD_A_SHA), ("b", OLD_B_SHA)]
    # 补偿：候选倒序 + 原图内容 + 内核返回的新基线哈希。
    assert [entry["tree"].id for entry in restore] == ["b", "a"]
    assert [tree_to_dict(entry["tree"]) for entry in restore] == [
        tree_to_dict(old_b), tree_to_dict(old_a)]
    assert restore[0]["base_document_sha256"] == NEW_B_SHA
    assert restore[1]["base_document_sha256"] == NEW_A_SHA
    # SQLite 写事务整体回滚：没有半行目录、也没有审计行。
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_compensation_failure_is_explicit_divergence(conn):
    old_a = tree("a", OLD_A_SHA)
    new_a = replace(old_a, name="new-a", document_sha256=NEW_A_SHA)
    client = FakeCoreClient(
        graphs=[old_a],
        batch_results=[batch_reply(new_a), CoreBatchError(503)],
    )

    with pytest.raises(CatalogSyncDiverged, match="catalog_sync_diverged"):
        await _candidate_service(conn, client).commit(
            db_write=write_catalog_row,
            candidates=[old_a],
            audit=write_then_fail_commit,
            recheck=None,
        )

    assert client.calls[-1]["writes"][0]["base_document_sha256"] == NEW_A_SHA
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_core_batch_failure_before_sqlite_commit_rolls_back_without_compensation(conn):
    old_a = tree("a", OLD_A_SHA)
    client = FakeCoreClient(
        graphs=[old_a],
        batch_results=[CoreBatchError(409, {"detail": {
            "code": "graph_conflict", "actual_document_sha256": "f" * 64}})],
    )

    with pytest.raises(GraphConflictError) as caught:
        await _candidate_service(conn, client).commit(
            db_write=write_catalog_row,
            candidates=[old_a],
            audit=record_audit,
            recheck=None,
        )

    assert caught.value.actual_document_sha256 == "f" * 64
    # 内核未提交、SQLite 也未提交；不会尝试补偿（没有已提交的图需要还原）。
    assert [call["method"] for call in client.calls] == [
        "list_graphs", "replace_graph_batch"]
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_stale_base_is_rejected_before_any_batch_or_db_write(conn):
    old_a = tree("a", OLD_A_SHA)
    stale = replace(old_a, document_sha256="c" * 64)
    calls = {"db": False}
    client = FakeCoreClient(graphs=[old_a], batch_results=[])

    def db_write(_conn):
        calls["db"] = True

    with pytest.raises(GraphConflictError) as caught:
        await _candidate_service(conn, client).commit(
            db_write=db_write, candidates=[stale], audit=record_audit)

    assert caught.value.actual_document_sha256 == OLD_A_SHA
    assert [call["method"] for call in client.calls] == ["list_graphs"]
    assert calls["db"] is False


@async_test
async def test_kernel_422_maps_to_validation_failed_and_nothing_commits(conn):
    old_a = tree("a", OLD_A_SHA)
    payload = {"detail": [{
        "code": "reasoning_unsupported", "zh": "不支持", "en": "unsupported",
        "node_id": "direct", "edge_id": "", "port": "", "level": "hard",
        "params": {"model": "fast", "efforts": ["off"]},
    }]}
    client = FakeCoreClient(
        graphs=[old_a], batch_results=[CoreBatchError(422, payload)])

    with pytest.raises(ValidationFailed) as caught:
        await _candidate_service(conn, client).commit(
            db_write=write_catalog_row, candidates=[old_a], audit=record_audit)

    assert [issue.code for issue in caught.value.issues] == ["reasoning_unsupported"]
    assert _db_rows(conn, "catalog_writes") == []


@async_test
async def test_sqlite_failure_before_batch_does_not_call_the_kernel(conn):
    old_a = tree("a", OLD_A_SHA)
    client = FakeCoreClient(graphs=[old_a], batch_results=[])

    def db_write(_conn):
        raise sqlite3.Error("commit failed")

    with pytest.raises(sqlite3.Error):
        await _candidate_service(conn, client).commit(
            db_write=db_write, candidates=[old_a], audit=record_audit)

    assert [call["method"] for call in client.calls] == ["list_graphs"]


@async_test
async def test_no_candidate_write_never_calls_the_graph_batch(conn):
    client = FakeCoreClient(graphs=[], batch_results=[])
    order: list[str] = []
    service = _candidate_service(conn, client)

    synced = await service.commit(
        db_write=lambda c: (order.append("db_write"), write_catalog_row(c))[1],
        candidates=[],
        audit=lambda c: (order.append("audit"), record_audit(c))[1],
        recheck=lambda _c: order.append("recheck"),
    )

    assert synced == []
    assert client.calls == []
    assert order == ["recheck", "db_write", "audit"]
    assert _db_rows(conn, "catalog_writes") == [("written",)]
    assert _db_rows(conn, "audit") == [(1, "model")]


@async_test
async def test_recheck_runs_inside_the_write_transaction_before_db_write(conn):
    old_a = tree("a", OLD_A_SHA)
    new_a = replace(old_a, document_sha256=NEW_A_SHA)
    client = FakeCoreClient(
        graphs=[old_a], batch_results=[batch_reply(new_a)])
    order: list[str] = []

    async def recheck(_conn):
        # 事务已开始：SQLite 的写锁已被本请求持有。
        assert _conn.in_transaction
        order.append("recheck")

    def db_write(c):
        assert c.in_transaction
        order.append("db_write")
        write_catalog_row(c)

    def audit(c):
        assert c.in_transaction
        order.append("audit")
        record_audit(c)

    await _candidate_service(conn, client).commit(
        db_write=db_write, candidates=[old_a], audit=audit, recheck=recheck)

    assert order == ["recheck", "db_write", "audit"]
    assert _db_rows(conn, "catalog_writes") == [("written",)]
    assert _db_rows(conn, "audit") == [(1, "model")]


# --------------------------------------------------------------------------- #
# 外围客户端边界：RoutingTree → JSON 序列化只发生在 CoreClient
# --------------------------------------------------------------------------- #
@async_test
async def test_core_client_replace_graph_batch_serializes_at_the_boundary():
    tree_a = tree("a", OLD_A_SHA)
    seen = {}

    async def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json=batch_reply(
            replace(tree_a, name="updated", document_sha256=NEW_A_SHA)))

    client = CoreClient("http://kernel:8400", http=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    try:
        reply = await client.replace_graph_batch([
            {"tree": tree_a, "base_document_sha256": OLD_A_SHA}])
    finally:
        await client.http.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/internal/v1/graph-batches"
    assert seen["body"] == {"writes": [{
        "tree": tree_to_dict(tree_a),
        "base_document_sha256": OLD_A_SHA,
    }]}
    assert reply["trees"][0]["document_sha256"] == NEW_A_SHA
    assert "authorization" not in seen


@async_test
async def test_core_client_replace_graph_batch_preserves_kernel_status_and_payload():
    async def handler(request):
        return httpx.Response(409, json={"detail": {
            "code": "graph_conflict", "actual_document_sha256": "f" * 64}})

    client = CoreClient("http://kernel:8400", http=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(CoreBatchError) as caught:
            await client.replace_graph_batch([])
    finally:
        await client.http.aclose()

    assert caught.value.status_code == 409
    assert caught.value.payload["detail"]["actual_document_sha256"] == "f" * 64


@async_test
async def test_core_client_list_graphs_parses_kernel_documents():
    tree_a = tree("a", OLD_A_SHA)
    tree_b = tree("b", OLD_B_SHA, enabled=True, default=True)
    seen = {}

    async def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "trees": [tree_to_dict(tree_a), tree_to_dict(tree_b)],
            "snapshot_revision": "", "graph_status": {},
        })

    client = CoreClient("http://kernel:8400", http=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    try:
        graphs = await client.list_graphs()
    finally:
        await client.http.aclose()

    assert seen["path"] == "/internal/v1/trees"
    assert [item.id for item in graphs] == ["a", "b"]
    assert [item.document_sha256 for item in graphs] == [OLD_A_SHA, OLD_B_SHA]
    assert graphs[1].enabled is True and graphs[1].default is True


# --------------------------------------------------------------------------- #
# 内核批次端点：原子条件写 + 新文档哈希
# --------------------------------------------------------------------------- #
def kernel_app(tmp_path):
    return create_core_app(CoreSettings(graph_dir=str(tmp_path / "graphs")))


def install_kernel_graphs(app, *trees: RoutingTree) -> None:
    for item in trees:
        app.state.graph_repo.create(item)
    app.state.publisher.refresh()


def graph_path(app, tree_id: str):
    return app.state.graph_repo.graphs / f"{tree_id}.json"


def updated_tree(item: RoutingTree, *, name=None, upstream=None) -> RoutingTree:
    """候选画布：在完整文档上改行为字段（运行时 upstream 会改变快照内容）。"""
    document = tree_to_dict(item)
    if name is not None:
        document["name"] = name
    if upstream is not None:
        document["backends"]["models"][0]["upstream"] = upstream
    return tree_from_dict(document)


def batch_payload(*writes) -> dict:
    return {"writes": [
        {"tree": tree_to_dict(item), "base_document_sha256": base}
        for item, base in writes
    ]}


def test_kernel_batch_commits_all_candidates_and_returns_new_document_hashes(tmp_path):
    app = kernel_app(tmp_path)
    first = tree("first", "", enabled=True, default=True)
    second = tree("second", "", enabled=True)
    install_kernel_graphs(app, first, second)
    current = {item.id: item for item in app.state.graph_repo.list()}
    before_set = app.state.snapshot_set

    first_update = updated_tree(current["first"], name="first-v2",
                                upstream="first-v2-upstream")
    second_update = updated_tree(current["second"], name="second-v2",
                                 upstream="second-v2-upstream")
    response = TestClient(app).post("/internal/v1/graph-batches", json=batch_payload(
        (first_update, current["first"].document_sha256),
        (second_update, current["second"].document_sha256)))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["trees"]] == ["first", "second"]
    assert [item["name"] for item in body["trees"]] == ["first-v2", "second-v2"]
    # 响应里的 document_sha256 是提交后的真实基线，供外围补偿使用。
    for committed in body["trees"]:
        path = graph_path(app, committed["id"])
        assert hashlib_sha(path.read_bytes()) == committed["document_sha256"]
    assert app.state.snapshot_set.snapshot_revision != before_set.snapshot_revision
    assert app.state.snapshot_set.default_snapshot.tree.name == "first-v2"


def hashlib_sha(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()


def test_kernel_batch_stale_base_rejects_the_whole_batch_without_touching_files(tmp_path):
    app = kernel_app(tmp_path)
    first = tree("first", "", enabled=True, default=True)
    second = tree("second", "", enabled=True)
    install_kernel_graphs(app, first, second)
    current = {item.id: item for item in app.state.graph_repo.list()}
    before = {
        tree_id: graph_path(app, tree_id).read_bytes()
        for tree_id in ("first", "second")
    }
    before_set = app.state.snapshot_set

    response = TestClient(app).post("/internal/v1/graph-batches", json=batch_payload(
        (replace(current["first"], name="first-v2"), current["first"].document_sha256),
        (replace(current["second"], name="second-v2"), "0" * 64),
    ))

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "graph_conflict"
    assert detail["actual_document_sha256"] == current["second"].document_sha256
    # 整批不落盘：合法候选也没有被写；发布集不变。
    assert {
        tree_id: graph_path(app, tree_id).read_bytes()
        for tree_id in ("first", "second")
    } == before
    assert app.state.snapshot_set is before_set


def test_kernel_batch_invalid_candidate_is_422_and_touches_no_file(tmp_path):
    app = kernel_app(tmp_path)
    first = tree("first", "", enabled=True, default=True)
    install_kernel_graphs(app, first)
    current = app.state.graph_repo.get("first")
    before = graph_path(app, "first").read_bytes()

    # 非法候选：target 推理档位在画布声明的运行时模型里不受支持。
    document = tree_to_dict(current)
    for node in document["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": "fast", "reasoning": "nope"}]
    response = TestClient(app).post("/internal/v1/graph-batches", json={
        "writes": [{"tree": document,
                    "base_document_sha256": current.document_sha256}]})

    assert response.status_code == 422, response.text
    issues = response.json()["detail"]
    assert any(issue["code"] == "reasoning_unsupported" for issue in issues)
    assert graph_path(app, "first").read_bytes() == before


def test_kernel_batch_rejects_a_resulting_route_set_without_default(tmp_path):
    app = kernel_app(tmp_path)
    first = tree("first", "", enabled=True, default=True)
    install_kernel_graphs(app, first)
    current = app.state.graph_repo.get("first")
    before = graph_path(app, "first").read_bytes()
    before_set = app.state.snapshot_set

    candidate = replace(current, default=False)
    response = TestClient(app).post("/internal/v1/graph-batches", json=batch_payload(
        (candidate, current.document_sha256)))

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "graph_storage_error"
    assert graph_path(app, "first").read_bytes() == before
    assert app.state.snapshot_set is before_set


def test_kernel_batch_empty_writes_is_a_noop_success(tmp_path):
    app = kernel_app(tmp_path)
    before_set = app.state.snapshot_set

    response = TestClient(app).post("/internal/v1/graph-batches", json={"writes": []})

    assert response.status_code == 200, response.text
    assert response.json() == {"trees": []}
    assert app.state.snapshot_set is before_set


def test_kernel_batch_rejects_duplicate_tree_ids_before_writing(tmp_path):
    app = kernel_app(tmp_path)
    first = tree("first", "", enabled=True, default=True)
    install_kernel_graphs(app, first)
    current = app.state.graph_repo.get("first")
    before = graph_path(app, "first").read_bytes()
    candidate = replace(current, name="dup")

    response = TestClient(app).post("/internal/v1/graph-batches", json=batch_payload(
        (candidate, current.document_sha256),
        (replace(candidate, name="dup-2"), current.document_sha256)))

    assert response.status_code == 422, response.text
    assert graph_path(app, "first").read_bytes() == before


def test_kernel_batch_unknown_tree_id_is_a_conflict(tmp_path):
    app = kernel_app(tmp_path)
    first = tree("first", "", enabled=True, default=True)
    install_kernel_graphs(app, first)
    ghost = tree("ghost", "", enabled=False)

    response = TestClient(app).post("/internal/v1/graph-batches", json=batch_payload(
        (ghost, "e" * 64)))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "graph_conflict", "actual_document_sha256": None}
