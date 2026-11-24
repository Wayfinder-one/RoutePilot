"""决策组（graph）写用例：CRUD、部门设置与多图生命周期（Task 5）。

v4 文档自带 ``enabled`` / ``default`` / ``departments``，仓储只按文件内容扫描：
本服务是生命周期的唯一写入方。

- `create`：强制 ``enabled=False, default=False``（生命周期是服务端状态）→
  画布自身完整校验（结构 + 启用级；`tree_issues`）→ 文档写入 → 审计；
  不发布快照。
- `copy`：锁内读取源画布并做同一套完整校验（`tree_issues`）→ 文档写入
  （副本强制关闭）→ 审计；不发布快照。
- `update`：锁内按 root 优先合并已存 envelope → 部门补丁 → 画布自身完整校验
  （启用/未启用同一套）。启用图更新走 ``_commit_enabled_changes()``：重算并
  发布整个启用集合；纯草稿更新只写文件、不发布。
- `enable`：第一张启用图自动成为默认图，后续启用只设 ``enabled=True``；
  已启用图是 200 无操作（不写盘）。候选路由集在写盘前校验（部门冲突/默认图
  缺失都不落盘）。
- `disable`：已停用图是 200 无操作；默认图之外仍有启用图时抛
  ``DefaultGraphRequired``（409）；否则写 ``enabled=False, default=False``
  并发布剩余（可能为空）路由集。
- `set_default`：目标设 ``enabled=True, default=True``，旧默认图保持启用但改为
  非默认，同一批次原子提交。
- `set_departments`：复用普通更新路径，只提交 ``departments`` 补丁。

所有启用集合写入共用一个助手 ``_commit_enabled_changes()``：校验写入树的启用级
问题 → ``route_set_indices(candidate_enabled)``（部门冲突/默认图缺失绝不落盘）→
``stage_updates`` 只暂存实际写盘的图 → 提交 → ``publisher.prepare`` 候选快照 →
指纹复核 → 审计 → 发布。失败时批次回滚并丢弃暂存文件。

发布是纯内存替换（`SnapshotPublisher.publish`），绝不改文件或数据库；
服务抛出的异常由 API 层映射（ValidationFailed→422、GraphConflictError→409、
MissingError→404、DefaultGraphRequired→409、GraphReadOnlyError/GraphStorageError→503）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace

from router.control.errors import (
    AlreadyExists,
    DefaultGraphRequired,
    MissingError,
    ValidationFailed,
)
from router.control.validation import tree_issues
from router.routing.graph.validate import Issue
from router.routing.route_set import RouteSetInvariantError, route_set_indices
from router.routing.graph.model import canvas_to_dict, graph_to_dict
from router.routing.runtime import RUNTIME_FIELDS, runtime_to_dict
from router.routing.tree import RoutingTree, tree_from_dict, tree_revision_of
from router.store.repositories import ChangeRepo, now_iso
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphBatch,
    GraphChangedDuringLoad,
    GraphConflictError,
    GraphStorageError,
    RoutingTreeRepo,
)

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _Unset:
    """「字段缺失」哨兵：必须与显式 null（``None``）区分。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


def graph_parse_failed(exc: Exception) -> list[Issue]:
    return [Issue("graph_parse_error", params={"detail": str(exc)})]


def _route_set_issue(exc: RouteSetInvariantError) -> Issue:
    """路由集不变量错误 → 422 Issue（部门冲突携带部门与涉事图 id）。"""
    return Issue(
        exc.code,
        params={
            "department": exc.department,
            "tree_ids": list(exc.tree_ids),
        },
    )


