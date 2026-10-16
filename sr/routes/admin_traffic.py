"""admin traffic 端点（D2/A1：自 sr/server.py 机械迁出，零行为变化）。"""

from __future__ import annotations

import sr.db as db
from datetime import datetime
from datetime import timedelta
from fastapi.responses import JSONResponse
from sr.engine import evaluate

from fastapi import APIRouter, Request

from sr.routes._deps import (
    _conf_cuts,
    _dash,
    _load_tree,
    _signal_from_row,
    _tag_value,
    _tags_brief,
    _tags_payload,
    _traffic_filter,
)

router = APIRouter()


@router.get("/admin/traffic")
async def admin_traffic(request: Request, f: str | None = None, proto: str | None = None,
                        hours: int | None = None, page: int = 1):
    """流量列表（§9.2 形状）：``?f=&proto=&hours=&page=``，每页 50。

    f 五种快捷筛选各一条 WHERE（裁定 4，见 _traffic_filter）；outcome 用库中
    实际值（upstream/block/error）；tags_brief 每行入库序前 3 条。
    """
    conn = db.get(request.app.state.db_path)
    try:
        where: list[str] = []
        params: list = []
        if proto:
            where.append("proto = ?")
            params.append(proto)
        if hours:
            # backlog 修：ts 存本地 ISO（带偏移），曾用字符串比较——夏令时/换时区
            # 下偏移不一，字典序会错。改用 SQLite datetime() 归一到 UTC 再比。
            cutoff = (datetime.now().astimezone()
                      - timedelta(hours=hours)).isoformat(timespec="seconds")
            where.append("datetime(ts) >= datetime(?)")
            params.append(cutoff)
        if f:
            clause, fparams = _traffic_filter(conn, f, request.app.state.db_path)
            where.append(clause)
            params += fparams
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) c FROM requests{wsql}",
                             params).fetchone()["c"]
        page = max(1, page)
        rows = conn.execute(
            f"SELECT * FROM requests{wsql} ORDER BY ts DESC, rowid DESC "
            "LIMIT 50 OFFSET ?", [*params, (page - 1) * 50]).fetchall()
        brief = _tags_brief(conn, [r["id"] for r in rows])
        current_version = db.get_tree_version(conn)   # M2：控制台比对用
    finally:
        conn.close()
    return JSONResponse({"total": total, "tree_version": current_version,
                         "rows": [
        {"id": r["id"], "ts": r["ts"], "proto": r["proto"],
         "model_asked": r["model_asked"], "tags_brief": brief.get(r["id"], []),
         "hit_branch": r["hit_branch"], "routed_to": r["routed_to"],
         "session_tag": r["session_tag"], "outcome": r["outcome"],
         "status": r["status"], "answer_ms": r["answer_ms"],
         "upstream_ms": r["upstream_ms"], "tree_version": r["tree_version"]}
        for r in rows]})

@router.get("/admin/traffic/{rid}")
async def admin_traffic_detail(request: Request, rid: str):
    """请求详情（标签 + 路径）：requests 全列 + tags 全行（入库序）。"""
    conn = db.get(request.app.state.db_path)
    try:
        r = conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        if r is None:
            return JSONResponse({"detail": "request not found"}, status_code=404)
        tags = [{"tag": t["tag"], "value": _tag_value(t["value"]),
                 "confidence": t["confidence"], "is_rule": bool(t["is_rule"])}
                for t in conn.execute("SELECT tag, value, confidence, is_rule "
                                      "FROM tags WHERE request_id = ? "
                                      "ORDER BY rowid", (rid,))]
    finally:
        conn.close()
    return JSONResponse({"id": r["id"], "ts": r["ts"], "proto": r["proto"],
                         "model_asked": r["model_asked"],
                         "hit_branch": r["hit_branch"],
                         "routed_to": r["routed_to"],
                         "session_id": r["session_id"],
                         "session_tag": r["session_tag"],
                         "outcome": r["outcome"], "status": r["status"],
                         "answer_ms": r["answer_ms"],
                         "upstream_ms": r["upstream_ms"], "cost": r["cost"],
                         "tree_version": r["tree_version"],
                         "tags": tags})

@router.post("/admin/traffic/{rid}/replay")
async def admin_traffic_replay(request: Request, rid: str):
    """按当前规则重放（§9 端点表；不调模型，Task 11 补）：
    requests 行 + tags 行重建信号包（_signal_from_row，与 tree/try 的
    request_id 路径同源）→ **当前树** evaluate → 与当时记录的 hit_branch /
    routed_to 对比 → verdict + diff（形状对齐 §9.2：tags/decision 取
    tree/try 口径，was 取 traffic 行字段口径）。

    - verdict 三值：「与当时一致…」（diff={}）/「与当时不一致…」（diff 列
      不一致字段）/「无法重放…」（沿用/解锁行跳过打标不落全量标签，diff=
      null——不冒充一致或不一致）。
    - now 口径：树决定 block → routed_to="block"；hit_path → "N.M" 串，
      与写入路径（裁定 2）同一格式。注意当时 routed_to 是**最终去向**（含
      上游回退/试水），重放只还原树决定——主链成功时两者相等，回退/试水行
      会如实报「不一致」（试水掷签本身不可复现，树有 trial 时同 tree/try）。
    """
    conn = db.get(request.app.state.db_path)
    try:
        r = conn.execute("SELECT * FROM requests WHERE id = ?",
                         (rid,)).fetchone()
        if r is None:
            return JSONResponse({"detail": "request not found"},
                                status_code=404)
        tag_rows = conn.execute(
            "SELECT tag, value, confidence, is_rule FROM tags "
            "WHERE request_id = ? ORDER BY rowid", (rid,)).fetchall()
        cuts = _conf_cuts(conn)
        if not tag_rows:
            return JSONResponse({
                "id": rid, "tags": [],
                "was": {"hit_branch": r["hit_branch"],
                        "routed_to": r["routed_to"]},
                "now": None, "diff": None,
                "verdict": "无法重放：该请求无入库标签"
                           "（会话沿用/解锁行不落全量标签）"})
        signals = {t["tag"]: _signal_from_row(t["tag"], t["value"],
                                              t["confidence"],
                                              t["is_rule"])
                   for t in tag_rows}
        decision = evaluate(_load_tree(conn, request.app.state.db_path), signals, cuts)
    finally:
        conn.close()
    was = {"hit_branch": r["hit_branch"], "routed_to": r["routed_to"]}
    now = {"hit_branch": ".".join(map(str, decision.hit_path))
           if decision.hit_path else None,
           "routed_to": "block" if decision.kind == "block"
           else decision.model}
    diff = {k: {"was": was[k], "now": now[k]}
            for k in ("hit_branch", "routed_to") if was[k] != now[k]}
    if not diff:
        verdict = (f"与当时一致：hit_branch {_dash(was['hit_branch'])}，"
                   f"routed_to {_dash(was['routed_to'])}")
    else:
        verdict = "与当时不一致：" + "；".join(
            f"{k} {_dash(v['was'])}→{_dash(v['now'])}"
            for k, v in diff.items())
    return JSONResponse({
        "id": rid, "tags": _tags_payload(signals, cuts),
        "was": was, "now": now, "diff": diff, "verdict": verdict,
        "decision": {"kind": decision.kind, "model": decision.model,
                     "think": decision.think,
                     "fallback": decision.fallbacks,
                     "source": decision.source,
                     "hit_path": decision.hit_path}})
