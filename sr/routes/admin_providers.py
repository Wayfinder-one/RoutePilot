"""admin providers 端点（D2/A1：自 sr/server.py 机械迁出，零行为变化）。"""

from __future__ import annotations

import json
import sr.db as db
from fastapi import Request
from fastapi.responses import JSONResponse
from sr.treeio import WRITE_ACTOR

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/admin/providers")
async def admin_providers(request: Request, ):
    """供应商列表（providers 表全行；extra JSON 反序列化，ok 转布尔）。"""
    conn = db.get(request.app.state.db_path)
    try:
        rows = conn.execute("SELECT * FROM providers ORDER BY name").fetchall()
    finally:
        conn.close()
    return JSONResponse([{"name": r["name"], "proto": r["proto"],
                          "base_url": r["base_url"], "key_env": r["key_env"],
                          "extra": json.loads(r["extra"] or "{}"),
                          "ok": bool(r["ok"]),
                          "break_reason": r["break_reason"]} for r in rows])

@router.post("/admin/providers")
async def admin_add_provider(request: Request):
    """新建/更新供应商（§9）：接入模型前先有它——base_url + key_env（密钥环境
    变量名，密钥本身不入库）。name 已存在则覆盖更新（含 base_url/key_env/extra），
    并把该供应商名下 models.proto 同步为新 proto（§8.1「proto 跟随 provider」）；
    写 changes 审计。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request body must be a JSON object"},
                            status_code=400)
    name, proto, base_url = body.get("name"), body.get("proto"), body.get("base_url")
    if not (isinstance(name, str) and name.strip()):
        return JSONResponse({"detail": "name is required"}, status_code=400)
    if proto not in ("openai", "anthropic"):
        return JSONResponse(
            {"detail": "proto must be 'openai' or 'anthropic'"}, status_code=400)
    if not (isinstance(base_url, str) and base_url.strip()):
        return JSONResponse({"detail": "base_url is required"}, status_code=400)
    key_env = body.get("key_env")
    if key_env is not None and not isinstance(key_env, str):
        return JSONResponse({"detail": "key_env must be a string or null"},
                            status_code=400)
    extra = body.get("extra") or {}
    if not isinstance(extra, dict):
        return JSONResponse({"detail": "extra must be an object"},
                            status_code=400)
    name, base_url = name.strip(), base_url.strip()
    key_env = (key_env or "").strip() or None
    conn = db.get(request.app.state.db_path)
    try:
        before = conn.execute("SELECT * FROM providers WHERE name = ?",
                              (name,)).fetchone()
        before_json = None if before is None else {
            "name": before["name"], "proto": before["proto"],
            "base_url": before["base_url"], "key_env": before["key_env"],
            "extra": json.loads(before["extra"] or "{}")}
        conn.execute(
            "INSERT INTO providers (name, proto, base_url, key_env, extra) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET proto = excluded.proto, "
            "base_url = excluded.base_url, key_env = excluded.key_env, "
            "extra = excluded.extra",
            (name, proto, base_url, key_env, db._dump(extra)))
        conn.execute("UPDATE models SET proto = ? WHERE provider = ?",
                     (proto, name))          # §8.1 proto 跟随 provider
        after = {"name": name, "proto": proto, "base_url": base_url,
                 "key_env": key_env, "extra": extra}
        db.log_change(conn, WRITE_ACTOR, "providers", name,
                      before=before_json, after=after)
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({**after, "ok": True, "break_reason": None})
