"""admin tree 端点（D2/A1：自 sr/server.py 机械迁出，零行为变化）。"""

from __future__ import annotations

import copy
import json
import sr.db as db
import sr.eval as eval_mod
import time
import uuid
from datetime import datetime
from datetime import timedelta
from fastapi import Request
from fastapi.responses import JSONResponse
from sr.engine import TEMPLATES
from sr.engine import evaluate
from sr.engine import evaluate_trace
from sr.health import run_checkup_safe
from sr.tagger import TagRequest
from sr.tags import TAGS
from sr.treeio import WRITE_ACTOR
from sr.treeio import _idx_conflict
from sr.treeio import _node_from_json
from sr.treeio import _renumber
from sr.treeio import _tree_from_json
from sr.treeio import _tree_full_422
from sr.treeio import _tree_json
from sr.treeio import _write_tree_rows

from fastapi import APIRouter, Request

from sr.routes._deps import (
    _checkup_or_422,
    _conf_cuts,
    _context_thresholds,
    _est_cost,
    _hits_30d,
    _load_tree,
    _signal_from_row,
    _tags_payload,
)

router = APIRouter()


@router.get("/admin/tree")
async def admin_tree(request: Request, ):
    """分流树（§9.2 首块形状；rows_to_tree 逆组装，cond/action 原样透出）。"""
    conn = db.get(request.app.state.db_path)
    try:
        body = _tree_json(_load_tree(conn, request.app.state.db_path), db.get_tree_version(conn))
    finally:
        conn.close()
    return JSONResponse(body)

@router.put("/admin/tree")
async def admin_put_tree(request: Request):
    """整树替换（§9/§10）：请求体 = GET /admin/tree 同形状 → 体检（hard
    422 拦截，findings 硬错误优先）→ 过则事务内 DELETE+INSERT 整树 +
    ``tree_version+1`` + changes 审计。写侧与 Task 9 读侧同样零剥键，
    GET→PUT 回环无损（含 kids 子分支行）。"""
    try:
        body = await request.json()
        nodes = _tree_from_json(body)
    except Exception as exc:                   # JSON 或形状非法 → 400
        return JSONResponse({"detail": f"invalid tree payload: {exc}"},
                            status_code=400)
    conn = db.get(request.app.state.db_path)
    try:
        blocked = _checkup_or_422(conn, nodes)
        if blocked is not None:
            return blocked
        old = _tree_json(_load_tree(conn, request.app.state.db_path), db.get_tree_version(conn))
        _write_tree_rows(conn, nodes)
        db.bump_tree_version(conn)
        db.log_change(conn, WRITE_ACTOR, "tree", "*",
                      before={"version": old["version"], "nodes": old["nodes"]},
                      after={"nodes": body["nodes"]})
        conn.commit()
        version = db.get_tree_version(conn)
    finally:
        conn.close()
    return JSONResponse(_tree_json(nodes, version))

@router.post("/admin/tree/audit")
async def admin_tree_audit(request: Request, ):
    """体检当前树、不写库（§9）：findings 按 §10 CHECKS 顺序全量返回。"""
    conn = db.get(request.app.state.db_path)
    try:
        findings = run_checkup_safe(_load_tree(conn, request.app.state.db_path), db.model_ids(conn),
                                    TAGS, hits=_hits_30d(conn),
                                    estimates=eval_mod.estimate_deviations(conn))
    finally:
        conn.close()
    return JSONResponse({"findings": [f.to_json() for f in findings]})

