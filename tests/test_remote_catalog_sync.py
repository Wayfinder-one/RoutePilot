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

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.core_app import create_core_app
from router.control.errors import ValidationFailed
from router.control.remote_catalog_sync import (
    CatalogSyncDiverged,
    CatalogWriteNotApplied,
    CoreBatchError,
    RemoteCatalogSyncService,
)
from router.core_client import CoreClient
from router.core_config import CoreSettings
from router.routing.document import graph_document_bytes
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


# --------------------------------------------------------------------------- #
# 评审修复 1：进程内提交协调器 —— 补偿窗口内的第二笔 commit 不得穿插
# --------------------------------------------------------------------------- #
def document_sha(item: RoutingTree) -> str:
    """一份画布即将落盘字节的 SHA-256（内核 ``document_sha256`` 的定义）。"""
    return hashlib.sha256(graph_document_bytes(item)).hexdigest()


def document_tree(tree_id: str, **overrides) -> RoutingTree:
    """带真实 ``document_sha256`` 的画布（模拟内核读取结果）。"""
    raw = tree_from_dict(tree_dict(tree_id, **overrides))
    return replace(raw, document_sha256=document_sha(raw))


def fresh_conn(path) -> sqlite3.Connection:
    connection = connect(str(path))
    connection.execute("CREATE TABLE catalog_writes (id TEXT PRIMARY KEY)")
    connection.execute(
        "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT)")
    connection.commit()
    return connection


class InterleavingCoreClient:
    """Stateful kernel fake：第一笔提交的补偿批次会挂起，等测试释放。

    这样测试可以在「内核已提交、SQLite 已回滚、反向批次进行中」的补偿窗口里
    启动第二笔 ``commit()``，验证进程内协调器阻止其穿插。
    """

    def __init__(self, *graphs):
        self.documents = {item.id: item for item in graphs}
        self.batch_calls: list[list[str]] = []
        self.forward_committed = asyncio.Event()
        self.reverse_entered = asyncio.Event()
        self.allow_reverse = asyncio.Event()

    async def list_graphs(self):
        return list(self.documents.values())

    async def replace_graph_batch(self, writes):
        self.batch_calls.append([write["tree"].id for write in writes])
        if len(self.batch_calls) == 1:
            reply = self._apply(writes)
            self.forward_committed.set()
            return reply
        if len(self.batch_calls) == 2:
            self.reverse_entered.set()
            await self.allow_reverse.wait()
        return self._apply(writes)

    def _apply(self, writes):
        for write in writes:
            current = self.documents[write["tree"].id]
            assert current.document_sha256 == write["base_document_sha256"], (
                "stale writer: base " + write["base_document_sha256"]
                + " != current " + current.document_sha256)
        reply = []
        for write in writes:
            stored = replace(write["tree"],
                             document_sha256=document_sha(write["tree"]))
            self.documents[stored.id] = stored
            reply.append(tree_to_dict(stored))
        return {"trees": reply}


