from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn
from router.api.request_presenter import present_record
from router.request_snapshots import REQUEST_FIELDS, canonical_graph_document
from router.store.repositories import ChangeRepo

router = APIRouter()


def _public_record(record: dict) -> dict:
    """Present one ledger record and drop internal (``_``-prefixed) fields.

    ``_graph_snapshot`` carries the whole graph document for the snapshot
    endpoint; the list must never ship it.
    """
    present_record(record)
    return {key: value for key, value in record.items() if not key.startswith("_")}


@router.get("/requests/recent")
def recent_requests(request: Request, limit: int = 20):
    records = request.app.state.requestlog.recent_requests(limit)
    return {"requests": [_public_record(record) for record in records]}


@router.get("/requests/{request_id:path}/snapshot")
def request_snapshot(request_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Live snapshot from the memory ring, else the immutable SQLite rows."""
    live = request.app.state.requestlog.snapshot_for(request_id)
    if live is not None:
        return live
    row = conn.execute(
        "SELECT s.graph_snapshot_id AS graph_snapshot_id, s.trace AS trace, "
        "s.created_at AS snapshot_created_at, g.graph AS graph, "
        "l.id AS id, l.ts AS ts, l.decision AS decision, "
        "l.model AS model, l.target_id AS target_id, l.tree_id AS tree_id, "
        "l.tree_revision AS tree_revision, l.snapshot_revision AS snapshot_revision, "
        "l.selection_reason AS selection_reason, l.outcome_state AS outcome_state "
        "FROM request_snapshots s "
        "JOIN graph_snapshots g ON g.snapshot_id = s.graph_snapshot_id "
        "LEFT JOIN request_logs l ON l.id = s.request_id "
        "WHERE s.request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail="unknown request")
    try:
        tree = json.loads(row["graph"])
        trace = json.loads(row["trace"])
    except (TypeError, ValueError):
        # 损坏的快照不能被当成空图回放。
        raise HTTPException(500, detail="request snapshot is corrupt") from None
    if not isinstance(tree, dict):
        raise HTTPException(500, detail="request snapshot is corrupt")
    # 旧 v5 行存的是完整 envelope：出口用与写入相同的过滤函数去掉易变元数据，
    # 历史 ID 不重算、历史图不删除。
    tree = canonical_graph_document(tree)
    source = {
        "id": row["id"] if row["id"] is not None else request_id,
        "ts": row["ts"] if row["ts"] is not None else row["snapshot_created_at"],
        "decision": row["decision"],
        "model": row["model"],
        "target_id": row["target_id"],
        "tree_id": row["tree_id"],
        "tree_revision": row["tree_revision"],
        "snapshot_revision": row["snapshot_revision"],
        "selection_reason": row["selection_reason"],
        "outcome_state": row["outcome_state"],
    }
    return {
        "request_id": request_id,
        "graph_snapshot_id": row["graph_snapshot_id"],
        "tree": tree,
        "trace": trace if isinstance(trace, list) else [],
        "request": {key: source[key] for key in REQUEST_FIELDS if source.get(key) is not None},
    }


@router.get("/requests/stats")
def request_stats(request: Request):
    return request.app.state.requestlog.stats()


@router.get("/changes")
def get_changes(limit: int = 50, conn: sqlite3.Connection = Depends(get_conn)):
    return {"changes": ChangeRepo(conn).list(limit)}


@router.get("/overview")
def get_overview(request: Request, days: int = 14, conn: sqlite3.Connection = Depends(get_conn)):
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).date().isoformat()
    daily = [dict(row) for row in conn.execute(
        "SELECT substr(ts, 1, 10) AS d, COUNT(*) AS count FROM request_logs "
        "WHERE substr(ts, 1, 10) >= ? GROUP BY d ORDER BY d", (cutoff,)
    )]
    routes = [dict(row) for row in conn.execute(
        "SELECT COALESCE(model, '(none)') AS model, COUNT(*) AS count FROM request_logs GROUP BY model ORDER BY count DESC"
    )]
    total = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
    sticky = conn.execute("SELECT COUNT(*) FROM request_logs WHERE sticky_hit = 1").fetchone()[0]
    requestlog = request.app.state.requestlog
    return {
        "daily": daily,
        "routes": routes,
        "total": total,
        "sticky": sticky,
        "confidence": requestlog.confidence_histogram(),
        "confidence_since": requestlog.started_at,
    }
