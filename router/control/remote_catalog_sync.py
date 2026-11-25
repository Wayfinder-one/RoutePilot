"""跨服务目录写：SQLite 单写事务 + 内核条件图批次的原子补偿。

生产拓扑下目录（模型 / provider / JEV）在 SQLite，画布 JSON 在内核；一笔目录写
不再拥有「本地单事务」覆盖两侧。本服务实现设计 §6.2 的提交顺序：

1. 先在 SQLite 写事务里执行 ``recheck``（事务内、写目录行之前重算最新引用）、
   严格校验每份候选画布，再执行 ``db_write``；随后把一份条件批次交给内核：每份候选画布携带它在影响预览/扫描
   时的 ``document_sha256`` 作为基线。内核在一个发布锁里复核全部基线、严格校验
   全部候选、校验结果启用集合，然后一次性提交——任一条件不满足则整批拒绝，任何
   文件都不会被改写。
2. 内核提交成功后写审计行并提交 SQLite。SQLite 的任一步失败（目录行、审计或
   COMMIT 本身）都会先回滚 SQLite，再按**候选倒序**、**原画布内容**、**内核返回
   的新基线哈希**调用一次反向批次，把内核恢复到写入前的内容。
3. 反向批次自身失败时无法确认两侧一致：抛 ``CatalogSyncDiverged``（HTTP 503
   ``catalog_sync_diverged``）并记录严重错误，绝不返回成功或静默留下部分更新。

边界内不做图文件 I/O、不持有发布锁：内核是唯一图写者（全局约束 2）。没有候选
画布的纯 SQLite 写只提交数据库，不调用图批次。

异常映射保持既有目录写契约：
- 内核 409 ``graph_conflict`` → ``GraphConflictError(actual)`` → 409；
- 内核 422 Issue 数组 → ``ValidationFailed`` → 422；
- 内核 503 ``graph_storage_error`` → ``GraphStorageError`` → 503；
- 内核不可用 → ``CoreGraphError``/``CoreBatchError`` 由 app 级处理器映射为 503。
"""
from __future__ import annotations

import inspect
import logging

from router.control.errors import ValidationFailed
from router.control.graphs import candidate_document_issues
from router.core_client import CoreBatchError
from router.routing.graph.validate import Issue
from router.routing.tree import RoutingTree
from router.store.repositories import write_transaction
from router.store.routing_trees import GraphConflictError, GraphStorageError

logger = logging.getLogger(__name__)

__all__ = [
    "CatalogSyncDiverged",
    "CoreBatchError",
    "RemoteCatalogSyncService",
]


class CatalogSyncDiverged(RuntimeError):
    """SQLite 已回滚但内核反向批次失败：两侧一致性无法确认（HTTP 503）。

    这是「目录写失败但图已改、且补偿也失败」的显式信号：部署需要人工核对内核
    图目录与 SQLite 目录。调用方绝不能把它当成功，也不能覆盖成普通错误。
    """


def _issues_from_payload(payload) -> list[Issue] | None:
    """内核 422 载荷（presented Issue 数组）→ 领域 ``Issue`` 列表。

    ``None`` 表示载荷不是可识别的 Issue 数组（由调用方保留原错误）。
    """
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, list) or not detail:
        return None
    issues: list[Issue] = []
    for item in detail:
        if not isinstance(item, dict) or not isinstance(item.get("code"), str):
            return None
        params = item.get("params")
        issues.append(Issue(
            item["code"],
            node_id=item.get("node_id") or "",
            edge_id=item.get("edge_id") or "",
            port=item.get("port") or "",
            level=item.get("level") or "hard",
            params=dict(params) if isinstance(params, dict) else {},
        ))
    return issues


def _domain_error(exc: CoreBatchError):
    """把内核批次的失败状态映射回目录写的领域异常（无法映射时返回 ``None``）。"""
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    detail = payload.get("detail")
    if exc.status_code == 409 and isinstance(detail, dict) \
            and detail.get("code") == "graph_conflict":
        return GraphConflictError(detail.get("actual_document_sha256"))
    if exc.status_code == 422:
        failed = _issues_from_payload(payload)
        if failed is not None:
            return ValidationFailed(failed)
    if exc.status_code == 503 and isinstance(detail, dict) \
            and detail.get("code") == "graph_storage_error":
        return GraphStorageError(detail.get("message") or "graph_storage_error")
    return None


def _rollback_sqlite_best_effort(conn) -> None:
    """COMMIT 自身失败时事务可能仍开着：先尽力回滚再补偿内核。

    ``write_transaction`` 在 with-body 抛异常时已 ROLLBACK；只有 COMMIT 语句本身
    失败会留下打开的事务。补偿（网络调用）不应在持有 SQLite 写锁时进行。
    """
    try:
        if conn.in_transaction:
            conn.rollback()
    except Exception:  # noqa: BLE001 - 补偿优先，回滚失败仍继续
        logger.exception("catalog sync could not roll back the SQLite transaction")


async def _run_recheck(recheck, conn) -> None:
    """执行可选的 ``recheck(conn)``：允许同步回调，也允许在事务内读内核的异步回调。"""
    if recheck is None:
        return
    result = recheck(conn)
    if inspect.isawaitable(result):
        await result


