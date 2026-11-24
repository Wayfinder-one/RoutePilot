"""决策组（graph）写用例：CRUD/启停的校验、候选快照与发布次序（brief Step 3）。

v4 文档自带 ``enabled`` / ``default``，仓储只按文件内容扫描：本服务是生命周期
的唯一写入方。

- `create`：强制 ``enabled=False, default=False``（生命周期是服务端状态）→
  画布自身完整校验（结构 + 启用级；`tree_issues`）→（锁内）文档写入 → 审计；
  不发布快照。
- `copy`：锁内读取源画布并做同一套完整校验（`tree_issues`）→（锁内）文档写入
  （副本强制关闭）→ 审计；不发布快照。
- `update`：锁内按 root 优先合并已存 envelope → 画布自身完整校验（启用/未启用
  同一套）→ 已启用图先 prepare 候选快照 → `base_document_sha256` 冲突检查后按
  给定文档原样写入 → 核对落盘哈希 → 用当前路由集指纹 publish → 审计。草稿
  （未启用图）更新不发布。
- `enable`：启用校验 → 先 prepare 候选快照（不可构建的候选绝不落盘）→ 一个
  ``stage_updates`` 批次把其它启用图写成关闭、目标写成默认启用 → 核对每份
  落盘文档哈希 → 用当前路由集指纹 publish → 审计。
- `disable`：把目标写成关闭；它原本是默认启用图时发布空快照（无启用图是合法
  路由集）→ 审计。

发布是纯内存替换（`SnapshotPublisher.publish`），绝不改文件或数据库；
服务抛出的异常由 API 层映射（ValidationFailed→422、GraphConflictError→409、
MissingError→404、GraphReadOnlyError/GraphStorageError→503）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace

from router.control.errors import AlreadyExists, MissingError, ValidationFailed
from router.control.validation import tree_issues
from router.routing.document import graph_document_bytes
from router.routing.graph.validate import Issue
from router.routing.route_set import RouteSetInvariantError, route_set_indices
from router.routing.graph.model import canvas_to_dict, graph_to_dict
from router.routing.runtime import RUNTIME_FIELDS, runtime_to_dict
from router.routing.tree import RoutingTree, tree_from_dict
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


@dataclass(frozen=True)
class GraphPatch:
    """PUT /admin/trees/{id} 的显式载荷。

    字段缺失（``UNSET``）= 继承已存值（spec §2.8 合并）；显式 null（``None``）
    原样交给 ``tree_from_dict``，恢复重构前的 dict 载荷语义：root/timezone 解析
    失败 → 422 Issue，sticky → 默认粘性配置。name 的显式 null 由路由折算成 ""
    （清空名称），与 base 的 ``_clean_name(None)`` 一致。

    四个扁平运行字段同样是补丁字段：缺失继承已存画布的运行配置，显式值覆盖。
    不存在任何运行配置控制字段或「按数据库物化」中间契约。

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

            candidate = None
            expected_sha = None
            if current.enabled:
                # 先构造候选快照（内存只读）：不可构建的候选绝不落盘。写入按给定
                # 文档原样落盘，因此发布指纹在写盘后由当前路由集重新计算；候选树
                # 的 document_sha256 换成即将写出的字节哈希，快照元数据才与磁盘一致。
                expected_sha = hashlib.sha256(graph_document_bytes(tree)).hexdigest()
                document = dataclass_replace(tree, document_sha256=expected_sha)
                candidate = self.publisher.prepare(self.conn, document, "")
            updated = self.trees.update(tree, base_document_sha256=base_document_sha256)
            if candidate is not None:
                self._require_document_sha(updated, expected_sha)
                self._publish_candidate(
                    candidate, default_tree_id=tree_id, document_sha256=expected_sha)
            self._audit("update", {"id": tree_id, "tree_revision": updated.tree_revision})
            return updated

    def copy(self, tree_id: str, new_id: str, new_name: str) -> RoutingTree:
        with self.publisher.locked():
            source = self.trees.get(tree_id)
            if source is None:
                raise MissingError(f"unknown tree {tree_id}")
            issues = tree_issues(self.conn, source)
            if issues:
                raise ValidationFailed(issues)
            try:
                copied = self.trees.copy(tree_id, new_id, new_name)
            except KeyError:
                raise MissingError(f"unknown tree {tree_id}") from None
            except GraphAlreadyExistsError:
                raise AlreadyExists(f"tree {new_id} already exists") from None
            self._audit("copy", {"source": tree_id, "id": new_id})
        return copied

    # ── 启停（过渡期单默认图语义；Task 5 扩展为多图启停）───────────────
    def enable(self, tree_id: str) -> None:
        """启用一张图 = 让它成为唯一启用且默认的图。

        过渡期保持旧的单活动图语义：一个批次里把其它启用图写成关闭、目标写成
        ``enabled=True, default=True``。候选快照在写盘前构造，落盘后核对每份文档
        哈希，再用当前路由集指纹发布。
        """
        with self.publisher.locked():
            tree = self.trees.get(tree_id)
            if tree is None:
                raise MissingError(f"unknown tree {tree_id}")
            issues = tree_issues(self.conn, tree)
            if issues:
                raise ValidationFailed(issues)
            enabled_tree = dataclass_replace(tree, enabled=True, default=True)
            enabled_sha = hashlib.sha256(graph_document_bytes(enabled_tree)).hexdigest()
            # 候选快照树携带即将写出的文档哈希（与磁盘一致）。
            enabled_tree = dataclass_replace(enabled_tree, document_sha256=enabled_sha)
            candidate = self.publisher.prepare(self.conn, enabled_tree, "")
            updates = [
                (dataclass_replace(other, enabled=False, default=False),
                 other.document_sha256)
                for other in self.trees.enabled() if other.id != tree_id
            ]
            updates.append((enabled_tree, tree.document_sha256))
            expected = {
                update[0].id: hashlib.sha256(graph_document_bytes(update[0])).hexdigest()
                for update in updates
            }
            batch = self.trees.stage_updates(updates)
            try:
                batch.commit()
            except BaseException:
                self._rollback_batch(batch)
                raise
            batch.discard()
            for written_id, expected_sha in expected.items():
                self._require_document_sha(self.trees.get(written_id), expected_sha)
            self._publish_candidate(
                candidate, default_tree_id=tree_id, document_sha256=enabled_sha)
            self._audit("enable", {"id": tree_id})

    def disable(self, tree_id: str) -> None:
        with self.publisher.locked():
            tree = self.trees.get(tree_id)
            if tree is None:
                raise MissingError(f"unknown tree {tree_id}")
            was_enabled = tree.enabled
            candidate = (
                self.publisher.prepare(self.conn, None, "") if was_enabled else None)
            disabled = dataclass_replace(tree, enabled=False, default=False)
            expected_sha = hashlib.sha256(graph_document_bytes(disabled)).hexdigest()
            updated = self.trees.update(disabled, base_document_sha256=tree.document_sha256)
            self._require_document_sha(updated, expected_sha)
            if candidate is not None:
                # 过渡期：唯一启用图被关闭后没有默认图，镜像旧行为发布空快照。
                self._publish_candidate(
                    candidate, default_tree_id=None, document_sha256="")
            self._audit("disable", {"id": tree_id})

    # ── 内部 ────────────────────────────────────────────────────────────
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

    @staticmethod
    def _require_document_sha(tree: RoutingTree | None, expected_sha: str) -> None:
        """写盘后核对：磁盘文档哈希必须等于本服务打算写出的字节哈希。"""
        if tree is None or tree.document_sha256 != expected_sha:
            raise GraphChangedDuringLoad(
                "graph changed between document write and snapshot publish")

    def _publish_candidate(self, candidate, *, default_tree_id: str | None,
                           document_sha256: str) -> None:
        """发布前复核默认图仍指向刚写入的文档，再用当前路由集指纹发布。

        写盘与发布之间可能插入其它副本的启停：复核默认图 id 与文档哈希，
        不匹配时抛与旧 refresh() 一致的冲突，绝不把自相矛盾的（快照, 指纹）
        组合发布出去。
        """
        for _attempt in range(2):
            fingerprint = self.trees.route_fingerprint()
            try:
                enabled = self.trees.enabled()
                current_default, _ = route_set_indices(enabled)
            except RouteSetInvariantError:
                continue
            tree = (
                next((item for item in enabled if item.id == current_default), None)
                if current_default is not None else None
            )
            current_sha = tree.document_sha256 if tree is not None else ""
            if current_default == default_tree_id and current_sha == document_sha256:
                self.publisher.publish(
                    dataclass_replace(candidate, fingerprint=fingerprint))
                return
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")

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
