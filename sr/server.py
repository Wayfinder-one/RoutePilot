"""sr/server.py —— /v1 双协议路由主流程 + admin 读/写端点 + /metrics
（spec §7.1 端点 / §7.2 十二步 / §7.5 流式红线 / §9.1-9.2 / §10 / §11.1）。

A1（audit §3）拆分后：本文件只留「入口职责」——create_app 装配 + _handle 主流程 +
admin 端点。领域逻辑已分居：
- sr/metrics.py      指标（Metrics/_bump_request_metrics/_is_fallback）
- sr/settings.py     settings 读写与值校验（_load_settings/validate_settings）
- sr/treeio.py       树 JSON 双向序列化/重编号/落库（_tree_json/_tree_from_json/…）
- sr/upstream.py     上游调用/流式管道（_call_nonstream/_call_stream/_relay_stream）
- sr/adapters/cross.py  非流式跨协议经 IRResponse 中转（A3）
- sr/adapters/registry.py  协议注册表 ADAPTERS（A3，server/upstream/cross 共用）
拆分后 config.py 不再反向 import 本模块（依赖方向倒挂已消除）。

模块职责（集成 Task 1-7 全部模块，spec §7.2 十二步逐项对齐，对照表见任务报告）：
- :func:`create_app` —— 工厂：``POST /v1/chat/completions``（OpenAI）、
  ``POST /v1/messages``（Anthropic）、``GET /v1/models``（auto + 全部别名，§7.1）；
  admin 读端点（§9.1 引导序列七 GET + ``/admin/traffic/{id}``）、``GET /metrics``
  （§11.1 手工 OpenMetrics）；admin 写端点（tree 整树替换/新增/删除/审计/试算/
  预估/模板、models 接入/修改/探测、settings/bands、export/import，§9 表 + §9.4
  探测序列）；控制台托管 ``GET /`` 归 Task 12。
- :func:`_handle` —— 十二步主流程：协议解析 → IR → 主文本 → model 判定
  （auto 走树 / 具体名或别名直通 R2 / 其他 400）→ 会话沿用 → 打标 → 走树 →
  供应商解析 → 上游调用 → 入站名回写 → requests+tags 入库 → 诊断响应头。
- admin 读端点 —— §9.2 形状逐字：tree（rows_to_tree 逆组装，cond/action 原样
  透出规范键）、providers/models/settings/bands、traffic（f 五种快捷筛选各一条
  WHERE + 分页 50）、traffic/{id}（详情 + 全量标签）、i18n/{lang}（sr.i18n.MESSAGES）。

约定：每请求自开 sqlite 连接（db.get，WAL），事务由本模块 commit 后关闭；
树缓存为模块级 dict（裁定 4），每请求开头 SELECT tree_version，变了才重载。
"""

from __future__ import annotations

from __future__ import annotations
import asyncio
import copy
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (JSONResponse, PlainTextResponse, FileResponse,
                               StreamingResponse)
import sr.db as db
from sr import config
from sr.adapters import UpstreamError
from sr.adapters.cross import from_upstream as _cross_from_upstream
from sr.engine import (Branch, Node, RouteDecision, TEMPLATES, evaluate,
                       evaluate_trace, extract_session, resolve_session,
                       rows_to_tree)
import sr.eval as eval_mod
from sr.health import run_checkup_safe
from sr.i18n import MESSAGES, detect_lang
from sr.metrics import Metrics, _bump_request_metrics, _is_fallback
from sr.models import reset_circuit
from sr.settings import _load_settings, validate_settings
from sr.tags import (DEFAULT_CONF_THRESHOLDS, DEFAULT_CONTEXT_THRESHOLDS,
                     TAGS, conf_band)
from sr.tagger import Tagger, TagRequest, TagResult
from sr.treeio import (WRITE_ACTOR, _branch_from_json, _idx_conflict,
                       _node_from_json, _renumber, _row_idxs, _take_idx,
                       _tree_from_json, _tree_full_422, _tree_json,
                       _write_tree_rows)
from sr.upstream import (ADAPTERS, _call_nonstream, _call_stream,
                         _relay_stream)

# D2/A1：辅助函数/常量已迁至 sr/routes/_deps.py；star-import 让 _handle 的
# 引用保持零改动（_deps 末尾 __all__ 动态列出全部顶层名字）。
from sr.routes._deps import *  # noqa: F401,F403
from sr.routes import ROUTERS

