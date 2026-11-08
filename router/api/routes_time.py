from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.control.config import ConfigurationService
from router.control.errors import ValidationFailed
from router.store.repositories import TimePeriodRepo, now_iso
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()


class TimePeriodsIn(BaseModel):
    """PUT /admin/time-periods 请求 DTO：未知字段一律拒绝；periods 的 list
    与时段条目校验保留原文案（TimePeriod.from_dict / 服务层 ValueError）。"""

    model_config = ConfigDict(extra="forbid")

    periods: Any = None


@router.get("/time-periods")
def get_time_periods(conn: sqlite3.Connection = Depends(get_conn)):
    return {"periods": TimePeriodRepo(conn).load(), "now": now_iso()}


@router.put("/time-periods")
def put_time_periods(
    body: TimePeriodsIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    periods = body.periods
    if not isinstance(periods, list):
        raise HTTPException(422, detail="periods must be a list")
    service = ConfigurationService(conn, trees, request.app.state.publisher)
    try:
        service.save_periods(periods)
    except ValidationFailed:
        raise  # 活动图校验失败：交给 app 级处理器返回 422 + Issue 数组（spec §5）
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return {"ok": True, "periods": TimePeriodRepo(conn).load()}
