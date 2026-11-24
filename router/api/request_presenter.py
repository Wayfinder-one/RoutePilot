"""Request-record presentation: the one place a ledger row becomes UI fields.

The record itself is the machine contract (what the router decided, what the
upstream answered).  This module owns everything the console shows *about*
that record and nothing else:

* ``present_record()`` attaches the ordered ``route_label`` / ``metrics`` /
  ``fields`` facts the console renders.  Live records and SQLite-hydrated rows
  go through this same function, so restored rows never need a separate
  client-side adapter.
* ``present_result_event()`` is the terminal ``request_outcome`` trace event.
* ``OutcomeRecorder`` finalises a record exactly once (upstream facts, usage,
  outcome event, display fields, ledger enqueue).

Outcome state names are the shared vocabulary in ``router.request_outcome``
(``outcome_state`` is persisted by ``router.request_logging`` and recorded by
the upstream transport); this module re-exports them for its display tables.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from router.request_outcome import (
    OUTCOME_CANCELLED,
    OUTCOME_CLIENT_DISCONNECTED,
    OUTCOME_COMPLETED,
    OUTCOME_INCOMPLETE,
    OUTCOME_ROUTING_ERROR,
    OUTCOME_STATES,
    OUTCOME_UPSTREAM_ERROR,
)
from router.routing.graph.model import (
    ConditionNode,
    LogicNode,
    QuestionNode,
    StartNode,
    TargetNode,
)

OUTCOME_DISPLAY = {
    OUTCOME_COMPLETED: {"zh": "已完成", "en": "Completed"},
    OUTCOME_UPSTREAM_ERROR: {"zh": "上游错误", "en": "Upstream error"},
    OUTCOME_CLIENT_DISCONNECTED: {"zh": "客户端断开", "en": "Client disconnected"},
    OUTCOME_CANCELLED: {"zh": "已取消", "en": "Cancelled"},
    OUTCOME_INCOMPLETE: {"zh": "进行中", "en": "In progress"},
    OUTCOME_ROUTING_ERROR: {"zh": "路由失败", "en": "Routing failed"},
}

_NODE_TYPES = (
    (StartNode, "start"),
    (QuestionNode, "question"),
    (ConditionNode, "condition"),
    (LogicNode, "logic"),
    (TargetNode, "target"),
)


def _label(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _shown(value):
    return "—" if value is None or value == "" else value


def _milliseconds(value) -> str:
    return "—" if value is None else f"{round(value)}ms"


def route_label(record: dict) -> dict[str, str]:
    """Human label for ``selection_reason``; derived from the record only."""
    if record.get("outcome_state") == OUTCOME_ROUTING_ERROR:
        return _label("路由中止", "Routing stopped")
    if record.get("selection_reason") == "direct":
        return _label("固定模型测试", "Direct model test")
    if record.get("selection_reason") == "sticky" or record.get("sticky_hit"):
        return _label("会话粘性直达", "Session affinity")
    final = next((event for event in reversed(record.get("trace") or [])
                  if isinstance(event, dict) and event.get("event") == "final"), None)
    kind = final.get("kind") if final else None
    if kind == "otherwise":
        return _label("默认目标兜底", "Default target fallback")
    if kind == "error_fallback" or record.get("selection_reason") == "error_fallback":
        return _label("错误兜底目标", "Error fallback target")
    if record.get("selection_reason") == "otherwise":
        # Hydrated ledger rows keep selection_reason but not the trace events.
        return _label("默认目标兜底", "Default target fallback")
    return _label("目标命中", "Target selected")


def present_record(record: dict) -> dict:
    """Attach (or refresh) the ordered UI facts and return the same record.

    Ordered UI facts stay in the in-memory record, outside the SQLite ledger;
    this is idempotent so the outcome refresh can call it again.
    """
    record["route_label"] = route_label(record)
    # 快照链接只指向绘制画板；整图不随列表下发，打开链接时才读取。
    if record.get("graph_snapshot_id"):
        record["snapshot_url"] = "/console/routing.html?request=" + quote(
            str(record.get("id") or ""), safe="")
    else:
        record.pop("snapshot_url", None)
    # 计算日志只呈现路由自己的决策成本：决策耗时（进入路由 → 选出模型，
    # 含决策模型耗时）与轮次。上游状态、端到端总耗时与 token 用量属于
    # 网关数据面，不进智能路由的展示口径（"智能路由只负责智能路由的事情"）。
    record["metrics"] = [
        [_label("决策耗时", "Decision time"), _milliseconds(record.get("routing_ms"))],
        [_label("决策模型耗时", "Decision model time"), _milliseconds(record.get("jev_ms"))],
        [_label("轮次", "Waves"), _shown(record.get("waves"))],
    ]
    record["fields"] = [
        [_label("请求 ID", "Request ID"), _shown(record.get("id"))],
        [_label("决策组", "Decision group"), _shown(record.get("tree_id"))],
        [_label("决策组版本", "Tree revision"), _shown(record.get("tree_revision"))],
        [_label("快照", "Snapshot"), _shown(record.get("snapshot_revision"))],
        [_label("策略路径", "Policy path"), _shown(record.get("policy_path"))],
        [_label("会话哈希", "Session hash"), _shown(record.get("session_hash"))],
        [_label("JEV 模型", "JEV model"), _shown(record.get("jev_model"))],
        [_label("推理等级", "Reasoning level"), _shown(record.get("reasoning"))],
        [_label("推理回退", "Reasoning fallback"), _shown(record.get("reasoning_warning"))],
    ]
    return record


def present_result_event(record: dict) -> dict:
    """Terminal ``request_outcome`` event for the record's current state."""
    state = record["outcome_state"]
    status = OUTCOME_DISPLAY[state]
    tone = ("bad" if state == OUTCOME_UPSTREAM_ERROR else
            "warn" if state in (OUTCOME_CANCELLED, OUTCOME_CLIENT_DISCONNECTED) else
            "warn" if record.get("error_class") == "client_4xx" else "hit")
    return {
        "event": "request_outcome", "phase": "outcome",
        "label": _label("执行结果", "Execution result"),
        "cell": status, "summary": status,
        "kind_label": _label("结果", "Outcome"),
        # 上游状态、总耗时属于请求执行结果，不属于路由决策；这里只保留
        # 路由选出的模型，避免把网关侧指标混进计算日志。
        "facts": [
            [_label("模型", "Model"), _shown(record.get("model"))],
        ],
        "tone": tone, "outcome_state": state,
    }


