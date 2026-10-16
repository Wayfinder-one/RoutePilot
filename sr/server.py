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
from sr.health import run_checkup_safe
from sr.i18n import MESSAGES, detect_lang
from sr.metrics import Metrics, _bump_request_metrics, _is_fallback  # A1：指标（原在 server）
from sr.models import reset_circuit
from sr.settings import _load_settings, validate_settings   # A1：设置模块（原在 server）
from sr.tags import (DEFAULT_CONF_THRESHOLDS, DEFAULT_CONTEXT_THRESHOLDS,
                     TAGS, conf_band)
from sr.tagger import Tagger, TagRequest, TagResult
from sr.treeio import (WRITE_ACTOR, _branch_from_json, _idx_conflict,
                       _node_from_json, _renumber, _row_idxs, _take_idx,
                       _tree_from_json, _tree_full_422, _tree_json,
                       _write_tree_rows)   # A1：树 JSON/重编号/落库（原在 server）
from sr.upstream import (ADAPTERS, _call_nonstream, _call_stream,
                         _relay_stream)   # A1：上游调用/流式管道 + 协议注册表（原在 server）

# anthropic 客户端 max_tokens 缺失时的出站兜底（裁定 9）
DEFAULT_MAX_TOKENS = 1024
# 反馈解锁的显式头取值（§4.4 反馈来源 1：x-sr-feedback）
FEEDBACK_VALUES = {"satisfied", "need_clarification", "wrong_answer", "want_different"}

# §9.4 流式首包断言预算（秒）：「大量故障只在流式暴露」，首包 3s 内必须到。
STREAM_FIRST_BYTE_S = 3.0

# 影响预估（POST /admin/tree/estimate）的均摊 token 假设：重放只有标签包、没有
# 真实 usage，成本按 价目 × 以下常量 折算（§9.2 cost_delta 口径）；复盘回填
# estimates.actual 时按 requests.cost 实算，不经此常量（§11.4）。
AVG_IN = 800        # 平均输入 tokens / 请求
AVG_OUT = 600       # 平均输出 tokens / 请求

# 服务端日志（M-c：内部异常只进日志，不回吐客户端）
logger = logging.getLogger("sr.server")

# 树缓存（裁定 4：模块级 dict，按库路径分槽 {db_path: {"version", "nodes"}}；
# 每请求开头 SELECT tree_version，变了才重载该槽）。同进程先后挂多个库（pytest
# tmp_path）时 version 都从 1 起步——按路径分槽后多库场景是自然命中而非特判重置
# （审核 §4 清单：原单槽 + "path" 键特判的收口），生产单库行为不变。
_TREE_CACHE: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# 1) 小工具：树缓存 / 模型查找（detect_lang 在 sr/i18n.py；设置见 sr/settings.py）
# ─────────────────────────────────────────────────────────────────────────────
# A1：_load_settings / validate_settings 已迁 sr/settings.py（server 顶层 import 它，
# 消除 config.py 对 sr.server 的反向 import）。


def _conf_cuts(conn) -> list[float]:
    """conf_settings 单行切点 → list（缺行回默认 [0.30, 0.60, 0.85]，§3.1）。"""
    row = conn.execute("SELECT thresholds FROM conf_settings WHERE id = 1").fetchone()
    return json.loads(row["thresholds"]) if row else list(DEFAULT_CONF_THRESHOLDS)


def _context_thresholds(conn) -> list[int]:
    """tag_settings context 行切点 → list（缺行回默认 [20, 100, 400]，§4.5）。

    M8（audit Major）：原 tag_rules 只认模块常量，PUT /admin/bands 持久化的
    context 四档改了不生效（死旋钮）。每请求一次 SELECT 注入打标点——控制台
    改档立即生效（§4.2 第 5 步），SQLite 单点查代价可忽略，刻意不加缓存层。
    """
    row = conn.execute(
        "SELECT thresholds FROM tag_settings WHERE tag = 'context'").fetchone()
    return json.loads(row["thresholds"]) if row \
        else list(DEFAULT_CONTEXT_THRESHOLDS)


