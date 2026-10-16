"""§11.1 指标：内存计数器 + 手工 OpenMetrics 文本。

A1（audit §3）：自 sr/server.py 拆出（原「6) §11.1 指标」段）。依赖白名单无
prometheus_client，禁新增依赖——裁定 6：手工拼 classic text。

部署约束（M2 前置 7，audit A4）：Metrics 是**进程内**内存 dict（启动 backfill
自 DB），uvicorn 多 worker 下 /metrics 抓到哪个 worker 就出哪份——总量既不准也不
稳定。M1 部署口径 = **单 worker**（``sr serve`` 只调 ``uvicorn.run()``，不暴露
``--workers``，即 uvicorn 默认单进程）。多进程聚合（multiprocess 模式或显式分片）
留 M3；``_FAIL_COUNTS``（sr/models.py 熔断计数）同为进程级，受限一致。

依赖方向：本模块 → sr.engine（RouteDecision，仅 _is_fallback 类型标注/判定用）。
**不** import sr.server。
"""

from __future__ import annotations

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
    """§11.1 计数器（内存 dict；每 app 实例一份，app.state.metrics 隔离）。

    - counter：``inc(name, labels, n)``；
    - summary（时延）：``observe(name, labels, seconds)`` 渲染为 ``_count/_sum``；
    - gauge：``set(name, value, labels)``（评测/预估闭环的 setter，M3 接线）。
    """

    def __init__(self):
        self.counters: dict[tuple, float] = {}
        self.summaries: dict[tuple, list] = {}     # key → [count, sum_seconds]
        self.gauges: dict[tuple, float] = {}

    def inc(self, name: str, labels: dict | None = None, n: float = 1) -> None:
        key = (name, _label_key(labels))
        self.counters[key] = self.counters.get(key, 0) + n

    def observe(self, name: str, labels: dict | None, seconds: float) -> None:
        s = self.summaries.setdefault((name, _label_key(labels)), [0, 0.0])
        s[0] += 1
        s[1] += seconds

    def set(self, name: str, value: float, labels: dict | None = None) -> None:
        self.gauges[(name, _label_key(labels))] = value

    def backfill_requests(self, conn) -> None:
        """启动 backfill：requests 表按 (proto, hit_branch, routed_to, outcome)
        聚合灌入 sr_requests_total（裁定 6 只 backfill 此一族；hit_branch 为
        NULL 的非树请求 route 记 "none"——运行期 live 路径用诊断头值，重启后
        两口径对非树行不逐一对齐，记任务报告）。"""
        rows = conn.execute(
            "SELECT proto, hit_branch, routed_to, outcome, COUNT(*) n "
            "FROM requests GROUP BY proto, hit_branch, routed_to, outcome")
        for r in rows:
            self.inc("sr_requests_total",
                     {"proto": r["proto"], "route": r["hit_branch"] or "none",
                      "model": r["routed_to"] or "", "outcome": r["outcome"] or ""},
                     r["n"])

    def render(self) -> str:
        """§11.1 清单 → classic OpenMetrics 文本（HELP/TYPE 恒出，样本有则出）。"""
        lines: list[str] = []
        for name, typ, help_text in _METRIC_FAMILIES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            if typ == "counter":
                for (n, labels), v in sorted(self.counters.items()):
                    if n == name:
                        lines.append(f"{name}{_label_str(labels)} {v:g}")
            elif typ == "summary":
                for (n, labels), (c, s) in sorted(self.summaries.items()):
                    if n == name:
                        lines.append(f"{name}_count{_label_str(labels)} {c}")
                        lines.append(f"{name}_sum{_label_str(labels)} {s:.6f}")
            else:
                for (n, labels), v in sorted(self.gauges.items()):
                    if n == name:
                        lines.append(f"{name}{_label_str(labels)} {v:g}")
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
                          decision: RouteDecision | None = None) -> None:
    """一次 /v1 请求的请求级计数（在 _log_request 的每个落账点调用）。

    - route 标签取诊断头同源值（树命中 = "N.M" 路径，否则 direct/sticky/
      unlocked/fallback，取不到记 "none"）；
    - model 标签 = 实际去向（block / 模型 id；error 行 routed_to 为 NULL → ""）；
    - primary≠final 且非试水记 sr_fallback_total（Task 10 折入项：旧实现用诊断头
      字符串 ``diag.get("x-sr-route") != "trial"`` 排除试水，而 source=trial 时该头
      是 "N.M"——恒真死代码；改按 decision.source 判定，不再依赖诊断头字符串）。
      判定与本函数外的 requests.outcome 共用 :func:`_is_fallback`（M1）。
    """
    route = diag.get("x-sr-route", "none")
    metrics.inc("sr_requests_total", {"proto": proto, "route": route,
                                      "model": routed_to or "", "outcome": outcome})
    if hit_branch:
        metrics.inc("sr_route_hits_total", {"route": hit_branch})
    if session_tag == "sticky":
        metrics.inc("sr_session_sticky_total")
    elif session_tag == "unlocked":
        metrics.inc("sr_session_unlock_total")
    if _is_fallback(primary, final, decision):
        metrics.inc("sr_fallback_total", {"from": primary, "to": final})
