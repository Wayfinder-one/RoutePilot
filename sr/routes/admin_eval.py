"""admin eval 端点（D2/A1：自 sr/server.py 机械迁出，零行为变化）。"""

from __future__ import annotations

import sr.db as db
import sr.eval as eval_mod
from fastapi import Request
from fastapi.responses import JSONResponse
from sr.treeio import WRITE_ACTOR

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/admin/eval")
async def admin_eval(request: Request):
    """评测（§11.4）：``{"source":"replay","days":7}`` 或
    ``{"source":"labels","labels":[{text,expect_model|expect_branch}]}``。
    结果落 changes(tbl='eval') + 置 sr_eval_route_match_rate gauge。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request body must be an object"},
                            status_code=400)
    source = body.get("source")
    conn = db.get(request.app.state.db_path)
    try:
        if source == "replay":
            report = eval_mod.replay_eval(conn, request.app.state.db_path,
                                          days=int(body.get("days") or 7))
        elif source == "labels":
            recs = body.get("labels")
            if not isinstance(recs, list):
                return JSONResponse({"detail": "labels must be a list"},
                                    status_code=400)
            report = await eval_mod.labels_eval(conn, request.app.state.db_path, recs, request.app.state.tagger)
        else:
            return JSONResponse(
                {"detail": "source must be 'replay' or 'labels'"},
                status_code=400)
        eval_mod.log_eval(conn, report, actor=WRITE_ACTOR)
        conn.commit()
    finally:
        conn.close()
    if report.rate is not None:
        request.app.state.metrics.set("sr_eval_route_match_rate", report.rate)
    return JSONResponse(report.to_json())

@router.get("/admin/evals")
async def admin_evals(request: Request, limit: int = 3):
    """最近 N 次评测报告（§11.4：控制台「分流树」页查看最近三次）。"""
    conn = db.get(request.app.state.db_path)
    try:
        reports = eval_mod.recent_evals(conn, max(1, min(limit, 20)))
    finally:
        conn.close()
    return JSONResponse(reports)
