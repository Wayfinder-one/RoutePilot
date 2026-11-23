"""控制面配置写用例：写入、校验、审计与候选快照构造共享一个事务。

破坏一致性约束的任何一步失败都回滚数据库；发布（内存替换 app.state.snapshot）
只发生在数据库提交之后。引用扫描、审计与发布由服务负责；API 只做 DTO 解析
与异常映射。

严格自包含画布语义：数据库目录保存本身只写目录行 + 审计，不再无差别重写任何
画布 JSON。画布运行快照只在新建/导入/显式刷新/确认影响清单后生成（由后续单元
的确认同步实现）。旧图/无活动图的数据库回退装配仍保留在快照构造路径里。

服务层异常 → HTTP 的映射关系（routes_* 统一处理）：
- ``ValidationFailed`` → 422（Issue 数组）
- ``ValueError`` → 422（detail 字符串）
- ``MissingError`` → 404（detail 字符串）
- ``AlreadyExists`` / ``ReferencedByError`` → 409（detail 字符串 / dict）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from router.control.errors import (
    AlreadyExists,
    MissingError,
    ReferencedByError,
    ValidationFailed,
)
from router.control.validation import build_enable_context
from router.routing.graph.validate import validate_graph_for_enable
from router.routing.snapshot import PreparedSnapshot
from router.store.repositories import (
    ChangeRepo,
    ModelRepo,
    ProviderRepo,
    TimePeriodRepo,
    model_ids_using_provider,
    model_references,
    now_iso,
    write_transaction,
)
from router.store.routing_trees import GraphBatch, GraphChangedDuringLoad, GraphStorageError

logger = logging.getLogger(__name__)


@dataclass
class _ConfigWriteResult:
    """写回调返回候选快照或暂存批次；正式图替换由外层统一执行。

    ``prepared`` 允许为 ``None``：配置写回调只负责校验、审计与暂存图批次，
    提交图文件与构造候选快照都在 ``_write_and_publish()`` 里完成。
    目录写不再暂存图批次（``graph_batch`` 恒为 ``None``），字段仅为过渡骨架保留。
    """

    prepared: PreparedSnapshot | None
    graph_batch: GraphBatch | None


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
    def _write_and_publish(self, write):
        with self.publisher.locked():
            result = None
            try:
                with write_transaction(self.conn):
                    result = write(self.conn)
                    if result.graph_batch is not None:
                        result.graph_batch.commit()
                        result.prepared = self._prepared_snapshot()
                    elif result.prepared is None:
                        result.prepared = self._prepared_snapshot()
                    result.prepared = self._recheck_fingerprint(result.prepared)
            except BaseException:
                self._rollback_graph_batch(result.graph_batch if result else None)
                raise
            if result.graph_batch is not None:
                result.graph_batch.discard()
            self.publisher.publish(result.prepared)

    def _recheck_fingerprint(self, prepared):
        """COMMIT 前在同一事务连接上复核磁盘指纹（等价旧 SnapshotPublisher.refresh 的复核语义）。

        同一连接能看到本事务未提交的写入，重建候选快照读到的是事务内配置。若图在
        候选快照构造后变了，用新指纹重建一次；仍不稳定就在提交前抛与 GraphService
        相同的冲突并回滚，绝不把过期指纹打给已发布快照。
        """
        for _attempt in range(2):
            fingerprint = self.trees.fingerprint()
            if fingerprint == prepared.fingerprint:
                return prepared
            prepared = self.publisher.prepare(
                self.conn, self.trees.active(), fingerprint)
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")

    # ── 配置写路径的校验 ────────────────────────────────────────────────
    def _require_valid_active_graph(self) -> None:
        """用「本事务即将提交」的数据库配置校验活动图。

        只读数据库行构造宽松上下文，不得在校验前写图文件：校验失败时数据库回滚，
        磁盘不应残留半份新配置；旧库的非法历史声明也不能在这里抛 500。
        """
        tree = self.trees.active()
        if tree is None:
            return
        issues = validate_graph_for_enable(tree, build_enable_context(self.conn))
        if issues:
            raise ValidationFailed(issues)

    def _prepared_snapshot(self):
        # 必须先取指纹再读树：两次读取之间图若被改写，_recheck_fingerprint 会看到
        # 不匹配并重建一次。反过来（先读树、后取指纹）会产出（过期树, 当前指纹），
        # 复核只看指纹标签，于是把过期树当最新快照发布出去。
        fingerprint = self.trees.fingerprint()
        return self.publisher.prepare(self.conn, self.trees.active(), fingerprint)

    def _finish_config_write(self, audit) -> _ConfigWriteResult:
        """校验并记录审计；目录写不再暂存任何图批次（画布由显式同步更新）。"""
        self._require_valid_active_graph()
        audit()
        return _ConfigWriteResult(None, None)

    def _rollback_graph_batch(self, batch: GraphBatch | None) -> None:
        """在数据库事务退出后恢复图；回滚失败时按已提交数据刷新实际快照。

        调用点必须是事务 ``with`` 之外：事务内的刷新会看到未提交的数据库状态，
        与已回滚/半回滚的文件互相矛盾。
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

    # ── 模型与提供方 ────────────────────────────────────────────────────
    # audit_* 参数仅为审计行保留请求原始值（与历史 /admin/changes 明细一致）；
    # 其余参数是已归一化的写值。
    def create_model(self, *, model_id: str, upstream: str, provider: str,
                     profile_text: str | None, audit_profile) -> None:
        def write(conn):
            if not ModelRepo(conn).create(model_id, upstream, provider, profile_text or "{}"):
                raise AlreadyExists(f"model {model_id} 已存在")
            return self._finish_config_write(lambda: ChangeRepo(conn).record(now_iso(), "model", {
                "action": "create", "id": model_id,
                "upstream": upstream, "provider": provider,
                "profile": audit_profile,
            }))
        self._write_and_publish(write)

    def update_model(self, *, model_id: str, upstream: str | None,
                     provider: str | None, profile_text: str | None,
                     audit_upstream, audit_provider, audit_profile) -> None:
        def write(conn):
            if not ModelRepo(conn).update(
                    model_id, upstream=upstream, provider=provider, profile=profile_text):
                raise MissingError(f"unknown model {model_id}")
            return self._finish_config_write(lambda: ChangeRepo(conn).record(now_iso(), "model", {
                "action": "update", "id": model_id,
                "upstream": audit_upstream, "provider": audit_provider,
                "profile": audit_profile,
            }))
        self._write_and_publish(write)

    def delete_model(self, model_id: str) -> None:
        def write(conn):
            if ModelRepo(conn).get(model_id) is None:
                raise MissingError(f"unknown model {model_id}")
            refs = model_references(self.trees.list(), model_id)
            if refs:
                raise ReferencedByError({
                    "message": f"model {model_id} 被路由树引用", "references": refs})
            if not ModelRepo(conn).delete(model_id):
                raise MissingError(f"unknown model {model_id}")
            # 删除未被引用的模型不会让活动图失效，无需启用校验。
            return self._finish_config_write(
                lambda: ChangeRepo(conn).record(
                    now_iso(), "model", {"action": "delete", "id": model_id}),
            )
        self._write_and_publish(write)

    def upsert_provider(self, *, name: str, base_url: str, key_env: str | None,
                        reasoning_format: str | None, api_key: str | None) -> None:
        def write(conn):
            existing = ProviderRepo(conn).get(name)
            ProviderRepo(conn).upsert(
                name, base_url, key_env=key_env,
                reasoning_format=reasoning_format, api_key=api_key)
            return self._finish_config_write(lambda: ChangeRepo(conn).record(now_iso(), "provider", {
                "action": "upsert", "name": name, "base_url": base_url,
                "key_env": key_env if key_env is not None else (existing or {}).get("key_env", ""),
                "reasoning_format": reasoning_format or (existing or {}).get("reasoning_format", "openai"),
                "api_key": "unchanged" if api_key is None else "cleared" if api_key == "" else "set",
            }))
        self._write_and_publish(write)

    def delete_provider(self, name: str) -> None:
        def write(conn):
            if ProviderRepo(conn).get(name) is None:
                raise MissingError(f"unknown provider {name}")
            used_by = model_ids_using_provider(conn, name)
            if used_by:
                raise ReferencedByError({"message": "provider 被模型引用", "models": used_by})
            if not ProviderRepo(conn).delete(name):
                raise MissingError(f"unknown provider {name}")
            return self._finish_config_write(
                lambda: ChangeRepo(conn).record(
                    now_iso(), "provider", {"action": "delete", "name": name}),
            )
        self._write_and_publish(write)
