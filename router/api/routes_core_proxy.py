"""Peripheral graph-management proxy to the decision kernel.

The kernel owns the graph JSON and its management API (``/internal/v1/trees*``
and ``/internal/v1/try``).  Task 8 mounts this router at ``/admin`` in place of
the local tree routes; Task 6 ships it as an independently testable module while
``create_app()`` keeps the old local mounts.

Design rules:

* exactly the ten routes below are proxied — no catch-all, so a new kernel
  management route is a conscious change here (and a visible one in the API);
* the request JSON body is forwarded untouched (validation, including the 422
  issue arrays, belongs to the kernel) and the kernel status/body are returned
  verbatim (a non-object body is wrapped under ``detail``);
* personnel authentication stays in the peripheral's ``/admin`` middleware: this
  module never reads the browser ``Authorization`` header and never sends it to
  the kernel;
* successful mutations append one peripheral ``changes`` audit row (the kernel
  has no SQLite) — best effort, an audit failure never fails the proxied write;
  ``GET`` and the dry-run ``/try`` write no audit row.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from router.api.deps import get_conn
from router.store.repositories import ChangeRepo, now_iso

router = APIRouter()
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Transport helpers
# --------------------------------------------------------------------------- #
async def _payload(request: Request):
    """Forward the request JSON body verbatim; an empty body means "no body"."""
    raw = await request.body()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # Malformed JSON is not a kernel decision: reject it at the boundary.
        raise HTTPException(400, detail="invalid_json") from None


def _json_response(status_code: int, body) -> JSONResponse:
    """Kernel reply → peripheral reply verbatim; wrap a non-object body."""
    if not isinstance(body, dict):
        body = {"detail": body}
    return JSONResponse(status_code=status_code, content=body)


async def _forward(request: Request, method: str, path: str, *, conn=None,
                   audit=None) -> JSONResponse:
    """Proxy one management call at ``path`` and audit a successful mutation."""
    payload = None if method == "GET" else await _payload(request)
    try:
        status, body = await request.app.state.core.admin_json(
            method, path, payload)
    except httpx.HTTPError:
        # The kernel is the only graph writer: without it, management is
        # unavailable (the same 503 class as the kernel's own graph errors).
        logger.exception("kernel graph request failed: %s %s", method, path)
        return JSONResponse(
            status_code=503, content={"detail": {"code": "core_unavailable"}})
    if audit is not None and 200 <= status < 300:
        action, detail = audit(payload, body)
        _record_audit(conn, action, detail)
    return _json_response(status, body)


def _tree_path(tree_id: str) -> str:
    return "/trees/" + quote(tree_id, safe="")


def _core_tree_id(body) -> str:
    """Tree id reported by the kernel answer (empty when it reported none)."""
    tree = body.get("tree") if isinstance(body, dict) else None
    tree_id = tree.get("id") if isinstance(tree, dict) else None
    return tree_id if isinstance(tree_id, str) else ""


def _core_tree_revision(body) -> str:
    tree = body.get("tree") if isinstance(body, dict) else None
    revision = tree.get("tree_revision") if isinstance(tree, dict) else None
    return revision if isinstance(revision, str) else ""


def _requested_id(payload) -> str:
    tree_id = payload.get("id") if isinstance(payload, dict) else None
    return tree_id if isinstance(tree_id, str) else ""


def _record_audit(conn, action: str, detail: dict) -> None:
    """Append one peripheral ``changes`` row (kind ``tree``), mirroring local ops.

    The kernel already committed the graph: an audit failure must never turn the
    proxied write into an error, so it is logged and dropped here (same policy as
    the local graph routes' audit callback).
    """
    if conn is None:
        return
    try:
        ChangeRepo(conn).record(now_iso(), "tree", {"action": action, **detail})
    except Exception:  # noqa: BLE001 - best-effort audit, never fail the write
        logger.exception(
            "proxied graph change committed but peripheral audit write failed: %s",
            action)


# --------------------------------------------------------------------------- #
# The ten proxied management routes
# --------------------------------------------------------------------------- #
@router.get("/trees")
async def list_trees(request: Request):
    return await _forward(request, "GET", "/trees")


@router.post("/trees")
async def create_tree(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return await _forward(
        request, "POST", "/trees", conn=conn,
        audit=lambda payload, body: (
            "create", {"id": _core_tree_id(body) or _requested_id(payload)}))


@router.get("/trees/{tree_id}")
async def get_tree(tree_id: str, request: Request):
    return await _forward(request, "GET", _tree_path(tree_id))


@router.put("/trees/{tree_id}")
async def update_tree(
        tree_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn)):
    return await _forward(
        request, "PUT", _tree_path(tree_id), conn=conn,
        audit=lambda payload, body: (
            "update",
            {"id": tree_id, "tree_revision": _core_tree_revision(body)}))


@router.post("/trees/{tree_id}/copy")
async def copy_tree(
        tree_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn)):
    return await _forward(
        request, "POST", _tree_path(tree_id) + "/copy", conn=conn,
        audit=lambda payload, body: (
            "copy", {"source": tree_id,
                     "id": _core_tree_id(body) or _requested_id(payload)}))


@router.post("/trees/{tree_id}/enable")
async def enable_tree(
        tree_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn)):
    return await _forward(
        request, "POST", _tree_path(tree_id) + "/enable", conn=conn,
        audit=lambda payload, body: ("enable", {"id": tree_id}))


@router.post("/trees/{tree_id}/disable")
async def disable_tree(
        tree_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn)):
    return await _forward(
        request, "POST", _tree_path(tree_id) + "/disable", conn=conn,
        audit=lambda payload, body: ("disable", {"id": tree_id}))


@router.put("/trees/{tree_id}/departments")
async def update_departments(
        tree_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn)):
    # 部门设置与普通保存共用同一条内核写入路径，审计动作同样是 "update"。
    return await _forward(
        request, "PUT", _tree_path(tree_id) + "/departments", conn=conn,
        audit=lambda payload, body: (
            "update",
            {"id": tree_id, "tree_revision": _core_tree_revision(body)}))


@router.post("/trees/{tree_id}/default")
async def set_default(
        tree_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn)):
    return await _forward(
        request, "POST", _tree_path(tree_id) + "/default", conn=conn,
        audit=lambda payload, body: ("set_default", {"id": tree_id}))


@router.post("/try")
async def try_tree(request: Request):
    # Dry-run only: never writes a graph, so there is nothing to audit.
    return await _forward(request, "POST", "/try")
