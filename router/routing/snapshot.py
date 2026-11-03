from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from router.api.upstream import Backend, load_backends, model_reasoning_from_db
from router.jev.config import JevConfigRepo, JevSettings
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MAX_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
)
from router.routing.conditions import TimePeriod
from router.routing.graph.validate import EnableContext, validate_graph_for_enable
from router.routing.model import (
    RoutingTree,
    canonical_json,
    tree_behavior_dict,
    tree_revision_of,
)
from router.store.repositories import ModelRepo, SettingsRepo, TimePeriodRepo
from router.store.routing_trees import GraphChangedDuringLoad, GraphStorageError, RoutingTreeRepo


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

    @property
    def tree_id(self) -> str | None:
        return self.tree.id if self.tree is not None else None


def _capability(row: dict) -> str:
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
) -> RoutingSnapshot:
    revision = tree_revision_of(tree) if tree is not None else ""
    if tree is not None and tree.tree_revision != revision:
        tree = replace(tree, tree_revision=revision)
    backend_map = MappingProxyType(dict(backends))
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


def build_enable_context(conn) -> EnableContext:
    models = ModelRepo(conn).list()
    return EnableContext(
        known_models={row["id"] for row in models},
        model_reasoning=model_reasoning_from_db(conn),
        known_periods={row["name"] for row in TimePeriodRepo(conn).load()},
        jev_available=JevConfigRepo(conn).load() is not None,
    )


def build_snapshot_for_tree(conn, tree: RoutingTree | None) -> RoutingSnapshot:
    if tree is not None:
        try:
            issues = validate_graph_for_enable(tree, build_enable_context(conn))
        except RecursionError as exc:
            raise GraphStorageError("active graph exceeds safe validation depth") from exc
        if issues:
            codes = ",".join(sorted({issue.code for issue in issues}))
            raise GraphStorageError(f"invalid active graph: {codes}")
    input_tokens, context_tokens = read_jev_window(conn)
    return build_snapshot(
        tree=tree,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(row) for row in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: _capability(row) for row in ModelRepo(conn).list()},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
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
            if self._fingerprint is not None and self.trees.fingerprint() == self._fingerprint:
                # 文件恢复成最后成功内容时，指纹不变，也必须清除旧读取错误。
                self._last_error = ""
                return self.app.state.snapshot
            return self.refresh(conn)

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
