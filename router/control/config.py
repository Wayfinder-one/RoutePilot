"""控制面发布骨架：数据库事务内构造/复核候选快照，COMMIT 之后发布。

确认同步（目录写 + 图批次 + 审计）已由 ``RemoteCatalogSyncService`` 承担；
本模块只保留两条仍被回归测试钉住的发布不变量：

* ``_commit_locked()``：``publisher.locked()`` 内开一个数据库事务，事务内构造
  候选快照并在 COMMIT 前复核磁盘指纹；失败整体回滚，发布只发生在 COMMIT 之后
  （纯内存替换，绝不进数据库事务）。
* ``_prepared_snapshot_set()``：先取指纹、再读启用图，``_recheck_fingerprint()``
  在指纹变化时用新指纹重建完整启用集合，绝不让过期路由集被发布。

只改草稿的目录写不再走这里（见 ``RemoteCatalogSyncService``）；``save_periods``
与目录写路径的旧 ``commit_catalog_write`` 已删除。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from router.routing.snapshot import PreparedRouteSet
from router.store.repositories import write_transaction
from router.store.routing_trees import (
    GraphBatch,
    GraphChangedDuringLoad,
    GraphStorageError,
)

logger = logging.getLogger(__name__)


@dataclass
class _ConfigWriteResult:
    """写回调返回候选快照或暂存批次；图批次提交与发布会由外层统一执行。

    ``prepared`` 允许为 ``None``：配置写回调只负责校验、审计与暂存图批次，
    提交图文件与构造候选快照都在 ``_commit_locked()`` 里完成。
    ``prepare`` 控制是否在事务内构造并复核候选快照；``publish`` 控制 COMMIT
    之后是否在写锁内发布（仅候选改写了启用图时发布；候选可以是启用图，也可以
    是启用/非启用图的任意组合）。
    """

    prepared: PreparedRouteSet | None
    graph_batch: GraphBatch | None
    prepare: bool = True
    publish: bool = True


class ConfigurationService:
    def __init__(self, conn, trees, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    # ── 共享骨架：publisher.locked() → 写事务（写入/校验/审计/同步图 JSON/提交图批次/
    #    候选快照/复核指纹）→ COMMIT → publish（纯内存替换）。复核在事务内、提交之前
    #    进行：指纹仍不稳定时在 COMMIT 前抛冲突，DB、审计与候选快照一并回滚，且不发布。
    #    publish 不进数据库事务。exception 分支位于事务之外：数据库先 ROLLBACK，随后
    #    才恢复图文件，回滚失败时的快照刷新因此不会读到未回滚事务。──────────────
    def _commit_locked(self, write, *, after_prepare=None) -> _ConfigWriteResult:
        """发布骨架：「锁 + 事务 + 图批次 + 指纹复核 + 发布」。

        ``write(conn)`` 在事务内返回 ``_ConfigWriteResult``；``after_prepare(conn)``
        在候选快照构造与指纹复核之后、COMMIT 之前执行（审计行用）。整个事务期间
        持有 ``publisher.locked()``；发布是 COMMIT 之后的纯内存替换。
        """
        with self.publisher.locked():
            result: _ConfigWriteResult | None = None
            try:
                with write_transaction(self.conn):
                    result = write(self.conn)
                    if result.graph_batch is not None:
                        result.graph_batch.commit()
                    if result.prepare:
                        if result.prepared is None or result.graph_batch is not None:
                            result.prepared = self._prepared_snapshot_set()
                        result.prepared = self._recheck_fingerprint(result.prepared)
                    if after_prepare is not None:
                        after_prepare(self.conn)
            except BaseException:
                self._rollback_graph_batch(result.graph_batch if result else None)
                raise
            if result.graph_batch is not None:
                result.graph_batch.discard()
            if result.publish and result.prepared is not None:
                self.publisher.publish(result.prepared)
        return result

    def _recheck_fingerprint(self, prepared):
        """COMMIT 前在同一事务连接上复核磁盘指纹（等价旧 SnapshotPublisher.refresh 的复核语义）。

        同一连接能看到本事务未提交的写入，重建候选快照读到的是事务内配置。若图在
        候选快照构造后变了，用新指纹重建一次——重建始终重新读取 ``route_fingerprint()``
        与 ``enabled()``，覆盖**完整启用集合**，绝不回退到单图；仍不稳定就在提交前
        抛与 GraphService 相同的冲突并回滚，绝不把过期指纹打给已发布快照。
        """
        for _attempt in range(2):
            fingerprint = self.trees.route_fingerprint()
            if fingerprint == prepared.fingerprint:
                return prepared
            prepared = self.publisher.prepare(
                self.trees.enabled(), fingerprint)
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")

    def _write_and_publish(self, write):
        self._commit_locked(write)

    def _prepared_snapshot_set(self) -> PreparedRouteSet:
        """构造覆盖完整启用集合的候选路由集（只发布前复核用，不含单图回退）。

        必须先取指纹再读启用图：两次读取之间图若被改写，``_recheck_fingerprint``
        会看到不匹配并重建一次。反过来（先读图、后取指纹）会产出
        （过期路由集, 当前指纹），复核只看指纹标签，于是把过期路由集发布出去。
        路由集不变量（恰好一张默认图、部门唯一）由 ``publisher.prepare()`` 在
        装配边界统一映射成 ``GraphStorageError``（503），本方法不再另做索引推导。
        """
        fingerprint = self.trees.route_fingerprint()
        return self.publisher.prepare(self.trees.enabled(), fingerprint)

    def _rollback_graph_batch(self, batch: GraphBatch | None) -> None:
        """在数据库事务退出后恢复图；回滚失败时按已提交数据刷新实际快照。

        调用点必须是事务 ``with`` 之外：事务内的刷新会看到未提交的数据库状态，
        与已回滚/半回滚的文件互相矛盾。``publisher.refresh()`` 会重新扫描全部
        启用图并重建**完整**路由集（不是单图/默认图），因此回滚失败后内存不会
        停留在只含默认图的旧状态。
        """
        if batch is None:
            return
        try:
            batch.rollback()
        except GraphStorageError:
            logger.exception("graph batch rollback failed")
            try:
                self.publisher.refresh()
            except Exception:
                logger.exception("snapshot refresh after rollback failure also failed")
            raise
