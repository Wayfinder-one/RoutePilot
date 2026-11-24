from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from router.routing.document import graph_document_bytes, parse_graph_document
from router.routing.graph.model import canonical_json
from router.routing.tree import RoutingTree

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GraphStorageError(RuntimeError):
    pass


class GraphReadOnlyError(GraphStorageError):
    pass


class GraphAlreadyExistsError(GraphStorageError):
    pass


class GraphChangedDuringLoad(GraphStorageError):
    pass


class GraphConflictError(GraphStorageError):
    def __init__(self, actual_document_sha256: str | None):
        super().__init__("graph_conflict")
        self.actual_document_sha256 = actual_document_sha256


@dataclass(frozen=True)
class _StagedGraph:
    """一份待替换的图：原字节、目标字节与同目录暂存文件。"""

    path: Path
    original_bytes: bytes
    target_bytes: bytes
    staged_path: Path


class GraphBatch:
    """多图写入批次：预检/暂存全部完成后才逐份替换，失败可整体回滚。

    提交只替换文件，不在内部自行回滚：调用方负责在数据库事务失败时调用
    ``rollback()``，并在回滚失败时刷新快照（见 ConfigurationService）。
    """

    def __init__(self, repo: "RoutingTreeRepo", entries: list[_StagedGraph]):
        self.repo = repo
        self.entries = entries
        self.committed_paths: set[Path] = set()
        self._active = True

    def commit(self) -> None:
        if not self._active:
            raise GraphStorageError("graph batch is closed")
        try:
            for entry in self.entries:
                self.repo._replace_staged(entry.staged_path, entry.path)
                self.committed_paths.add(entry.path)
        except OSError as exc:
            raise GraphStorageError("graph batch write failed") from exc

    def rollback(self) -> None:
        """恢复本批次已替换的文件；只覆盖仍等于本批次目标字节的文件。"""
        if not self._active:
            return
        failures: list[tuple[Path, BaseException]] = []
        for entry in reversed(self.entries):
            if entry.path not in self.committed_paths:
                continue
            try:
                current = entry.path.read_bytes()
                if current != entry.target_bytes:
                    raise GraphStorageError(
                        f"graph changed during rollback: {entry.path.stem}")
                self.repo._atomic_write(entry.path, entry.original_bytes)
                self.committed_paths.remove(entry.path)
            except (OSError, GraphStorageError) as exc:
                failures.append((entry.path, exc))
        self.discard()
        if failures:
            raise GraphStorageError(
                "graph rollback failed; snapshot refresh required") from failures[0][1]

    def discard(self) -> None:
        if not self._active:
            return
        self._active = False
        for entry in self.entries:
            try:
                entry.staged_path.unlink()
            except FileNotFoundError:
                pass