@router.post("/admin/tree/try")
async def admin_tree_try(request: Request):
    """试算（§9.2）：``{text}`` 现打标或 ``{request_id}`` 取入库信号包重放；
    复用注入 tagger.tag + evaluate_trace（§5.2 试算形状），不调上游。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request body must be a JSON object"},
                            status_code=400)
    conn = db.get(request.app.state.db_path)
    try:
        cuts = _conf_cuts(conn)
        rid = body.get("request_id")
        if rid is not None:
            rows = conn.execute(
                "SELECT tag, value, confidence, is_rule FROM tags "
                "WHERE request_id = ? ORDER BY rowid", (rid,)).fetchall()
            if not rows:
                return JSONResponse({"detail": "request not found"},
                                    status_code=404)
            signals = {r["tag"]: _signal_from_row(r["tag"], r["value"],
                                                  r["confidence"],
                                                  r["is_rule"])
                       for r in rows}
            answer_ms = 0.0                  # 重放无打标耗时（§9.2 answer_ms）
        else:
            text = body.get("text")
            if not isinstance(text, str):
                return JSONResponse(
                    {"detail": "body requires \"text\" or \"request_id\""},
                    status_code=400)
            t1 = time.monotonic()
            # M8：试算现打标与 /v1 主链同一注入口径（否则 try 展示的 context
            # 档与真实路由不一致）。A2：同一注入 tagger（async）。
            try_result = await request.app.state.tagger.tag(TagRequest(
                text=text, headers=dict(request.headers), n_messages=1,
                now=datetime.now().astimezone(), last_msg=None,
                context_thresholds=_context_thresholds(conn)))
            signals = try_result.signals
            answer_ms = (time.monotonic() - t1) * 1000
        trace = evaluate_trace(_load_tree(conn, request.app.state.db_path), signals, cuts)
    finally:
        conn.close()
    d = trace["decision"]
    return JSONResponse({
        "tagger": request.app.state.tagger.name,
        "tags": _tags_payload(signals, cuts),
        "path": trace["path"], "also_match": trace["also_match"],
        "decision": {"kind": d.kind, "model": d.model, "think": d.think,
                     "fallback": d.fallbacks, "source": d.source,
                     "hit_path": d.hit_path},
        "answer_ms": round(answer_ms, 1)})

@router.post("/admin/tree/estimate")
async def admin_tree_estimate(request: Request):
    """影响预估（§9.2）：近 N 天入库标签重放「现树 vs 现树在 pos 插候选」。

    - 样本 = 窗口内 requests×tags 信号包（沿用/直通行无全量标签，天然缺席）；
    - 改道 = 候选节点成为新树命中点（hit_path 首位 == 候选）；
    - from_fallback = 改道样本的原去向是兜底（命中兜底节点或防御性
      fallback），from_other = 其余改道；
    - cost_delta = Σ(新成本 − 原成本)，成本按 models.price_in/out ×
      AVG_IN=800 / AVG_OUT=600 折算（均摊假设，常量注释见模块头）；
      任一涉及模型价格缺失 → null（§9.2）。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request body must be a JSON object"},
                            status_code=400)
    try:
        cand = _node_from_json(body.get("node"), require_idx=False, seen=set())
    except ValueError as exc:
        return JSONResponse({"detail": f"invalid candidate node: {exc}"},
                            status_code=400)
    pos, days = body.get("pos"), body.get("days", 7)
    if not isinstance(pos, int) or isinstance(pos, bool) \
            or not isinstance(days, int) or isinstance(days, bool) or days < 1:
        return JSONResponse(
            {"detail": "pos must be an integer and days a positive integer"},
            status_code=400)
    conn = db.get(request.app.state.db_path)
    try:
        nodes = _load_tree(conn, request.app.state.db_path)
        if not 1 <= pos <= len(nodes):
            return JSONResponse(
                {"detail": f"pos out of range 1..{len(nodes)}"}, status_code=400)
        cand.idx = 10 ** 6 + pos            # 临时 idx：不与现树冲突、仅定位用
        cand_nodes = list(nodes[:pos]) + [cand] + list(nodes[pos:])
        cuts = _conf_cuts(conn)
        prices = {r["id"]: (r["price_in"], r["price_out"])
                  for r in conn.execute(
                      "SELECT id, price_in, price_out FROM models")}
        cutoff = (datetime.now().astimezone()
                  - timedelta(days=days)).isoformat(timespec="seconds")
        packs: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT t.request_id rid, t.tag, t.value, t.confidence, "
                "t.is_rule FROM tags t JOIN requests r ON r.id = t.request_id "
                "WHERE r.ts >= ? ORDER BY t.rowid", (cutoff,)):
            packs.setdefault(r["rid"], {})[r["tag"]] = _signal_from_row(
                r["tag"], r["value"], r["confidence"], r["is_rule"])
        fb_idx = {n.idx for n in nodes if n.kind == "fallback"}
        total = diverted = from_fb = 0
        cost_delta = 0.0
        price_missing = False
        for signals in packs.values():
            total += 1
            base = evaluate(nodes, signals, cuts)
            new = evaluate(cand_nodes, signals, cuts)
            if not new.hit_path or new.hit_path[0] != cand.idx:
                continue                    # 候选节点没抢到流量
            diverted += 1
            if base.source == "fallback" or \
                    (base.hit_path and base.hit_path[0] in fb_idx):
                from_fb += 1                # 原去向是兜底（节点或防御决定）
            c0, miss0 = _est_cost(base.model, prices)
            c1, miss1 = _est_cost(new.model, prices)
            price_missing = price_missing or miss0 or miss1
            if c0 is not None and c1 is not None:
                cost_delta += c1 - c0
    finally:
        conn.close()
    return JSONResponse({"sample_days": days,
                         "daily": round(total / days, 2),
                         "ratio": round(diverted / total, 4) if total else 0.0,
                         "from_fallback": from_fb,
                         "from_other": diverted - from_fb,
                         "cost_delta": None if price_missing
                         else round(cost_delta, 6)})

