"""Unified routing-tree management API (Task 9 Step 3/4).

One routing tree is a plain JSON document; these endpoints are the only way to
create/edit/copy/enable/disable it.  The HTTP contract is ruled by the plan and
consumed verbatim by the console store (``router/console/routing-tree-store.js``):

* ``GET  /trees``                  -> ``{"trees": [full tree_to_dict...],
                                         "snapshot_revision": "<published>"}``
* ``POST /trees {id?,name,root}``  -> 201 ``{"tree", "snapshot_revision"}``
  (``enabled`` in the payload is 422 — the same rule as PUT)
* ``GET  /trees/{id}``             -> ``{"tree"}`` / 404
* ``PUT  /trees/{id} {name,root}`` -> ``{"tree", "snapshot_revision", "warnings"}``
  (a payload carrying ``enabled`` is 422; a body ``id`` must equal the path id;
  the generic edit NEVER changes activation)
* ``POST /trees/{id}/copy {id?,name}`` -> 201 ``{"tree", "snapshot_revision"}``
* ``POST /trees/{id}/enable|disable``  -> ``{"trees", "snapshot_revision"}``

``POST /admin/try`` is deliberately more permissive than enable (spec §9): it
requires only STRUCTURAL validity so any saved draft is tryable; the full
enable findings are returned as non-blocking ``warnings``.  A deployment
without a DecisionRunner still gets a 200 with an ``nl_error`` trace (an
always-failing stand-in runner is used for the try).

Two invariants the console cannot enforce are enforced here (design §7):

* **Write + publish are ONE critical section.**  Every mutating endpoint wraps
  its database write, audit row and snapshot refresh in
  ``request.app.state.publisher.locked()`` — otherwise a concurrent write can
  commit B while an earlier one publishes A (stale hot path).
* **Save vs enable validation levels.**  Saving a closed tree only runs
  structural checks (an incomplete draft is a legal state); saving the
  currently-enabled tree and enabling any tree run the full health check.
  422 bodies are the typed validation findings:
  ``{"detail": [{code, zh, en, node_id, level}...]}``.

Task 9 Step 6 mounts this router under ``/admin`` and installs
``app.state.publisher`` / ``app.state.runner`` / ``app.state.runtime`` in the
app factory.  ``POST /try`` (Task 9 Step 4) compiles ONE stored tree into its
own snapshot and runs it through the same shared ``RoutingRuntime`` — it never
reads or replaces ``app.state.snapshot``, so trying a closed tree cannot change
what ``/v1`` serves.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn
from router.api.upstream import model_reasoning_from_db, reasoning_map_for_rows
from router.pipeline.evaluator import Hit
from router.pipeline.tree import (Block, Branch, NLChoice, Route,
                                  RoutingTree, tree_from_dict, tree_to_dict)
from router.pipeline.validation import (
    HARD,
    EnableContext,
    Issue,
    validate_tree_for_enable,
    validate_tree_structure,
)
from router.store.repositories import (
    ChangeRepo,
    ModelRepo,
    _now_iso,
    _uid,
    write_transaction,
)
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()

#: Same id rule as model ids (also enforced by the console before it posts).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# --------------------------------------------------------------------------- #
# payload helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return _now_iso()


def _issue(code: str, zh: str, en: str, node_id: str = "") -> dict:
    return {"code": code, "zh": zh, "en": en, "node_id": node_id,
            "level": HARD}


def _raise_issues(issues: list[Issue]) -> None:
    raise HTTPException(422, detail=[dict(issue.__dict__) for issue in issues])


def _raise_enabled_not_editable() -> None:
    """Generic tree writes never change activation (design §7.1)."""
    raise HTTPException(422, detail=[_issue(
        "enabled_not_editable",
        "通用编辑接口不能修改启用状态，请使用 enable/disable 操作",
        "The generic edit endpoint cannot change enabled; use enable/disable")])


def _invalid_id(tree_id: str) -> HTTPException:
    return HTTPException(422, detail=[_issue(
        "invalid_tree_id",
        f"树 id 必须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$，收到 {tree_id!r}",
        "Tree id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$, got "
        f"{tree_id!r}")])


def _clean_name(raw) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(422, detail=[_issue(
            "invalid_name", "树名称必须是字符串", "Tree name must be a string")])
    return raw.strip()


def _clean_id(raw) -> str:
    tree_id = _uid() if raw is None else raw
    if not isinstance(tree_id, str) or not _ID_RE.match(tree_id):
        raise _invalid_id(tree_id if isinstance(tree_id, str) else repr(tree_id))
    return tree_id


def _parse_tree(tree_id: str, name: str, raw_root) -> RoutingTree:
    """Parse one tree envelope, mapping every shape error to 422 (Issue array).

    ``ValueError`` is the tree serde trust boundary; ``RecursionError`` maps to
    the same 422 (the depth/size budget itself is enforced by validation, but a
    pathological JSON may hit the parser first).
    """
    try:
        return tree_from_dict({"id": tree_id, "name": name, "root": raw_root})
    except RecursionError:
        raise HTTPException(422, detail=[_issue(
            "tree_too_deep",
            "树 JSON 嵌套过深，无法解析", "Tree JSON is nested too deeply")])
    except ValueError as exc:
        raise HTTPException(422, detail=[_issue(
            "invalid_tree", f"树 JSON 不合法：{exc}", f"Invalid tree JSON: {exc}")])


def _published_revision(request: Request) -> str:
    snapshot = getattr(request.app.state, "snapshot", None)
    return getattr(snapshot, "snapshot_revision", "") if snapshot is not None else ""


def _publisher(request: Request):
    publisher = getattr(request.app.state, "publisher", None)
    if publisher is None:                     # pragma: no cover - app factory
        raise HTTPException(500, detail="snapshot publisher is not installed")
    return publisher


def _runner_available(request: Request) -> bool:
    """No DecisionRunner means an nl_choice tree cannot be enabled/saved live."""
    return getattr(request.app.state, "runner", None) is not None


def _trees_payload(conn: sqlite3.Connection, request: Request) -> dict:
    return {"trees": [tree_to_dict(tree) for tree in RoutingTreeRepo(conn).list()],
            "snapshot_revision": _published_revision(request)}


def _tree_or_404(conn: sqlite3.Connection, tree_id: str) -> RoutingTree:
    tree = RoutingTreeRepo(conn).get(tree_id)
    if tree is None:
        raise HTTPException(404, detail=f"unknown tree {tree_id}")
    return tree


# --------------------------------------------------------------------------- #
# validation services (also consumed by the model/provider/admin write paths)
# --------------------------------------------------------------------------- #
def active_tree(conn: sqlite3.Connection) -> RoutingTree | None:
    """The single enabled tree (None = all closed, a legal state)."""
    return next((tree for tree in RoutingTreeRepo(conn).list() if tree.enabled),
                None)


def _current_questions(request: Request):
    """The tagger's currently loaded signal-question config (None = default)."""
    return getattr(request.app.state.tagger, "questions", None)