def _load_tree(conn, db_path: str) -> list:
    """树缓存读取（裁定 4 + §12 worker 口径）：开头一条 SELECT tree_version；
    该库路径的槽没变用内存树，变了（或该库首次载入）从 SQLite 重载并原子替换
    该槽。槽按 db_path 分键（审核 §4 清单），同进程多库互不串树。"""
    version = db.get_tree_version(conn)
    slot = _TREE_CACHE.get(db_path)
    if slot is None or slot["version"] != version:
        rows = conn.execute("SELECT * FROM tree").fetchall()
        _TREE_CACHE[db_path] = {"version": version, "nodes": rows_to_tree(rows)}
    return _TREE_CACHE[db_path]["nodes"]


def _lookup_model(conn, name: str):
    """请求名 → models 行：先按 id，再扫 aliases JSON 数组（别名视同该模型，R2）。
    查无返回 None（调用方回 400）。"""
    row = conn.execute("SELECT * FROM models WHERE id = ?", (name,)).fetchone()
    if row is not None:
        return row
    for r in conn.execute("SELECT * FROM models WHERE enabled = 1 ORDER BY id"):
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except ValueError:
            aliases = []
        if name in aliases:
            return r
    return None


def _last_msg(conn, session_id: str | None) -> str | None:
    """sessions.last_msg（§4.6 similarity/reask 的比对基准）；无会话回 None。"""
    if not session_id:
        return None
    row = conn.execute("SELECT last_msg FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    return row["last_msg"] if row is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# 2) 错误信封 / 诊断头 / 入库
# ─────────────────────────────────────────────────────────────────────────────


def _error(proto: str, status: int, message: str, etype: str,
           code: str | None = None, headers: dict | None = None) -> JSONResponse:
    """按客户端协议回错误信封（§7.3 / §7.4 各自形状）。"""
    if proto == "anthropic":
        payload: dict = {"type": "error", "error": {"type": etype, "message": message}}
    else:
        err: dict = {"message": message, "type": etype}
        if code:
            err["code"] = code
        payload = {"error": err}
    return JSONResponse(payload, status_code=status, headers=headers or {})


def _store_tags(conn, request_id: str, signals: dict | None) -> None:
    """tag_all 产物 → tags 表 17 行（裁定 3：全键入库）。

    value 过 JSON 序列化或标量原样（int 档位 → "1"、list → JSON 数组、None →
    "null"），confidence 规则标签为 NULL，is_rule 按 tag_all 的标记（§3.1 列口径）。
    """
    for tag, sig in (signals or {}).items():
        v = sig["value"]
        conn.execute(
            "INSERT OR REPLACE INTO tags (request_id, tag, value, confidence, is_rule) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, tag,
             v if isinstance(v, str) else json.dumps(v, ensure_ascii=False),
             sig.get("confidence"), 1 if sig.get("is_rule") else 0))


def _log_request(conn, *, proto, asked, hit_branch, routed_to, session_id,
                 session_tag, outcome, status, answer_ms, upstream_ms, cost) -> str:
    """requests 表一行（§3.1 列口径）；返回行 id 供 tags 外键。"""
    rid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO requests (id, ts, proto, model_asked, hit_branch, routed_to, "
        "session_id, session_tag, outcome, status, answer_ms, upstream_ms, cost) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, db._now(), proto, asked, hit_branch, routed_to, session_id,
         session_tag, outcome, status, answer_ms, upstream_ms, cost))
    return rid


def _upsert_session(conn, session_id: str, model: str, text: str) -> None:
    """树命中成功后写会话（§4.4：current_model 供下一轮沿用，last_msg 供相似度）。"""
    conn.execute(
        "INSERT INTO sessions (id, current_model, last_active, turns, unlocks, last_msg) "
        "VALUES (?, ?, datetime('now'), 1, 0, ?) "
        "ON CONFLICT(id) DO UPDATE SET current_model = excluded.current_model, "
        "last_active = datetime('now'), turns = turns + 1, last_msg = excluded.last_msg",
        (session_id, model, text))


