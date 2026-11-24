"""请求图快照的纯序列化层：不读数据库、不做 I/O。

热路径（``RequestLogger.enqueue()``）只把整图文档挂到记录的私有字段
``_graph_snapshot`` 上；真正的两行投影（图去重行 + 请求 trace 行）由
``snapshot_values()`` 在 ``flush()`` 排空时一次性算出，再交给 worker 线程写库。

* 图快照按内容寻址：``graph`` 列存 ``canonical_graph_document()`` 产出的
  canonical JSON（键排序、无空格），``snapshot_id`` 是它的 SHA-256；同一张图的
  多条请求只落一行图。canonical 文档只删除 ``enabled``/``default``/``tree_revision``/
  ``updated_at``/``document_sha256`` 这些派生或易变 envelope 元数据——它们不属于
  「图身份 + 行为 + 布局」，留在文档里会让同一张图每次保存都产生新快照、把图去重
  打散。存储、ID 与内存展示共用同一份文档；逐请求 revision 仍由该请求的账本字段
  提供，不取共享图行的第一写者元数据。旧 v5 行保留历史 ID，在 API 读出口用同一个
  过滤函数去掉这些元数据。
* 请求快照按 ``request_id`` 一行：引用图 ID 并保存本次请求的 trace。
* 顶层 ``canvas`` 是布局坐标的唯一来源；``tree_to_dict()`` 已包含它，
  不引用会被后续编辑覆盖的当前 tree。

私有字段命名以 ``_`` 开头，API 列表必须在出口过滤掉，避免整图随列表下发。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from router.routing.graph.model import canonical_json
from router.routing.tree import tree_to_dict

#: 快照端点回显的请求字段白名单（与列表投影保持同一词汇表）。
REQUEST_FIELDS = (
    "id", "ts", "text", "decision", "model", "target_id", "tree_id",
    "tree_revision", "snapshot_revision", "selection_reason", "outcome_state",
)


#: Envelope metadata that changes on save/lifecycle/publish without changing the
#: graph.  ``default`` is lifecycle state like ``enabled``: switching the default
#: graph must not split graph dedup.  They are display-only: storage, content
#: addressing and the read outlet all drop them so one graph keeps one snapshot
#: row and one payload, while behaviour fields (``departments``, ``root``, run
#: fields) are always kept.
_VOLATILE_FIELDS = frozenset(
    {"enabled", "default", "tree_revision", "updated_at", "document_sha256"})


def canonical_graph_document(document: dict) -> dict:
    """The one graph document used for storage, addressing and display."""
    return {
        key: value for key, value in document.items()
        if key not in _VOLATILE_FIELDS
    }


def _created_at(record: dict) -> str:
    value = record.get("ts")
    if isinstance(value, str) and value:
        return value
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _trace_json(trace) -> str:
    try:
        return canonical_json(trace if isinstance(trace, list) else [])
    except (TypeError, ValueError):
        # trace 是 best-effort 运行数据：不可序列化的值降级成字符串，
        # 不能因为一条脏事件丢掉整批日志。
        return json.dumps(trace if isinstance(trace, list) else [],
                          ensure_ascii=False, default=str)


def capture_snapshot(record: dict, snapshot) -> None:
    """Attach the graph document + content hash to an in-flight record.

    Pure in-memory work: the tree is serialized once here so a later edit of
    the live graph cannot change what this request logged.
    """
    tree = getattr(snapshot, "tree", None)
    if tree is None:
        return
    document = canonical_graph_document(tree_to_dict(tree))
    content = canonical_json(document)
    snapshot_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
    record["graph_snapshot_id"] = snapshot_id
    record["_graph_snapshot"] = {
        "snapshot_id": snapshot_id,
        "snapshot_revision": getattr(snapshot, "snapshot_revision", None),
        "tree": document,
        "content": content,
    }


def snapshot_values(record: dict) -> tuple[tuple, tuple] | None:
    """Project the record's captured snapshot onto the two immutable DB rows.

    Returns ``(graph_row, request_row)`` in column order, or ``None`` when the
    record carries no graph (fixed-model/old requests).
    """
    stored = record.get("_graph_snapshot")
    request_id = record.get("id")
    if not isinstance(stored, dict) or not request_id:
        return None
    snapshot_id = stored.get("snapshot_id")
    content = stored.get("content")
    if not snapshot_id or not isinstance(content, str):
        return None
    created_at = _created_at(record)
    graph_row = (
        snapshot_id,
        record.get("tree_id"),
        stored.get("snapshot_revision") or record.get("snapshot_revision"),
        content,
        created_at,
    )
    request_row = (
        request_id,
        snapshot_id,
        _trace_json(record.get("trace")),
        created_at,
    )
    return graph_row, request_row


def memory_snapshot(record: dict) -> dict | None:
    """The snapshot-endpoint payload for a live (still in the ring) record."""
    stored = record.get("_graph_snapshot")
    request_id = record.get("id")
    if not isinstance(stored, dict) or not request_id:
        return None
    trace = record.get("trace")
    return {
        "request_id": request_id,
        "graph_snapshot_id": stored.get("snapshot_id"),
        "tree": stored.get("tree"),
        "trace": list(trace) if isinstance(trace, list) else [],
        "request": {
            key: record[key] for key in REQUEST_FIELDS if key in record
        },
    }
