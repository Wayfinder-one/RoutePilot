"""§11.1 指标：SQLite 共享计数器 + 手工 OpenMetrics 文本。

A1（audit §3）：自 sr/server.py 拆出（原「6) §11.1 指标」段）。依赖白名单无
prometheus_client，禁新增依赖——裁定 6：手工拼 classic text。

多进程口径（A4/M3-10，2026-10-16）：旧实现是**进程内**内存 dict，uvicorn 多 worker
下 ``/metrics`` 抓到哪个 worker 就出哪份——总量既不准也不稳定。现改为 **DB 聚合**：
``Metrics(db_path)`` 的 inc/observe/set 直接 UPSERT 到 ``metric_counters`` 表
（spec §3.1），``render()`` 从表里聚合读——所有 worker 共享同一份计数，任一进程
scrape 都得到全量。``Metrics()``（无 db_path）保留纯内存模式，供单测与无库场景使用。
``sr serve --workers N``（N>1）走工厂模式（``sr/serveapp.py``）；checkpoint 模式禁止
多 worker（每个 worker 会各加载一份模型 → 显存成倍），见 ``sr/__main__.py`` 拒绝逻辑。

``_FAIL_COUNTS``（sr/models.py 熔断计数）仍是进程级，但 ``is_circuit_open`` 同时
查询 DB 的 ``models.ok``——跨 worker 一致（任一 worker 熔断，其余 worker 同样跳过）。

依赖方向：本模块 → sr.engine（RouteDecision，仅 _is_fallback 类型标注/判定用）。
**不** import sr.server。
"""

from __future__ import annotations

import sqlite3
import threading

from sr.engine import RouteDecision

# §11.1 指标清单（family 名, 类型, HELP；顺序 = 渲染顺序 = §11.1 逐字）。
# sr_eval_route_match_rate / sr_estimate_error 两 gauge 归评测闭环（M3），
# 此处先声明 family（无样本时只出 HELP/TYPE 行，名称在 /metrics 恒可见）。
_METRIC_FAMILIES: list[tuple[str, str, str]] = [
    ("sr_requests_total", "counter",
     "Total /v1 requests by proto/route/model/outcome"),
    ("sr_route_hits_total", "counter", "Tree branch hits by route path"),
    ("sr_answer_latency_seconds", "summary", "Tagging latency (answer_ms)"),
    ("sr_upstream_latency_seconds", "summary",
     "Upstream first-byte latency by model"),
    ("sr_no_match_total", "counter",
     "Requests matched no branch (defensive fallback)"),
    ("sr_fallback_total", "counter", "Fallback chain promotions"),
    ("sr_retry_total", "counter", "Upstream attempt failures by model/status"),
    ("sr_session_sticky_total", "counter", "Session sticky hits"),
    ("sr_session_unlock_total", "counter", "Feedback unlocks"),
    ("sr_budget_triggered_total", "counter", "Budget protection triggers"),
    ("sr_circuit_open_total", "counter", "Circuit breaker openings by model"),
    ("sr_eval_route_match_rate", "gauge", "Latest eval route match rate"),
    ("sr_estimate_error", "gauge", "Latest estimate review error"),
]


def _label_key(labels: dict | None) -> tuple:
    """标签 dict → 可哈希键（排序保证同标签集归并）。"""
    return tuple(sorted((labels or {}).items()))


def _label_str(labels: tuple) -> str:
    """标签键 → Prometheus 文本 ``{k="v",...}``（值转义 \\ \" 与换行）。"""
    if not labels:
        return ""

    def esc(v) -> str:
        s = str(v)
        s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return s

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in labels) + "}"