def _cost_of(conn, model_id: str, usage: dict | None) -> float | None:
    """按 models 价目 × usage 估成本（§3.1 cost 列）；无价目/无 usage → NULL。"""
    if not usage:
        return None
    row = conn.execute("SELECT price_in, price_out FROM models WHERE id = ?",
                       (model_id,)).fetchone()
    if row is None or row["price_in"] is None or row["price_out"] is None:
        return None
    pin = usage.get("prompt_tokens", usage.get("input_tokens") or 0) or 0
    pout = usage.get("completion_tokens", usage.get("output_tokens") or 0) or 0
    return (pin * row["price_in"] + pout * row["price_out"]) / 1_000_000


# ─────────────────────────────────────────────────────────────────────────────
# 7) admin 读端点辅助：traffic 筛选（§9.2 形状，Task 9）
#    树逆组装（_branch_json/_tree_json）与写侧树解析/重编号/落库已迁 sr/treeio.py（A1）；
#    上游调用/流式管道（_chain/_call_*/_relay_stream）已迁 sr/upstream.py（A1）；
#    非流式跨协议 shim（_from_upstream）已迁 sr/adapters/cross.py（A3）。
# ─────────────────────────────────────────────────────────────────────────────


def _confmin_tags(clauses: list) -> list[str]:
    """cond 子句列表（含 or/not 嵌套）里带 conf_min 的标签键。"""
    out: list[str] = []
    for c in clauses:
        if not isinstance(c, dict):
            continue
        if "or" in c:
            out += _confmin_tags(c["or"])
        elif "not" in c:
            out += _confmin_tags([c["not"]])
        elif "conf_min" in c:
            out.append(c["tag"])
    return out


def _traffic_filter(conn, f: str, db_path: str) -> tuple[str, list]:
    """§9.2 ``f`` 五种快捷筛选 → 一条 WHERE（裁定 4）。

    - blocked / fallback / unlocked：outcome / session_tag 单等值；
    - small：「去了小模型」（设计方案 §4.3）→ 去向 = 阶梯最低档（settings.ladder
      首个）；
    - low_conf：命中分支条件含 conf_min 且该标签 conf_band < 2——分支按
      hit_branch "节点.位置" 定位（Task 5 裁定 hit_path=[节点idx, 分支序号]），
      conf_band 由 tags.confidence 对 conf_settings 切点计数（达到切点个数，
      与 sr.tags.conf_band 同语义）在 SQL 内联计算；confidence NULL（规则标签）
      比较为 NULL → EXISTS 不成立，天然排除。
    """
    simple = {"blocked": "outcome = 'block'",
              "fallback": "outcome = 'fallback'",
              "unlocked": "session_tag = 'unlocked'"}
    if f in simple:
        return simple[f], []
    if f == "small":
        # backlog 修：空阶梯曾落 routed_to = ''（恒空但语义隐晦）。显式化——
        # 无阶梯则「去了小模型」为空集（同 low_conf 的无门槛早退）。
        lad = [s.strip() for s in (_load_settings(conn).get("ladder") or "").split(",")
               if s.strip()]
        if not lad:
            return "1 = 0", []
        return "routed_to = ?", [lad[0]]
    if f == "low_conf":
        gated: dict[str, list[str]] = {}
        for node in _load_tree(conn, db_path):
            for i, b in enumerate(node.branches):
                tags = sorted(set(_confmin_tags(b.cond)))
                if tags:
                    gated[f"{node.idx}.{i}"] = tags
        if not gated:                       # 树上没有任何把握度门槛 → 恒空集
            return "1 = 0", []
        cuts = _conf_cuts(conn)
        # 每个比较单独加括号：SQL 里 + 优先级高于 >=，裸写会变成 confidence 与
        # 「切点+布尔」之和比较（实测踩坑：0.90 的行也被算进 band<2）
        band_sql = " + ".join(["(t.confidence >= ?)"] * len(cuts))
        groups, gparams = [], []
        for hb, tags in sorted(gated.items()):
            ph = ",".join("?" * len(tags))
            groups.append(
                f"(requests.hit_branch = ? AND EXISTS (SELECT 1 FROM tags t "
                f"WHERE t.request_id = requests.id AND t.tag IN ({ph}) "
                f"AND ({band_sql}) < 2))")
            gparams += [hb, *tags, *cuts]
        return "(" + " OR ".join(groups) + ")", gparams
    raise HTTPException(status_code=400, detail=f"unknown filter: {f}")


