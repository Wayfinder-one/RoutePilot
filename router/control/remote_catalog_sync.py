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

**进程内串行**：``commit()`` 的 SQLite 写事务 + 内核条件批次 + 反向补偿整体持有
一个进程级（按事件循环）提交协调器。否则第二笔目录写可能在「内核已提交、SQLite
已回滚、反向批次进行中」的补偿窗口穿插，改写候选图让反向批次基线过期——报告伪
``catalog_sync_diverged`` 且原图无法恢复。协调器只覆盖本进程的
``RemoteCatalogSyncService``：多进程/多副本部署时不同进程的提交仍可能交错，它们的
正向图写由内核基线的 ``document_sha256`` 校验（409 整批拒绝），但补偿窗口内的跨进程
交错仍会以 ``catalog_sync_diverged`` 暴露为需要人工核对的显式分歧。

**提交状态歧义**：内核批次请求失败分三类。``ConnectError`` / ``ConnectTimeout`` /
pool/protocol 准备失败 = 请求确定未发出，内核未变，只需回滚 SQLite；读超时、连接
中断，以及内核的 5xx / ``graph_batch_committed`` 答案（提交后响应构造失败）= 内核
状态未知（请求可能已提交、响应丢失），必须 ``list_graphs()`` 做**三态**探测：

- 全部候选仍是**原图哈希** → 确定未提交：SQLite 已回滚，不补偿；
- 全部候选已是**候选哈希** → 已提交：用原图 + 探测到的新基线补偿；
- 其他任何情况（混合态，或第三方在窗口内改成第三哈希 / 删除）→ 显式
  ``CatalogSyncDiverged`` 并记录每图状态，绝不静默当未提交。

探测本身失败同样显式 ``catalog_sync_diverged``。

异常映射保持既有目录写契约：
- 内核 409 ``graph_conflict`` → ``GraphConflictError(actual)`` → 409；
- 内核 422 Issue 数组 → ``ValidationFailed`` → 422；
- 内核 503 ``graph_storage_error`` → ``GraphStorageError`` → 503；
- 内核不可用 → ``CoreGraphError``/``CoreBatchError`` 由 app 级处理器映射为 503。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging

from router.control.errors import ValidationFailed
from router.control.graphs import candidate_document_issues
from router.core_client import CoreBatchError
from router.routing.document import graph_document_bytes
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


#: Attribute that keeps the per-loop commit coordinator on the event loop object
#: itself. Production runs one event loop, so this is effectively the process-wide
#: ``asyncio.Lock`` shared by every ``RemoteCatalogSyncService`` instance. The test
#: suite creates a fresh ``asyncio.run()`` loop per case: a module-level lock would
#: bind to the first loop that contends it (``asyncio`` caches the loop on the
#: lock), so it could neither be reused across test loops nor be garbage collected
#: (the lock references its loop). Keeping it on the loop gives one coordinator per
#: loop and lets a closed loop and its lock be collected together.
_COORDINATOR_ATTR = "_routepilot_catalog_commit_coordinator"


