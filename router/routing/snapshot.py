from __future__ import annotations

import copy
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from router.control.validation import build_enable_context
from router.jev.config import JevConfigRepo, JevSettings
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MAX_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
)
from router.routing.conditions import TimePeriod
from router.routing.graph.compiled import CompiledGraph, compile_graph
from router.routing.graph.model import canonical_json
from router.routing.graph.validate import validate_graph_for_enable
from router.routing.tree import RoutingTree, tree_behavior_dict, tree_revision_of
from router.store.repositories import ModelRepo, SettingsRepo, TimePeriodRepo
from router.store.routing_trees import GraphChangedDuringLoad, GraphStorageError, RoutingTreeRepo
from router.upstream.catalog import Backend, load_backends


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


def model_capability(row: dict) -> str:
    """models 行 → capability 文本（profile.capability，缺省空串）。

    公开入口：启动快照、草稿试算快照共用同一读取规则（原私有名 `_capability`）。
    """
    profile = row.get("profile")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            return ""
    if not isinstance(profile, dict):
        return ""
    value = profile.get("capability")
    return value.strip() if isinstance(value, str) else ""


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


def _int_setting(repo: SettingsRepo, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = repo.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def read_jev_window(conn) -> tuple[int, int]:
    """返回 (input_tokens, context_tokens)；缺省/非法值回落到默认值。"""
    repo = SettingsRepo(conn)
    context_tokens = _int_setting(repo, "jev_context_tokens", DEFAULT_CONTEXT_TOKENS, MIN_CONTEXT_TOKENS, MAX_TOKENS)
    input_tokens = _int_setting(repo, "jev_input_tokens", DEFAULT_INPUT_TOKENS, MIN_INPUT_TOKENS, context_tokens)
    if input_tokens > context_tokens:
        input_tokens = context_tokens
    return input_tokens, context_tokens


def build_snapshot_for_tree(conn, tree: RoutingTree | None) -> RoutingSnapshot:
    compiled = None
    if tree is not None:
        issues = validate_graph_for_enable(tree, build_enable_context(conn))
        if issues:
            codes = ",".join(sorted({issue.code for issue in issues}))
            raise GraphStorageError(f"invalid active graph: {codes}")
        # 只有通过启用校验的图才编译静态拓扑；重复 id / 未知端口已在上述校验中拒绝。
        compiled = compile_graph(tree.root)
    input_tokens, context_tokens = read_jev_window(conn)
    return build_snapshot(
        tree=tree,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(row) for row in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: model_capability(row) for row in ModelRepo(conn).list()},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
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