def _tag_value(raw: str):
    """tags.value 存储串 → 渲染值：JSON 数组/对象反序列化（pii 多标签），其余原样
    （§3.1：分类存枚举键、档位存索引串，显示层翻译，不入库显示词）。"""
    if raw[:1] in ("[", "{"):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _tags_brief(conn, ids: list[str]) -> dict[str, list]:
    """每行 tags_brief：入库序（rowid）前 3 条 {tag, value}（§9.2 示例形状）。"""
    out: dict[str, list] = {}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    for r in conn.execute(
            f"SELECT request_id, tag, value FROM tags WHERE request_id IN ({ph}) "
            "ORDER BY rowid", ids):
        brief = out.setdefault(r["request_id"], [])
        if len(brief) < 3:
            brief.append({"tag": r["tag"], "value": _tag_value(r["value"])})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 8) 十二步主流程（spec §7.2 逐项；对照表见任务报告）
# ─────────────────────────────────────────────────────────────────────────────


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
                context_thresholds=ctx_thresholds))
            signals = tag_result.signals
            answer_ms = (time.monotonic() - t1) * 1000
            metrics.observe("sr_answer_latency_seconds", {}, answer_ms / 1000.0)
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
                        context_thresholds=ctx_thresholds))
                    signals = tag_result.signals
                else:
                    signals = pre_signals         # 反馈探测前向的产物直接复用
                answer_ms = (time.monotonic() - t1) * 1000
                metrics.observe("sr_answer_latency_seconds", {}, answer_ms / 1000.0)
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
        if decision.kind == "block":
            msg_map = decision.block_msg or {}
            message = msg_map.get(detect_lang(accept_lang)) or msg_map.get("en") \
                or "blocked by safety policy"
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to="block", session_id=session_id,
                               session_tag=session_tag, outcome="block", status=403,
                               answer_ms=answer_ms, upstream_ms=None, cost=None)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to="block", outcome="block",
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision)
            conn.commit()
            return _error(proto, 403, message, "permission_error", "content_blocked",
                          diag)

        if not decision.model:
            # 防御性 fallback（无兜底的坏树，正常不可达）：x-sr-route=fallback + 502
            metrics.inc("sr_no_match_total")
            rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                               routed_to=None, session_id=session_id,
                               session_tag=session_tag, outcome="error", status=502,
                               answer_ms=answer_ms, upstream_ms=None, cost=None)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to=None, outcome="error",
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision)
            conn.commit()
            return _error(proto, 502, "routing produced no model",
                          "api_error", "no_route", diag)

        # ── M2（audit Major）预算保护执行点（审核报告 §2-M2：三键原为空转死功能）──
        # evaluate 之后、上游调用之前：当日花费 ≥ 日限额 × 水位 → 把 tree/trial
        # 决定改写到 budget_route（保留原 think 与 fallbacks，outcome 的 fallback
        # 判定自然以改写后 model 为主模型）。口径：sticky/unlocked/direct 不覆盖——
        # 显式行为（用户点名 / 会话沿用 / 反馈解锁）优先于预算收敛，只有「树替用户
        # 做的决定」才被预算改写。cost 列主链当前恒 None（M3 回填），SUM 机制真实、
        # 触发条件待 M3 cost 回填后生效。
        if settings.get("budget_protect") == "1" \
                and decision.source in ("tree", "trial"):
            daily = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM requests "
                "WHERE ts >= date('now', 'start of day')").fetchone()[0]
            # .get 带默认（代码既有约定，同 max_retries/session_idle）：M1 之前
            # 初始化的旧库缺新键时不至 KeyError 成每请求 500；PUT /admin/settings
            # 全键写入后即愈合。
            if daily >= float(settings.get("budget_daily_limit_usd") or 10.0) \
                    * float(settings.get("budget_water") or 0.8):
                decision.model = settings["budget_route"]
                metrics.inc("sr_budget_triggered_total")
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
                               upstream_ms=None, cost=None)
            _store_tags(conn, rid, signals)
            _bump_request_metrics(metrics, proto=proto, diag=diag,
                                  routed_to=None, outcome="error",
                                  session_tag=session_tag, hit_branch=hit_branch,
                                  decision=decision)
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
        rid = _log_request(conn, proto=proto, asked=asked, hit_branch=hit_branch,
                           routed_to=final_model, session_id=session_id,
                           session_tag=session_tag, outcome=outcome, status=200,
                           answer_ms=answer_ms, upstream_ms=upstream_ms,
                           cost=_cost_of(conn, final_model, None))
        _store_tags(conn, rid, signals)
        metrics.observe("sr_upstream_latency_seconds", {"model": final_model},
                        (upstream_ms or 0) / 1000.0)
        _bump_request_metrics(metrics, proto=proto, diag=diag,
                              routed_to=final_model, outcome=outcome,
                              session_tag=session_tag, hit_branch=hit_branch,
                              primary=decision.model, final=final_model,
                              decision=decision)
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
                _relay_stream(proto, backend.proto, asked, resp, client, aiter, first),
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