def _signal_values_for(questions):
    """Values the configured provider taggers can emit (validation coverage).

    ``None`` (no questions config / no provider tagger) keeps values
    unconstrained: the enable check then judges nothing about provider values,
    exactly as before.  Otherwise the provider questions decide which signal
    values a signal-based tree can actually match at runtime (B-I-2).
    """
    if questions is None:
        return None
    from router.pipeline.validation import provider_signal_values
    return provider_signal_values(questions)


def build_enable_context(conn: sqlite3.Connection, *, runner_available: bool,
                         models: list[dict] | None = None,
                         capabilities: Mapping[str, str] | None = None,
                         questions: Mapping | None = None
                         ) -> EnableContext:
    """Full runtime-readiness context for the current / PROSPECTIVE catalog.

    ``models`` (and ``capabilities``) may describe a catalog that is not stored
    yet — a pending model write.  In that case the reasoning map must be built
    from those SAME prospective rows, not from the stored DB: otherwise a write
    removing a reasoning level the active tree references would be accepted
    (old DB map still says it is declared) while the next startup, reading the
    real rows, refuses to boot (audit I-1: "save accepted / restart rejected").

    ``questions`` is the signal-question config the tagger will run with; it
    feeds the provider-value coverage check (``signal_values``).  ``None``
    leaves provider values unconstrained (existing DB-only behavior).
    """
    from router.pipeline.snapshot import load_capabilities

    prospective = models is not None
    if models is None:
        models = ModelRepo(conn).list()
    if capabilities is None:
        capabilities = load_capabilities(conn)
    model_reasoning = (reasoning_map_for_rows(conn, models) if prospective
                       else model_reasoning_from_db(conn))
    return EnableContext(
        known_models={row["id"] for row in models},
        capabilities=dict(capabilities),
        model_reasoning=model_reasoning,
        decision_runner_available=bool(runner_available),
        signal_values=_signal_values_for(questions))