class RemoteCatalogSyncService:
    """目录写 + 内核画布批次 + SQLite 失败补偿（模型 / provider / JEV 共用）。"""

    def __init__(self, conn, client):
        self.conn = conn
        self.client = client

    async def commit(self, *, db_write, candidates, audit,
                     recheck=None) -> list[str]:
        """原子提交目录写 + 候选画布；返回同步的画布 id 列表（候选顺序）。

        ``recheck(conn)``：可选。在 SQLite 写事务内、``db_write`` 之前执行；它可以
        是协程函数（例如需要重新读取内核影响清单），也可以保持同步。它新增引用时
        必须让事务回滚（409），绝不带着过期名单提交。
        ``db_write(conn)``：写数据库目录行；``audit(conn)``：写审计行。二者都在同一
        事务内，任一异常（含 COMMIT）都触发内核反向批次补偿。
        ``candidates`` 是保留原始 ``document_sha256`` 的新 ``RoutingTree``。
        """
        candidates = list(candidates)
        if not candidates:
            # 纯 SQLite 目录写：不改变任何画布，绝不调用图批次。
            with write_transaction(self.conn):
                await _run_recheck(recheck, self.conn)
                db_write(self.conn)
                audit(self.conn)
            return []

        # 原图在事务之前读取：补偿必须能还原「内核本次替换前」的完整文档，而且
        # 基线不匹配时应在写数据库之前就以 409 拒绝（内核批次仍会再复核一次）。
        originals = await self._load_originals(candidates)
        writes = [
            {"tree": tree, "base_document_sha256": tree.document_sha256}
            for tree in candidates
        ]
        kernel_committed = False
        new_hashes: dict[str, str] = {}
        try:
            with write_transaction(self.conn):
                await _run_recheck(recheck, self.conn)
                # 候选严格校验（与内核同一入口）：非法候选必须在写目录行与调用
                # 内核批次之前变成 422，避免把不可落盘的文档发到内核再回滚。
                for candidate in candidates:
                    issues = candidate_document_issues(candidate)
                    if issues:
                        raise ValidationFailed(issues)
                db_write(self.conn)
                try:
                    reply = await self.client.replace_graph_batch(writes)
                except CoreBatchError as exc:
                    raise _domain_error(exc) or exc from exc
                kernel_committed = True
                new_hashes = _committed_hashes(reply)
                audit(self.conn)
        except BaseException as exc:  # noqa: BLE001 - 失败后必须决定是否补偿
            if kernel_committed:
                # SQLite 事务已被 write_transaction 回滚（COMMIT 失败时补齐回滚）；
                # 内核已提交，必须反向恢复。
                _rollback_sqlite_best_effort(self.conn)
                await self._restore_after_kernel_commit(
                    candidates, originals, new_hashes, exc)
            raise
        return [tree.id for tree in candidates]

    async def _load_originals(
            self, candidates: list[RoutingTree]) -> dict[str, RoutingTree]:
        """读取内核当前文档并核对候选基线；任何过期基线整体 409。"""
        current = {tree.id: tree for tree in await self.client.list_graphs()}
        originals: dict[str, RoutingTree] = {}
        for candidate in candidates:
            original = current.get(candidate.id)
            actual = original.document_sha256 if original is not None else None
            if not candidate.document_sha256 or actual != candidate.document_sha256:
                raise GraphConflictError(actual)
            originals[candidate.id] = original
        return originals

    async def _restore_after_kernel_commit(
            self, candidates: list[RoutingTree],
            originals: dict[str, RoutingTree],
            new_hashes: dict[str, str],
            cause: BaseException) -> None:
        """在候选倒序上用原图内容 + 新基线哈希调用一次反向批次。"""
        writes = []
        for candidate in reversed(candidates):
            base = new_hashes.get(candidate.id)
            original = originals.get(candidate.id)
            if not isinstance(base, str) or not base or original is None:
                logger.critical(
                    "catalog sync cannot compensate: %s has no committed base",
                    candidate.id, exc_info=cause)
                raise CatalogSyncDiverged("catalog_sync_diverged") from cause
            writes.append({"tree": original, "base_document_sha256": base})
        try:
            await self.client.replace_graph_batch(writes)
        except Exception as exc:  # noqa: BLE001 - 补偿失败必须显式上报
            logger.critical(
                "catalog sync compensation failed; kernel graphs and SQLite may "
                "have diverged (graph order: %s)",
                [candidate.id for candidate in reversed(candidates)],
                exc_info=exc)
            raise CatalogSyncDiverged("catalog_sync_diverged") from exc


def _committed_hashes(reply) -> dict[str, str]:
    """内核成功响应 ``{"trees": [...]}`` → {tree_id: document_sha256}。

    结构异常时返回空映射：调用方会把它当作「无法构造补偿基线」的严重分歧，
    而不是拿一张空基线去恢复。
    """
    trees = reply.get("trees") if isinstance(reply, dict) else None
    if not isinstance(trees, list):
        return {}
    hashes: dict[str, str] = {}
    for item in trees:
        if not isinstance(item, dict):
            continue
        tree_id = item.get("id")
        sha = item.get("document_sha256")
        if isinstance(tree_id, str) and isinstance(sha, str) and sha:
            hashes[tree_id] = sha
    return hashes