# ─────────────────────────────────────────────────────────────────────────────
# 9) admin 写端点辅助：树 JSON 解析 / 重编号 / 落库 / 体检参数（Task 10，§9-§10）
# ─────────────────────────────────────────────────────────────────────────────


def _hits_30d(conn) -> dict[str, int] | None:
    """近 30 天 hit_branch → 命中数（check_no_hits 数据源，"N.M" = 节点 idx +
    分支位置序号，Task 5 裁定）；窗口内无任何流量 → None（体检跳过该检查：
    零流量报「全部无命中」是噪声不是结论）。"""
    cutoff = (datetime.now().astimezone()
              - timedelta(days=30)).isoformat(timespec="seconds")
    total = conn.execute("SELECT COUNT(*) c FROM requests WHERE ts >= ?",
                         (cutoff,)).fetchone()["c"]
    if not total:
        return None
    return {r["hit_branch"]: r["c"] for r in conn.execute(
        "SELECT hit_branch, COUNT(*) c FROM requests "
        "WHERE ts >= ? AND hit_branch IS NOT NULL GROUP BY hit_branch", (cutoff,))}




def _checkup_or_422(conn, nodes: list[Node]):
    """保存前体检（§10 强制行为）：有 hard → 422 ``{"findings": […]}``（硬错误
    优先排序，控制台首屏直指拦截原因；全量 findings 由 /admin/tree/audit 提供）；

    通过 → None。

    走 :func:`run_checkup_safe`：体检自身崩溃按 hard finding 降级（M5），
    不得变成 PUT 500。
    """
    findings = run_checkup_safe(nodes, db.model_ids(conn), TAGS, hits=_hits_30d(conn))
    if any(f.level == "hard" for f in findings):
        ordered = sorted(findings, key=lambda f: f.level != "hard")
        return JSONResponse({"findings": [f.to_json() for f in ordered]},
                            status_code=422)
    return None


def _signal_from_row(tag: str, raw: str, confidence, is_rule) -> dict:
    """tags 行 → 引擎可求值的信号包（tags 表重放的唯一入口：try 的
    request_id 路径与 estimate 重放共用）。

    值形态按标签 kind 复原（§3.1 存储约定 × engine 求值需求）：ordered 标签
    存档位索引的数字串（_store_tags 对非字符串 json.dumps → "1"），_tag_value
    只反序列化 [/{ 开头的串——裸数字串须按 kind 转 int，否则 engine 的
    is/gte/lte 整数比较（§5.1）恒 False；multi（pii）的 JSON 数组由 _tag_value
    解析；choice/noul/free 原样字符串。未知标签保守原样（不过求值会 miss）。"""
    value = _tag_value(raw)
    spec = TAGS.get(tag)
    if spec is not None and spec.kind == "ordered" and isinstance(value, str) \
            and value.lstrip("-").isdigit():
        value = int(value)
    return {"value": value, "confidence": confidence, "is_rule": is_rule}


def _tags_payload(signals: dict, cuts: list) -> list[dict]:
    """信号包 → §9.2 tags 列表形状（tree/try 与 traffic replay 共用的渲染）。"""
    return [{"tag": t, "value": s["value"],
             "confidence": s["confidence"],
             "conf_band": None if s["confidence"] is None
             else conf_band(s["confidence"], cuts),
             "is_rule": bool(s.get("is_rule"))}
            for t, s in signals.items()]


