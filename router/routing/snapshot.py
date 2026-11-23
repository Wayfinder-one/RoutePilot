from __future__ import annotations

import copy
import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from router.control.runtime import (
    enable_context_for_runtime,
    snapshot_parts,
)
from router.jev.config import JevSettings
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
)
from router.routing.conditions import TimePeriod
from router.routing.graph.compiled import CompiledGraph, compile_graph
from router.routing.graph.model import canonical_json
from router.routing.graph.validate import validate_graph_for_enable
from router.routing.tree import RoutingTree, tree_behavior_dict, tree_revision_of
from router.routing.types import RoutingMiss
from router.store.routing_trees import GraphChangedDuringLoad, GraphStorageError, RoutingTreeRepo
from router.upstream.catalog import Backend


@dataclass(frozen=True)
class RoutingSnapshot:
    tree: RoutingTree | None
    backends: Mapping[str, Backend]
    time_periods: tuple[TimePeriod, ...]
    jev: JevSettings | None
    capabilities: Mapping[str, str]
    tree_revision: str
    snapshot_revision: str
    content: str
    jev_input_tokens: int = DEFAULT_INPUT_TOKENS
    jev_context_tokens: int = DEFAULT_CONTEXT_TOKENS
    # 通过启用校验后才编译的不可变拓扑；未校验的草稿/测试快照为 None。
    compiled: CompiledGraph | None = None

    @property
    def tree_id(self) -> str | None:
        return self.tree.id if self.tree is not None else None

    def require_compiled(self) -> CompiledGraph:
        """Return the immutable topology compiled for this snapshot.

        Snapshots published for an enabled graph always carry one.  A caller
        that built a snapshot by hand must pass ``compile_graph(root)``; this
        error makes the missing step explicit instead of silently rebuilding
        per-request indexes.
        """
        if self.compiled is None:
            raise RoutingMiss(
                "snapshot has no compiled graph; build it with compile_graph(root)"
            )
        return self.compiled


def _snapshot_backend(backend: Backend) -> Backend:
    """Detach a backend's mutable leaves before publishing it.

    ``Backend`` is frozen, but ``headers`` / ``reasoning`` are dicts. Copying
    them here means the MappingProxy only wraps snapshot-owned values, never
    mutable state still reachable from a live catalog result.
    """
    return replace(backend, headers=dict(backend.headers),
                   reasoning=copy.deepcopy(backend.reasoning))


def _backend_document(backends: Mapping[str, Backend]) -> list[dict]:
    return [
        {
            "id": model_id,
            "url": backend.url,
            "upstream": backend.upstream_name,
            "provider": backend.provider,
            "reasoning": backend.reasoning,
        }
        for model_id, backend in sorted(backends.items())
    ]


def build_snapshot(
    *,
    tree: RoutingTree | None,
    backends: Mapping[str, Backend],
    time_periods: list[TimePeriod],
    jev: JevSettings | None,
    capabilities: Mapping[str, str],
    jev_input_tokens: int = DEFAULT_INPUT_TOKENS,
    jev_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    compiled: CompiledGraph | None = None,
) -> RoutingSnapshot:
    revision = tree_revision_of(tree) if tree is not None else ""
    if tree is not None and tree.tree_revision != revision:
        tree = replace(tree, tree_revision=revision)
    backend_map = MappingProxyType({
        model_id: _snapshot_backend(backend)
        for model_id, backend in backends.items()})
    period_tuple = tuple(time_periods)
    capability_map = MappingProxyType(dict(capabilities))
    document = {
        "tree_id": tree.id if tree is not None else None,
        "tree": tree_behavior_dict(tree) if tree is not None else None,
        "tree_revision": revision,
        "backends": _backend_document(backend_map),
        "time_periods": [period.to_dict() for period in period_tuple],
        "jev": jev.behavior_dict() if jev is not None else None,
        "jev_window": {
            "context_tokens": jev_context_tokens,
            "input_tokens": jev_input_tokens,
        },
        "capabilities": dict(sorted(capability_map.items())),
    }
    content = canonical_json(document)
    snapshot_revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return RoutingSnapshot(
        tree=tree,
        backends=backend_map,
        time_periods=period_tuple,
        jev=jev,
        capabilities=capability_map,
        tree_revision=revision,
        snapshot_revision=snapshot_revision,
        content=content,
        jev_input_tokens=jev_input_tokens,
        jev_context_tokens=jev_context_tokens,
        compiled=compiled,
    )