async def _handle(proto: str, request: Request, db_path: str, tagger: Tagger,
                  metrics: Metrics):
    """``POST /v1/chat/completions`` / ``POST /v1/messages`` 共用主流程。

    打标经注入的 :class:`sr.tagger.Tagger`（A2，审核报告 §3-A2）：
    ``result = await tagger.tag(TagRequest(...))``，signals 形状与旧 tag_all
    完全一致；``tag_result.vec`` 非 None（M2 checkpoint 嵌入头）时在会话
    写库路径落 sessions.last_vec。
    """
    conn = None
    try:
        # ── 1–2 按路径判定协议 + 解析请求体 → IR ──
        try:
            body = await request.json()
        except Exception:
            return _error(proto, 400, "request body is not valid JSON",
                          "invalid_request_error", "invalid_json",
                          {"x-sr-tagger": tagger.name})
        if not isinstance(body, dict):
            return _error(proto, 400, "request body must be a JSON object",
                          "invalid_request_error", "invalid_json",
                          {"x-sr-tagger": tagger.name})
        ir = ADAPTERS[proto].parse(body)
        if proto == "anthropic" and ir.params.get("max_tokens") is None:
            # anthropic 客户端 max_tokens 必填，缺失兜底 1024（裁定 9，声明记任务报告）
            ir.params["max_tokens"] = DEFAULT_MAX_TOKENS

        # ── 3 提取主文本（最后一条 user 消息） ──
        text = ADAPTERS[proto].main_text(ir)
        asked = ir.model_asked

        conn = db.get(db_path)
        settings = _load_settings(conn)
        nodes = _load_tree(conn, db_path)
        tree_version = db.get_tree_version(conn)     # M2：落 requests.tree_version
        cuts = _conf_cuts(conn)
        ctx_thresholds = _context_thresholds(conn)   # M8：tag_settings context 行
        req_headers = dict(request.headers)
        accept_lang = request.headers.get("accept-language")
        session_id: str | None = None
        session_tag = "new"
        answer_ms = 0.0
        signals: dict | None = None
        tag_result: TagResult | None = None   # A2：注入打标器的原始产物（vec 通道）
        hit_branch: str | None = None

        # ── 4 model 判定：auto → 树；具体名/别名 → 直通 R2；其他 → 400 ──
        if asked == "auto":
            mode = "tree"
        else:
            row = _lookup_model(conn, str(asked))
            if row is None or not row["enabled"]:
                return _error(proto, 400, f"unknown model: {asked}",
                              "invalid_request_error", "model_not_found",
                              {"x-sr-tagger": tagger.name})
            mode = "direct"

        if mode == "direct":
            # R2：显式模型一律直通——不参与沿用、不走树，但仍打标入库（§4.4 末段）
            decision = RouteDecision(kind="route", model=row["id"], source="direct")
            session_id = extract_session(
                proto, body, req_headers,
                request.client.host if request.client else "",
                sticky_enabled=(settings.get("session_sticky") == "1"))
            t1 = time.monotonic()
            tag_result = await tagger.tag(TagRequest(
                text=text, headers=req_headers, n_messages=len(ir.messages),
                now=datetime.now().astimezone(),
                last_msg=_last_msg(conn, session_id),
                last_vec=_last_vec(conn, session_id),      # B2：上轮嵌入（similarity/reask）
                context_thresholds=ctx_thresholds))
            signals = tag_result.signals
            answer_ms = (time.monotonic() - t1) * 1000
            metrics.observe("sr_answer_latency_seconds", {}, answer_ms / 1000.0,
                                 conn=conn)
        else:
            # ── 5 查会话沿用（§4.4）──
            session_id = extract_session(
                proto, body, req_headers,
                request.client.host if request.client else "",
                sticky_enabled=(settings.get("session_sticky") == "1"))
            feedback = request.headers.get("x-sr-feedback")       # §4.4 反馈来源 1
            if feedback not in FEEDBACK_VALUES:
                feedback = None
            pre_signals: dict | None = None
            if feedback is None and settings.get("sticky_detect_feedback") == "1":
                # §4.4 反馈来源 2：开启后沿用路径仍做一次前向，只为读 user_feedback
                tag_result = await tagger.tag(TagRequest(
                    text=text, headers=req_headers, n_messages=len(ir.messages),
                    now=datetime.now().astimezone(),
                    last_msg=_last_msg(conn, session_id),
                    last_vec=_last_vec(conn, session_id),   # B2：反馈探测路径同样带上
                    context_thresholds=ctx_thresholds))
                pre_signals = tag_result.signals
                feedback = pre_signals["user_feedback"]["value"]
            sess = resolve_session(conn, session_id, pre_signals, settings,
                                   feedback=feedback)
            if sess is not None:                  # sticky / unlocked：跳过打标（§14.2）
                decision = sess
                session_tag = sess.source
                signals = pre_signals             # 仅反馈探测模式有产物
            else:
                session_tag = "new"
                # ── 6 打标签（一次前向） ──
                t1 = time.monotonic()
                if pre_signals is None:
                    tag_result = await tagger.tag(TagRequest(
                        text=text, headers=req_headers,
                        n_messages=len(ir.messages),
                        now=datetime.now().astimezone(),
                        last_msg=_last_msg(conn, session_id),
                        last_vec=_last_vec(conn, session_id),   # B2
                        context_thresholds=ctx_thresholds))
                    signals = tag_result.signals
                else:
                    signals = pre_signals         # 反馈探测前向的产物直接复用
                answer_ms = (time.monotonic() - t1) * 1000
                metrics.observe("sr_answer_latency_seconds", {}, answer_ms / 1000.0,
                                 conn=conn)
                # ── 7 走分流树 → RouteDecision ──
                decision = evaluate(nodes, signals, cuts)

        # ── 12(预备) 诊断头取值（裁定 1）──
        diag: dict = {"x-sr-tagger": tagger.name, "x-sr-session": session_tag}
        if decision.source == "direct":
            diag["x-sr-route"] = "direct"
        elif decision.source in ("sticky", "unlocked"):
            diag["x-sr-route"] = decision.source
        elif decision.source == "fallback":       # 坏树全 miss 的防御决定
            diag["x-sr-route"] = "fallback"
        elif decision.hit_path:
            diag["x-sr-route"] = ".".join(map(str, decision.hit_path))
            hit_branch = diag["x-sr-route"]       # 裁定 2：hit_branch 存 "N.M" 串

        # block → 403 + 双语 block_msg 按 accept-language（裁定 6）
        # audit step 3：预算改写是否命中（在拦截/无模型路径之前先定义——那些
        # 分支也要落账，虽然它们不可能是预算行）
        budget_forced = False

        if decision.kind == "block":
            msg_map = decision.block_msg or {}
            message = msg_map.get(detect_lang(accept_lang)) or msg_map.get("en") \
                or "blocked by safety policy"
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to="block", session_id=session_id,
                               session_tag=session_tag, outcome="block", status=403,
                               answer_ms=answer_ms, upstream_ms=None, cost=None,
                               tree_version=tree_version, budget=budget_forced)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to="block", outcome="block",
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision, conn=conn)
            conn.commit()
            return _error(proto, 403, message, "permission_error", "content_blocked",
                          diag)

        if not decision.model:
            # 防御性 fallback（无兜底的坏树，正常不可达）：x-sr-route=fallback + 502
            metrics.inc("sr_no_match_total", conn=conn)
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to=None, session_id=session_id,
                               session_tag=session_tag, outcome="error", status=502,
                               answer_ms=answer_ms, upstream_ms=None, cost=None,
                               tree_version=tree_version, budget=budget_forced)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to=None, outcome="error",
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision, conn=conn)
            conn.commit()
            return _error(proto, 502, "routing produced no model",
                          "api_error", "no_route", diag)

        # ── M2（audit Major）预算保护执行点（审核报告 §2-M2：三键原为空转死功能）──
        # evaluate 之后、上游调用之前：当日花费 ≥ 日限额 × 水位 → 把 tree/trial
        # 决定改写到 budget_route（保留原 think 与 fallbacks，outcome 的 fallback
        # 判定自然以改写后 model 为主模型）。口径：sticky/unlocked/direct 不覆盖——
        # 显式行为（用户点名 / 会话沿用 / 反馈解锁）优先于预算收敛，只有「树替用户
        # 做的决定」才被预算改写。cost 列已由 M3 回填（非流式即时 / 流式续传后），
        # SUM 机制与触发条件均真实生效。
        if settings.get("budget_protect") == "1" \
                and decision.source in ("tree", "trial"):
            # 预算窗口归一（audit 快修）：见 _daily_spend 的 docstring——两侧过
            # `datetime()`，起点按本地时区算，避免非 UTC 机器上错一天。
            daily = _daily_spend(conn)
            # .get 带默认（代码既有约定，同 max_retries/session_idle）：M1 之前
            # 初始化的旧库缺新键时不至 KeyError 成每请求 500；PUT /admin/settings
            # 全键写入后即愈合。
            if daily >= float(settings.get("budget_daily_limit_usd") or 10.0) \
                    * float(settings.get("budget_water") or 0.8):
                decision.model = settings["budget_route"]
                budget_forced = True      # audit step 3：逐行标记，replay 口径据此排除
                metrics.inc("sr_budget_triggered_total", conn=conn)
                diag["x-sr-budget"] = "1"

        # ── 8–9 供应商解析 + 上游调用（候选链 + 重试 + 熔断 + 首包锁定） ──
        try:
            if ir.stream:
                backend, final_model, resp, client, aiter, first, upstream_ms = \
                    await _call_stream(conn, ir, decision, settings, metrics)
            else:
                backend, final_model, data, upstream_ms = \
                    await _call_nonstream(conn, ir, decision, settings, metrics)
        except UpstreamError as exc:
            # 全链失败 → 502 错误信封（outcome=error，裁定 2 三值枚举）
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to=None, session_id=session_id,
                               session_tag=session_tag, outcome="error",
                               status=exc.status, answer_ms=answer_ms,
                               upstream_ms=None, cost=None,
                               tree_version=tree_version, budget=budget_forced)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to=None, outcome="error",
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision, conn=conn)
            conn.commit()
            return _error(proto, exc.status or 502, exc.message,
                          exc.type if exc.type != "config_error" else "api_error",
                          "upstream_error", diag)

        # ── 11 requests + tags 入库（流式在首包锁定后、续传前落账；裁定 2/3） ──
        diag["x-sr-model"] = final_model
        diag["x-sr-upstream-model"] = backend.upstream_name
        # M1（audit Major）：outcome 按「最终去向 != 决策主模型」写 "fallback"——
        # 旧实现成功落账恒 "upstream"，而 _traffic_filter 按 outcome='fallback'
        # 筛选（:784）→ GET /admin/traffic?f=fallback 恒空，流量页口径与
        # sr_fallback_total 指标口径互相矛盾。判定**只此一处**（_is_fallback），
        # 与 _bump_request_metrics 的 sr_fallback_total 同源。
        outcome = "fallback" if _is_fallback(decision.model, final_model, decision) \
            else "upstream"
        # M3：cost 回填——非流式在此时已拿到上游 usage（价目 × token）；流式首包
        # 尚无 usage，落账先 NULL，续传结束后由 _relay_with_cost 回填（见下）。
        usage = data.get("usage") if (not ir.stream and isinstance(data, dict)) else None
        rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                           routed_to=final_model, session_id=session_id,
                           session_tag=session_tag, outcome=outcome, status=200,
                           answer_ms=answer_ms, upstream_ms=upstream_ms,
                           cost=_cost_of(conn, final_model, usage),
                           tree_version=tree_version, budget=budget_forced)
        _store_tags(conn, rid, signals)
        metrics.observe("sr_upstream_latency_seconds", {"model": final_model},
                        (upstream_ms or 0) / 1000.0, conn=conn)
        _bump_request_metrics(metrics, proto=proto, diag=diag,
                              routed_to=final_model, outcome=outcome,
                              session_tag=session_tag, hit_branch=hit_branch,
                              primary=decision.model, final=final_model,
                              decision=decision, conn=conn)
        if mode == "tree" and session_id:
            # 树命中成功才写会话（直通 R2 不参与沿用；沿用轮 resolve_session 已记账）
            if session_tag == "new":
                _upsert_session(conn, session_id, final_model, text)
            else:
                conn.execute("UPDATE sessions SET last_msg = ? WHERE id = ?",
                             (text, session_id))
            # A2/M2 接线：checkpoint 打标器回传嵌入向量（result.vec）时写
            # sessions.last_vec（db.py 列已存在，BLOB 存 JSON bytes，读侧
            # json.loads 还原）——heuristic 恒 None 此分支不触发，M2
            # CheckpointTagger 落地即生效。
            if tag_result is not None and tag_result.vec is not None:
                conn.execute(
                    "UPDATE sessions SET last_vec = ? WHERE id = ?",
                    (json.dumps(tag_result.vec).encode("utf-8"), session_id))
        conn.commit()
        # conn 交由外层 finally 统一关闭（在返回前，流式续传不占库连接）
        conn = None

        # ── 9(续)/10 响应体按客户端协议转换 + 入站名回写 + 诊断头 ──
        if ir.stream:
            return StreamingResponse(
                _relay_with_cost(proto, backend.proto, asked, resp, client, aiter,
                                 first, db_path, rid, final_model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", **diag})
        return JSONResponse(_cross_from_upstream(proto, backend.proto, data, asked),
                            headers=diag)
    except Exception:                             # 兜底：错误信封按客户端协议
        if conn is not None:
            conn.rollback()
        # M-c（终审 Minor）：文案不回吐 Python 异常原文（可能含路径/SQL/环境细节），
        # 完整异常进服务端日志供运维定位。
        logger.exception("internal error handling %s %s", proto, asked)
        return _error(proto, 500, "internal error", "api_error",
                      "internal", {"x-sr-tagger": tagger.name})
    finally:
        if conn is not None:                      # 400/block/502 等提前返回路径也关连接
            conn.close()

def create_app(db_path: str, tagger: Tagger,
               console_path: str | None = None) -> FastAPI:
    """构造路由应用（简报 Produces 接口逐字）。

    :param db_path: SQLite 路径（调用方先 init_db；本层每请求自开连接）。
    :param tagger: 注入的打标器（A2，审核报告 §3-A2）——``sr.tagger.Tagger``
        协议实例（``HeuristicTagger()`` / M2 的 ``CheckpointTagger``）；
        ``name`` 进 x-sr-tagger 诊断头，``tag`` 为 async。必填位置参数，
        无字符串兼容分支。
    :param console_path: GET / 托管的控制台 HTML（Task 12，§9.1）；缺省仓库根
        console-v7.html，``sr serve --console`` 可换。文件缺失不拦启动——
        控制台 404，路由主链不受影响。

    构造时从 requests 表 backfill sr_requests_total（裁定 6），计数器挂
    ``app.state.metrics``——A4/M3-10 后是 **DB 聚合视图**（`metric_counters` 表），
    同库的多个 app 实例/worker **共享**同一份计数；`Metrics()` 无 db_path 时仍是
    纯内存（单测用）。
    """
    app = FastAPI(title="smart-router", docs_url=None, redoc_url=None)
    app.state.db_path = db_path
    app.state.tagger = tagger
    metrics = Metrics(db_path)   # A4/M3-10：DB 聚合，跨 worker / app 实例共享
    conn = db.get(db_path)
    try:
        metrics.backfill_requests(conn)
    finally:
        conn.close()
    app.state.metrics = metrics
    app.state.console_path = console_path

    # ── 控制台托管（§9.1：单文件双模；页面自行 bootDetect 判 connected/demo）──
    @app.get("/")
    async def console_root():
        p = Path(app.state.console_path) if app.state.console_path \
            else _CONSOLE_DEFAULT
        if not p.is_file():
            return JSONResponse(
                {"detail": f"console file not found: {p}"}, status_code=404)
        return FileResponse(p, media_type="text/html; charset=utf-8")

    # ── /v1 双协议主流程（§7.1）──
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _handle("openai", request, db_path, tagger, metrics)

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await _handle("anthropic", request, db_path, tagger, metrics)

    # ── admin 读端点（§9.1 引导序列 + §9.2 形状；Task 9）──











    # ── admin 写端点（§9 表「写」各行 + §10 保存前体检 + §9.4 探测；Task 10）──


















    # ── /metrics（§11.1 手工 OpenMetrics，裁定 6）──
    @app.get("/metrics")
    async def metrics_endpoint():
        return PlainTextResponse(
            metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/v1/models")
    async def list_models():
        """§7.1 目录：auto + 全部别名（enabled 模型的 aliases 并集，去重保序）。"""
        conn = db.get(db_path)
        try:
            ids = ["auto"]
            for r in conn.execute(
                    "SELECT aliases FROM models WHERE enabled = 1 ORDER BY id"):
                try:
                    aliases = json.loads(r["aliases"] or "[]")
                except ValueError:
                    aliases = []
                for a in aliases:
                    if a not in ids:
                        ids.append(a)
        finally:
            conn.close()
        return JSONResponse({"object": "list",
                             "data": [{"id": i, "object": "model",
                                       "owned_by": "smart-router"} for i in ids]},
                            headers={"x-sr-tagger": tagger.name})

    # D2/A1：admin 端点按域注册（sr/routes/*，APIRouter；纯搬迁零行为变化）
    for _router in ROUTERS:
        app.include_router(_router)
    return app