def node_labels(snapshot) -> dict:
    """Trace/console display names for the snapshot's graph nodes."""
    if snapshot is None or snapshot.tree is None:
        return {}
    labels = {}
    for node in snapshot.tree.root.nodes:
        node_type = next(
            (name for node_class, name in _NODE_TYPES if isinstance(node, node_class)),
            "unknown",
        )
        labels[node.id] = {"title": node.title, "type": node_type}
    return labels


def _usage_fields(usage) -> dict:
    fields = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    if isinstance(usage, dict):
        for key in fields:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                fields[key] = value
    return fields


class OutcomeRecorder:
    """Finalise one request record exactly once (later ``finish`` calls are no-ops).

    Structurally implements ``router.request_outcome.OutcomeSink``.
    """

    def __init__(self, requestlog, record: dict, started: float):
        self.requestlog = requestlog
        self.record = record
        self.started = started
        self._done = False

    def finish(self, *, outcome_state: str, error_class: str | None = None,
               upstream_model: str | None = None, upstream_status: int | None = None,
               upstream_ms: float | None = None, ttft_ms: float | None = None,
               usage: dict | None = None) -> bool:
        if outcome_state not in OUTCOME_STATES:
            raise ValueError(f"unknown outcome_state {outcome_state!r}")
        if self._done:
            return False
        self._done = True
        self.record.update({
            "upstream_model": upstream_model,
            "upstream_status": upstream_status,
            "upstream_ms": upstream_ms,
            "ttft_ms": ttft_ms,
            "total_ms": (time.monotonic() - self.started) * 1000.0,
            "error_class": error_class,
            "outcome_state": outcome_state,
            **_usage_fields(usage),
        })
        self.record["trace"].append(present_result_event(self.record))
        present_record(self.record)
        self.requestlog.enqueue_result(self.record)
        return True
