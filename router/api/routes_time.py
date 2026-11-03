from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn, get_tree_repo
from router.api.routes_trees import active_tree_issues
from router.store.repositories import ChangeRepo, TimePeriodRepo, _now_iso, write_transaction
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()


@router.get("/time-periods")
def get_time_periods(conn: sqlite3.Connection = Depends(get_conn)):
    return {"periods": TimePeriodRepo(conn).load(), "now": _now_iso()}


@router.put("/time-periods")
def put_time_periods(
    body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    periods = body.get("periods")
    if not isinstance(periods, list):
        raise HTTPException(422, detail="periods must be a list")
    publisher = request.app.state.publisher
    try:
        with publisher.locked():
            with write_transaction(conn):
                TimePeriodRepo(conn).save(periods)
                issues = active_tree_issues(conn, trees)
                if issues:
                    raise HTTPException(422, detail=[dict(issue.__dict__) for issue in issues])
                ChangeRepo(conn).record(_now_iso(), "time-periods", {"periods": len(periods)})
            publisher.refresh(conn)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return {"ok": True, "periods": TimePeriodRepo(conn).load()}
