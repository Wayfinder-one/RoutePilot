"""admin models 端点（D2/A1：自 sr/server.py 机械迁出，零行为变化）。"""

from __future__ import annotations

import asyncio
import httpx
import json
import os
import sr.db as db
import time
from fastapi import Request
from fastapi.responses import JSONResponse
from sr.models import reset_circuit
from sr.settings import _load_settings
from sr.treeio import WRITE_ACTOR

from fastapi import APIRouter, Request

from sr.routes._deps import (
    STREAM_FIRST_BYTE_S,
)

router = APIRouter()


@router.get("/admin/models")
async def admin_models(request: Request, ):
    """模型列表（含阶梯、熔断状态，§8 表）：caps/aliases 反序列化、
    ok/enabled 转布尔、on_ladder = 是否在 settings.ladder 阶梯上。"""
    conn = db.get(request.app.state.db_path)
    try:
        ladder = [s.strip() for s in
                  (_load_settings(conn).get("ladder") or "").split(",")
                  if s.strip()]
        rows = conn.execute("SELECT * FROM models ORDER BY id").fetchall()
    finally:
        conn.close()
    return JSONResponse([{"id": r["id"], "provider": r["provider"],
                          "proto": r["proto"], "upstream": r["upstream"],
                          "caps": json.loads(r["caps"] or "[]"),
                          "aliases": json.loads(r["aliases"] or "[]"),
                          "step": r["step"], "price_in": r["price_in"],
                          "price_out": r["price_out"], "ok": bool(r["ok"]),
                          "break_reason": r["break_reason"],
                          "enabled": bool(r["enabled"]),
                          "on_ladder": r["id"] in ladder} for r in rows])