@async_test
async def test_second_commit_cannot_interleave_with_compensation(tmp_path):
    """补偿窗口内的第二笔目录写必须等待，不能制造伪 ``catalog_sync_diverged``。

    A：内核批次提交成功 → SQLite 审计失败 → SQLite 回滚 → 反向批次（挂起）。
    补偿窗口内启动 B（其候选基于 A 已提交后的扫描视图）。没有协调器时 B 会先用
    这个临时视图再写一次内核图，A 的反向批次基线过期 → 伪分歧且原图不恢复。
    协调器保证 A 的 SQLite 事务 + 内核批次 + 补偿整体串行：B 在 A 恢复原图后才
    进入提交，于是它的过期基线得到 409 ``graph_conflict``（重新预览即可），
    两侧一致且原图恢复。
    """
    old = document_tree("main")
    client = InterleavingCoreClient(old)
    conn_a = fresh_conn(tmp_path / "a.db")
    conn_b = fresh_conn(tmp_path / "b.db")
    try:
        service_a = RemoteCatalogSyncService(conn_a, client)
        service_b = RemoteCatalogSyncService(conn_b, client)

        def failing_audit(conn):
            raise sqlite3.Error("audit failed")

        task_a = asyncio.create_task(service_a.commit(
            db_write=write_catalog_row,
            candidates=[replace(old, name="a-v2")],
            audit=failing_audit,
        ))
        await asyncio.wait_for(client.forward_committed.wait(), timeout=2)
        await asyncio.wait_for(client.reverse_entered.wait(), timeout=2)

        # A 的内核批次已提交：第二笔写扫描到的是临时提交视图（新基线）。
        concurrent_view = client.documents["main"]
        candidate_b = replace(concurrent_view, name="b-v2")
        task_b = asyncio.create_task(service_b.commit(
            db_write=write_catalog_row,
            candidates=[candidate_b],
            audit=record_audit,
        ))
        for _ in range(5):
            await asyncio.sleep(0)
        # 协调器：B 在 A 完成补偿前不能进入提交，也不会发出第二次内核批次。
        assert client.batch_calls == [["main"], ["main"]]
        assert not task_b.done()

        client.allow_reverse.set()
        with pytest.raises(sqlite3.Error, match="audit failed"):
            await task_a
        # A 已恢复原图；B 的基线来自临时视图，事务前基线门 409 且零写入。
        with pytest.raises(GraphConflictError):
            await task_b

        restored = client.documents["main"]
        assert document_sha(restored) == old.document_sha256
        assert restored.name == old.name
        assert _db_rows(conn_a, "catalog_writes") == []
        assert _db_rows(conn_a, "audit") == []
        assert _db_rows(conn_b, "catalog_writes") == []
        assert _db_rows(conn_b, "audit") == []
    finally:
        conn_a.close()
        conn_b.close()


# --------------------------------------------------------------------------- #
# 评审修复 2：传输失败歧义 —— 「确定未发出」vs「内核状态未知」
# --------------------------------------------------------------------------- #
class StatefulKernelTransport:
    """``httpx.MockTransport`` 级别的有状态内核（trees + graph-batches）。"""

    def __init__(self, *graphs):
        self.documents = {item.id: item for item in graphs}
        self.calls: list[tuple[str, str]] = []
        #: 每次 graph-batches 的 writes（正向批次 + 反向补偿都记录）。
        self.batch_writes: list[list[dict]] = []
        self.batch_failures: list[BaseException] = []
        #: 直接以 HTTP 答案失败（如内核 500 ``graph_batch_committed``）。
        self.batch_answers: list[httpx.Response] = []
        # 传输失败前内核是否已经提交（模拟「请求已送达、响应丢失」）。
        self.commit_before_failure = False
        # 提交前只应用第一份写（模拟无法解释的部分状态）。
        self.partial_commit_before_failure = False
        # 失败后、probe 之前执行（模拟第三方在窗口内改图）。
        self.after_failure = None
        # 失败后让 probe（GET /trees）也失败。
        self.fail_reads_after_failure = False
        self.fail_reads = False

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/internal/v1/trees":
            if self.fail_reads:
                raise httpx.ConnectError("kernel probe failed", request=request)
            return httpx.Response(200, json={
                "trees": [tree_to_dict(item) for item in self.documents.values()]})
        if request.url.path == "/internal/v1/graph-batches":
            writes = json.loads(request.content)["writes"]
            self.batch_writes.append(writes)
            if self.batch_answers:
                answer = self.batch_answers.pop(0)
                if self.commit_before_failure:
                    self._apply(writes,
                                partial=self.partial_commit_before_failure)
                return answer
            if self.batch_failures:
                failure = self.batch_failures.pop(0)
                if self.commit_before_failure:
                    self._apply(writes,
                                partial=self.partial_commit_before_failure)
                if self.after_failure is not None:
                    self.after_failure(self)
                if self.fail_reads_after_failure:
                    self.fail_reads = True
                raise failure
            return self._apply(writes)
        raise AssertionError(
            f"unexpected kernel call {request.method} {request.url.path}")

    def _apply(self, writes, *, partial=False):
        if partial:
            writes = writes[:1]
        for write in writes:
            current = self.documents[write["tree"]["id"]]
            if current.document_sha256 != write["base_document_sha256"]:
                return httpx.Response(409, json={"detail": {
                    "code": "graph_conflict",
                    "actual_document_sha256": current.document_sha256}})
        reply = []
        for write in writes:
            item = tree_from_dict(write["tree"])
            stored = replace(item, document_sha256=document_sha(item))
            self.documents[stored.id] = stored
            reply.append(tree_to_dict(stored))
        return httpx.Response(200, json={"trees": reply})

    def batch_count(self) -> int:
        return sum(1 for _method, path in self.calls
                   if path == "/internal/v1/graph-batches")

    def tree_read_count(self) -> int:
        return sum(1 for _method, path in self.calls
                   if path == "/internal/v1/trees")


