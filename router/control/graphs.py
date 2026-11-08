"""决策组（graph）写用例：CRUD/启停的校验、候选快照与发布次序（brief Step 3）。

- `create` / `copy`：结构校验 →（锁内）文档写入 → 审计；不发布快照（新图为关闭态）。
- `update`：锁内按 root 优先合并已存 envelope → 启用与否分档校验 → 已启用图
  prepare 候选快照 → `base_document_sha256` 冲突检查后文件成功写入 →
  指纹复核（Task 1 review carry-forward：等价于旧 refresh() 的提交前复核，
  不匹配抛 `GraphChangedDuringLoad`）→ publish → 审计。
- `enable` / `disable`：启用校验 → prepare（候选，内存只读）→ 文件成功写入
  （current 指针）→ 指纹复核 → publish → 审计。

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
from router.routing.graph.validate import Issue, validate_graph_structure
from router.routing.graph.model import graph_to_dict
from router.routing.tree import RoutingTree, tree_from_dict
from router.store.repositories import ChangeRepo, now_iso
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphChangedDuringLoad,
    GraphConflictError,
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
    """

    name: str | None | _Unset = UNSET
    root: dict | None | _Unset = UNSET
    sticky: dict | None | _Unset = UNSET
    timezone: str | None | _Unset = UNSET


class GraphService:
    def __init__(self, conn: sqlite3.Connection, trees: RoutingTreeRepo, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, tree: RoutingTree) -> RoutingTree:
        issues = validate_graph_structure(tree)
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
            if current.enabled:
                issues = tree_issues(self.conn, tree)
            else:
                issues = validate_graph_structure(tree)
            if issues:
                raise ValidationFailed(issues)

            candidate = None
            if current.enabled:
                fingerprint = (
                    tree.id, hashlib.sha256(graph_document_bytes(tree)).hexdigest())
                candidate = self.publisher.prepare(self.conn, tree, fingerprint)
            updated = self.trees.update(tree, base_document_sha256=base_document_sha256)
            if candidate is not None:
                self._require_fingerprint(candidate.fingerprint)
                self.publisher.publish(candidate)
            self._audit("update", {"id": tree_id, "tree_revision": updated.tree_revision})
            return updated

    def copy(self, tree_id: str, new_id: str, new_name: str) -> RoutingTree:
        with self.publisher.locked():
            try:
                copied = self.trees.copy(tree_id, new_id, new_name)
            except KeyError:
                raise MissingError(f"unknown tree {tree_id}") from None
            except GraphAlreadyExistsError:
                raise AlreadyExists(f"tree {new_id} already exists") from None
            self._audit("copy", {"source": tree_id, "id": new_id})
        return copied

    # ── 启停 ────────────────────────────────────────────────────────────
    def enable(self, tree_id: str) -> None:
        with self.publisher.locked():
            tree = self.trees.get(tree_id)
            if tree is None:
                raise MissingError(f"unknown tree {tree_id}")
            issues = tree_issues(self.conn, tree)
            if issues:
                raise ValidationFailed(issues)
            enabled_tree = dataclass_replace(tree, enabled=True)
            candidate = self.publisher.prepare(
                self.conn, enabled_tree, (tree.id, tree.document_sha256))
            try:
                self.trees.enable(tree_id)
            except KeyError:
                raise MissingError(f"unknown tree {tree_id}") from None
            self._require_fingerprint(candidate.fingerprint)
            self.publisher.publish(candidate)
            self._audit("enable", {"id": tree_id})

    def disable(self, tree_id: str) -> None:
        with self.publisher.locked():
            candidate = (
                self.publisher.prepare(self.conn, None, (None, ""))
                if self.trees.active_id() == tree_id else None
            )
            if not self.trees.disable(tree_id):
                raise MissingError(f"unknown tree {tree_id}")
            if candidate is not None:
                self._require_fingerprint(candidate.fingerprint)
                self.publisher.publish(candidate)
            self._audit("disable", {"id": tree_id})

    # ── 内部 ────────────────────────────────────────────────────────────
    @staticmethod
    def _envelope(current: RoutingTree, patch: GraphPatch) -> RoutingTree:
        """enabled 是外部状态：持久化 envelope 与 current 一致（启停走专用端点）。

        patch 缺失（``UNSET``）的字段继承 current；显式 null 原样交给
        ``tree_from_dict``，恢复 base 对 root/timezone/sticky 的 null 语义。
        """
        try:
            return tree_from_dict({
                "id": current.id,
                "name": current.name if patch.name is UNSET else patch.name,
                "enabled": current.enabled,
                "root": graph_to_dict(current.root) if patch.root is UNSET else patch.root,
                "sticky": ({"enabled": current.sticky.enabled,
                            "ttl_seconds": current.sticky.ttl_seconds}
                           if patch.sticky is UNSET else patch.sticky),
                "timezone": current.timezone if patch.timezone is UNSET else patch.timezone,
            })
        except ValueError as exc:
            raise ValidationFailed(graph_parse_failed(exc)) from exc

    def _require_fingerprint(self, fingerprint: tuple[str | None, str]) -> None:
        """发布前复核：内存候选指纹必须与磁盘一致（等价旧 refresh() 的复核语义）。"""
        if self.trees.fingerprint() != fingerprint:
            raise GraphChangedDuringLoad(
                "graph changed between document write and snapshot publish")

    def _audit(self, action: str, detail: dict) -> None:
        try:
            with self.conn:
                ChangeRepo(self.conn).record(now_iso(), "tree", {"action": action, **detail})
        except sqlite3.Error:
            logger.exception("graph change committed but audit write failed: %s", action)