@dataclass(frozen=True)
class GraphPatch:
    """PUT /admin/trees/{id} 的显式载荷。

    字段缺失（``UNSET``）= 继承已存值（spec §2.8 合并）；显式 null（``None``）
    原样交给 ``tree_from_dict``，恢复重构前的 dict 载荷语义：root/timezone 解析
    失败 → 422 Issue，sticky → 默认粘性配置。name 的显式 null 由路由折算成 ""
    （清空名称），与 base 的 ``_clean_name(None)`` 一致。

    四个扁平运行字段同样是补丁字段：缺失继承已存画布的运行配置，显式值覆盖。
    不存在任何运行配置控制字段或「按数据库物化」中间契约。

    ``departments`` 是普通保存可写的第三个字段（与 ``PUT /departments`` 共用
    同一条合并/校验/发布路径）；``enabled/default`` 是服务端生命周期，只能通过
    专用启停/默认图端点写入，``_envelope()`` 永远从已存文档继承。

    原始 API 的扁平 PATCH 中，若 ``jev`` 改为不同的非空服务 ID，必须在同一请求
    显式携带对应的 ``jev_window``；服务端拒绝继承旧服务窗口。相同服务 ID 的
    非窗口字段更新可以继承窗口，``jev: null`` 由图校验决定。console/inspector
    与目录确认同步始终把服务快照和窗口成对替换。
    """

    name: str | None | _Unset = UNSET
    root: dict | None | _Unset = UNSET
    canvas: dict | None | _Unset = UNSET
    sticky: dict | None | _Unset = UNSET
    timezone: str | None | _Unset = UNSET
    departments: list | None | _Unset = UNSET
    backends: dict | None | _Unset = UNSET
    jev: dict | None | _Unset = UNSET
    jev_window: dict | None | _Unset = UNSET
    time_periods: list | None | _Unset = UNSET