class Metrics:
    """§11.1 计数器。

    两种模式：

    - ``Metrics()``：纯内存 dict（每 app 实例一份，测试互不串扰）；
    - ``Metrics(db_path)``：**DB 聚合**——写入 UPSERT 到 ``metric_counters``，
      读出按 (name, labels) 聚合，多 worker / 多实例共享同一份（A4/M3-10）。

    counter：``inc(name, labels, n)``；summary（时延）：``observe(...)`` 渲染为
    ``_count/_sum``；gauge：``set(name, value, labels)``。
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self.counters: dict[tuple, float] = {}
        self.summaries: dict[tuple, list] = {}     # key → [count, sum_seconds]
        self.gauges: dict[tuple, float] = {}

    # ── DB 模式内部 ────────────────────────────────────────────────────────
    @property
    def db_mode(self) -> bool:
        return self.db_path is not None

    def _c(self) -> sqlite3.Connection:
        """懒开共享连接（每进程一份；check_same_thread=False + 锁串行化）。"""
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                   isolation_level=None)   # autocommit
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
        return self._conn

    def _upsert(self, name: str, labels: dict | None, kind: str,
                count: float, total: float = 0.0, *, add: bool,
                conn: sqlite3.Connection | None = None) -> None:
        add_count = "count+excluded.count" if add else "excluded.count"
        add_sum = "sum+excluded.sum" if add else "excluded.sum"
        sql = ("INSERT INTO metric_counters(name, labels, kind, count, sum) "
               "VALUES(?,?,?,?,?) ON CONFLICT(name, labels, kind) DO UPDATE SET "
               f"count={add_count}, sum={add_sum}")
        args = (name, _label_str(_label_key(labels)), kind, count, total)
        if conn is not None:
            # 复用调用方（请求/端点）的连接：与请求写在**同一事务**里，
            # 既不与它抢写锁（旧实现各自连接 → `database is locked`，
            # 把 502 路径打成 500），也不额外开一次 commit。
            conn.execute(sql, args)
            return
        with self._lock:
            self._c().execute(sql, args)

    # ── 写入 API（conn 可选：给了就并入调用方事务，A4/M3-10） ──────────────
    def inc(self, name: str, labels: dict | None = None, n: float = 1,
            conn=None) -> None:
        if self.db_mode:
            self._upsert(name, labels, "counter", n, add=True, conn=conn)
            return
        key = (name, _label_key(labels))
        self.counters[key] = self.counters.get(key, 0) + n

    def observe(self, name: str, labels: dict | None, seconds: float,
                conn=None) -> None:
        if self.db_mode:
            self._upsert(name, labels, "summary", 1, seconds, add=True, conn=conn)
            return
        s = self.summaries.setdefault((name, _label_key(labels)), [0, 0.0])
        s[0] += 1
        s[1] += seconds

    def set(self, name: str, value: float, labels: dict | None = None,
            conn=None) -> None:
        if self.db_mode:
            self._upsert(name, labels, "gauge", value, add=False, conn=conn)
            return
        self.gauges[(name, _label_key(labels))] = value

    def backfill_requests(self, conn) -> None:
        """启动 backfill：requests 表按 (proto, hit_branch, routed_to, outcome)
        聚合灌入 sr_requests_total。

        幂等（A4/M3-10）：DB 模式下**已有本族样本则跳过**——多 worker 各自
        ``create_app`` 时都会调用本方法，重复执行不得把计数翻倍。
        """
        if self.db_mode:
            q = ("SELECT 1 FROM metric_counters WHERE name='sr_requests_total' "
                 "LIMIT 1")
            if conn.execute(q).fetchone() is not None:
                return
        rows = conn.execute(
            "SELECT proto, hit_branch, routed_to, outcome, COUNT(*) n "
            "FROM requests GROUP BY proto, hit_branch, routed_to, outcome")
        for r in rows:
            self.inc("sr_requests_total",
                     {"proto": r["proto"], "route": r["hit_branch"] or "none",
                      "model": r["routed_to"] or "", "outcome": r["outcome"] or ""},
                     r["n"])

    # ── 渲染 ───────────────────────────────────────────────────────────────
    def _rows(self) -> list[tuple[str, str, str, float, float]]:
        """DB 模式：读全量样本 (name, labels_str, kind, count, sum)，按名/标签排序。"""
        with self._lock:
            return [tuple(r) for r in self._c().execute(
                "SELECT name, labels, kind, count, sum FROM metric_counters "
                "ORDER BY name, labels")]

    def render(self) -> str:
        """§11.1 清单 → classic OpenMetrics 文本（HELP/TYPE 恒出，样本有则出）。"""
        lines: list[str] = []
        if self.db_mode:
            db_rows = self._rows()
        else:
            # 内存模式：统一成与 DB 同样的样本形状
            db_rows = [
                (n, _label_str(labels), "counter", v, 0.0)
                for (n, labels), v in sorted(self.counters.items())]
            db_rows += [
                (n, _label_str(labels), "summary", c, s)
                for (n, labels), (c, s) in sorted(self.summaries.items())]
            db_rows += [
                (n, _label_str(labels), "gauge", v, 0.0)
                for (n, labels), v in sorted(self.gauges.items())]
        for name, typ, help_text in _METRIC_FAMILIES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            for n, labels_str, kind, count, total in db_rows:
                if n != name:
                    continue
                if kind == "counter" and typ == "counter":
                    lines.append(f"{name}{labels_str} {count:g}")
                elif kind == "summary" and typ == "summary":
                    lines.append(f"{name}_count{labels_str} {count:g}")
                    lines.append(f"{name}_sum{labels_str} {total:.6f}")
                elif kind == "gauge" and typ == "gauge":
                    lines.append(f"{name}{labels_str} {count:g}")
        return "\n".join(lines) + "\n"


def _is_fallback(primary: str | None, final: str | None,
                 decision: RouteDecision | None) -> bool:
    """回退判定（M1 的**唯一**口径：最终去向 ≠ 决策主模型，试水不算回退）。

    `outcome="fallback"` 的写侧（requests 行）与 `sr_fallback_total` 的指标侧
    共用本函数——旧实现只有指标侧有这个判定，写侧恒 "upstream"，两处口径矛盾
    （流量页 f=fallback 恒空）。试水（decision.source == 'trial'）改道不是回退
    失败升级，排除（Task 10 折入项口径不变）。
    """
    return bool(final and primary and final != primary
                and (decision is None or decision.source != "trial"))


def _bump_request_metrics(metrics: Metrics, *, proto: str, diag: dict,
                          routed_to: str | None, outcome: str,
                          session_tag: str, hit_branch: str | None,
                          primary: str | None = None, final: str | None = None,
                          decision: RouteDecision | None = None,
                          conn=None) -> None:
    """一次 /v1 请求的请求级计数（在 _log_request 的每个落账点调用）。

    - route 标签取诊断头同源值（树命中 = "N.M" 路径，否则 direct/sticky/
      unlocked/fallback，取不到记 "none"）；
    - model 标签 = 实际去向（block / 模型 id；error 行 routed_to 为 NULL → ""）；
    - primary≠final 且非试水记 sr_fallback_total（Task 10 折入项：旧实现用诊断头
      字符串 ``diag.get("x-sr-route") != "trial"`` 排除试水，而 source=trial 时该头
      是 "N.M"——恒真死代码；改按 decision.source 判定，不再依赖诊断头字符串）。
      判定与本函数外的 requests.outcome 共用 :func:`_is_fallback`（M1）。
    - ``conn``：请求连接（DB 模式下并入请求事务，避免与请求写抢锁；A4/M3-10）。
    """
    route = diag.get("x-sr-route", "none")
    metrics.inc("sr_requests_total", {"proto": proto, "route": route,
                                      "model": routed_to or "", "outcome": outcome},
                conn=conn)
    if hit_branch:
        metrics.inc("sr_route_hits_total", {"route": hit_branch}, conn=conn)
    if session_tag == "sticky":
        metrics.inc("sr_session_sticky_total", conn=conn)
    elif session_tag == "unlocked":
        metrics.inc("sr_session_unlock_total", conn=conn)
    if _is_fallback(primary, final, decision):
        metrics.inc("sr_fallback_total", {"from": primary, "to": final}, conn=conn)