@router.get("/admin/tree/estimates")
async def admin_tree_estimates(request: Request, ):
    """预估列表（§11.4 复盘）：estimates 全行，actual NULL = 未复盘。

    audit step 3：**读端点不再有副作用**——旧实现在 GET 里惰性 `backfill_estimates`
    （GET 写库：审计说不清、并发下重复触发）。回填改为显式
    `POST /admin/tree/estimates/backfill`（运维/定时器调用）。
    行里带 `tree_version`：预估基于哪一版树算的，复盘对比时能对上版本。
    """
    conn = db.get(request.app.state.db_path)
    try:
        rows = conn.execute("SELECT * FROM estimates "
                            "ORDER BY ts DESC, rowid DESC").fetchall()
        err = eval_mod.estimate_error(conn)
    finally:
        conn.close()
    if err is not None:
        request.app.state.metrics.set("sr_estimate_error", err)
    return JSONResponse([{"id": r["id"], "ts": r["ts"], "node": r["node"],
                          "params": json.loads(r["params"]),
                          "est": json.loads(r["est"]),
                          "tree_version": r["tree_version"],
                          "actual": json.loads(r["actual"]) if r["actual"]
                          else None} for r in rows])


@router.post("/admin/tree/estimates/backfill")
async def admin_tree_estimates_backfill(request: Request):
    """显式回填预估复盘（§11.4）：ts 满 7 天的未复盘行按真实流量算 actual，幂等。

    从 GET 挪出来的写路径（audit step 3）：返回本次回填行数 + 最新偏差 gauge。
    """
    conn = db.get(request.app.state.db_path)
    try:
        n = eval_mod.backfill_estimates(conn)
        if n:
            conn.commit()
        err = eval_mod.estimate_error(conn)
    finally:
        conn.close()
    if err is not None:
        request.app.state.metrics.set("sr_estimate_error", err)
    return JSONResponse({"backfilled": n, "estimate_error": err})

