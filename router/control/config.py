"""控制面配置写用例：写入、校验、审计与候选快照构造共享一个事务。

破坏一致性约束的任何一步失败都回滚数据库；发布（内存替换 app.state.snapshot_set）
只发生在数据库提交之后。引用扫描、审计与发布由服务负责；API 只做 DTO 解析
与异常映射。

严格自包含画布语义：数据库目录保存本身只写目录行 + 审计，不再无差别重写任何
画布 JSON。画布运行快照只在新建/导入/显式刷新/确认影响清单后生成（由后续单元
的确认同步实现）。启用图校验也只读画布自身的内嵌运行配置，因此目录写永远不会
因为「数据库与画布不一致」而被拒绝。

确认同步（``commit_catalog_write``）在一个写锁 + 一个数据库事务里完成三件事：

1. ``recheck(conn)`` 在事务内、写目录行之前重算最新引用/影响清单。路由层的
   影响扫描发生在加锁之前，若画布/模型在该窗口内新出现，这里必须把它变成
   409（``graph_conflict`` / ``ReferencedByError``），而不是漏同步或漏拦引用。
2. 逐份候选画布先按「即将写出的字节」做一次严格文档解析
   （``graph_document_bytes`` → ``parse_graph_document``），再做启用级校验。
   启用级 ``tree_issues`` 不会重新校验模型 profile 与 provider 推理协议是否
   自洽，只靠它会让无法被 ``RoutingTreeRepo`` 读回的文档落盘（随后
   ``trees.list()`` 503）；启用与非启用候选都必须过这一关。
3. 目录写、候选校验、图批次暂存/提交、候选快照构造与指纹复核、审计在同一
   事务内，失败一起回滚；候选快照与指纹复核始终覆盖**完整启用集合**，发布
   只在 COMMIT 之后、写锁内、且候选改写了至少一张启用图时发生（纯内存替换，
   绝不进数据库事务）。只被影响的启用图不是默认图时同样必须发布：否则磁盘
   新画布要等到 reloader 轮询才进入内存路由集。

服务层异常 → HTTP 的映射关系（routes_* 统一处理）：
- ``ValidationFailed`` → 422（Issue 数组）
- ``ValueError`` → 422（detail 字符串）
- ``MissingError`` → 404（detail 字符串）
- ``AlreadyExists`` / ``ReferencedByError`` → 409（detail 字符串 / dict）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from router.control.errors import ValidationFailed
from router.control.graphs import graph_parse_failed
from router.control.validation import enabled_tree_issues, tree_issues
from router.routing.document import graph_document_bytes, parse_graph_document
from router.routing.snapshot import PreparedRouteSet
from router.routing.tree import RoutingTree
from router.store.repositories import (
    ChangeRepo,
    TimePeriodRepo,
    now_iso,
    write_transaction,
)
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
        """目录写与确认同步共用的「锁 + 事务 + 图批次 + 指纹复核 + 发布」骨架。

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
                self.conn, self.trees.enabled(), fingerprint)
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")

    def _write_and_publish(self, write):
        self._commit_locked(write)

    # ── 配置写路径的校验 ────────────────────────────────────────────────
    def _require_valid_enabled_graphs(self) -> None:
        """用画布自身的内嵌运行配置校验全部启用图。

        快照发布会装配全部启用图，因此任一启用图校验失败都必须整体拒绝。
        数据库目录不参与启用判断：目录行与画布不一致只影响检查提示，不会让
        目录写失败。校验失败时数据库回滚，磁盘不应残留半份新配置。
        """
        issues = enabled_tree_issues(self.conn, self.trees)
        if issues:
            raise ValidationFailed(issues)

    def _validate_candidate(self, tree: RoutingTree) -> None:
        """严格校验一份候选画布，保证它写盘后仍能被仓库读回。

        先做启用级 ``tree_issues``（能给出可定位的推理档位等 issue），再按
        ``stage_updates`` 将会写出的字节做一次完整文档解析：启用级校验只看
        target 的推理档位是否在画布声明的档位表里，不会用候选的 provider 推理
        协议重新校验模型 profile；只靠它会让「provider 协议翻转后非启用候选
        profile 非法」这类候选直接落盘，随后 ``trees.list()`` 抛
        ``GraphStorageError``。
        """
        issues = tree_issues(self.conn, tree)
        if issues:
            raise ValidationFailed(issues)
        try:
            # 校验「即将写出的字节」：仓储按给定 v4 文档原样落盘，生命周期字段
            # 不再在写盘路径上归一，因此直接解析这份文档。
            raw = graph_document_bytes(tree)
            parse_graph_document(
                raw,
                tree_id=tree.id,
                updated_at=tree.updated_at,
                document_sha256=tree.document_sha256,
            )
        except (ValueError, TypeError) as exc:
            raise ValidationFailed(graph_parse_failed(exc)) from exc

    def _prepared_snapshot_set(self) -> PreparedRouteSet:
        """构造覆盖完整启用集合的候选路由集（只发布前复核用，不含单图回退）。

        必须先取指纹再读启用图：两次读取之间图若被改写，``_recheck_fingerprint``
        会看到不匹配并重建一次。反过来（先读图、后取指纹）会产出
        （过期路由集, 当前指纹），复核只看指纹标签，于是把过期路由集发布出去。
        路由集不变量（恰好一张默认图、部门唯一）由 ``publisher.prepare()`` 在
        装配边界统一映射成 ``GraphStorageError``（503），本方法不再另做索引推导。
        """
        fingerprint = self.trees.route_fingerprint()
        return self.publisher.prepare(
            self.conn, self.trees.enabled(), fingerprint)

    # ── 确认后的目录同步（模型/provider/JEV 影响写用例共用骨架）────────────
    # 与 _write_and_publish 同一套语义：publisher.locked() → 单写事务（引用复核/
    # 目录写/候选严格校验/预检并暂存全部画布/图批次提交/候选快照/复核指纹/审计）
    # → COMMIT → 仅在候选改写了启用图时发布新快照。候选快照与指纹复核始终按完整
    # 启用集合构造。候选画布保留原始 document_sha256，stage_updates() 在事务内
    # 逐份核对基线哈希（GraphBatch 负责非默认先写、默认最后写与回滚），因此不
    # 需要另建多文件写入协议。
    def commit_catalog_write(self, *, db_write, candidates, audit,
                             recheck=None) -> list[str]:
        """目录写 + 候选画布原子提交；返回同步的画布 id（候选顺序）。

        ``recheck(conn)``：可选。在写锁内、事务内、``db_write`` 之前执行，用于
        重算最新引用/影响清单（路由层扫描与加锁之间的竞态不会漏同步或漏拦）。
        ``audit(conn)``：候选快照构造与指纹复核之后、COMMIT 之前写审计行。

        ``candidates`` 可以同时含启用图与非启用图（草稿）。发布门必须在写锁内、
        事务内决定：路由层构造候选之后、本方法拿到写锁之前可能有一笔 enable
        落盘；若在加锁前读取启用集合，就会出现「批次写入了刚被启用的画布，却
        因为旧集合里没有它而跳过发布」的竞态。因此这里在暂存批次之后、同一把锁
        内重新扫描启用集合，只要任一候选命中启用图（含非默认启用图）就发布；
        候选全是草稿或纯数据库写（无候选）时不发布。候选快照与指纹复核仍按完整
        启用集合执行，路由集不变量损坏由装配边界统一映射为 503。
        """
        candidates = list(candidates)

        def write(conn):
            if recheck is not None:
                recheck(conn)
            db_write(conn)
            for tree in candidates:
                self._validate_candidate(tree)
            if not candidates:
                # 纯数据库目录写：没有候选时不可能改变任何快照内容。
                return _ConfigWriteResult(
                    prepared=None, graph_batch=None,
                    prepare=False, publish=False)
            batch = self.trees.stage_updates([
                (tree, tree.document_sha256) for tree in candidates
            ])
            try:
                # 发布门与 ``stage_updates`` 处于同一临界区；加锁前发生的启停
                # 在这里可见。只改草稿不发布路由集，改任一启用图（默认或非
                # 默认）都必须发布完整启用集合。
                enabled_changed = any(
                    tree.enabled and any(
                        tree.id == candidate.id for candidate in candidates)
                    for tree in self.trees.enabled())
                return _ConfigWriteResult(
                    prepared=None, graph_batch=batch,
                    prepare=True, publish=enabled_changed)
            except BaseException:
                # 暂存之后、批次交给外层事务之前抛出的任何异常（含发布门读取
                # 失败）都必须撤销暂存：外层拿不到 GraphBatch，隐藏临时文件
                # 会永远留在 graphs/ 目录里。
                batch.discard()
                raise

        self._commit_locked(write, after_prepare=lambda _conn: audit(self.conn))
        return [tree.id for tree in candidates]

    def _finish_config_write(self, audit) -> _ConfigWriteResult:
        """校验并记录审计；目录写不再暂存任何图批次（画布由显式同步更新）。"""
        self._require_valid_enabled_graphs()
        audit()
        return _ConfigWriteResult(None, None)

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
                self.publisher.refresh(self.conn)
            except Exception:
                logger.exception("snapshot refresh after rollback failure also failed")
            raise

    def save_periods(self, periods: list) -> None:
        """时段替换写用例：TimePeriodRepo.save 的 ValueError（结构/重名）原样上抛。"""
        self._write_and_publish(lambda conn: self._save_periods_in_tx(periods))

    def _save_periods_in_tx(self, periods: list):
        TimePeriodRepo(self.conn).save(periods)
        return self._finish_config_write(
            lambda: ChangeRepo(self.conn).record(
                now_iso(), "time-periods", {"periods": len(periods)}),
        )