def enable_issues(conn: sqlite3.Connection, tree: RoutingTree, *,
                  runner_available: bool,
                  models: list[dict] | None = None,
                  capabilities: Mapping[str, str] | None = None,
                  questions: Mapping | None = None) -> list[Issue]:
    """Full health check for enabling/saving-live one tree (design §7.2).

    ``questions`` is forwarded to :func:`build_enable_context`; callers pass
    the currently loaded tagger questions (or, for the signal-question write
    path, the PROSPECTIVE config so the check matches what will be persisted).
    """
    return validate_tree_for_enable(tree, build_enable_context(
        conn, runner_available=runner_available, models=models,
        capabilities=capabilities, questions=questions))


def hard_issues(issues: list[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.level == HARD]


def capability_write_issues(conn: sqlite3.Connection, request: Request,
                            models: list[dict]) -> list[Issue]:
    """Hard issues a prospective model-catalog write would introduce.

    A model write is not a tree save, so the active tree is re-checked against
    the prospective catalog here: removing/renaming a capability, a reasoning
    level or a model that the ACTIVE tree references would otherwise be
    accepted now and break the hot path (degraded reasoning) or the next
    startup (``reasoning_unsupported`` / ``unknown_model``).  Uses the
    prospective rows for capabilities AND reasoning, i.e. the same rules boot
    applies; a closed/incomplete tree is not affected (no active tree -> []).
    """
    from router.decision.catalog import capability_of_model

    active = active_tree(conn)
    if active is None:
        return []
    capabilities = {row["id"]: capability_of_model(row) for row in models}
    return hard_issues(enable_issues(
        conn, active, runner_available=_runner_available(request),
        models=models, capabilities=capabilities,
        questions=_current_questions(request)))


# --------------------------------------------------------------------------- #
# tree CRUD
# --------------------------------------------------------------------------- #
@router.get("/trees")
def get_trees(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _trees_payload(conn, request)


@router.post("/trees", status_code=201)
def create_tree(body: dict, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)):
    # B-M-2: activation belongs to enable/disable; silently ignoring `enabled`
    # here would make a create payload look like it enabled something.
    if "enabled" in body:
        _raise_enabled_not_editable()
    tree_id = _clean_id(body.get("id"))
    name = _clean_name(body.get("name"))
    tree = _parse_tree(tree_id, name, body.get("root"))
    hard = hard_issues(validate_tree_structure(tree))
    if hard:
        _raise_issues(hard)

    publisher = _publisher(request)
    try:
        # B-I-1: the id-uniqueness check and the insert are ONE critical
        # section — two concurrent creates of the same id cannot both pass.
        with publisher.locked():
            repo = RoutingTreeRepo(conn)
            if repo.get(tree_id) is not None:
                raise HTTPException(409,
                                    detail=f"tree {tree_id} already exists")
            with write_transaction(conn):
                created = repo.create(tree_id, name, tree.root)
                ChangeRepo(conn).record(_now(), "tree",
                                        {"action": "create", "id": tree_id})
    except sqlite3.IntegrityError:            # concurrent create of the same id
        raise HTTPException(409,
                            detail=f"tree {tree_id} already exists") from None
    return {"tree": tree_to_dict(created),
            "snapshot_revision": _published_revision(request)}


@router.get("/trees/{tree_id}")
def get_tree(tree_id: str, request: Request,
             conn: sqlite3.Connection = Depends(get_conn)):
    return {"tree": tree_to_dict(_tree_or_404(conn, tree_id))}


@router.put("/trees/{tree_id}")
def update_tree(tree_id: str, body: dict, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)):
    # The generic edit endpoint must never change activation (design §7.1):
    # "enabled" present with ANY value is a caller bug, not a silent no-op.
    if "enabled" in body:
        _raise_enabled_not_editable()
    # B-M-2: the path id is authoritative; a mismatching body id is a client
    # bug (the console never sends it, so accepting it would hide a bad caller).
    if "id" in body and body["id"] != tree_id:
        raise HTTPException(422, detail=[_issue(
            "tree_id_mismatch",
            "请求体 id 与路径 id 不一致",
            "The body id does not match the path tree_id")])

    # Parsing is pure: only when the caller supplies a name may it happen before
    # the lock (tests rely on this as the race window seam); the validation
    # LEVEL and the publish decision below are chosen on a lock-internal re-read.
    name = _clean_name(body["name"]) if "name" in body else None
    parsed = (_parse_tree(tree_id, name, body.get("root"))
              if name is not None else None)

    publisher = _publisher(request)
    with publisher.locked():
        # B-I-1: re-read INSIDE the lock. A concurrent enable may have made
        # this tree live since the request started, so the health-check level
        # and the refresh decision must come from the CURRENT row, not a stale
        # pre-lock read (that race could persist an enabled tree that never
        # passed the full health check, and leave the published snapshot stale).
        current = _tree_or_404(conn, tree_id)
        effective_name = current.name if name is None else name
        tree = (parsed if parsed is not None
                else _parse_tree(tree_id, effective_name, body.get("root")))
        questions = _current_questions(request)
        if current.enabled:
            issues = enable_issues(conn, tree,
                                   runner_available=_runner_available(request),
                                   questions=questions)
        else:
            issues = validate_tree_structure(tree)
        hard = hard_issues(issues)
        if hard:
            _raise_issues(hard)
        warnings = [dict(issue.__dict__) for issue in issues
                    if issue.level != HARD]

        with write_transaction(conn):
            updated = RoutingTreeRepo(conn).update(tree_id, effective_name,
                                                   tree.root)
            ChangeRepo(conn).record(_now(), "tree", {
                "action": "update", "id": tree_id,
                "tree_revision": updated.tree_revision})
        if current.enabled:
            # saving the LIVE tree changes the effective configuration, so the
            # snapshot is rebuilt and published; a closed draft publishes nothing
            publisher.refresh(conn)
        payload = {"tree": tree_to_dict(updated),
                   "snapshot_revision": _published_revision(request),
                   "warnings": warnings}
    return payload


