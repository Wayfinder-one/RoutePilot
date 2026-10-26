"""Immutable effective routing snapshot and content-addressed revisions.

A request is judged on exactly one snapshot: the compiled tree, the backend map,
the signal questions, the DecisionRunner version evidence, the static signal
requirements and the trimming switch.  Admin writes build a **new** snapshot and
publish it with one assignment (:func:`install_snapshot`), so a request that
starts under the old configuration keeps evaluating — and ledgering — the old
configuration even if a tree switch/edit lands while its tagger is still running.

Four boundaries (design §8):

* **compile** — :func:`router.pipeline.compiler.compile_tree` expands model
  capabilities into candidate texts;
* **assembly** — :func:`load_snapshot` only reads the DB (active tree, model
  catalog, backends, questions revision); the hot path never does;
* **revision** — :func:`snapshot_content` is the canonical append-only effective
  document and ``snapshot_revision`` its sha256, so a ledger row can be replayed
  (decisions, not upstream calls) with :func:`snapshot_from_content`;
* **publish** — :func:`install_snapshot` is a single assignment.

Backend content is deliberately reduced to routing-relevant fields: the
Authorization header is never hashed, stored or logged.  ``config_revisions``
content is addressed by sha256, so :func:`ensure_snapshot_revision` is idempotent.

Module import discipline: this is a pipeline-layer module; api/store imports are
lazy (inside the DB-assembly functions) so pure tests and hot paths can import it
without touching sqlite/app code.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from router.pipeline.compiler import (
    compile_tree,
    effective_node_from_dict,
    effective_node_to_dict,
    tree_signal_requirements,
)
from router.pipeline.scope import SignalScope, full_scope
from router.pipeline.tree import Node, RoutingTree, tree_revision_of

if TYPE_CHECKING:                                # pragma: no cover - typing only
    import sqlite3

    from fastapi import FastAPI

    from router.api.upstream import Backend


#: ``config_revisions.kind`` of one effective snapshot document (design §8.2).
REVISION_SNAPSHOT = "snapshot"

#: Legacy Station-era revision kinds: ``router.store.repositories`` still imports
#: them for the old pipeline document until the StationRepo removal task retires
#: that bridge (Task 11).  Do not extend these.
REVISION_PIPELINE = "pipeline"
REVISION_QUESTIONS = "questions"


def canonical_json(value) -> str:
    """The one canonical serialization used for every revision sha.

    Sorted keys, UTF-8 (``ensure_ascii=False``), compact separators; arrays keep
    their order (candidate order is behaviour).  Matches the JEV
    ``questions_sha256`` contract exactly.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Frozen config data
# --------------------------------------------------------------------------
def freeze_data(value):
    """Recursively freeze configuration data for snapshot storage.

    Mappings become read-only ``MappingProxyType`` objects, lists become tuples
    (so in-place edits raise); scalars pass through unchanged.
    """
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_data(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_data(item) for item in value)
    return value