def build_snapshot_for_tree(conn, tree: RoutingTree | None) -> RoutingSnapshot:
    """活动画布快照：唯一来源是画布内嵌运行配置。

    - ``tree is None``：空 Auto 快照（无活动画布时没有隐式数据库装配）。
    - ``tree is not None``：用画布自身运行配置做启用校验 → 编译 → 装配快照。
      数据库只用于解析已存密钥；目录行与画布不一致不改变快照内容。
    """
    if tree is None:
        return build_snapshot(
            tree=None,
            backends={},
            time_periods=[],
            jev=None,
            capabilities={},
        )
    issues = validate_graph_for_enable(tree, enable_context_for_runtime(tree.runtime))
    if issues:
        codes = ",".join(sorted({issue.code for issue in issues}))
        raise GraphStorageError(f"invalid active graph: {codes}")
    # 只有通过启用校验的图才编译静态拓扑；重复 id / 未知端口已在上述校验中拒绝。
    compiled = compile_graph(tree.root)
    return build_snapshot(
        tree=tree,
        **snapshot_parts(conn, tree.runtime),
        compiled=compiled,
    )


def load_snapshot(conn, trees: RoutingTreeRepo) -> RoutingSnapshot:
    return build_snapshot_for_tree(conn, trees.active())


@dataclass(frozen=True)
class PreparedSnapshot:
    snapshot: RoutingSnapshot
    fingerprint: tuple[str | None, str]


class SnapshotPublisher:
    def __init__(self, app, trees: RoutingTreeRepo):
        self.app = app
        self.trees = trees
        self._lock = threading.RLock()
        self._fingerprint: tuple[str | None, str] | None = None
        self._loaded_at = ""
        self._last_error = ""

    @contextmanager
    def locked(self):
        with self._lock:
            yield self

    def prepare(self, conn, tree: RoutingTree | None,
                fingerprint: tuple[str | None, str]) -> PreparedSnapshot:
        return PreparedSnapshot(build_snapshot_for_tree(conn, tree), fingerprint)

    def publish(self, prepared: PreparedSnapshot) -> RoutingSnapshot:
        with self._lock:
            self.app.state.snapshot = prepared.snapshot
            self._fingerprint = prepared.fingerprint
            self._loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_error = ""
            return prepared.snapshot

    def refresh(self, conn) -> RoutingSnapshot:
        with self._lock:
            try:
                fingerprint = self.trees.fingerprint()
                prepared = PreparedSnapshot(load_snapshot(conn, self.trees), fingerprint)
                if self.trees.fingerprint() != fingerprint:
                    raise GraphChangedDuringLoad("graph changed while building snapshot")
                return self.publish(prepared)
            except Exception as exc:
                self._last_error = str(exc)
                raise

    def refresh_if_changed(self, conn) -> RoutingSnapshot | None:
        with self._lock:
            try:
                if self._fingerprint is not None and self.trees.fingerprint() == self._fingerprint:
                    # 文件恢复成最后成功内容时，指纹不变，也必须清除旧读取错误。
                    self._last_error = ""
                    return self.app.state.snapshot
                return self.refresh(conn)
            except Exception as exc:
                self._last_error = str(exc)
                raise

    def status(self) -> dict:
        fingerprint = self._fingerprint or (None, "")
        return {
            "ready": self.app.state.snapshot is not None,
            "stale": bool(self._last_error),
            "active_id": fingerprint[0],
            "document_sha256": fingerprint[1],
            "loaded_at": self._loaded_at,
            "last_load_error": self._last_error,
        }