class RoutingTreeRepo:
    """图目录仓储：``graphs/*.json`` 是唯一事实源，生命周期只来自文件内容。

    v4 文档自带 ``enabled`` / ``default``；仓储不再维护 ``current`` 指针，
    也不在读写路径上归一生命周期。新建/复制图的「默认关闭」由
    ``GraphService`` 负责，启停由 ``GraphService`` 通过批次写入文件字段实现。
    """

    def __init__(self, root: str | Path, *, writer: bool = True):
        self.root = Path(root)
        self.graphs = self.root / "graphs"
        self.writer = writer

    def initialize(self) -> None:
        self._require_writer()
        self.graphs.mkdir(parents=True, exist_ok=True)

    def get(self, tree_id: str) -> RoutingTree | None:
        self._validate_id(tree_id)
        path = self.graphs / f"{tree_id}.json"
        if not path.exists():
            return None
        return self._read_tree(path, tree_id)

    def list(self) -> list[RoutingTree]:
        trees = []
        for path in self.graphs.glob("*.json"):
            tree_id = path.stem
            self._validate_id(tree_id)
            trees.append(self._read_tree(path, tree_id))
        return sorted(trees, key=lambda tree: (tree.updated_at, tree.id))

    def enabled(self) -> list[RoutingTree]:
        """所有 ``enabled=True`` 的图（排序同 ``list()``）。"""
        return [tree for tree in self.list() if tree.enabled]

    def route_fingerprint(self) -> str:
        """路由集指纹：只含启用图的 id/default/文档哈希，草稿修改不改变它。

        启停、默认切换与启用图文件修改都会改变指纹，因此发布器用它判断
        是否需要重载运行集。
        """
        rows = [
            {'id': tree.id, 'default': tree.default, 'sha256': tree.document_sha256}
            for tree in self.enabled()
        ]
        rows.sort(key=lambda row: row['id'])
        return hashlib.sha256(canonical_json(rows).encode('utf-8')).hexdigest()

    def create(self, tree: RoutingTree) -> RoutingTree:
        """按给定 v4 文档原样落盘；生命周期强制由 ``GraphService`` 负责。"""
        self._require_writer()
        self._validate_id(tree.id)
        path = self.graphs / f"{tree.id}.json"
        if path.exists():
            raise GraphAlreadyExistsError(tree.id)
        self._atomic_write(path, graph_document_bytes(tree))
        return self._read_tree(path, tree.id)

    def update(self, tree: RoutingTree, *, base_document_sha256: str) -> RoutingTree:
        self._require_writer()
        self._validate_id(tree.id)
        path = self.graphs / f"{tree.id}.json"
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        actual = hashlib.sha256(current).hexdigest() if current is not None else None
        if not base_document_sha256 or actual != base_document_sha256:
            raise GraphConflictError(actual)
        self._atomic_write(path, graph_document_bytes(tree))
        return self._read_tree(path, tree.id)

    def stage_updates(self, updates) -> GraphBatch:
        """预检所有基线哈希并暂存目标文档，返回可提交/回滚的批次。

        顺序固定为非默认图在前、默认图最后：中途失败或崩溃时，当前生效的
        默认图仍是旧文档，不会出现半份新配置对外生效。
        """
        self._require_writer()
        entries: list[_StagedGraph] = []
        batch = GraphBatch(self, entries)
        try:
            ordered = sorted(updates, key=lambda item: item[0].default)
            for tree, expected_sha256 in ordered:
                self._validate_id(tree.id)
                path = self.graphs / f"{tree.id}.json"
                try:
                    original = path.read_bytes()
                except FileNotFoundError:
                    original = None
                actual = hashlib.sha256(original).hexdigest() if original is not None else None
                if not expected_sha256 or actual != expected_sha256:
                    raise GraphConflictError(actual)
                target = graph_document_bytes(tree)
                staged_path = self._stage_file(path, target)
                entries.append(_StagedGraph(path, original, target, staged_path))
            return batch
        except BaseException:
            batch.discard()
            raise

    def copy(self, source_id: str, new_id: str, new_name: str) -> RoutingTree:
        source = self.get(source_id)
        if source is None:
            raise KeyError(source_id)
        return self.create(replace(source, id=new_id, name=new_name, enabled=False, default=False))

    def _read_tree(self, path: Path, tree_id: str) -> RoutingTree:
        try:
            content = path.read_bytes()
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            return parse_graph_document(
                content,
                tree_id=tree_id,
                updated_at=modified,
                document_sha256=hashlib.sha256(content).hexdigest(),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise GraphStorageError(f"invalid graph document: {tree_id}") from exc

    def _require_writer(self) -> None:
        if not self.writer:
            raise GraphReadOnlyError("graph_read_only")

    @staticmethod
    def _validate_id(tree_id: str) -> None:
        if not _ID_RE.fullmatch(tree_id):
            raise GraphStorageError(f"invalid tree id: {tree_id!r}")

    @staticmethod
    def _stage_file(path: Path, content: bytes) -> Path:
        """同目录暂存完整内容并 fsync；失败时清理临时文件。"""
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return Path(temporary)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _replace_staged(staged_path: Path, path: Path) -> None:
        os.replace(staged_path, path)

    @classmethod
    def _atomic_write(cls, path: Path, content: bytes) -> None:
        # 类级调用：批次故障注入（实例属性）不得影响回滚路径。
        staged = cls._stage_file(path, content)
        try:
            cls._replace_staged(staged, path)
        except BaseException:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            raise