def thaw_data(value):
    """Recursively copy frozen configuration into mutable plain data.

    Used right before handing a snapshot's questions to the (synchronous, JEV)
    tagger: every request gets its own deep copy, never a ``MappingProxyType``
    masquerading as a dict.
    """
    if isinstance(value, Mapping):
        return {key: thaw_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_data(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Backend routing document (no secrets)
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Legacy Station document bridge (removed with the StationRepo task)
# --------------------------------------------------------------------------
def pipeline_document(stations) -> dict:
    """Canonical Station document (what GET /admin/pipeline calls `stations`)."""
    from router.pipeline.model import station_to_dict

    return {"stations": [station_to_dict(station) for station in stations]}


def pipeline_content(stations) -> str:
    return canonical_json(pipeline_document(stations))


def pipeline_revision_of(stations) -> str:
    return sha256_text(pipeline_content(stations))


# --------------------------------------------------------------------------
# The immutable snapshot
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RoutingSnapshot:
    """Everything one request may read from the configuration, frozen."""

    tree: RoutingTree | None
    scope: SignalScope
    required_signals: frozenset[str]
    questions: Mapping | None
    questions_revision: str
    decision_revision: str
    backends: Mapping[str, "Backend"]          # MappingProxyType: read-only
    backends_revision: str
    tree_revision: str
    snapshot_revision: str
    jev_trim: bool

    @property
    def tree_id(self) -> str | None:
        return self.tree.id if self.tree is not None else None


def _document(*, tree: RoutingTree | None, tree_revision: str,
              questions: Mapping | None, questions_revision: str,
              decision_revision: str, backends: Mapping[str, "Backend"],
              backends_revision: str, jev_trim: bool) -> dict:
    """The canonical append-only effective document (what a replay restores).

    It stores exactly what the snapshot stores (plus the backend routing
    document, from which the live ``Backend`` objects are rebuilt without
    secrets) so ``snapshot_content`` is idempotent and content-addressable.
    """
    return {"tree_id": tree.id if tree is not None else None,
            "tree_revision": tree_revision,
            "root": None if tree is None else effective_node_to_dict(tree.root),
            "questions": None if questions is None else thaw_data(questions),
            "questions_revision": questions_revision,
            "decision_revision": decision_revision,
            "backends": backends_document(backends),
            "backends_revision": backends_revision,
            "jev_trim": bool(jev_trim)}


def snapshot_content(snapshot: RoutingSnapshot) -> str:
    """Canonical text stored in ``config_revisions(kind='snapshot')``."""
    return canonical_json(_document(
        tree=snapshot.tree, tree_revision=snapshot.tree_revision,
        questions=snapshot.questions,
        questions_revision=snapshot.questions_revision,
        decision_revision=snapshot.decision_revision,
        backends=snapshot.backends,
        backends_revision=snapshot.backends_revision,
        jev_trim=snapshot.jev_trim))


def build_snapshot(*, tree: RoutingTree | None,
                   backends: Mapping[str, "Backend"],
                   questions: Mapping | None,
                   decision_info: Mapping | None,
                   jev_trim: bool) -> RoutingSnapshot:
    """Pure constructor: compile-time inputs in, frozen revisions out.

    ``tree`` must be the **effective** tree (``compile_tree`` applied first);
    the compile stage also produced the static signal requirements, which are
    derived here once per snapshot build — never per request.
    """
    backend_map = MappingProxyType(dict(backends))
    frozen_questions = None if questions is None else freeze_data(questions)
    info = dict(decision_info or {})

    if tree is not None:
        required_signals, scope = tree_signal_requirements(tree.root)
        tree_revision = tree_revision_of(tree.root)
    else:
        # No active tree: /v1 answers 503 before any signal is read.  A full
        # scope is the safe over-approximation should anything still ask.
        required_signals, scope = frozenset(), full_scope("no active tree")
        tree_revision = ""

    questions_revision = "" if frozen_questions is None else sha256_text(
        canonical_json(thaw_data(frozen_questions)))
    decision_revision = "" if not info else sha256_text(canonical_json(info))
    backends_revision = backends_revision_of(backend_map)
    document = _document(
        tree=tree, tree_revision=tree_revision, questions=frozen_questions,
        questions_revision=questions_revision,
        decision_revision=decision_revision, backends=backend_map,
        backends_revision=backends_revision, jev_trim=jev_trim)
    return RoutingSnapshot(
        tree=tree, scope=scope, required_signals=required_signals,
        questions=frozen_questions, questions_revision=questions_revision,
        decision_revision=decision_revision, backends=backend_map,
        backends_revision=backends_revision, tree_revision=tree_revision,
        snapshot_revision=sha256_text(canonical_json(document)),
        jev_trim=bool(jev_trim))


def _backends_from_document(rows) -> Mapping[str, "Backend"]:
    """Rebuild the live ``Backend`` map from the stored routing document.

    Auth headers are intentionally NOT restored: replay restores routing
    decisions, not upstream calls.  Routing-relevant fields are identical, so
    ``backends_revision_of`` recomputes the revision stored in the document.
    """
    from router.api.upstream import Backend

    mapping: dict[str, Backend] = {}
    for row in rows or ():
        mapping[row["id"]] = Backend(
            url=row["url"], headers={},
            upstream_name=row.get("upstream", ""),
            provider=row.get("provider", ""),
            reasoning=row.get("reasoning"))
    return MappingProxyType(mapping)


def snapshot_from_content(content: str, backends: Mapping[str, "Backend"] | None = None
                          ) -> RoutingSnapshot:
    """Restore a historical effective snapshot from its append-only content.

    Used by decision replay (design §8.2/§10): the compiled tree is parsed back
    with its expanded candidate texts, questions become frozen again, and
    ``snapshot_revision`` is recomputed as the sha256 of the canonical document
    (therefore equal to the original revision).  ``backends`` may supply the
    live ``Backend`` objects for this process; their routing-relevant fields
    must match the stored document for ``backends_revision`` to stay coherent.
    """
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("snapshot content must be a JSON object")
    raw_root = parsed.get("root")
    tree = None
    if raw_root is not None:
        tree = RoutingTree(
            id=parsed.get("tree_id") or "", name="", enabled=True,
            root=effective_node_from_dict(raw_root),
            tree_revision=parsed.get("tree_revision") or "", updated_at="")
        required_signals, scope = tree_signal_requirements(tree.root)
    else:
        required_signals, scope = frozenset(), full_scope("no active tree")
    questions = parsed.get("questions")
    if questions is not None:
        questions = freeze_data(questions)
    backend_map = _backends_from_document(parsed.get("backends")) \
        if backends is None else MappingProxyType(dict(backends))
    return RoutingSnapshot(
        tree=tree, scope=scope, required_signals=required_signals,
        questions=questions,
        questions_revision=parsed.get("questions_revision") or "",
        decision_revision=parsed.get("decision_revision") or "",
        backends=backend_map,
        backends_revision=parsed.get("backends_revision") or "",
        tree_revision=parsed.get("tree_revision") or "",
        snapshot_revision=sha256_text(canonical_json(parsed)),
        jev_trim=bool(parsed.get("jev_trim", False)))


# --------------------------------------------------------------------------
# DB assembly / append-only revision / publish
# --------------------------------------------------------------------------
def load_capabilities(conn: "sqlite3.Connection") -> dict[str, str]:
    """model id -> validated ``profile.capability`` text (compile input).

    Public because :func:`load_snapshot` and the admin ``/admin/try`` path must
    compile a tree from the same catalog read (no duplicate assembly).
    """
    from router.decision.catalog import capability_of_model
    from router.store.repositories import ModelRepo

    return {row["id"]: capability_of_model(row)
            for row in ModelRepo(conn).list()}


def load_questions(conn: "sqlite3.Connection") -> dict | None:
    """Load the questions config currently recorded in ``config_revisions``."""
    from router.store.repositories import ConfigRevisionRepo, SettingsRepo

    revision = SettingsRepo(conn).get("questions_revision")
    if not revision:
        return None
    row = ConfigRevisionRepo(conn).get(REVISION_QUESTIONS, revision)
    if row is None:
        return None
    try:
        parsed = json.loads(row["content"])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_snapshot(conn: "sqlite3.Connection", *, jev_trim: bool,
                  decision_info: Mapping | None = None,
                  questions: Mapping | None = None) -> RoutingSnapshot:
    """Assemble the current effective snapshot from the DB (no hot-path use).

    Loads the active tree + model catalog capabilities (compiled here), the
    backend map (with live auth headers), the recorded questions configuration
    and the caller's DecisionRunner version evidence (``runner.info()``; the
    adapter instance itself never lives in the DB).  ``questions`` may override
    the recorded config at boot before it has been recorded.
    """
    from router.api.upstream import load_backends
    from router.store.routing_trees import RoutingTreeRepo

    active = next((tree for tree in RoutingTreeRepo(conn).list() if tree.enabled),
                  None)
    compiled = None
    if active is not None:
        compiled = compile_tree(active, load_capabilities(conn))
    if questions is None:
        questions = load_questions(conn)
    return build_snapshot(tree=compiled, backends=load_backends(conn),
                          questions=questions, decision_info=decision_info or {},
                          jev_trim=jev_trim)


def ensure_snapshot_revision(conn: "sqlite3.Connection",
                             snapshot: RoutingSnapshot) -> str:
    """Append the effective document to ``config_revisions`` (idempotent).

    Returns the content sha, which is exactly ``snapshot.snapshot_revision``.
    Called on config writes/boot (not per request): a ledger row's
    ``snapshot_revision`` must be resolvable from the store.
    """
    from router.store.repositories import _tx, record_revision

    content = snapshot_content(snapshot)
    # ``_tx`` joins an outer transaction instead of committing it early, so a
    # publisher can record-and-publish inside one composer transaction.
    with _tx(conn):
        return record_revision(conn, REVISION_SNAPSHOT, content)


def install_snapshot(app: "FastAPI", snapshot: RoutingSnapshot) -> None:
    """Publish a snapshot atomically.

    ONE assignment is the visibility point for the hot path.  The legacy
    ``app.state.pipeline`` / ``app.state.backends`` Station aliases are gone:
    every reader goes through ``app.state.snapshot``.
    """
    app.state.snapshot = snapshot


class SnapshotPublisher:
    """Serialise configuration writes with the snapshot publication they imply.

    ``install_snapshot`` alone is atomic, but one admin write is really two
    steps: mutate the store, then assemble+publish the snapshot that mutation
    implies.  Two concurrent writes can commit ``A`` then ``B`` while
    publishing ``B`` then ``A`` — the hot path then serves a stale snapshot
    whose configuration is no longer in the database.  The fix is to hold
    :meth:`locked` across the WHOLE mutation+refresh section::

        with publisher.locked():
            with write_transaction(conn):
                ... mutation + audit ...
            publisher.refresh(conn)

    The lock is a ``threading.RLock`` because the admin endpoints are sync
    functions executed in FastAPI's threadpool, and because :meth:`refresh`
    takes the same lock itself (a caller that already holds :meth:`locked`
    simply re-enters).  ``refresh`` additionally keeps assembly, revision
    recording and the single publish assignment together, so a reader can
    never observe a half-published snapshot.
    """

    def __init__(self, app: "FastAPI", *, runner=None, jev_trim: bool = True,
                 questions: Mapping | None = None) -> None:
        self.app = app
        #: Optional fallback DecisionRunner.  ``app.state.runner`` wins when
        #: present (the app factory installs it once at startup).
        self._runner = runner
        self._jev_trim = bool(jev_trim)
        #: Boot-time questions override (before ``settings.questions_revision``
        #: has been recorded); ``None`` = read the recorded config from the DB.
        self._questions = questions
        self._lock = threading.RLock()

    # ── critical section ──────────────────────────────────────────────────
    @contextmanager
    def locked(self):
        """Hold the publication lock across one configuration write."""
        with self._lock:
            yield self

    # ── assembly + publish ────────────────────────────────────────────────
    def decision_info(self) -> dict:
        """DecisionRunner version evidence; one uniform rule everywhere.

        ``runner.info() if runner is not None else {}`` — the same shape is
        used at boot, on every publish and for ``/admin/try`` snapshots, so
        ``snapshot_revision`` is comparable across all three.
        """
        runner = getattr(self.app.state, "runner", self._runner)
        return runner.info() if runner is not None else {}

    def refresh(self, conn: "sqlite3.Connection", *,
                questions: Mapping | None = None) -> RoutingSnapshot:
        """Assemble from the current DB state, record the revision, publish.

        Assembly (active tree → compile → ``build_snapshot``), the append-only
        ``config_revisions(kind='snapshot')`` row and the ONE publish
        assignment happen under the lock, so the published snapshot always
        corresponds to one consistent database state.
        """
        with self._lock:
            effective_questions = self._questions if questions is None else questions
            snapshot = load_snapshot(
                conn, jev_trim=self._jev_trim,
                decision_info=self.decision_info(),
                questions=effective_questions)
            ensure_snapshot_revision(conn, snapshot)
            install_snapshot(self.app, snapshot)
            return snapshot