@router.post("/trees/{tree_id}/copy", status_code=201)
def copy_tree(tree_id: str, body: dict, request: Request,
              conn: sqlite3.Connection = Depends(get_conn)):
    new_id = _clean_id(body.get("id"))
    new_name = _clean_name(body.get("name"))

    publisher = _publisher(request)
    try:
        # B-I-1: source existence, target uniqueness and the copy are one
        # critical section (no check-then-write window).
        with publisher.locked():
            repo = RoutingTreeRepo(conn)
            if repo.get(tree_id) is None:
                raise HTTPException(404, detail=f"unknown tree {tree_id}")
            if repo.get(new_id) is not None:
                raise HTTPException(409,
                                    detail=f"tree {new_id} already exists")
            with write_transaction(conn):
                copied = repo.copy(tree_id, new_id, new_name)
                ChangeRepo(conn).record(_now(), "tree", {
                    "action": "copy", "source": tree_id, "id": new_id})
    except KeyError:                          # source deleted concurrently
        raise HTTPException(404, detail=f"unknown tree {tree_id}") from None
    except sqlite3.IntegrityError:
        raise HTTPException(409,
                            detail=f"tree {new_id} already exists") from None
    return {"tree": tree_to_dict(copied),
            "snapshot_revision": _published_revision(request)}


