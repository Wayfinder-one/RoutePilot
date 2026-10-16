"""评测闭环（M3，spec §11.3/§11.4）：历史回放评测 + 标注集评测。

- :func:`replay_eval` —— 近 N 天入库的标签（session_tag='new'，全量标签行）重建
  信号包 → 当前树求值 → 与当时记录的去向比对；产出 route_match_rate、按分支的
  混淆矩阵、不一致样本清单（带 request_id 供控制台开试算）。
- :func:`labels_eval` —— 标注集 jsonl（``{text, expect_model|expect_branch}``）
  经注入打标器打标 → 当前树求值 → 与期望比对。
- :func:`log_eval` —— 结果落 changes 表（tbl='eval'，§11.4）。

信号重建与 sr.server._signal_from_row 同口径（值形态按标签 kind 复原：ordered 存
数字串须转 int，否则引擎整数比较恒 False）——本模块独立实现，避免 CLI 反向 import
server（A1 依赖方向）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sr.db as db
from sr.engine import evaluate, rows_to_tree
from sr.tags import DEFAULT_CONF_THRESHOLDS, TAGS


@dataclass
class EvalReport:
    source: str                 # 'replay' | 'labels'
    days: int | None
    total: int
    matched: int
    branches: dict = field(default_factory=dict)   # 分支/期望 → {total, matched}
    mismatches: list = field(default_factory=list)
    ts: str = ""
    # audit step 3：被排除的样本（口径透明，避免「一致率怎么变了」无法解释）
    excluded: dict = field(default_factory=dict)   # 原因 → 条数

    @property
    def rate(self) -> float | None:
        return (self.matched / self.total) if self.total else None

    def to_json(self) -> dict:
        return {"source": self.source, "days": self.days, "total": self.total,
                "matched": self.matched, "rate": self.rate,
                "branches": self.branches, "mismatches": self.mismatches,
                "excluded": self.excluded, "ts": self.ts}


def _conf_cuts(conn) -> list[float]:
    row = conn.execute("SELECT thresholds FROM conf_settings WHERE id = 1").fetchone()
    return json.loads(row["thresholds"]) if row else list(DEFAULT_CONF_THRESHOLDS)


def _decode(tag: str, raw: str):
    """tags.value 存储串 → 引擎可求值形态（与 server._tag_value/_signal_from_row 同口径）。"""
    value = raw
    if isinstance(raw, str) and raw[:1] in ("[", "{"):
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    spec = TAGS.get(tag)
    if spec is not None and spec.kind == "ordered" and isinstance(value, str) \
            and value.lstrip("-").isdigit():
        value = int(value)
    return value


def _signals_for(conn, rid: str) -> dict:
    sig = {}
    for r in conn.execute("SELECT tag, value, confidence, is_rule FROM tags "
                          "WHERE request_id = ? ORDER BY rowid", (rid,)):
        sig[r["tag"]] = {"value": _decode(r["tag"], r["value"]),
                         "confidence": r["confidence"],
                         "is_rule": bool(r["is_rule"])}
    return sig


def _branch_by_path(nodes, hit_path):
    """按 `evaluate` 的 hit_path（[node_idx, 分支序号]）取回 Branch 对象。

    用于 replay 口径：判断「命中的分支配了比例试水」需要看 Branch 本身
    （action.trial / 分支级 trial），不能只依赖 `decision.source`——试水是掷签的。
    """
    if not hit_path or len(hit_path) < 2:
        return None
    node_idx, ordinal = hit_path[0], hit_path[1]
    for n in nodes:
        if n.idx != node_idx:
            continue
        for i, b in enumerate(getattr(n, "branches", []) or []):
            if i == ordinal:
                return b
    return None


def replay_eval(conn, db_path: str, days: int = 7) -> EvalReport:
    """历史回放评测（§11.4）：近 N 天**纯树决定**的请求（有全量标签）重建信号包跑当前树，
    与当时 records 的 hit_branch / routed_to 比对。

    口径修正（audit step 3）：旧实现把**所有** `session_tag='new'` 且 hit_branch 非空的行
    都拿来比对，于是四类「非树决定」的行必然被判成不一致，把一致率系统性压低：

    - `block` / `error`：安全拦截与错误行——不经过上游，也没有树去向可比；
    - `fallback`：回退链命中（routed_to 是回退后的模型，不是树的决定）；
    - `trial`：比例试水改道——**按命中分支是否配了 trial 判定**（确定性），
      不能看 `evaluate` 的 source：试水是掷签的，重放时可能没掷中就误判成一致；
    - 预算保护行：`_handle` 在 evaluate **之后**把模型改写成 `budget_route`；现已在
      `requests.budget` 列**逐行落库**（audit step 3），这里直接按列排除——旧的做法是
      「routed_to == 当前 budget_route」猜，会把正常的廉价模型行一起排除。

    被排除的条数写进 `report.excluded`，一致率变化可解释。
    """
    cutoff = (datetime.now().astimezone()
              - timedelta(days=days)).isoformat(timespec="seconds")
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    cuts = _conf_cuts(conn)
    excluded: dict = {}
    for reason, where in (
            ("block/error/fallback",
             "COALESCE(outcome,'') IN ('block','error','fallback')"),
            ("budget", "COALESCE(budget,0) = 1"),
            ("sticky/unlocked", "session_tag IS NOT 'new'"),
            ("no_branch", "hit_branch IS NULL")):
        n = conn.execute(
            f"SELECT COUNT(*) FROM requests WHERE ts >= ? AND {where}",
            (cutoff,)).fetchone()[0]
        if n:
            excluded[reason] = n
    rows = conn.execute(
        "SELECT id, hit_branch, routed_to FROM requests "
        "WHERE ts >= ? AND session_tag = 'new' AND hit_branch IS NOT NULL "
        "AND COALESCE(outcome, '') NOT IN ('block', 'error', 'fallback') "
        "AND COALESCE(budget, 0) = 0 "
        "ORDER BY ts DESC", (cutoff,)).fetchall()
    report = EvalReport(source="replay", days=days, total=0, matched=0,
                        ts=db._now(), excluded=dict(excluded))
    for r in rows:
        signals = _signals_for(conn, r["id"])
        if not signals:
            continue
        decision = evaluate(nodes, signals, cuts)
        if decision.kind != "route" or decision.source != "tree":
            report.excluded["trial/other"] = \
                report.excluded.get("trial/other", 0) + 1
            continue                       # 试水掷中/防御分支等：不算树的决定
        br = _branch_by_path(nodes, decision.hit_path)
        if br is not None and (br.trial or (br.action or {}).get("trial")):
            # 命中分支配了比例试水：当时去向可能是 trial.to，重放不可确定 → 排除
            report.excluded["trial"] = report.excluded.get("trial", 0) + 1
            continue
        now_branch = ".".join(map(str, decision.hit_path)) if decision.hit_path else None
        now_model = decision.model
        report.total += 1
        key = r["hit_branch"]
        stat = report.branches.setdefault(key, {"total": 0, "matched": 0})
        stat["total"] += 1
        if now_branch == r["hit_branch"] and now_model == r["routed_to"]:
            report.matched += 1
            stat["matched"] += 1
        else:
            report.mismatches.append({
                "request_id": r["id"], "was_branch": r["hit_branch"],
                "now_branch": now_branch, "was_model": r["routed_to"],
                "now_model": now_model})
    return report


async def labels_eval(conn, db_path: str, records: list, tagger) -> EvalReport:
    """标注集评测（§11.4）：每条 ``{text, expect_model|expect_branch}`` 经注入打标器
    打标 → 当前树求值 → 与期望比对。async（打标器 tag 是 async）。"""
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    cuts = _conf_cuts(conn)
    report = EvalReport(source="labels", days=None, total=0, matched=0, ts=db._now())
    for rec in records:
        text = rec.get("text") or ""
        expect_model = rec.get("expect_model")
        expect_branch = rec.get("expect_branch")
        res = await tagger.tag(_tag_request(text))
        decision = evaluate(nodes, res.signals, cuts)
        now_branch = ".".join(map(str, decision.hit_path)) if decision.hit_path else None
        key = expect_branch or expect_model or "?"
        stat = report.branches.setdefault(key, {"total": 0, "matched": 0})
        stat["total"] += 1
        report.total += 1
        ok = ((expect_model is None or decision.model == expect_model)
              and (expect_branch is None or now_branch == expect_branch))
        if ok:
            report.matched += 1
            stat["matched"] += 1
        else:
            report.mismatches.append({
                "text": text, "expect_model": expect_model,
                "expect_branch": expect_branch, "now_model": decision.model,
                "now_branch": now_branch})
    return report


def _tag_request(text: str):
    from datetime import datetime
    from sr.tagger import TagRequest
    return TagRequest(text=text, headers={}, n_messages=1,
                      now=datetime.now().astimezone(), last_msg=None)


def log_eval(conn, report: EvalReport, actor: str = "cli") -> None:
    """评测结果落 changes 表（tbl='eval'）；控制台「分流树」页读最近三次。"""
    db.log_change(conn, actor, "eval", report.source,
                  before=None, after=report.to_json())


def backfill_estimates(conn, days: int = 7) -> int:
    """回填 ``estimates.actual``（§11.4）：ts 已满 N 天的未复盘行，按真实流量算
    daily（日均进该节点条数）与 cost_delta（节点内请求的平均成本）。

    "进节点" 口径：requests.hit_branch（"N.M"）的节点段 == estimates.node。返回
    本次回填条数；幂等（只填 actual IS NULL 的行）。
    """
    cutoff = (datetime.now().astimezone()
              - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, ts, node FROM estimates WHERE actual IS NULL AND ts <= ?",
        (cutoff,)).fetchall()
    filled = 0
    for r in rows:
        try:
            start = datetime.fromisoformat(r["ts"])
        except (TypeError, ValueError):
            start = datetime.now().astimezone()
        end = (start + timedelta(days=days)).isoformat(timespec="seconds")
        hit = conn.execute(
            "SELECT COUNT(*) c, AVG(cost) a FROM requests "
            "WHERE ts >= ? AND ts < ? AND hit_branch LIKE ?",
            (r["ts"], end, f"{r['node']}.%")).fetchone()
        actual = {"daily": round((hit["c"] or 0) / days, 4),
                  "cost_delta": round(hit["a"] or 0.0, 6),
                  "reviewed_at": db._now()}
        conn.execute("UPDATE estimates SET actual = ? WHERE id = ?",
                     (db._dump(actual), r["id"]))
        filled += 1
    return filled


def estimate_error(conn) -> float | None:
    """最近一次已复盘预估的偏差 |actual.daily − est.daily| / max(est.daily, 1)（§11.4）。

    无已复盘行 → None（gauge 不出样本）。
    """
    row = conn.execute(
        "SELECT est, actual FROM estimates WHERE actual IS NOT NULL "
        "ORDER BY rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        est = json.loads(row["est"]); actual = json.loads(row["actual"])
    except (TypeError, ValueError):
        return None
    base = est.get("daily") or 0
    return abs((actual.get("daily") or 0) - base) / max(abs(base), 1.0)


def estimate_deviations(conn, threshold: float = 0.5) -> list:
    """偏差 > threshold 的已复盘预估（node 列表）——供体检 info（§11.4「估得不准」）。"""
    out = []
    for row in conn.execute(
            "SELECT node, est, actual FROM estimates WHERE actual IS NOT NULL"):
        try:
            est = json.loads(row["est"]); actual = json.loads(row["actual"])
        except (TypeError, ValueError):
            continue
        base = est.get("daily") or 0
        dev = abs((actual.get("daily") or 0) - base) / max(abs(base), 1.0)
        if dev > threshold:
            out.append({"node": row["node"], "est_daily": base,
                        "actual_daily": actual.get("daily"), "deviation": dev})
    return out


def recent_evals(conn, limit: int = 3) -> list:
    """changes 表里最近 N 次评测报告（tbl='eval'，新的在前）。"""
    rows = conn.execute(
        "SELECT after FROM changes WHERE tbl = 'eval' ORDER BY rowid DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["after"]))
        except (TypeError, ValueError):
            continue
    return out