class GraphService:
    def __init__(self, conn: sqlite3.Connection, trees: RoutingTreeRepo, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, tree: RoutingTree) -> RoutingTree:
        # 生命周期是服务端状态：新建图一律关闭、非默认（仓储只按给定文档落盘）。
        tree = dataclass_replace(tree, enabled=False, default=False)
        issues = tree_issues(self.conn, tree)
        if issues:
            raise ValidationFailed(issues)
        with self.publisher.locked():
            try:
                created = self.trees.create(tree)
            except GraphAlreadyExistsError:
                raise AlreadyExists(f"tree {tree.id} already exists") from None
            self._audit("create", {"id": tree.id})
        return created

    def update(self, tree_id: str, patch: GraphPatch,
               *, base_document_sha256) -> RoutingTree:
        """普通保存：合并 envelope 后写盘；只有启用图更新才重算并发布路由集。

        生命周期字段只能由 ``_envelope()`` 从已存文档继承，普通保存永远不能写
        ``enabled/default``。启用图保存走启用集合提交助手：部门冲突/默认图缺失
        在写盘前拒绝；草稿保存只改文件、不发布。
        """
        with self.publisher.locked():
            current = self.trees.get(tree_id)
            if current is None:
                raise MissingError(f"unknown tree {tree_id}")
            tree = self._envelope(current, patch)
            if not isinstance(base_document_sha256, str) \
                    or not _SHA256_RE.fullmatch(base_document_sha256):
                raise GraphConflictError(current.document_sha256)
            issues = tree_issues(self.conn, tree)
            if issues:
                raise ValidationFailed(issues)
            if not current.enabled:
                updated = self.trees.update(
                    tree, base_document_sha256=base_document_sha256)
                self._audit(
                    "update", {"id": tree_id, "tree_revision": updated.tree_revision})
                return updated
            candidate_enabled = [
                tree if item.id == tree_id else item
                for item in self.trees.enabled()]
            self._commit_enabled_changes(
                # 客户端基线哈希交给 stage_updates 复核：过期即 409 graph_conflict，
                # 文件字节保持不变（草稿路径走 repo.update 的同一套基线检查）。
                [(tree, base_document_sha256)],
                candidate_enabled=candidate_enabled,
                action="update",
                # 审计记录写入文档的行为 revision：``_envelope()`` 产出的对象只有
                # 解析层字段（tree_revision 恒为 ""），必须从同一份树现算。
                detail={"id": tree_id, "tree_revision": tree_revision_of(tree)},
            )
            return self.trees.get(tree_id)

    def copy(self, tree_id: str, new_id: str, new_name: str) -> RoutingTree:
        with self.publisher.locked():
            source = self.trees.get(tree_id)
            if source is None:
                raise MissingError(f"unknown tree {tree_id}")
            issues = tree_issues(self.conn, source)
            if issues:
                raise ValidationFailed(issues)
            try:
                # 副本强制关闭生命周期：新建/复制/导入都不得直接接管流量。
                copied = self.trees.copy(tree_id, new_id, new_name)
            except KeyError:
                raise MissingError(f"unknown tree {tree_id}") from None
            except GraphAlreadyExistsError:
                raise AlreadyExists(f"tree {new_id} already exists") from None
            self._audit("copy", {"source": tree_id, "id": new_id})
        return copied

    # ── 多图生命周期：独立启停、默认图事务 ─────────────────────────────
    def enable(self, tree_id: str) -> None:
        """只启用目标图，不影响其它启用图；第一张启用图同时成为默认图。

        已启用图是幂等的 200 无操作（不写盘、不发布）。启用校验与候选路由集
        （部门唯一/默认图唯一）都在 ``stage_updates()`` 之前完成，冲突绝不落盘。
        """
        with self.publisher.locked():
            tree = self.trees.get(tree_id)
            if tree is None:
                raise MissingError(f"unknown tree {tree_id}")
            if tree.enabled:
                return
            issues = tree_issues(self.conn, tree)
            if issues:
                raise ValidationFailed(issues)
            enabled = self.trees.enabled()
            updated = dataclass_replace(tree, enabled=True, default=not enabled)
            self._commit_enabled_changes(
                [(updated, tree.document_sha256)],
                candidate_enabled=[updated, *enabled],
                action="enable",
                detail={"id": tree_id},
            )

    def disable(self, tree_id: str) -> None:
        """只停用目标图；已停用图是幂等的 200 无操作。

        默认图之外仍有启用图时禁止直接停用（必须先 ``set_default`` 切换），
        服务抛 ``DefaultGraphRequired``（API 映射 409）；停用最后一张启用图后
        发布空路由集（合法：``/readyz`` 保持 ready，公开 ``/v1`` 返回 503
        ``no_active_tree``）。
        """
        with self.publisher.locked():
            tree = self.trees.get(tree_id)
            if tree is None:
                raise MissingError(f"unknown tree {tree_id}")
            if not tree.enabled:
                return
            enabled = self.trees.enabled()
            remaining = [item for item in enabled if item.id != tree_id]
            if tree.default and remaining:
                raise DefaultGraphRequired(tree_id)
            disabled = dataclass_replace(tree, enabled=False, default=False)
            self._commit_enabled_changes(
                [(disabled, tree.document_sha256)],
                candidate_enabled=remaining,
                action="disable",
                detail={"id": tree_id},
            )

    def set_default(self, tree_id: str) -> None:
        """把目标图设为默认并启用；旧默认图保持启用但改为非默认（原子批次）。

        已是默认图时幂等 200（不写盘、不发布）。新旧两份文档在同一个
        ``stage_updates`` 批次里提交，失败时整批回滚。
        """
        with self.publisher.locked():
            tree = self.trees.get(tree_id)
            if tree is None:
                raise MissingError(f"unknown tree {tree_id}")
            if tree.default:
                return
            enabled = self.trees.enabled()
            previous_defaults = [
                item for item in enabled if item.default and item.id != tree_id]
            updated = dataclass_replace(tree, enabled=True, default=True)
            writes = [(updated, tree.document_sha256)]
            for old in previous_defaults:
                writes.append((
                    dataclass_replace(old, enabled=True, default=False),
                    old.document_sha256,
                ))
            candidate_enabled = [
                dataclass_replace(item, enabled=True, default=False)
                for item in enabled if item.id != tree_id
            ]
            candidate_enabled.append(updated)
            self._commit_enabled_changes(
                writes,
                candidate_enabled=candidate_enabled,
                action="set_default",
                detail={"id": tree_id},
            )

    def set_departments(self, tree_id: str, departments,
                        *, base_document_sha256) -> RoutingTree:
        """部门设置：复用普通保存路径，只提交 ``departments`` 补丁。

        与 ``update()`` 完全共享校验/写入/发布/审计：普通保存同样可以改部门，
        但生命周期字段永不通过该路径写入。
        """
        return self.update(
            tree_id,
            GraphPatch(departments=departments),
            base_document_sha256=base_document_sha256,
        )

    # ── 内部 ────────────────────────────────────────────────────────────
    def _commit_enabled_changes(self, writes, *, candidate_enabled,
                                action: str, detail: dict):
        """启用集合写入的唯一提交点：校验 → 暂存 → 提交 → 发布 → 回滚。

        ``writes`` 只含实际写盘的图（目标树, 当前文档哈希）；``candidate_enabled``
        是变更后的完整启用集合，用于写盘前校验路由集不变量。任何校验失败都在
        ``stage_updates()`` 之前抛出，磁盘与已发布快照保持原样。提交成功后的失败
        （候选快照不可构建/指纹变化）整批回滚，绝不发布半份路由集。
        """
        with self.publisher.locked():
            for tree, _expected_sha in writes:
                issues = tree_issues(self.conn, tree)
                if issues:
                    raise ValidationFailed(issues)
            try:
                route_set_indices(candidate_enabled)
            except RouteSetInvariantError as exc:
                raise ValidationFailed([_route_set_issue(exc)]) from exc
            batch = self.trees.stage_updates(writes)
            try:
                batch.commit()
                fingerprint = self.trees.route_fingerprint()
                prepared = self.publisher.prepare(
                    self.conn, self.trees.enabled(), fingerprint)
                if self.trees.route_fingerprint() != fingerprint:
                    raise GraphChangedDuringLoad("route set changed during publish")
                self._audit(action, detail)
            except BaseException:
                self._rollback_batch(batch)
                raise
            else:
                batch.discard()
                self.publisher.publish(prepared)
            return prepared

    @staticmethod
    def _envelope(current: RoutingTree, patch: GraphPatch) -> RoutingTree:
        """enabled/default 是外部状态：持久化 envelope 与已存画布一致（启停走专用端点）。

        patch 缺失（``UNSET``）的字段继承 current；显式 null 原样交给
        ``tree_from_dict``，恢复 base 对 root/timezone/sticky 的 null 语义。
        四个运行字段同样按补丁合并：缺失 = 继承已存画布运行配置，显式值 = 覆盖
        （显式 null 交给解析器按字段语义拒绝）。
        """
        if patch.canvas is UNSET:
            canvas = None if patch.root is not UNSET else canvas_to_dict(current.root)
        else:
            canvas = patch.canvas
        stored_runtime = runtime_to_dict(current.runtime)
        payload = {
            "id": current.id,
            "name": current.name if patch.name is UNSET else patch.name,
            "enabled": current.enabled,
            "default": current.default,
            "departments": (list(current.departments) if patch.departments is UNSET
                            else patch.departments),
            "root": graph_to_dict(current.root) if patch.root is UNSET else patch.root,
            "canvas": canvas,
            "sticky": ({"enabled": current.sticky.enabled,
                        "ttl_seconds": current.sticky.ttl_seconds}
                       if patch.sticky is UNSET else patch.sticky),
            "timezone": current.timezone if patch.timezone is UNSET else patch.timezone,
        }
        for field in RUNTIME_FIELDS:
            payload[field] = stored_runtime[field] if getattr(patch, field) is UNSET \
                else getattr(patch, field)
        if patch.jev_window is UNSET and isinstance(patch.jev, dict):
            new_service_id = patch.jev.get("id")
            current_service_id = (
                current.runtime.jev.id if current.runtime.jev is not None else None)
            if isinstance(new_service_id, str) and new_service_id \
                    and new_service_id != current_service_id:
                raise ValidationFailed(graph_parse_failed(ValueError(
                    "jev_window is required when changing JEV service id")))
        try:
            return tree_from_dict(payload)
        except ValueError as exc:
            raise ValidationFailed(graph_parse_failed(exc)) from exc

    def _rollback_batch(self, batch: GraphBatch) -> None:
        """批次提交失败时尽力恢复旧文件；回滚失败留给调用方/刷新兜底。"""
        try:
            batch.rollback()
        except GraphStorageError:
            logger.exception("graph batch rollback failed")
            raise
        finally:
            batch.discard()

    def _audit(self, action: str, detail: dict) -> None:
        try:
            with self.conn:
                ChangeRepo(self.conn).record(now_iso(), "tree", {"action": action, **detail})
        except sqlite3.Error:
            logger.exception("graph change committed but audit write failed: %s", action)
