from __future__ import annotations

import copy
import hashlib
import threading
from collections.abc import Iterable
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
from router.routing.route_set import RouteSetInvariantError, route_set_indices
from router.routing.tree import RoutingTree, tree_behavior_dict, tree_revision_of
from router.routing.types import NoActiveTreeError, NoDefaultTreeError, RoutingMiss
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
    """单图运行快照：唯一来源是画布内嵌运行配置。

    - ``tree is None``：空 Auto 快照（没有启用图时没有隐式数据库装配）。
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
        raise GraphStorageError(f"invalid enabled graph: {codes}")
    # 只有通过启用校验的图才编译静态拓扑；重复 id / 未知端口已在上述校验中拒绝。
    compiled = compile_graph(tree.root)
    return build_snapshot(
        tree=tree,
        **snapshot_parts(conn, tree.runtime),
        compiled=compiled,
    )


@dataclass(frozen=True)
class RouteSetSelection:
    """一次按部门选图的结果：``reason`` 为 ``department`` / ``default``。"""

    snapshot: RoutingSnapshot
    reason: str


@dataclass(frozen=True)
class RoutingSnapshotSet:
    """不可变发布单元：所有启用图的快照 + 默认图/部门索引。

    ``snapshots`` / ``department_tree_ids`` 用 ``MappingProxyType`` 固定；
    ``content`` 只含非密钥版本信息（默认图 id、排序后的部门映射、每图
    ``snapshot_revision``），``snapshot_revision`` 是它的 sha256。
    """

    snapshots: Mapping[str, RoutingSnapshot]
    default_tree_id: str | None
    department_tree_ids: Mapping[str, str]
    snapshot_revision: str
    content: str
    fingerprint: str

    @property
    def default_snapshot(self) -> RoutingSnapshot | None:
        """默认启用图的快照；零启用图返回 ``None``。"""
        return self.snapshots.get(self.default_tree_id) if self.default_tree_id else None

    def select(self, department: str = "") -> RouteSetSelection:
        """按部门选图：精确匹配部门图，否则回退默认图。

        零启用图抛 ``NoActiveTreeError``；有启用图但缺默认图（不变量损坏）
        抛 ``NoDefaultTreeError``。
        """
        if not self.snapshots:
            raise NoActiveTreeError('no routing tree is enabled')
        tree_id = self.department_tree_ids.get(department) if department else None
        if tree_id is not None:
            return RouteSetSelection(self.snapshots[tree_id], 'department')
        if self.default_tree_id is None:
            raise NoDefaultTreeError('no default routing tree is enabled')
        return RouteSetSelection(self.snapshots[self.default_tree_id], 'default')


def assemble_snapshot_set(
        snapshots: Iterable[RoutingSnapshot], fingerprint: str) -> RoutingSnapshotSet:
    """把各启用图快照装配成不可变路由集（集合不变量在装配点校验）。

    路由集成员必须都带树且都是**启用**图：空 Auto 快照与停用草稿都不属于
    路由集（``select()`` 只会选启用图，混入停用图会让它被当作可服务图）。
    ``RouteSetInvariantError`` 由调用方决定如何呈现；``load_snapshot_set()``
    在装配边界转成存储错误。
    """
    ordered = list(snapshots)
    if any(item.tree is None for item in ordered):
        raise ValueError('routing snapshot set requires snapshots with a tree')
    disabled = sorted(item.tree.id for item in ordered if not item.tree.enabled)
    if disabled:
        raise ValueError(
            'routing snapshot set requires enabled graphs only: '
            + ', '.join(disabled))
    ordered.sort(key=lambda item: item.tree.id)
    default_id, mapping = route_set_indices([item.tree for item in ordered])
    department_tree_ids = dict(sorted(mapping.items()))
    document = {
        'default_tree_id': default_id,
        'department_tree_ids': department_tree_ids,
        'snapshots': {item.tree_id: item.snapshot_revision for item in ordered},
    }
    content = canonical_json(document)
    revision = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return RoutingSnapshotSet(
        snapshots=MappingProxyType({item.tree_id: item for item in ordered}),
        default_tree_id=default_id,
        department_tree_ids=MappingProxyType(department_tree_ids),
        snapshot_revision=revision,
        content=content,
        fingerprint=fingerprint,
    )


def build_snapshot_set_for_trees(conn, trees: Iterable[RoutingTree],
                                 fingerprint: str) -> RoutingSnapshotSet:
    """对每份启用图执行启用校验/编译/装配，再装配成路由集。"""
    return assemble_snapshot_set(
        [build_snapshot_for_tree(conn, tree) for tree in trees], fingerprint)


def invalid_route_set_error(exc: RouteSetInvariantError) -> GraphStorageError:
    """装配边界的统一错误映射：路由集不变量损坏 → 存储错误（HTTP 503）。

    ``assemble_snapshot_set()`` 是纯函数，继续抛 ``RouteSetInvariantError``；
    凡是在加载/发布边界把磁盘或候选树交给它（或直接索引启用集合）的地方都经
    这里转换，避免损坏的 standalone JSON 把管理写路径打成 500。
    """
    return GraphStorageError(f"invalid route set: {exc.code}")


def load_snapshot_set(conn, repo: RoutingTreeRepo) -> RoutingSnapshotSet:
    """从磁盘扫描结果构造完整启用路由集。

    先取指纹再读树；集合不变量（恰好一张默认图、部门唯一）在装配边界转成
    ``GraphStorageError``：发布器保留旧路由集并报告 ``stale``，而不是把损坏
    文件打成 500。
    """
    fingerprint = repo.route_fingerprint()
    try:
        return build_snapshot_set_for_trees(conn, repo.enabled(), fingerprint)
    except RouteSetInvariantError as exc:
        raise invalid_route_set_error(exc) from exc


@dataclass(frozen=True)
class PreparedRouteSet:
    """待发布的路由集及其指纹（COMMIT 前复核用）。"""

    snapshot_set: RoutingSnapshotSet
    fingerprint: str


class SnapshotPublisher:
    def __init__(self, app, trees: RoutingTreeRepo):
        self.app = app
        self.trees = trees
        self._lock = threading.RLock()
        self._fingerprint: str | None = None
        self._loaded_at = ""
        self._last_error = ""

    @contextmanager
    def locked(self):
        with self._lock:
            yield self

    def prepare(self, conn, trees: Iterable[RoutingTree],
                fingerprint: str) -> PreparedRouteSet:
        """装配候选路由集；不变量损坏在装配边界转成 ``GraphStorageError``。

        候选树可能来自手装的 standalone JSON（如启用图但没有默认图），
        与 ``load_snapshot_set()`` 一样走存储错误（503），而不是把
        GraphService/ConfigurationService 的写路径打成 500。
        """
        try:
            snapshot_set = build_snapshot_set_for_trees(conn, trees, fingerprint)
        except RouteSetInvariantError as exc:
            raise invalid_route_set_error(exc) from exc
        return PreparedRouteSet(snapshot_set, fingerprint)

    def publish(self, prepared: PreparedRouteSet) -> RoutingSnapshotSet:
        with self._lock:
            snapshot_set = prepared.snapshot_set
            if snapshot_set.fingerprint != prepared.fingerprint:
                # GraphService 的候选路由集在写盘前只能带占位指纹；发布是唯一
                # 确定「发布时指纹」的点，必须把它盖到集合上，发布状态里的
                # 集合指纹才与 ``status()["route_fingerprint"]`` 一致。
                snapshot_set = replace(snapshot_set, fingerprint=prepared.fingerprint)
            self.app.state.snapshot_set = snapshot_set
            self._fingerprint = prepared.fingerprint
            self._loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_error = ""
            return snapshot_set

    def refresh(self, conn) -> RoutingSnapshotSet:
        with self._lock:
            try:
                route_set = load_snapshot_set(conn, self.trees)
                if self.trees.route_fingerprint() != route_set.fingerprint:
                    raise GraphChangedDuringLoad("graph changed while building snapshot")
                return self.publish(PreparedRouteSet(route_set, route_set.fingerprint))
            except Exception as exc:
                self._last_error = str(exc)
                raise

    def refresh_if_changed(self, conn) -> RoutingSnapshotSet | None:
        with self._lock:
            try:
                if self._fingerprint is not None and self.trees.route_fingerprint() == self._fingerprint:
                    # 文件恢复成最后成功内容时，指纹不变，也必须清除旧读取错误。
                    self._last_error = ""
                    return self.app.state.snapshot_set
                return self.refresh(conn)
            except Exception as exc:
                self._last_error = str(exc)
                raise

    def status(self) -> dict:
        route_set = self.app.state.snapshot_set
        return {
            "ready": route_set is not None,
            "stale": bool(self._last_error),
            "default_tree_id": route_set.default_tree_id if route_set is not None else None,
            "enabled_tree_ids": sorted(route_set.snapshots) if route_set is not None else [],
            "snapshot_revision": route_set.snapshot_revision if route_set is not None else "",
            "route_fingerprint": self._fingerprint or "",
            "loaded_at": self._loaded_at,
            "last_load_error": self._last_error,
        }