@router.post("/admin/models")
async def admin_add_model(request: Request):
    """接入新模型（§9）：proto 跟随 provider（§8.1）；provider 不存在 400、
    id 已存在 409；写 changes 审计。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "request body must be a JSON object"},
                            status_code=400)
    mid, provider, upstream = body.get("id"), body.get("provider"), \
        body.get("upstream")
    if not (isinstance(mid, str) and mid and isinstance(provider, str)
            and isinstance(upstream, str) and upstream):
        return JSONResponse(
            {"detail": "id/provider/upstream are required strings"},
            status_code=400)
    caps, aliases = body.get("caps", []), body.get("aliases", [])
    if not isinstance(caps, list) or not isinstance(aliases, list):
        return JSONResponse({"detail": "caps/aliases must be lists"},
                            status_code=400)
    step = body.get("step")
    if step is not None and (not isinstance(step, int) or isinstance(step, bool)):
        return JSONResponse({"detail": "step must be an integer or null"},
                            status_code=400)
    price_in, price_out = body.get("price_in"), body.get("price_out")
    for p in (price_in, price_out):
        if (p is not None and not isinstance(p, (int, float))) \
                or isinstance(p, bool):
            return JSONResponse(
                {"detail": "price_in/price_out must be numbers or null"},
                status_code=400)
    enabled = bool(body.get("enabled", True))
    conn = db.get(request.app.state.db_path)
    try:
        prov = conn.execute("SELECT proto FROM providers WHERE name = ?",
                            (provider,)).fetchone()
        if prov is None:
            return JSONResponse({"detail": f"unknown provider: {provider!r}"},
                                status_code=400)
        if conn.execute("SELECT 1 FROM models WHERE id = ?", (mid,)).fetchone():
            return JSONResponse({"detail": f"model exists: {mid}"},
                                status_code=409)
        conn.execute(
            "INSERT INTO models (id, provider, proto, upstream, caps, aliases, "
            "step, price_in, price_out, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, provider, prov["proto"], upstream, db._dump(caps),
             db._dump(aliases), step, price_in, price_out,
             1 if enabled else 0))
        db.log_change(conn, WRITE_ACTOR, "models", mid, before=None,
                      after={"id": mid, "provider": provider,
                             "upstream": upstream, "caps": caps,
                             "aliases": aliases, "step": step,
                             "price_in": price_in, "price_out": price_out,
                             "enabled": enabled})
        ladder = [s.strip() for s in
                  (_load_settings(conn).get("ladder") or "").split(",")
                  if s.strip()]
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"id": mid, "provider": provider,
                         "proto": prov["proto"], "upstream": upstream,
                         "caps": caps, "aliases": aliases, "step": step,
                         "price_in": price_in, "price_out": price_out,
                         "ok": True, "break_reason": None,
                         "enabled": enabled, "on_ladder": mid in ladder})

@router.put("/admin/models/{mid}")
async def admin_update_model(mid: str, request: Request):
    """修改模型（§9：阶梯/启用/停用等）：白名单字段部分更新 + 审计；
    未知字段 400、未知模型 404。ok/break_reason 入白名单（M6）：管理员可
    手动置熔断展示态——注意只写库，内存 _FAIL_COUNTS 请走 reset 端点清。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "request body is not valid JSON"},
                            status_code=400)
    allowed = {"upstream", "caps", "aliases", "step",
               "price_in", "price_out", "enabled", "ok", "break_reason"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        return JSONResponse(
            {"detail": f"updatable fields: {sorted(allowed)}"}, status_code=400)
    if not isinstance(body.get("caps", []), list) \
            or not isinstance(body.get("aliases", []), list):
        return JSONResponse({"detail": "caps/aliases must be lists"},
                            status_code=400)
    if "step" in body and (not isinstance(body["step"], int)
                           or isinstance(body["step"], bool)) \
            and body["step"] is not None:
        return JSONResponse({"detail": "step must be an integer or null"},
                            status_code=400)
    for k in ("price_in", "price_out"):
        if k in body and ((not isinstance(body[k], (int, float))
                           and body[k] is not None)
                          or isinstance(body[k], bool)):
            return JSONResponse(
                {"detail": f"{k} must be a number or null"}, status_code=400)
    # M6：ok 只许 0/1（JSON true 是 int 子类，显式拒 bool）；break_reason
    # 只许字符串或 null。
    if "ok" in body and (not isinstance(body["ok"], int)
                         or isinstance(body["ok"], bool)
                         or body["ok"] not in (0, 1)):
        return JSONResponse({"detail": "ok must be 0 or 1"}, status_code=400)
    if "break_reason" in body and body["break_reason"] is not None \
            and not isinstance(body["break_reason"], str):
        return JSONResponse(
            {"detail": "break_reason must be a string or null"}, status_code=400)
    conn = db.get(request.app.state.db_path)
    try:
        row = conn.execute("SELECT * FROM models WHERE id = ?", (mid,)).fetchone()
        if row is None:
            return JSONResponse({"detail": f"model not found: {mid}"},
                                status_code=404)
        before = {k: row[k] for k in row.keys()}
        sets, params = [], []
        for k, v in body.items():
            if k in ("caps", "aliases"):
                v = db._dump(v)
            elif k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            params.append(v)
        conn.execute(f"UPDATE models SET {', '.join(sets)} WHERE id = ?",
                     [*params, mid])
        db.log_change(conn, WRITE_ACTOR, "models", mid, before=before,
                      after={**before, **body})
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})

@router.post("/admin/models/{mid}/reset")
async def admin_reset_model(request: Request, mid: str):
    """熔断复位（M6，audit Major：原 ≥3 次失败后进程内永久隔离，唯一恢复
    途径是重启进程）。清内存连续失败计数 + 恢复 models.ok=1 / break_reason=
    NULL——展示与行为一并复位（半开探测定时器仍留 M3）；写 changes 审计；
    模型不存在 404。"""
    conn = db.get(request.app.state.db_path)
    try:
        row = conn.execute("SELECT ok, break_reason FROM models WHERE id = ?",
                           (mid,)).fetchone()
        if row is None:
            return JSONResponse({"detail": f"model not found: {mid}"},
                                status_code=404)
        before = {"ok": bool(row["ok"]), "break_reason": row["break_reason"]}
        reset_circuit(conn, mid)
        db.log_change(conn, WRITE_ACTOR, "models", mid, before=before,
                      after={"ok": True, "break_reason": None})
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"id": mid, "ok": True})