def kernel_client(transport: StatefulKernelTransport) -> CoreClient:
    return CoreClient(
        "http://kernel:8400",
        http=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )


@async_test
async def test_definitely_not_sent_transport_failure_skips_compensation(conn):
    """连接失败 = 请求确定未发出：SQLite 回滚，无需 probe/补偿。"""
    old = document_tree("main")
    transport = StatefulKernelTransport(old)
    transport.batch_failures.append(httpx.ConnectError("connection refused"))
    client = kernel_client(transport)
    try:
        with pytest.raises(CoreBatchError) as caught:
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(old, name="v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert caught.value.status_code == 503
    assert caught.value.uncertain is False
    assert transport.tree_read_count() == 1        # 只有提交前的原图读取
    assert transport.batch_count() == 1            # 没有反向批次
    assert document_sha(transport.documents["main"]) == old.document_sha256
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_unknown_transport_failure_compensates_a_committed_batch(conn):
    """未知状态（读超时 + 内核已提交）：probe 发现候选已提交 → 原图反向补偿。"""
    old = document_tree("main")
    transport = StatefulKernelTransport(old)
    transport.commit_before_failure = True
    transport.batch_failures.append(httpx.ReadTimeout("response lost"))
    client = kernel_client(transport)
    try:
        with pytest.raises(CoreBatchError) as caught:
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(old, name="v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert caught.value.uncertain is True
    assert transport.tree_read_count() == 2        # 原图读取 + 状态 probe
    assert transport.batch_count() == 2            # 候选批次 + 反向补偿批次
    # 补偿批次：原图内容 + probe 到的新基线（= 候选文档哈希）。
    reverse = transport.batch_writes[1]
    assert reverse[0]["base_document_sha256"] == document_sha(
        replace(old, name="v2"))
    assert reverse[0]["tree"] == tree_to_dict(old)
    restored = transport.documents["main"]
    assert document_sha(restored) == old.document_sha256
    assert restored.name == old.name
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_unknown_transport_failure_without_commit_skips_compensation(conn):
    """未知状态但 probe 证明候选未提交：只需 SQLite 回滚，不补偿。

    probe 结论必须随异常传出（``CatalogWriteNotApplied``，``uncertain=False``）：
    原样重抛 incoming 的不确定错误会让 HTTP 层把它误报成「已提交但未确认」。
    """
    old = document_tree("main")
    transport = StatefulKernelTransport(old)
    transport.batch_failures.append(httpx.ReadTimeout("response lost"))
    client = kernel_client(transport)
    try:
        with pytest.raises(CoreBatchError) as caught:
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(old, name="v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert isinstance(caught.value, CatalogWriteNotApplied)
    assert caught.value.status_code == 503
    assert caught.value.uncertain is False
    assert transport.tree_read_count() == 2        # probe 执行了
    assert transport.batch_count() == 1            # 没有反向批次
    assert document_sha(transport.documents["main"]) == old.document_sha256
    assert _db_rows(conn, "catalog_writes") == []


@async_test
async def test_unknown_transport_failure_without_probe_is_explicit_divergence(conn):
    """未知状态且 probe 也失败：无法判断内核是否已提交 → 显式 503 分歧。"""
    old = document_tree("main")
    transport = StatefulKernelTransport(old)
    transport.commit_before_failure = True
    transport.fail_reads_after_failure = True
    transport.batch_failures.append(httpx.ReadTimeout("response lost"))
    client = kernel_client(transport)
    try:
        with pytest.raises(CatalogSyncDiverged, match="catalog_sync_diverged"):
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(old, name="v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_unknown_transport_failure_with_mixed_state_is_explicit_divergence(conn):
    """probe 只能解释部分候选（原子批次本不该出现）：按无法确认处理 → 分歧。"""
    first = document_tree("first")
    second = document_tree("second")
    transport = StatefulKernelTransport(first, second)
    transport.commit_before_failure = True
    transport.partial_commit_before_failure = True
    transport.batch_failures.append(httpx.ReadTimeout("response lost"))
    client = kernel_client(transport)
    try:
        with pytest.raises(CatalogSyncDiverged, match="catalog_sync_diverged"):
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(first, name="first-v2"),
                            replace(second, name="second-v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert transport.batch_count() == 1            # 不尝试用局部状态补偿
    assert _db_rows(conn, "catalog_writes") == []


def _third_party_edit(transport):
    """模拟补偿窗口内第三方图 CRUD：候选图变成既非原图也非候选的第三哈希。"""
    current = transport.documents["main"]
    edited = replace(current, name="edited-by-someone-else")
    transport.documents["main"] = replace(
        edited, document_sha256=document_sha(edited))


@async_test
async def test_probe_third_hash_is_explicit_divergence_without_compensation(conn):
    """内核已提交后又被第三方改成第三哈希：既不能当未提交，也不能乱补偿。"""
    old = document_tree("main")
    transport = StatefulKernelTransport(old)
    transport.commit_before_failure = True
    transport.after_failure = _third_party_edit
    transport.batch_failures.append(httpx.ReadTimeout("response lost"))
    client = kernel_client(transport)
    try:
        with pytest.raises(CatalogSyncDiverged, match="catalog_sync_diverged"):
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(old, name="v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert transport.tree_read_count() == 2        # 探测执行了
    assert transport.batch_count() == 1            # 第三态：不尝试用局部状态补偿
    assert transport.documents["main"].name == "edited-by-someone-else"
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []


@async_test
async def test_kernel_500_graph_batch_committed_is_compensated_after_probe(conn):
    """内核提交后响应构造失败（500 graph_batch_committed）：探测并反向补偿。"""
    old = document_tree("main")
    transport = StatefulKernelTransport(old)
    transport.commit_before_failure = True
    transport.batch_answers.append(httpx.Response(500, json={"detail": {
        "code": "graph_batch_committed",
        "message": "graph batch committed but the response failed",
    }}))
    client = kernel_client(transport)
    try:
        with pytest.raises(CoreBatchError) as caught:
            await RemoteCatalogSyncService(conn, client).commit(
                db_write=write_catalog_row,
                candidates=[replace(old, name="v2")],
                audit=record_audit,
            )
    finally:
        await client.aclose()

    assert caught.value.status_code == 500
    assert caught.value.uncertain is True
    assert transport.tree_read_count() == 2        # 原图读取 + 状态 probe
    assert transport.batch_count() == 2            # 候选批次 + 反向补偿批次
    restored = transport.documents["main"]
    assert document_sha(restored) == old.document_sha256
    assert restored.name == old.name
    assert _db_rows(conn, "catalog_writes") == []
    assert _db_rows(conn, "audit") == []
