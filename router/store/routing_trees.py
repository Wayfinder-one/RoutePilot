from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from router.routing.document import graph_document_bytes, parse_graph_document
from router.routing.model import RoutingTree

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


class RoutingTreeRepo:
    def __init__(self, root: str | Path, *, writer: bool = True):
        self.root = Path(root)
        self.graphs = self.root / "graphs"
        self.current = self.root / "current"
        self.writer = writer

    def initialize(self) -> None:
        self._require_writer()
        self.graphs.mkdir(parents=True, exist_ok=True)
        if not self.current.exists():
            self._atomic_write(self.current, b"")

    def active_id(self) -> str | None:
        try:
            raw = self.current.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise GraphStorageError("current pointer is unavailable") from exc
        value = raw[:-1] if raw.endswith("\n") else raw
        if not value:
            return None
        if not _ID_RE.fullmatch(value):
            raise GraphStorageError("current pointer contains an invalid tree id")
        return value

    def fingerprint(self) -> tuple[str | None, str]:
        active_id = self.active_id()
        if active_id is None:
            return None, ""
        try:
            content = (self.graphs / f"{active_id}.json").read_bytes()
        except OSError as exc:
            raise GraphStorageError(f"active graph is unavailable: {active_id}") from exc
        return active_id, hashlib.sha256(content).hexdigest()

    def active(self) -> RoutingTree | None:
        active_id = self.active_id()
        if active_id is None:
            return None
        tree = self.get(active_id)
        if tree is None:
            raise GraphStorageError(f"active graph is missing: {active_id}")
        return tree

    def get(self, tree_id: str) -> RoutingTree | None:
        self._validate_id(tree_id)
        path = self.graphs / f"{tree_id}.json"
        if not path.exists():
            return None
        return self._read_tree(path, tree_id, self.active_id())

    def list(self) -> list[RoutingTree]:
        active_id = self.active_id()
        trees = []
        for path in self.graphs.glob("*.json"):
            tree_id = path.stem
            self._validate_id(tree_id)
            trees.append(self._read_tree(path, tree_id, active_id))
        return sorted(trees, key=lambda tree: (tree.updated_at, tree.id))

    def create(self, tree: RoutingTree) -> RoutingTree:
        self._require_writer()
        self._validate_id(tree.id)
        path = self.graphs / f"{tree.id}.json"
        if path.exists():
            raise GraphAlreadyExistsError(tree.id)
        self._atomic_write(path, graph_document_bytes(replace(tree, enabled=False)))
        return self._read_tree(path, tree.id, self.active_id())

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
        active_id = self.active_id()
        self._atomic_write(path, graph_document_bytes(replace(tree, enabled=active_id == tree.id)))
        return self._read_tree(path, tree.id, active_id)

    def copy(self, source_id: str, new_id: str, new_name: str) -> RoutingTree:
        source = self.get(source_id)
        if source is None:
            raise KeyError(source_id)
        return self.create(replace(source, id=new_id, name=new_name, enabled=False))

    def enable(self, tree_id: str) -> RoutingTree:
        self._require_writer()
        tree = self.get(tree_id)
        if tree is None:
            raise KeyError(tree_id)
        self._atomic_write(self.current, (tree_id + "\n").encode("utf-8"))
        return self._read_tree(self.graphs / f"{tree_id}.json", tree_id, tree_id)

    def disable(self, tree_id: str) -> bool:
        self._require_writer()
        if self.get(tree_id) is None:
            return False
        if self.active_id() == tree_id:
            self._atomic_write(self.current, b"")
        return True

    def _read_tree(self, path: Path, tree_id: str, active_id: str | None) -> RoutingTree:
        try:
            content = path.read_bytes()
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            return parse_graph_document(
                content,
                tree_id=tree_id,
                enabled=active_id == tree_id,
                updated_at=modified,
                document_sha256=hashlib.sha256(content).hexdigest(),
            )
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise GraphStorageError(f"invalid graph document: {tree_id}") from exc

    def _require_writer(self) -> None:
        if not self.writer:
            raise GraphReadOnlyError("graph_read_only")

    @staticmethod
    def _validate_id(tree_id: str) -> None:
        if not _ID_RE.fullmatch(tree_id):
            raise GraphStorageError(f"invalid tree id: {tree_id!r}")

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
