"""Gate-node evaluator (design doc section 3.2): ordered branches, first hit.

A row passes when it matches (m == 1) with confidence >= conf_min. A branch
fires when ALL its rows pass; a branch with zero rows always fires, which is
how the fallback node is expressed. Gate actions: 'block' or 'route'.
"""

from __future__ import annotations

from router.pipeline.matching import match_row
from router.pipeline.model import Node, Row


def _row_ok(row: Row, signals: dict) -> bool:
    m, c = match_row(row.signal, row.value, signals)
    return m == 1 and c >= (row.conf_min if row.conf_min is not None else 0.0)


def eval_gate(node: Node, signals: dict) -> dict:
    for b in node.branches:
        if all(_row_ok(r, signals) for r in b.rows):
            kind = "block" if b.action == "block" else "route"
            return {"decision": {"kind": kind,
                                 "model": None if kind == "block" else b.model,
                                 "node_id": node.id, "branch_id": b.id},
                    "trace": {"node_id": node.id, "name": node.name, "type": "gate",
                              "result": "hit", "board": None,
                              "branch": {"name": b.name, "action": b.action,
                                         "model": b.model}}}
    return {"decision": None,
            "trace": {"node_id": node.id, "name": node.name, "type": "gate",
                      "result": "miss", "branch": None, "board": None}}
