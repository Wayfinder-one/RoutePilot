"""自包含决策事件：构造、校验、列表投影与回放投影。

每次实际路由决策产出一个自包含 JSON 对象，内嵌本次实际使用的完整当前图
文档（envelope + 画布 + 非密钥运行配置）、请求决策值、JEV 答案、回放 trace
与节点标签。事件**不生成** ``graph_snapshot_id``/``snapshot_revision``：回放
只读同一条事件里的 ``graph``/``trace``，不依赖当前图或任何快照表，因此图后来
修改或删除都不影响历史回放（Global Constraint 6）。

外围内存日志复用这里的两个投影：``summary_payload()`` 给最近列表（不含整图），
``replay_payload()`` 给单条回放（``tree``/``trace``/``request`` 全部来自同一条
事件）。

事件只记录决策本身，不记录 newapi 渠道、计费、模型上游状态或 Token 用量；
``SECRET_KEYS`` 的键名扫描 + ``json.dumps(allow_nan=False)`` 是可序列化与
「不含密钥字段」的唯一校验入口——``key_env`` 只是环境变量名，允许保留。

本模块属于内核：只依赖路由领域模型（``router.routing.tree``），不访问
SQLite、FastAPI、控制台或模型上游。
"""
from __future__ import annotations

import json
from urllib.parse import quote

from router.routing.tree import tree_to_dict

#: 按键名归一化（casefold、去掉 ``_``/``-``）后命中的密钥字段名。
SECRET_KEYS = {"apikey", "authorization", "keypresent", "keysource"}
EVENT_TYPE = "routepilot.decision"
SCHEMA_VERSION = 1
#: 回放入口：继续复用 ``routing.html?request=<决策 ID>``（Global Constraint 7）。
REPLAY_PATH = "/console/routing.html?request="


def build_decision_event(record, snapshot, *, department, requested_model):
    """Build one self-contained ``routepilot.decision`` event from a decision record.

    ``record`` is the existing ``_decision_record()`` machine record (non-secret
    fields only); ``snapshot`` is the snapshot the decision was actually made
    against, so its ``tree`` is embedded in full.  ``department`` and
    ``requested_model`` are request facts the record does not carry.
    """
    return validate_decision_event({
        "schema_version": SCHEMA_VERSION,
        "event_type": EVENT_TYPE,
        "id": record["id"],
        "ts": record["ts"],
        "request": {
            "text": record.get("text") or "",
            "requested_model": requested_model,
            "department": department,
            "session_hash": record.get("session_hash"),
        },
        "graph": tree_to_dict(snapshot.tree),
        "decision": {
            "status": "routing_error" if record.get("error_class") else "completed",
            "model": record.get("model"),
            "upstream_model": record.get("upstream_model"),
            "target_id": record.get("target_id"),
            "tree_id": record.get("tree_id"),
            "tree_revision": record.get("tree_revision"),
            "graph_selection": record.get("graph_selection"),
            "selection_reason": record.get("selection_reason"),
            "fallback_reason": record.get("fallback_reason"),
            "sticky_hit": record.get("sticky_hit", 0),
            "policy_path": record.get("policy_path"),
            "reasoning": record.get("reasoning"),
            "reasoning_warning": record.get("reasoning_warning"),
            "routing_ms": record.get("routing_ms"),
            "waves": record.get("waves", 0),
        },
        "jev": {
            "model": record.get("jev_model"), "choice": record.get("jev_choice"),
            "top": record.get("jev_top"), "margin": record.get("jev_margin"),
            "ms": record.get("jev_ms"),
            "probabilities": dict(record.get("jev_probabilities") or {}),
        },
        # 事件必须自包含：trace/answers/labels 拷贝一份，后续执行结果回写同一
        # record 时不得反向污染已存储的可回放事件。
        "answers": dict(record.get("answers") or {}),
        "trace": list(record.get("trace") or []),
        "node_labels": dict(record.get("node_labels") or {}),
    })


def validate_decision_event(value):
    """Validate and return ``value`` (the single event-shape/secret gate).

    Rejects anything that is not a ``routepilot.decision`` v1 object with an
    embedded graph, any dict key anywhere in the tree that names a secret field,
    and values that cannot be sent as one canonical JSON object.
    """
    if not isinstance(value, dict) or not isinstance(value.get("graph"), dict):
        raise ValueError("decision event requires graph")
    if value.get("event_type") != EVENT_TYPE or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported decision event")

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key.casefold().replace("_", "").replace("-", "")
                if normalized in SECRET_KEYS:
                    raise ValueError("secret field in decision event")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value


def summary_payload(event):
    """The recent-list projection of one complete event (no embedded graph).

    A 20-event list must not ship 20 complete graphs: the console only needs the
    decision facts and the replay link here; the graph itself is read by
    :func:`replay_payload` for one event at a time.
    """
    decision = event["decision"]
    jev = event["jev"]
    request = event["request"]
    return {
        "id": event["id"], "ts": event["ts"], "text": request["text"],
        "decision": "route", "model": decision["model"],
        "upstream_model": decision["upstream_model"],
        "target_id": decision["target_id"], "tree_id": decision["tree_id"],
        "tree_revision": decision["tree_revision"],
        "graph_selection": decision["graph_selection"],
        "selection_reason": decision["selection_reason"],
        "fallback_reason": decision["fallback_reason"],
        "sticky_hit": decision["sticky_hit"],
        "session_hash": request["session_hash"],
        "policy_path": decision["policy_path"],
        "reasoning": decision["reasoning"],
        "reasoning_warning": decision["reasoning_warning"],
        "routing_ms": decision["routing_ms"], "waves": decision["waves"],
        "outcome_state": decision["status"],
        "jev_model": jev["model"], "jev_choice": jev["choice"],
        "jev_top": jev["top"], "jev_margin": jev["margin"], "jev_ms": jev["ms"],
        "jev_probabilities": jev["probabilities"],
        "trace": event["trace"], "answers": event["answers"],
        "node_labels": event["node_labels"],
        "replay_url": REPLAY_PATH + quote(event["id"], safe=""),
    }


def replay_payload(event):
    """The replay projection: ``tree``/``trace``/``request`` from the same event.

    The embedded ``graph`` is the graph the decision actually used, so replay
    keeps working after the current graph is later edited or deleted and never
    reads the current graph or the removed snapshot tables (Global Constraint 6).
    """
    return {
        "request_id": event["id"], "tree": event["graph"],
        "trace": event["trace"], "request": event["request"],
        "decision": event["decision"], "answers": event["answers"],
        "node_labels": event["node_labels"],
    }
