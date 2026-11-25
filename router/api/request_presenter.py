"""Decision-record presentation: the one place a record becomes UI fields.

The record itself is the machine contract (what the router decided).  This
module owns everything the console shows *about* that record and nothing else:

* ``present_record()`` attaches the ordered ``route_label`` / ``metrics`` /
  ``fields`` facts the console renders.  The peripheral's in-memory decision-log
  summary and the kernel decision record both go through this same function.
* ``route_label()`` / ``node_labels()`` are the smaller display helpers used by
  the list/replay projections and the kernel decision trace.

Outcome state names are the shared vocabulary in ``router.request_outcome``;
this module re-exports the two the record presenter needs.
"""
from __future__ import annotations

from router.request_outcome import OUTCOME_INCOMPLETE, OUTCOME_ROUTING_ERROR
from router.routing.graph.model import (
    ConditionNode,
    LogicNode,
    QuestionNode,
    StartNode,
    TargetNode,
)

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
        # 内存事件只保留 selection_reason 的旧投影也走同一规则。
        return _label("默认目标兜底", "Default target fallback")
    return _label("目标命中", "Target selected")


def present_record(record: dict) -> dict:
    """Attach (or refresh) the ordered UI facts and return the same record.

    Ordered UI facts stay in the in-memory record / API response; the caller
    keeps no separate client-side adapter.  This is idempotent so a refreshed
    projection can call it again.

    The graph snapshot link and its revision are gone: a decision log summary
    already carries its own ``replay_url`` (``routing.html?request=<id>``) and
    the whole graph is read only by the single-event replay endpoint, never by
    the list.
    """
    record["route_label"] = route_label(record)
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
        [_label("策略路径", "Policy path"), _shown(record.get("policy_path"))],
        [_label("会话哈希", "Session hash"), _shown(record.get("session_hash"))],
        [_label("JEV 模型", "JEV model"), _shown(record.get("jev_model"))],
        [_label("推理等级", "Reasoning level"), _shown(record.get("reasoning"))],
        [_label("推理回退", "Reasoning fallback"), _shown(record.get("reasoning_warning"))],
    ]
    return record


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
