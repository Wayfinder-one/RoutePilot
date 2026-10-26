"""Immutable routing snapshot (JEV P1-B, docs/JEV-P1修复计划 §2 B1).

A request is judged on exactly one snapshot: the stations tuple, the backend
map, the config revisions that produced them and the trimming switch. Admin
writes build a **new** snapshot and publish it with one assignment
(``install_snapshot``), so a request that starts under the old configuration
keeps evaluating — and ledgering — the old configuration even if a
``PUT /admin/pipeline`` (or a questions edit, via the tagger's per-tag
fingerprint) lands while its tagger is still running. That removes the
historical mismatch where ``routes_v1`` read the tree before tagging but the
ledger versions after tagging.

Revisions are content-addressed: ``pipeline_revision`` /
``backends_revision`` are ``sha256(canonical JSON)``. ``pipeline_content()``
is the exact canonical text stored in ``config_revisions`` (migration v15), so
a ledger row can be replayed by looking the revision up and recomputing the
sha. Backend content is deliberately reduced to routing-relevant fields — the
Authorization header is never hashed (and never stored anywhere).

Module import discipline: this is a pipeline-layer module, so it imports the
api/store layers only lazily inside :func:`load_snapshot`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from router.decision.pack import (canonical_pack_json, empty_pack, freeze_pack,
                                  pack_revision)
from router.pipeline.model import Station, station_to_dict

if TYPE_CHECKING:                                # pragma: no cover - typing only
    import sqlite3

    from fastapi import FastAPI

    from router.api.upstream import Backend


#: revision kinds recorded in config_revisions (v15)
REVISION_PIPELINE = "pipeline"
REVISION_QUESTIONS = "questions"
#: capability pack: model catalog descriptions + decision policy (2026-10-26)
REVISION_CAPABILITY = "capability"


def canonical_json(value) -> str:
    """The one canonical serialization used for every revision sha.

    Matches the JEV ``questions_sha256`` contract exactly, so the tagger's
    per-request fingerprint can be looked up in ``config_revisions``.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pipeline_document(stations) -> dict:
    """Canonical pipeline document (what GET /admin/pipeline calls `stations`)."""
    return {"stations": [station_to_dict(s) for s in stations]}


def pipeline_content(stations) -> str:
    return canonical_json(pipeline_document(stations))


def pipeline_revision_of(stations) -> str:
    return sha256_text(pipeline_content(stations))


def backends_document(backends: Mapping[str, "Backend"]) -> list[dict]:
    """Routing-relevant backend config; auth material is never hashed/stored."""
    return [{"id": model_id,
             "url": backend.url,
             "upstream": backend.upstream_name,
             "provider": backend.provider,
             "reasoning": backend.reasoning}
            for model_id, backend in sorted(backends.items())]


def backends_revision_of(backends: Mapping[str, "Backend"]) -> str:
    return sha256_text(canonical_json(backends_document(backends)))


@dataclass(frozen=True)
class RoutingSnapshot:
    """Everything one request may read from the configuration, frozen."""

    stations: tuple[Station, ...]
    pipeline_revision: str
    pipeline_version: int
    backends: Mapping[str, "Backend"]          # MappingProxyType: read-only
    backends_revision: str
    jev_trim: bool
    #: Capability-routing pack (model id -> natural-language description,
    #: decision instruction, gates, fallback). Empty when not configured.
    capability: dict = field(default_factory=dict)
    #: Content hash of the pack ('' when no candidate is configured); the
    #: ledger value a capability decision can be replayed against.
    capability_revision: str = ""


def capability_content(pack: dict) -> str:
    """Canonical text stored in ``config_revisions[CARABILITY]`` for replay."""
    return canonical_pack_json(freeze_pack(pack))


def build_snapshot(*, stations, backends, pipeline_version: int,
                   jev_trim: bool, capability: dict | None = None) -> RoutingSnapshot:
    """Pure constructor: canonical revisions are derived from the content."""
    station_tuple = tuple(stations)
    backend_map = MappingProxyType(dict(backends))
    pack = freeze_pack(capability or empty_pack())
    return RoutingSnapshot(
        stations=station_tuple,
        pipeline_revision=pipeline_revision_of(station_tuple),
        pipeline_version=int(pipeline_version or 0),
        backends=backend_map,
        backends_revision=backends_revision_of(backend_map),
        jev_trim=bool(jev_trim),
        capability=pack,
        capability_revision=pack_revision(pack))


def load_snapshot(conn: "sqlite3.Connection", *, jev_trim: bool) -> RoutingSnapshot:
    """Read the current pipeline/backends/version + capability pack from the DB.

    The capability pack is built from the model catalog (``profile.capability``)
    and the global policy settings; both are edited through /admin, which
    refreshes the snapshot, so the hot path never reads the DB.
    """
    from router.api.upstream import load_backends
    from router.decision.pack import build_pack
    from router.store.repositories import ModelRepo, SettingsRepo, StationRepo

    stations = tuple(StationRepo(conn).load())
    version = SettingsRepo(conn).get("pipeline_version", "0") or "0"
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        version_int = 0
    capability = build_pack(ModelRepo(conn).list(), SettingsRepo(conn).all())
    return build_snapshot(stations=stations, backends=load_backends(conn),
                          pipeline_version=version_int, jev_trim=jev_trim,
                          capability=capability)


def ensure_capability_revision(conn: "sqlite3.Connection", pack: dict) -> str:
    """Append the capability pack content to ``config_revisions`` (idempotent).

    Returns the content sha. Called on config writes/boot (not per request):
    a ledger row's ``capability_revision`` must be resolvable from the store.
    """
    from router.store.repositories import record_revision

    content = capability_content(pack)
    revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with conn:
        record_revision(conn, REVISION_CAPABILITY, content)
    return revision


def install_snapshot(app: "FastAPI", snapshot: RoutingSnapshot) -> None:
    """Publish a snapshot atomically.

    ONE assignment is the visibility point for the hot path; the legacy
    ``app.state.pipeline`` / ``app.state.backends`` aliases are then refreshed
    for the admin/console code paths that still read them directly. Every
    pipeline/model/provider write must go through this helper (or a full
    rebuild), never update an alias on its own.
    """
    app.state.snapshot = snapshot
    # read-only compatibility aliases (same Station/Backend objects)
    app.state.pipeline = list(snapshot.stations)
    app.state.backends = snapshot.backends
