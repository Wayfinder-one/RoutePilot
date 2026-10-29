from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn
from router.store.repositories import ChangeRepo, SettingsRepo, _now_iso

router = APIRouter()
RESERVED_SETTINGS = {"snapshot_revision", "jev_config"}


@router.get("/requests/recent")
def recent_requests(request: Request, limit: int = 20):
    return {"requests": request.app.state.requestlog.recent_requests(limit)}


@router.get("/requests/stats")
def request_stats(request: Request):
    return request.app.state.requestlog.stats()


@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_conn)):
    return {"settings": SettingsRepo(conn).all()}


@router.put("/settings")
def put_settings(body: dict, conn: sqlite3.Connection = Depends(get_conn)):
    for key, value in body.items():
        if not isinstance(key, str) or not key or key.startswith("_") or key in RESERVED_SETTINGS:
            raise HTTPException(422, detail=f"reserved or invalid setting key {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise HTTPException(422, detail=f"setting {key!r} must be a scalar")
    with conn:
        for key, value in body.items():
            SettingsRepo(conn).set(key, str(value))
        ChangeRepo(conn).record(_now_iso(), "settings", {"keys": sorted(body)})
    return {"ok": True, "settings": SettingsRepo(conn).all()}


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
