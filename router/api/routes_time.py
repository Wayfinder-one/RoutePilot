"""时段目录（管理面事实源是 SQLite）：图读取经内核，写事务不触碰画布。

时段替换是纯 SQLite 目录写：候选画布恒为空，因此不调用内核条件图批次。启用
图校验仍必须基于内核当前画布集合（运行快照由内核装配），因此 ``recheck`` 在
SQLite 写事务内从 ``CoreClient`` 重新读取画布并做启用级校验；失败则数据库整体
回滚（与旧本地单事务语义一致）。时段目录读取只查 SQLite。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import catalog_service, commit_catalog_write, get_conn
from router.control.errors import ValidationFailed
from router.control.validation import tree_issues
from router.store.repositories import ChangeRepo, TimePeriodRepo, now_iso

router = APIRouter()


class TimePeriodsIn(BaseModel):
    """PUT /admin/time-periods 请求 DTO：未知字段一律拒绝；periods 的 list
    与时段条目校验保留原文案（TimePeriod.from_dict / 服务层 ValueError）。"""

    model_config = ConfigDict(extra="forbid")

    periods: Any = None


def _enabled_tree_issues(trees) -> list:
    """内核当前启用集合的启用级 Issue（按图 id 排序、逐图顺序合并）。"""
    issues: list = []
    for tree in sorted(trees, key=lambda item: item.id):
        if not tree.enabled:
            continue
        issues.extend(tree_issues(tree))
    return issues


@router.get("/time-periods")
def get_time_periods(conn: sqlite3.Connection = Depends(get_conn)):
    """时段目录读取只查 SQLite：不因任何图的内嵌配置而切换数据源。"""
    return {"periods": TimePeriodRepo(conn).load(), "now": now_iso()}


@router.put("/time-periods")
async def put_time_periods(
    body: TimePeriodsIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    periods = body.periods
    if not isinstance(periods, list):
        raise HTTPException(422, detail="periods must be a list")

    async def recheck(_conn):
        # 事务内从内核重读启用集合：路由层读取之后被启用/改写的画布若变得非法，
        # 整笔时段写回滚为 422，绝不带着过期启用集合提交。
        issues = _enabled_tree_issues(await request.app.state.core.list_graphs())
        if issues:
            raise ValidationFailed(issues)

    def db_write(c):
        TimePeriodRepo(c).save(periods)

    def audit(c):
        ChangeRepo(c).record(
            now_iso(), "time-periods", {"periods": len(periods)})

    try:
        # 纯 SQLite 目录写：没有候选画布，不调用内核图批次。
        await commit_catalog_write(
            request, catalog_service(request, conn),
            db_write=db_write, candidates=[], audit=audit, recheck=recheck,
        )
    except ValidationFailed:
        raise  # 启用图校验失败：交给 app 级处理器返回 422 + Issue 数组（spec §5）
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return {"ok": True, "periods": TimePeriodRepo(conn).load()}