@router.post("/admin/models/{mid}/test")
async def admin_model_test(request: Request, mid: str):
    """接入测试（§9.4 两步探测，设计方案 B5）：

    ① ``connect``——非流式连通：openai → ``GET {base}/models``；
       anthropic → 最小 ``POST /v1/messages``；
    ② ``stream_first_byte``——发 ``stream: true`` 最小请求，断言
       :data:`STREAM_FIRST_BYTE_S` 秒内收到第一个数据帧。

    逐步返回 ``{name, ok, ms, detail}``；上游不可达/超时 → 该步 ok=false
    （整体 ``ok`` 为 false，HTTP 仍 200——探测失败是结论不是故障）。key_env
    未设置时不带鉴权头（对真上游会 401 → ok=false，也是有效探测结论）。
    """
    conn = db.get(request.app.state.db_path)
    try:
        model = conn.execute("SELECT * FROM models WHERE id = ?",
                             (mid,)).fetchone()
        if model is None:
            return JSONResponse({"detail": f"model not found: {mid}"},
                                status_code=404)
        prov = conn.execute("SELECT * FROM providers WHERE name = ?",
                            (model["provider"],)).fetchone()
    finally:
        conn.close()
    if prov is None:
        return JSONResponse(
            {"detail": f"provider missing: {model['provider']}"}, status_code=400)
    key = os.environ.get(prov["key_env"] or "")
    proto = prov["proto"]
    headers: dict = {}
    if key:
        headers = ({"Authorization": f"Bearer {key}"} if proto == "openai"
                   else {"x-api-key": key,
                         "anthropic-version": "2023-06-01"})
    base = prov["base_url"].rstrip("/")
    steps: list[dict] = []
    async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        # ── step1 非流式连通（§9.4-1）──
        t0 = time.monotonic()
        try:
            if proto == "openai":
                url = f"{base}/models"
                resp = await client.get(url, headers=headers)
            else:
                url = f"{base}/v1/messages"
                resp = await client.post(url, headers=headers, json={
                    "model": model["upstream"], "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}]})
            steps.append({"name": "connect",
                          "ok": 200 <= resp.status_code < 300,
                          "ms": round((time.monotonic() - t0) * 1000, 1),
                          "detail": f"HTTP {resp.status_code} {url}"})
        except httpx.HTTPError as exc:
            steps.append({"name": "connect", "ok": False,
                          "ms": round((time.monotonic() - t0) * 1000, 1),
                          "detail": f"unreachable: {exc}"})
        # ── step2 流式首包（§9.4-2：3s 内第一个数据帧）──
        t0 = time.monotonic()
        url, payload = \
            (f"{base}/chat/completions", {"model": model["upstream"],
                                          "stream": True, "max_tokens": 1,
                                          "messages": [{"role": "user",
                                                        "content": "ping"}]}) \
            if proto == "openai" else \
            (f"{base}/v1/messages", {"model": model["upstream"],
                                     "stream": True, "max_tokens": 1,
                                     "messages": [{"role": "user",
                                                   "content": "ping"}]})
        resp = None
        try:
            req = client.build_request("POST", url, json=payload,
                                       headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code != 200:
                steps.append({"name": "stream_first_byte", "ok": False,
                              "ms": round((time.monotonic() - t0) * 1000, 1),
                              "detail": f"HTTP {resp.status_code} {url}"})
            else:
                first = await asyncio.wait_for(
                    resp.aiter_bytes().__anext__(),
                    timeout=STREAM_FIRST_BYTE_S)
                steps.append({"name": "stream_first_byte", "ok": bool(first),
                              "ms": round((time.monotonic() - t0) * 1000, 1),
                              "detail": f"first frame {len(first)} bytes"})
        except asyncio.TimeoutError:
            steps.append({"name": "stream_first_byte", "ok": False,
                          "ms": round((time.monotonic() - t0) * 1000, 1),
                          "detail": f"no first frame within "
                                    f"{STREAM_FIRST_BYTE_S}s"})
        except (httpx.HTTPError, StopAsyncIteration) as exc:
            steps.append({"name": "stream_first_byte", "ok": False,
                          "ms": round((time.monotonic() - t0) * 1000, 1),
                          "detail": f"unreachable/empty: {exc}"})
        finally:
            if resp is not None:
                await resp.aclose()
    return JSONResponse({"ok": all(s["ok"] for s in steps), "steps": steps})