def _commit_coordinator() -> asyncio.Lock:
    """The per-process (per-event-loop) serialization point of catalog commits.

    A whole commit — originals read, SQLite write transaction, kernel conditional
    batch, audit/COMMIT and the reverse compensation — runs under this lock. It
    prevents a second in-process catalog commit from interleaving with the
    compensation window: without it a concurrent commit can move a candidate
    graph after the first kernel batch commit, so the reverse batch sees a stale
    base, reports a false ``catalog_sync_diverged`` and leaves the original graph
    unrestored. Cross-process writers are not serialized here; their graph writes
    stay kernel-verified by ``document_sha256`` base hashes.
    """
    loop = asyncio.get_running_loop()
    lock = getattr(loop, _COORDINATOR_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(loop, _COORDINATOR_ATTR, lock)
    return lock


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

        整个提交（SQLite 写事务 + 内核条件批次 + 反向补偿）在本进程内串行执行，
        详情见 :func:`_commit_coordinator`。``recheck(conn)`` 是可选回调，在 SQLite
        写事务内、``db_write`` 之前执行；可以是协程（例如需要重新读取内核影响
        清单）。``db_write(conn)`` / ``audit(conn)`` 在同一事务内。
        ``candidates`` 是保留原始 ``document_sha256`` 的新 ``RoutingTree``。
        """
        async with _commit_coordinator():
            return await self._commit_serialized(
                db_write=db_write, candidates=candidates, audit=audit,
                recheck=recheck)

    async def _commit_serialized(self, *, db_write, candidates, audit,
                                 recheck=None) -> list[str]:
        """``commit`` 的串行主体：必须在协调器内调用（测试也可直接驱动）。"""
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
        kernel_uncertain = False
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
                    domain = _domain_error(exc)
                    if domain is not None:
                        raise domain from exc
                    # 未映射的答案（读超时/连接中断、5xx、graph_batch_committed）
                    # 无法证明内核未提交：先让 SQLite 事务回滚，事务之外再
                    # probe/补偿。只有内核已核实的提交前拒绝才是确定未提交。
                    kernel_uncertain = bool(getattr(exc, "uncertain", False))
                    raise
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
            elif kernel_uncertain:
                # 请求可能已送达内核：回滚 SQLite 后探测内核状态，必要时补偿。
                _rollback_sqlite_best_effort(self.conn)
                await self._reconcile_uncertain_batch(candidates, originals, exc)
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

    async def _reconcile_uncertain_batch(
            self, candidates: list[RoutingTree],
            originals: dict[str, RoutingTree],
            cause: BaseException) -> None:
        """内核批次状态不确定后的**三态**探测（提交可能已发生、答案丢失）。

        对每份候选同时比较「当前内核文档哈希」与**原图哈希**、**候选哈希**：

        - 全部仍是原图哈希 → 确定未提交：内核未变，SQLite 已回滚，不补偿；
        - 全部已是候选哈希 → 已提交：用原图 + 探测到的新基线调用反向批次恢复；
        - 其他任何情况（混合态，或第三方把某张图改成第三种哈希 / 删除）→
          无法确认两侧一致：记录每图状态并以 ``CatalogSyncDiverged`` 上报。

        只判断「是否等于候选哈希」会把第三态误判成未提交（评审 I3）：内核提交
        后第三方 CRUD 改了图，探测到的哈希既不是原图也不是候选，绝不能静默返回。
        """
        try:
            current = {tree.id: tree for tree in await self.client.list_graphs()}
        except Exception as probe_error:  # noqa: BLE001 - 探测失败即无法确认
            logger.critical(
                "catalog sync cannot determine kernel state after an uncertain "
                "graph-batch commit (graphs: %s)",
                [candidate.id for candidate in candidates],
                exc_info=probe_error)
            raise CatalogSyncDiverged("catalog_sync_diverged") from probe_error
        expected = {
            candidate.id: hashlib.sha256(
                graph_document_bytes(candidate)).hexdigest()
            for candidate in candidates
        }
        # 候选的 ``document_sha256`` 就是提交前原文档的哈希（_load_originals
        # 已逐份核对）；这里以显式读到的原图文档为准。
        original_hashes = {
            candidate.id: originals[candidate.id].document_sha256
            for candidate in candidates
        }
        current_hashes = {
            candidate.id: (
                current[candidate.id].document_sha256
                if current.get(candidate.id) is not None else None)
            for candidate in candidates
        }
        if all(current_hashes[tree_id] == original_hashes[tree_id]
               for tree_id in original_hashes):
            logger.warning(
                "uncertain graph-batch answer (%s) but every kernel graph still "
                "holds its original document; nothing to compensate (graphs: %s)",
                cause, sorted(original_hashes))
            return
        if all(current_hashes[tree_id] == expected[tree_id]
               for tree_id in expected):
            new_hashes = {
                candidate.id: current_hashes[candidate.id]
                for candidate in candidates
            }
            await self._restore_after_kernel_commit(
                candidates, originals, new_hashes, cause)
            return
        detail = ", ".join(
            f"{tree_id}="
            + ("original" if actual == original_hashes[tree_id]
               else "candidate" if actual == expected[tree_id]
               else f"third-state {actual}")
            for tree_id, actual in current_hashes.items())
        logger.critical(
            "kernel graph state is neither all-original nor all-candidate after "
            "an uncertain graph-batch commit; refusing to guess (per graph: %s)",
            detail, exc_info=cause)
        raise CatalogSyncDiverged(
            "catalog_sync_diverged: kernel graph state is mixed or changed by a "
            f"third party ({detail})") from cause

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
