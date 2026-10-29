from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn
from router.api.upstream import load_backends, model_reasoning_from_db
from router.jev.config import JevConfigRepo
from router.routing.conditions import TimePeriod
from router.routing.model import RoutingTree, node_to_dict, tree_from_dict, tree_to_dict
from router.routing.snapshot import _capability, build_snapshot
from router.routing.types import NoAvailableBackend, RoutingMiss, RoutingRequest
from router.routing.validation import EnableContext, validate_tree_for_enable, validate_tree_structure
from router.store.repositories import ChangeRepo, ModelRepo, TimePeriodRepo, _now_iso, _uid, write_transaction
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _raise_issues(issues) -> None:
    raise HTTPException(422, detail=[dict(issue.__dict__) for issue in issues])


def _clean_id(raw) -> str:
    value = _uid() if raw is None else raw
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise HTTPException(422, detail="invalid tree id")
    return value


def _clean_name(raw) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(422, detail="tree name must be a string")
    return raw.strip()


def _parse_tree(raw: dict) -> RoutingTree:
    try:
        return tree_from_dict(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _tree_or_404(conn: sqlite3.Connection, tree_id: str) -> RoutingTree:
    tree = RoutingTreeRepo(conn).get(tree_id)
    if tree is None:
        raise HTTPException(404, detail=f"unknown tree {tree_id}")
    return tree


def active_tree(conn: sqlite3.Connection) -> RoutingTree | None:
    return next((tree for tree in RoutingTreeRepo(conn).list() if tree.enabled), None)


def build_enable_context(conn: sqlite3.Connection) -> EnableContext:
    models = ModelRepo(conn).list()
    return EnableContext(
        known_models={row["id"] for row in models},
        capabilities={row["id"]: _capability(row) for row in models},
        model_reasoning=model_reasoning_from_db(conn),
        known_periods={period["name"] for period in TimePeriodRepo(conn).load()},
        jev_available=JevConfigRepo(conn).load() is not None,
    )


def active_tree_issues(conn: sqlite3.Connection):
    tree = active_tree(conn)
    return [] if tree is None else validate_tree_for_enable(tree, build_enable_context(conn))


def _published_revision(request: Request) -> str:
    return getattr(request.app.state.snapshot, "snapshot_revision", "")


def _trees_payload(conn: sqlite3.Connection, request: Request) -> dict:
    return {"trees": [tree_to_dict(tree) for tree in RoutingTreeRepo(conn).list()], "snapshot_revision": _published_revision(request)}


@router.get("/trees")
def get_trees(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _trees_payload(conn, request)


@router.post("/trees", status_code=201)
def create_tree(body: dict, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if "enabled" in body:
        raise HTTPException(422, detail="use enable/disable operations")
    tree = _parse_tree({
        "id": _clean_id(body.get("id")), "name": _clean_name(body.get("name")),
        "root": body.get("root"), "sticky": body.get("sticky"),
        "timezone": body.get("timezone", "Asia/Shanghai"),
    })
    issues = validate_tree_structure(tree)
    if issues:
        _raise_issues(issues)
    publisher = request.app.state.publisher
    try:
        with publisher.locked():
            with write_transaction(conn):
                created = RoutingTreeRepo(conn).create(tree)
                ChangeRepo(conn).record(_now_iso(), "tree", {"action": "create", "id": tree.id})
    except sqlite3.IntegrityError:
        raise HTTPException(409, detail=f"tree {tree.id} already exists") from None
    return {"tree": tree_to_dict(created), "snapshot_revision": _published_revision(request)}


@router.get("/trees/{tree_id}")
def get_tree(tree_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return {"tree": tree_to_dict(_tree_or_404(conn, tree_id))}


@router.put("/trees/{tree_id}")
def update_tree(tree_id: str, body: dict, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if "enabled" in body:
        raise HTTPException(422, detail="use enable/disable operations")
    if "id" in body and body["id"] != tree_id:
        raise HTTPException(422, detail="tree id mismatch")
    publisher = request.app.state.publisher
    with publisher.locked():
        current = _tree_or_404(conn, tree_id)
        tree = _parse_tree({
            "id": tree_id,
            "name": _clean_name(body.get("name", current.name)),
            "root": body.get("root", node_to_dict(current.root)),
            "sticky": body.get("sticky", {"enabled": current.sticky.enabled, "ttl_seconds": current.sticky.ttl_seconds}),
            "timezone": body.get("timezone", current.timezone),
        })
        issues = validate_tree_for_enable(tree, build_enable_context(conn)) if current.enabled else validate_tree_structure(tree)
        if issues:
            _raise_issues(issues)
        with write_transaction(conn):
            updated = RoutingTreeRepo(conn).update(tree)
            ChangeRepo(conn).record(_now_iso(), "tree", {"action": "update", "id": tree_id, "tree_revision": updated.tree_revision})
        if current.enabled:
            publisher.refresh(conn)
        return {"tree": tree_to_dict(updated), "snapshot_revision": _published_revision(request), "warnings": []}


@router.post("/trees/{tree_id}/copy", status_code=201)
def copy_tree(tree_id: str, body: dict, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    new_id = _clean_id(body.get("id"))
    new_name = _clean_name(body.get("name"))
    publisher = request.app.state.publisher
    try:
        with publisher.locked():
            _tree_or_404(conn, tree_id)
            with write_transaction(conn):
                copied = RoutingTreeRepo(conn).copy(tree_id, new_id, new_name)
                ChangeRepo(conn).record(_now_iso(), "tree", {"action": "copy", "source": tree_id, "id": new_id})
    except KeyError:
        raise HTTPException(404, detail=f"unknown tree {tree_id}") from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, detail=f"tree {new_id} already exists") from None
    return {"tree": tree_to_dict(copied), "snapshot_revision": _published_revision(request)}


@router.post("/trees/{tree_id}/enable")
def enable_tree(tree_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    publisher = request.app.state.publisher
    with publisher.locked():
        tree = _tree_or_404(conn, tree_id)
        issues = validate_tree_for_enable(tree, build_enable_context(conn))
        if issues:
            _raise_issues(issues)
        with write_transaction(conn):
            RoutingTreeRepo(conn).enable(tree_id)
            ChangeRepo(conn).record(_now_iso(), "tree", {"action": "enable", "id": tree_id})
        publisher.refresh(conn)
        return _trees_payload(conn, request)


@router.post("/trees/{tree_id}/disable")
def disable_tree(tree_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    publisher = request.app.state.publisher
    with publisher.locked():
        _tree_or_404(conn, tree_id)
        with write_transaction(conn):
            RoutingTreeRepo(conn).disable(tree_id)
            ChangeRepo(conn).record(_now_iso(), "tree", {"action": "disable", "id": tree_id})
        publisher.refresh(conn)
        return _trees_payload(conn, request)


def _try_snapshot(conn: sqlite3.Connection, tree: RoutingTree):
    models = ModelRepo(conn).list()
    return build_snapshot(
        tree=tree,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(period) for period in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: _capability(row) for row in models},
    )


@router.post("/try")
async def try_tree(body: dict, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    tree_id = body.get("tree_id")
    if not isinstance(tree_id, str) or not tree_id:
        raise HTTPException(422, detail="try needs a tree_id")
    tree = _tree_or_404(conn, tree_id)
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(422, detail="try text must be a string")
    session_id = body.get("session_id", "")
    if not isinstance(session_id, str) or len(session_id) > 256:
        raise HTTPException(422, detail="invalid session_id")
    now = None
    if body.get("now") is not None:
        try:
            now = datetime.fromisoformat(body["now"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, detail="now must be an ISO datetime") from exc
    issues = validate_tree_structure(tree)
    if issues:
        _raise_issues(issues)
    warnings = [dict(issue.__dict__) for issue in validate_tree_for_enable(tree, build_enable_context(conn))]
    snapshot = _try_snapshot(conn, tree)
    try:
        decision = await request.app.state.engine.route(
            snapshot, RoutingRequest(text=text, session_id=session_id), now=now, dry_run=True
        )
    except (RoutingMiss, NoAvailableBackend) as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    try:
        answer = decision.jev_answer
        return {
            "outcome": {
                "type": "hit", "model": decision.model, "target_id": decision.target_id,
                "reasoning": decision.reasoning, "selection_reason": decision.selection_reason,
                "fallback_reason": decision.fallback_reason, "sticky_hit": decision.sticky_hit,
                "policy_path": decision.policy_path,
            },
            "trace": decision.trace,
            "jev": None if answer is None else {
                "selected_key": answer.selected_key, "probabilities": answer.probabilities,
                "ranking": answer.ranking, "p_top": answer.p_top, "margin": answer.margin,
                "duration_ms": answer.duration_ms, "usage": answer.usage,
            },
            "tree_id": tree.id,
            "tree_revision": snapshot.tree_revision,
            "snapshot_revision": snapshot.snapshot_revision,
            "warnings": warnings,
        }
    finally:
        decision.release()
