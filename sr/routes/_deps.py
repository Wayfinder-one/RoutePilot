"""sr/routes/_deps.py —— 自 sr/server.py 机械搬迁的辅助函数与常量（D2/A1）。

依赖方向：sr.routes.* 与 sr.server 都 import 本模块；本模块**不** import 它们
（无环）。文件末尾的 `__all__` 动态列出全部顶层名字，供 server.py 用
star-import 让 `_handle` 的引用保持零改动。
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

DEFAULT_MAX_TOKENS = 1024
FEEDBACK_VALUES = {"satisfied", "need_clarification", "wrong_answer", "want_different"}
STREAM_FIRST_BYTE_S = 3.0
AVG_IN = 800
AVG_OUT = 600
logger = logging.getLogger("sr.server")
_TREE_CACHE: dict = {}
# D2 搬迁注意：本文件在 sr/routes/ 下，仓库根是 parents[2]（原 sr/server.py 时是
# parent.parent）。写错会让 GET / 托管 404。
_CONSOLE_DEFAULT = Path(__file__).resolve().parents[2] / "console-v7.html"


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
                 session_tag, outcome, status, answer_ms, upstream_ms, cost,
                 tree_version=None) -> str:
    """requests 表一行（§3.1 列口径）；返回行 id 供 tags 外键。

    ``tree_version``（M2）：判定当时的树版本；控制台对「记录版本 ≠ 当前版本」的
    历史行提示「分支归属可能已失效」（新增节点会重编 idx，旧 hit_branch 会静默错指）。
    """
    rid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO requests (id, ts, proto, model_asked, hit_branch, routed_to, "
        "session_id, session_tag, outcome, status, answer_ms, upstream_ms, cost, "
        "tree_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, db._now(), proto, asked, hit_branch, routed_to, session_id,
         session_tag, outcome, status, answer_ms, upstream_ms, cost, tree_version))
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
    """按 models 价目 × usage 估成本（§3.1 cost 列）；无价目/无 usage → NULL。

    usage 键容错：OpenAI prompt/completion_tokens；Anthropic input/output_tokens。
    """
    if not usage:
        return None
    row = conn.execute("SELECT price_in, price_out FROM models WHERE id = ?",
                       (model_id,)).fetchone()
    if row is None or row["price_in"] is None or row["price_out"] is None:
        return None
    pin = usage.get("prompt_tokens", usage.get("input_tokens") or 0) or 0
    pout = usage.get("completion_tokens", usage.get("output_tokens") or 0) or 0
    return (pin * row["price_in"] + pout * row["price_out"]) / 1_000_000
def _finalize_cost(db_path: str, rid: str, model_id: str, usage: dict) -> None:
    """流式续传结束后回填 requests.cost（M3）：流式落账在首包，当时没有 usage。"""
    conn = db.get(db_path)
    try:
        cost = _cost_of(conn, model_id, usage)
        if cost is not None:
            conn.execute("UPDATE requests SET cost = ? WHERE id = ?", (cost, rid))
            conn.commit()
    finally:
        conn.close()
async def _relay_with_cost(client_proto: str, up_proto: str, model_asked,
                           resp, client, aiter, first_frame: bytes,
                           db_path: str, rid: str, model_id: str):
    """_relay_stream 的薄包装：记录末包 usage，续传结束后回填该请求的 cost（M3）。

    不改变对外字节流（忠实透传/截断语义不变），只在流收束后补一次库写。
    """
    sink: dict = {}
    async for chunk in _relay_stream(client_proto, up_proto, model_asked, resp,
                                     client, aiter, first_frame, usage_sink=sink):
        yield chunk
    if sink.get("usage"):
        _finalize_cost(db_path, rid, model_id, sink["usage"])
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
    findings = run_checkup_safe(nodes, db.model_ids(conn), TAGS, hits=_hits_30d(conn),
                                estimates=eval_mod.estimate_deviations(conn))
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

def _dash(v) -> str:
    """verdict 文案里的空值占位（None → "—"，避免打印 "None"）。"""
    return "—" if v is None else str(v)

__all__ = sorted(n for n in list(globals()) if not n.startswith('__'))
