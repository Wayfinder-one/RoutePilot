"""admin config 端点（D2/A1：自 sr/server.py 机械迁出，零行为变化）。"""

from __future__ import annotations

import json
import sr.db as db
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse
from sr import config
from sr.i18n import MESSAGES
from sr.settings import _load_settings
from sr.settings import validate_settings
from sr.treeio import WRITE_ACTOR

from fastapi import APIRouter, Request

from sr.routes._deps import (
    _conf_cuts,
    logger,
)

router = APIRouter()


@router.get("/admin/settings")
async def admin_settings(request: Request, ):
    """全局设置（§3.1 settings 表全键值，值一律字符串）。"""
    conn = db.get(request.app.state.db_path)
    try:
        body = _load_settings(conn)
    finally:
        conn.close()
    return JSONResponse(body)

@router.get("/admin/bands")
async def admin_bands(request: Request, ):
    """标签分档 + 把握度分档（设置页；conf_settings 切点 + tag_settings 各行）。"""
    conn = db.get(request.app.state.db_path)
    try:
        body = {"conf_thresholds": _conf_cuts(conn),
                "tag_thresholds": [
                    {"tag": r["tag"], "thresholds": json.loads(r["thresholds"])}
                    for r in conn.execute("SELECT tag, thresholds FROM tag_settings "
                                          "ORDER BY tag")]}
    finally:
        conn.close()
    return JSONResponse(body)

@router.get("/admin/i18n/{lang}")
async def admin_i18n(request: Request, lang: str):
    """指定语言的翻译表（前端启动时加载，§9.1）。lang ∈ zh|en；其他回落
    zh（裁定 5 二选一取回落：§6.1「默认 zh」，控制台引导永不断链）。"""
    return JSONResponse(MESSAGES.get(lang) or MESSAGES["zh"])

@router.put("/admin/settings")
async def admin_put_settings(request: Request):
    """修改设置（§9）：键集必须与 DEFAULT_SETTINGS 完全一致（多/缺都 400，
    防拼写漂移）；**值类型与值域同校验**（I2：非法值 400，否则一次 200 写坏
    ``session_idle``/``max_retries`` 会让每个 /v1 请求 500 且持久不复原）。
    值一律转字符串（§3.1）。``tree_version`` 系统管理（只随树写操作递增）：
    校验要求在场（GET 原样回传），写库时跳过。校验函数
    :func:`validate_settings` 与 config.import_yaml 同源。"""
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
        errs = validate_settings(body, db.model_ids(conn))
        if errs:
            return JSONResponse({"detail": "; ".join(errs)}, status_code=400)
        before = _load_settings(conn)
        for k, v in body.items():
            if k == "tree_version":
                continue                    # 系统管理键：不收写入口
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)))
        db.log_change(conn, WRITE_ACTOR, "settings", "*", before=before,
                      after=body)
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})