@router.post("/admin/tree/nodes")
async def admin_tree_add_node(request: Request):
    """新增节点（§9）：候选插到求值位置 pos。树表以 idx 为序（§3.1），要使
    「插在 pos」对求值顺序真实生效必须重编后继节点 idx——否则预估口径与实际
    求值顺序脱节；代价是 requests.hit_branch 历史行失配（M1 取舍，记任务
    报告）。sticky 节点必须保持 idx 0（rows_to_tree 按 idx==0 推导），故
    pos ≥ 1。带 estimate 时写 estimates 表快照（§9.2 末段 / §11.4）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request body must be a JSON object"},
                            status_code=400)
    try:
        cand = _node_from_json(body.get("node"), require_idx=False, seen=set())
    except ValueError as exc:
        return JSONResponse({"detail": f"invalid node: {exc}"}, status_code=400)
    pos = body.get("pos")
    if not isinstance(pos, int) or isinstance(pos, bool):
        return JSONResponse({"detail": "pos must be an integer"}, status_code=400)
    conn = db.get(request.app.state.db_path)
    try:
        nodes = sorted(_load_tree(conn, request.app.state.db_path), key=lambda n: n.idx)
        if not 1 <= pos <= len(nodes):
            return JSONResponse(
                {"detail": f"pos out of range 1..{len(nodes)}"}, status_code=400)
        if len(nodes) >= 10:
            return _tree_full_422()      # 节点号 ≥10 必撞节点 0 的 100+ 段
        # _renumber 原地改写 idx；nodes 是树缓存（裁定 4）共享的 Node/Branch
        # 对象，直接重编会在「重编后 422/异常」路径把重编号幻影树留在缓存里
        # （GET 回读失真）——深拷贝后再重编，缓存对象零触碰。cand 是请求级
        # 新对象，插进副本原地重编，下方 estimates/审计仍读 cand.idx。
        pool = copy.deepcopy(nodes)
        pool[pos:pos] = [cand]
        _renumber(pool)
        new_nodes = pool
        conflict = _idx_conflict(new_nodes)
        if conflict is not None:
            return _tree_full_422(conflict)   # 修复前：IntegrityError → 500
        blocked = _checkup_or_422(conn, new_nodes)
        if blocked is not None:
            return blocked
        old = _tree_json(nodes, db.get_tree_version(conn))
        _write_tree_rows(conn, new_nodes)
        db.bump_tree_version(conn)
        if "estimate" in body:
            if not isinstance(body["estimate"], dict):
                return JSONResponse(
                    {"detail": "estimate must be the object returned by "
                               "POST /admin/tree/estimate"}, status_code=400)
            conn.execute(
                "INSERT INTO estimates (id, ts, node, params, est, actual, "
                "tree_version) VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (uuid.uuid4().hex, db._now(), str(cand.idx),
                 db._dump(body["node"]), db._dump(body["estimate"]),
                 db.get_tree_version(conn)))
        db.log_change(conn, WRITE_ACTOR, "tree", str(cand.idx),
                      before={"version": old["version"], "nodes": old["nodes"]},
                      after={"pos": pos, "node": body.get("node")})
        conn.commit()
        version = db.get_tree_version(conn)
    finally:
        conn.close()
    return JSONResponse(_tree_json(new_nodes, version))

@router.delete("/admin/tree/nodes/{idx}")
async def admin_tree_delete_node(request: Request, idx: int):
    """删除节点（§9）：连带其分支行；删后体检，hard 拦截（如删兜底 → 422）。"""
    conn = db.get(request.app.state.db_path)
    try:
        nodes = sorted(_load_tree(conn, request.app.state.db_path), key=lambda n: n.idx)
        victim = next((n for n in nodes if n.idx == idx), None)
        if victim is None:
            return JSONResponse({"detail": f"node {idx} not found"},
                                status_code=404)
        rest = [n for n in nodes if n is not victim]
        blocked = _checkup_or_422(conn, rest)
        if blocked is not None:
            return blocked
        old = _tree_json(nodes, db.get_tree_version(conn))
        _write_tree_rows(conn, rest)
        db.bump_tree_version(conn)
        db.log_change(conn, WRITE_ACTOR, "tree", str(idx),
                      before={"version": old["version"], "nodes": old["nodes"]},
                      after=None)
        conn.commit()
        version = db.get_tree_version(conn)
    finally:
        conn.close()
    return JSONResponse(_tree_json(rest, version))

@router.post("/admin/tree/template")
async def admin_tree_template(request: Request):
    """切换模板（§5.3）：TEMPLATES[name] 整树替换 + version+1 + 审计；
    未知模板名 400。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    name = body.get("name") if isinstance(body, dict) else None
    if name not in TEMPLATES:
        return JSONResponse(
            {"detail": f"unknown template: {name!r} (known: {sorted(TEMPLATES)})"},
            status_code=400)
    conn = db.get(request.app.state.db_path)
    try:
        old = _tree_json(_load_tree(conn, request.app.state.db_path), db.get_tree_version(conn))
        conn.execute("DELETE FROM tree")
        for node in TEMPLATES[name]:
            conn.execute(
                "INSERT INTO tree (idx, parent, name, desc, cond, action, "
                "trial, enabled) VALUES (?, NULL, ?, ?, '[]', 'null', NULL, 1)",
                (node["idx"], node["name"], node.get("desc")))
            for branch in node.get("branches", []):
                db._seed_branch(conn, node["idx"], branch)   # 与 init_db 同一条落库路径
        db.bump_tree_version(conn)
        db.log_change(conn, WRITE_ACTOR, "tree", "template",
                      before={"version": old["version"], "nodes": old["nodes"]},
                      after={"template": name})
        conn.commit()
        version = db.get_tree_version(conn)
    finally:
        conn.close()
    return JSONResponse({"version": version, "template": name})