# --------------------------------------------------------------------------- #
# activation (enable/disable) — always publishes
# --------------------------------------------------------------------------- #
@router.post("/trees/{tree_id}/enable")
def enable_tree(tree_id: str, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)):
    publisher = _publisher(request)
    with publisher.locked():
        # B-I-1: the content that will be enabled is re-read (and re-validated)
        # INSIDE the lock; validating the pre-lock snapshot let a concurrent PUT
        # swap in content that was never health-checked.
        repo = RoutingTreeRepo(conn)
        tree = _tree_or_404(conn, tree_id)
        issues = enable_issues(conn, tree,
                               runner_available=_runner_available(request),
                               questions=_current_questions(request))
        hard = hard_issues(issues)
        if hard:
            _raise_issues(hard)
        with write_transaction(conn):
            repo.enable(tree_id)              # closes every other tree, same txn
            ChangeRepo(conn).record(_now(), "tree",
                                    {"action": "enable", "id": tree_id})
        publisher.refresh(conn)
        payload = _trees_payload(conn, request)
    return payload


@router.post("/trees/{tree_id}/disable")
def disable_tree(tree_id: str, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    publisher = _publisher(request)
    with publisher.locked():
        _tree_or_404(conn, tree_id)           # re-read under the lock
        with write_transaction(conn):
            RoutingTreeRepo(conn).disable(tree_id)
            ChangeRepo(conn).record(_now(), "tree",
                                    {"action": "disable", "id": tree_id})
        publisher.refresh(conn)
        payload = _trees_payload(conn, request)
    return payload


# --------------------------------------------------------------------------- #
# try-run: one stored tree -> its own snapshot -> the shared runtime
# --------------------------------------------------------------------------- #
def _outcome_payload(outcome) -> dict:
    """The Lead-ruled try DTO: hit keeps the full terminal evidence, miss only
    the standard reason (design §10)."""
    if isinstance(outcome, Hit):
        return {"type": "hit", "kind": outcome.kind, "model": outcome.model,
                "reasoning": outcome.reasoning, "node_id": outcome.node_id,
                "score": outcome.score}
    return {"type": "miss", "reason": outcome.reason}


class _UnavailableRunner:
    """DecisionRunner stand-in for a deployment without a decision model.

    Draft trees may contain ``nl_choice`` nodes while the running tagger has no
    decision adapter; the evaluator converts this local RuntimeError into an
    ``nl_error`` and follows the node's otherwise/Miss, so a try-run still
    reports a full trace instead of failing with a 500 (spec §9).
    """

    def info(self) -> dict:
        return {}

    async def choose(self, request):
        raise RuntimeError("decision runner is not available")


def _compile_for_try(tree: RoutingTree, capabilities: Mapping[str, str]
                     ) -> RoutingTree:
    """Best-effort effective tree for ``/admin/try`` (spec §9: ANY saved tree).

    Unlike the strict snapshot compiler, a draft's direct-model candidate whose
    capability description is missing expands to an EMPTY text instead of
    blocking the try: trying must not require a complete, enableable tree.
    Terminal nodes are shared; only containers/options are copied.
    """
    from dataclasses import replace

    def compile_node(node):
        if isinstance(node, (Route, Block)):
            return node
        if isinstance(node, Branch):
            return replace(
                node,
                children=tuple(compile_node(child) for child in node.children),
                otherwise=(None if node.otherwise is None
                           else compile_node(node.otherwise)))
        options = []
        for option in node.options:                     # NLChoice
            if option.capability_model is not None:
                capability = (capabilities.get(option.capability_model)
                              or "").strip()
                text = (f"{option.capability_model}: {capability}"
                        if capability else "")
                options.append(replace(option, text=text))
            else:
                options.append(replace(
                    option, target=compile_node(option.target)))
        return replace(node, options=tuple(options),
                       otherwise=(None if node.otherwise is None
                                  else compile_node(node.otherwise)))

    return replace(tree, root=compile_node(tree.root))


def _try_snapshot(conn: sqlite3.Connection, request: Request,
                  tree: RoutingTree):
    """Compile ONE stored tree into its own effective snapshot (no publish).

    Best-effort compile with the current catalog (missing capability texts do
    not block), live backends, recorded questions and the shared runner's
    version evidence (``{}`` when no runner is installed).
    """
    from router.api.upstream import load_backends
    from router.pipeline.snapshot import (
        build_snapshot,
        load_capabilities,
        load_questions,
    )

    runner = getattr(request.app.state, "runner", None)
    compiled = _compile_for_try(tree, load_capabilities(conn))
    return build_snapshot(
        tree=compiled,
        backends=load_backends(conn),
        questions=load_questions(conn),
        decision_info=runner.info() if runner is not None else {},
        jev_trim=bool(request.app.state.settings.jev_trim))


def _try_runtime(request: Request):
    """The shared runtime, or an NL-safe stand-in when no runner is installed."""
    from router.pipeline.runtime import RoutingRuntime

    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:                        # pragma: no cover - app factory
        raise HTTPException(503, detail="routing runtime is not installed")
    if getattr(request.app.state, "runner", None) is not None:
        return runtime
    return RoutingRuntime(request.app.state.tagger, _UnavailableRunner())


@router.post("/try")
async def try_tree(body: dict, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)):
    """Route one sample text through any SAVED tree (spec §9).

    Contract (Lead-ruled): ``{tree_id,text,headers?,n_messages?}``; a missing
    tree_id is 422, an unknown tree 404.  Unlike enable, try only requires the
    tree to be STRUCTURALLY valid: a closed draft may be incomplete (design
    §7.2) but is still tryable (spec §9), so the full enable findings ride
    along as ``warnings`` instead of blocking.  The tree's own best-effort
    snapshot runs through the same runtime as ``/v1``; the online snapshot is
    neither read nor replaced.
    """
    from router.pipeline.errors import NoActiveTreeError, SignalPreparationError
    from router.pipeline.runtime import RuntimeRequest

    tree_id = body.get("tree_id")
    if not isinstance(tree_id, str) or not tree_id:
        raise HTTPException(422, detail=[_issue(
            "missing_tree_id", "试算必须提供 tree_id", "try needs a tree_id")])
    tree = RoutingTreeRepo(conn).get(tree_id)
    if tree is None:
        raise HTTPException(404, detail=f"unknown tree {tree_id}")
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(422, detail=[_issue(
            "invalid_text", "试算 text 必须是字符串",
            "try text must be a string")])
    headers = body.get("headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise HTTPException(422, detail=[_issue(
            "invalid_headers", "headers 必须是对象", "headers must be an object")])
    n_messages = body.get("n_messages", 1)
    if isinstance(n_messages, bool) or not isinstance(n_messages, int) \
            or n_messages < 0:
        raise HTTPException(422, detail=[_issue(
            "invalid_n_messages", "n_messages 必须是非负整数",
            "n_messages must be a non-negative integer")])

    # B-M-1: save-level gate only.  The full enable check would reject the very
    # drafts spec §9 wants to be tryable (e.g. missing fallback), so its
    # findings are informational warnings here.
    hard = hard_issues(validate_tree_structure(tree))
    if hard:
        _raise_issues(hard)
    warnings = [dict(issue.__dict__) for issue in enable_issues(
        conn, tree, runner_available=_runner_available(request),
        questions=_current_questions(request))]

    snapshot = _try_snapshot(conn, request, tree)
    runtime = _try_runtime(request)
    try:
        result = await runtime.route(snapshot, RuntimeRequest(
            text=text, headers=dict(headers), n_messages=n_messages))
    except NoActiveTreeError:                  # pragma: no cover - tree is set
        raise HTTPException(503, detail="no_active_tree") from None
    except SignalPreparationError:
        raise HTTPException(503, detail="signal_preparation_failed") from None
    return {
        "outcome": _outcome_payload(result.outcome),
        "signals": result.signals,
        "trace": list(result.trace),
        "input": result.input,
        "signal_ms": result.signal_ms,
        "decision_ms": result.decision_ms,
        "nl_calls": result.nl_calls,
        "tree_id": tree.id,
        "tree_revision": snapshot.tree_revision,
        "snapshot_revision": snapshot.snapshot_revision,
        # pure-NL runs never tag, so no TagResult fingerprint exists: fall back
        # to the fixed questions revision of the snapshot itself.
        "questions_sha256": (result.questions_sha256
                             or snapshot.questions_revision or None),
        "tagger_info": result.tagger_info,
        # Full enable-check findings, non-blocking (drafts are tryable).
        "warnings": warnings,
    }