@router.put("/admin/bands")
async def admin_put_bands(request: Request):
    """修改分档（§9）：与 GET /admin/bands 同形状回写（conf 切点 + 各标签
    档位表，只更新 thresholds、不动 keywords）；结构非法 400。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict) \
            or not isinstance(body.get("conf_thresholds"), list) \
            or not isinstance(body.get("tag_thresholds"), list):
        return JSONResponse(
            {"detail": "body must be {conf_thresholds: [...], "
                       "tag_thresholds: [{tag, thresholds}]}"},
            status_code=400)
    cuts = body["conf_thresholds"]
    if not cuts or len(set(cuts)) != len(cuts) or sorted(cuts) != cuts \
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                       and 0 < c < 1 for c in cuts):
        return JSONResponse(
            {"detail": "conf_thresholds must be distinct ascending "
                       "numbers in (0,1)"}, status_code=400)
    tag_rows = []
    for t in body["tag_thresholds"]:
        if not isinstance(t, dict) or not isinstance(t.get("tag"), str) \
                or not isinstance(t.get("thresholds"), list) \
                or not all(isinstance(x, (int, float))
                           and not isinstance(x, bool)
                           for x in t["thresholds"]):
            return JSONResponse(
                {"detail": f"invalid tag_thresholds item: {t!r}"},
                status_code=400)
        tag_rows.append((t["tag"], db._dump(t["thresholds"])))
    conn = db.get(request.app.state.db_path)
    try:
        before = {"conf_thresholds": _conf_cuts(conn),
                  "tag_thresholds": [
                      {"tag": r["tag"], "thresholds": json.loads(r["thresholds"])}
                      for r in conn.execute(
                          "SELECT tag, thresholds FROM tag_settings ORDER BY tag")]}
        conn.execute(
            "INSERT INTO conf_settings (id, thresholds) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET thresholds = excluded.thresholds",
            (db._dump(cuts),))
        for tag_, th in tag_rows:
            conn.execute(
                "INSERT INTO tag_settings (tag, thresholds) VALUES (?, ?) "
                "ON CONFLICT(tag) DO UPDATE SET thresholds = excluded.thresholds",
                (tag_, th))
        db.log_change(conn, WRITE_ACTOR, "bands", "*", before=before,
                      after=body)
        conn.commit()
    finally:
        conn.close()
    # ── M7（audit Major）conf 切点越档告警（审核报告 §2-M7）────────────────
    # 启发式置信度 ≤0.5 保证「永不误命中 conf_min≥2 精细分支」的前提是默认切点
    # [0.30,0.60,0.85]；第二切 ≤0.5 时 0.4/0.5 的启发式信号升入「比较确定」档，
    # §4.6 降级保守保证静默失效。合法配置不拦截，响应附双语 warning + 日志。
    # 单切点（档位数 1）下启发式最高档 1，永不越档，无需告警。
    if len(cuts) >= 2 and cuts[1] <= 0.5:
        warning = {
            "zh": "该切点组合下启发式打标（置信度 ≤ 0.5）可能进入「比较确定」"
                  "档：§4.6 的降级保守保证（永不误命中 conf_min ≥ 2 精细分支）"
                  "会减弱；检查点模式下不受影响。",
            "en": "With these cut points, heuristic tagging (confidence "
                  "<= 0.5) may reach the 'fairly sure' band: the §4.6 "
                  "degraded-mode guarantee (never false-hitting conf_min >= 2 "
                  "refined branches) is weakened. Checkpoint mode is "
                  "unaffected.",
        }
        logger.warning("M7: conf cuts %s weaken the heuristic degraded-mode "
                       "guarantee (§4.6)", cuts)
        return JSONResponse({"ok": True, "warning": warning})
    return JSONResponse({"ok": True})

@router.get("/admin/export")
async def admin_export(request: Request, ):
    """导出 YAML（§9.2：providers/models/tree/settings 四节；pyyaml
    safe_dump，依赖白名单 §13 明列）。核心逻辑在 sr/config.export_yaml
    （Task 11 裁定 1 抽出，CLI ``sr export`` 同源转调），本层只包 HTTP 响应。
    providers/models 只导可编辑配置——ok/break_reason 是熔断运行态，不进
    配置包。"""
    conn = db.get(request.app.state.db_path)
    try:
        text = config.export_yaml(conn)
    finally:
        conn.close()
    return PlainTextResponse(text, media_type="application/x-yaml")

@router.post("/admin/import")
async def admin_import(request: Request):
    """导入 YAML（§9）：normalize 全量校验（四节结构 + 字段/值域/引用）+
    run_checkup，hard 整包拒绝（422，不部分写入）；过则单事务写入
    providers → models → settings → tree（UPSERT，不删既有行）。
    核心逻辑在 sr/config.import_yaml（Task 11 裁定 1 抽出，CLI ``sr import``
    同源转调）：ValueError（YAML/结构/引用非法）→ 400 detail；hard findings
    → 422 findings；成功 → 200 {"ok": true}。I3：结构类异常（TypeError/
    KeyError，如**节存在但值为 null**）同样转 400 并记日志，不让裸 500 逃出。"""
    raw = await request.body()
    conn = db.get(request.app.state.db_path)
    try:
        findings = config.import_yaml(conn, raw)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except (TypeError, KeyError) as exc:      # I3：结构类异常兜底 → 400，不裸 500
        logger.warning("import package structurally invalid: %r", exc)
        return JSONResponse(
            {"detail": f"invalid package: {type(exc).__name__}: {exc}"},
            status_code=400)
    finally:
        conn.close()
    if findings is not None:
        return JSONResponse({"findings": findings}, status_code=422)
    return JSONResponse({"ok": True})
