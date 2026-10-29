from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping

from router.api.upstream import Backend, load_backends
from router.jev.config import JevConfigRepo, JevSettings
from router.routing.conditions import TimePeriod
from router.routing.model import (
    RoutingTree,
    canonical_json,
    tree_behavior_dict,
    tree_revision_of,
)
from router.store.repositories import ModelRepo, TimePeriodRepo, _tx, record_revision
from router.store.routing_trees import RoutingTreeRepo

REVISION_SNAPSHOT = "snapshot"


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
    )


def load_snapshot(conn) -> RoutingSnapshot:
    active = next((tree for tree in RoutingTreeRepo(conn).list() if tree.enabled), None)
    periods = [TimePeriod.from_dict(period) for period in TimePeriodRepo(conn).load()]
    capabilities = {row["id"]: _capability(row) for row in ModelRepo(conn).list()}
    snapshot = build_snapshot(
        tree=active,
        backends=load_backends(conn),
        time_periods=periods,
        jev=JevConfigRepo(conn).load(),
        capabilities=capabilities,
    )
    with _tx(conn):
        record_revision(conn, REVISION_SNAPSHOT, snapshot.content)
    return snapshot


class SnapshotPublisher:
    def __init__(self, app):
        self.app = app
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield self

    def refresh(self, conn) -> RoutingSnapshot:
        with self._lock:
            snapshot = load_snapshot(conn)
            self.app.state.snapshot = snapshot
            return snapshot