def _est_cost(model: str | None, prices: dict) -> tuple[float | None, bool]:
    """重放样本的单次成本估算（AVG_IN/AVG_OUT 假设见模块常量注释）。

    返回 (成本, 价格缺失)。model=None（防御性 fallback，本就没服务）按 0 计；
    模型不在价目表或价格 NULL → 价格缺失（调用方把 cost_delta 置 null，§9.2）。"""
    if model is None:
        return 0.0, False
    if model not in prices:
        return None, True
    pin, pout = prices[model]
    if pin is None or pout is None:
        return None, True
    return (pin * AVG_IN + pout * AVG_OUT) / 1_000_000, False


# ─────────────────────────────────────────────────────────────────────────────
# 10) create_app —— 端点装配（§7.1 /v1 + §9.1-9.2 admin 读端点 + Task 10 写端点
#     + /metrics + 控制台托管 GET /（Task 12，§9.1 双模））
# ─────────────────────────────────────────────────────────────────────────────

# 控制台文件默认仓库根的 console-v7.html（§9.1：单文件、无构建链；路径可经
# ``sr serve --console`` 换）
_CONSOLE_DEFAULT = Path(__file__).resolve().parent.parent / "console-v7.html"


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
    ``app.state.metrics``（每 app 实例独立，测试互不串扰）。
    """
    app = FastAPI(title="smart-router", docs_url=None, redoc_url=None)
    app.state.db_path = db_path
    app.state.tagger = tagger
    metrics = Metrics()
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
    @app.get("/admin/providers")
    async def admin_providers():
        """供应商列表（providers 表全行；extra JSON 反序列化，ok 转布尔）。"""
        conn = db.get(db_path)
        try:
            rows = conn.execute("SELECT * FROM providers ORDER BY name").fetchall()
        finally:
            conn.close()
        return JSONResponse([{"name": r["name"], "proto": r["proto"],
                              "base_url": r["base_url"], "key_env": r["key_env"],
                              "extra": json.loads(r["extra"] or "{}"),
                              "ok": bool(r["ok"]),
                              "break_reason": r["break_reason"]} for r in rows])

    @app.get("/admin/models")
    async def admin_models():
        """模型列表（含阶梯、熔断状态，§8 表）：caps/aliases 反序列化、
        ok/enabled 转布尔、on_ladder = 是否在 settings.ladder 阶梯上。"""
        conn = db.get(db_path)
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

    @app.get("/admin/tree")
    async def admin_tree():
        """分流树（§9.2 首块形状；rows_to_tree 逆组装，cond/action 原样透出）。"""
        conn = db.get(db_path)
        try:
            body = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
        finally:
            conn.close()
        return JSONResponse(body)

    @app.get("/admin/settings")
    async def admin_settings():
        """全局设置（§3.1 settings 表全键值，值一律字符串）。"""
        conn = db.get(db_path)
        try:
            body = _load_settings(conn)
        finally:
            conn.close()
        return JSONResponse(body)

    @app.get("/admin/bands")
    async def admin_bands():
        """标签分档 + 把握度分档（设置页；conf_settings 切点 + tag_settings 各行）。"""
        conn = db.get(db_path)
        try:
            body = {"conf_thresholds": _conf_cuts(conn),
                    "tag_thresholds": [
                        {"tag": r["tag"], "thresholds": json.loads(r["thresholds"])}
                        for r in conn.execute("SELECT tag, thresholds FROM tag_settings "
                                              "ORDER BY tag")]}
        finally:
            conn.close()
        return JSONResponse(body)

    @app.get("/admin/traffic")
    async def admin_traffic(f: str | None = None, proto: str | None = None,
                            hours: int | None = None, page: int = 1):
        """流量列表（§9.2 形状）：``?f=&proto=&hours=&page=``，每页 50。

        f 五种快捷筛选各一条 WHERE（裁定 4，见 _traffic_filter）；outcome 用库中
        实际值（upstream/block/error）；tags_brief 每行入库序前 3 条。
        """
        conn = db.get(db_path)
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
                clause, fparams = _traffic_filter(conn, f, db_path)
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
        finally:
            conn.close()
        return JSONResponse({"total": total, "rows": [
            {"id": r["id"], "ts": r["ts"], "proto": r["proto"],
             "model_asked": r["model_asked"], "tags_brief": brief.get(r["id"], []),
             "hit_branch": r["hit_branch"], "routed_to": r["routed_to"],
             "session_tag": r["session_tag"], "outcome": r["outcome"],
             "status": r["status"], "answer_ms": r["answer_ms"],
             "upstream_ms": r["upstream_ms"]} for r in rows]})

    @app.get("/admin/traffic/{rid}")
    async def admin_traffic_detail(rid: str):
        """请求详情（标签 + 路径）：requests 全列 + tags 全行（入库序）。"""
        conn = db.get(db_path)
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
                             "tags": tags})

    def _dash(v) -> str:
        """verdict 文案里的空值占位（None → "—"，避免打印 "None"）。"""
        return "—" if v is None else str(v)

    @app.post("/admin/traffic/{rid}/replay")
    async def admin_traffic_replay(rid: str):
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
        conn = db.get(db_path)
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
            decision = evaluate(_load_tree(conn, db_path), signals, cuts)
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

    @app.get("/admin/i18n/{lang}")
    async def admin_i18n(lang: str):
        """指定语言的翻译表（前端启动时加载，§9.1）。lang ∈ zh|en；其他回落
        zh（裁定 5 二选一取回落：§6.1「默认 zh」，控制台引导永不断链）。"""
        return JSONResponse(MESSAGES.get(lang) or MESSAGES["zh"])

    # ── admin 写端点（§9 表「写」各行 + §10 保存前体检 + §9.4 探测；Task 10）──
    @app.put("/admin/tree")
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
        conn = db.get(db_path)
        try:
            blocked = _checkup_or_422(conn, nodes)
            if blocked is not None:
                return blocked
            old = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
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

    @app.post("/admin/tree/audit")
    async def admin_tree_audit():
        """体检当前树、不写库（§9）：findings 按 §10 CHECKS 顺序全量返回。"""
        conn = db.get(db_path)
        try:
            findings = run_checkup_safe(_load_tree(conn, db_path), db.model_ids(conn),
                                        TAGS, hits=_hits_30d(conn))
        finally:
            conn.close()
        return JSONResponse({"findings": [f.to_json() for f in findings]})

    @app.post("/admin/tree/try")
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
        conn = db.get(db_path)
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
                try_result = await tagger.tag(TagRequest(
                    text=text, headers=dict(request.headers), n_messages=1,
                    now=datetime.now().astimezone(), last_msg=None,
                    context_thresholds=_context_thresholds(conn)))
                signals = try_result.signals
                answer_ms = (time.monotonic() - t1) * 1000
            trace = evaluate_trace(_load_tree(conn, db_path), signals, cuts)
        finally:
            conn.close()
        d = trace["decision"]
        return JSONResponse({
            "tagger": tagger.name,
            "tags": _tags_payload(signals, cuts),
            "path": trace["path"], "also_match": trace["also_match"],
            "decision": {"kind": d.kind, "model": d.model, "think": d.think,
                         "fallback": d.fallbacks, "source": d.source,
                         "hit_path": d.hit_path},
            "answer_ms": round(answer_ms, 1)})

    @app.post("/admin/tree/estimate")
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
        conn = db.get(db_path)
        try:
            nodes = _load_tree(conn, db_path)
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

    @app.get("/admin/tree/estimates")
    async def admin_tree_estimates():
        """预估列表（§11.4 复盘）：estimates 全行，actual NULL = 未复盘。"""
        conn = db.get(db_path)
        try:
            rows = conn.execute("SELECT * FROM estimates "
                                "ORDER BY ts DESC, rowid DESC").fetchall()
        finally:
            conn.close()
        return JSONResponse([{"id": r["id"], "ts": r["ts"], "node": r["node"],
                              "params": json.loads(r["params"]),
                              "est": json.loads(r["est"]),
                              "actual": json.loads(r["actual"]) if r["actual"]
                              else None} for r in rows])

    @app.post("/admin/tree/nodes")
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
        conn = db.get(db_path)
        try:
            nodes = sorted(_load_tree(conn, db_path), key=lambda n: n.idx)
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
                    "INSERT INTO estimates (id, ts, node, params, est, actual) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (uuid.uuid4().hex, db._now(), str(cand.idx),
                     db._dump(body["node"]), db._dump(body["estimate"])))
            db.log_change(conn, WRITE_ACTOR, "tree", str(cand.idx),
                          before={"version": old["version"], "nodes": old["nodes"]},
                          after={"pos": pos, "node": body.get("node")})
            conn.commit()
            version = db.get_tree_version(conn)
        finally:
            conn.close()
        return JSONResponse(_tree_json(new_nodes, version))

    @app.delete("/admin/tree/nodes/{idx}")
    async def admin_tree_delete_node(idx: int):
        """删除节点（§9）：连带其分支行；删后体检，hard 拦截（如删兜底 → 422）。"""
        conn = db.get(db_path)
        try:
            nodes = sorted(_load_tree(conn, db_path), key=lambda n: n.idx)
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

    @app.post("/admin/tree/template")
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
        conn = db.get(db_path)
        try:
            old = _tree_json(_load_tree(conn, db_path), db.get_tree_version(conn))
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

    @app.post("/admin/models")
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
        conn = db.get(db_path)
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

    @app.put("/admin/models/{mid}")
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
        conn = db.get(db_path)
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

    @app.post("/admin/models/{mid}/reset")
    async def admin_reset_model(mid: str):
        """熔断复位（M6，audit Major：原 ≥3 次失败后进程内永久隔离，唯一恢复
        途径是重启进程）。清内存连续失败计数 + 恢复 models.ok=1 / break_reason=
        NULL——展示与行为一并复位（半开探测定时器仍留 M3）；写 changes 审计；
        模型不存在 404。"""
        conn = db.get(db_path)
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

    @app.post("/admin/models/{mid}/test")
    async def admin_model_test(mid: str):
        """接入测试（§9.4 两步探测，设计方案 B5）：

        ① ``connect``——非流式连通：openai → ``GET {base}/models``；
           anthropic → 最小 ``POST /v1/messages``；
        ② ``stream_first_byte``——发 ``stream: true`` 最小请求，断言
           :data:`STREAM_FIRST_BYTE_S` 秒内收到第一个数据帧。

        逐步返回 ``{name, ok, ms, detail}``；上游不可达/超时 → 该步 ok=false
        （整体 ``ok`` 为 false，HTTP 仍 200——探测失败是结论不是故障）。key_env
        未设置时不带鉴权头（对真上游会 401 → ok=false，也是有效探测结论）。
        """
        conn = db.get(db_path)
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

    @app.put("/admin/settings")
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
        conn = db.get(db_path)
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

    @app.put("/admin/bands")
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
        conn = db.get(db_path)
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

    @app.get("/admin/export")
    async def admin_export():
        """导出 YAML（§9.2：providers/models/tree/settings 四节；pyyaml
        safe_dump，依赖白名单 §13 明列）。核心逻辑在 sr/config.export_yaml
        （Task 11 裁定 1 抽出，CLI ``sr export`` 同源转调），本层只包 HTTP 响应。
        providers/models 只导可编辑配置——ok/break_reason 是熔断运行态，不进
        配置包。"""
        conn = db.get(db_path)
        try:
            text = config.export_yaml(conn)
        finally:
            conn.close()
        return PlainTextResponse(text, media_type="application/x-yaml")

    @app.post("/admin/import")
    async def admin_import(request: Request):
        """导入 YAML（§9）：normalize 全量校验（四节结构 + 字段/值域/引用）+
        run_checkup，hard 整包拒绝（422，不部分写入）；过则单事务写入
        providers → models → settings → tree（UPSERT，不删既有行）。
        核心逻辑在 sr/config.import_yaml（Task 11 裁定 1 抽出，CLI ``sr import``
        同源转调）：ValueError（YAML/结构/引用非法）→ 400 detail；hard findings
        → 422 findings；成功 → 200 {"ok": true}。I3：结构类异常（TypeError/
        KeyError，如**节存在但值为 null**）同样转 400 并记日志，不让裸 500 逃出。"""
        raw = await request.body()
        conn = db.get(db_path)
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

    return app
