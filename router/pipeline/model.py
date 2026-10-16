"""Pipeline data model: Node -> Branch -> Row. JSON-serializable both ways.
node_from_dict is the trust boundary: anything malformed raises ValueError,
which the API layer turns into 422. Nothing downstream re-validates shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NODE_TYPES = ("gate", "score")
ACTIONS = ("block", "route")


@dataclass
class Row:
    signal: str
    value: str
    weight: float | None = None      # score rows
    conf_min: float | None = None    # gate rows


@dataclass
class Branch:
    id: str
    name: str = ""
    rows: list[Row] = field(default_factory=list)
    action: str = "route"            # gate only: 'block' | 'route'
    model: str | None = None
    min_score: float | None = None   # score only


@dataclass
class Node:
    id: str
    type: str                        # 'gate' | 'score'
    name: str = ""
    branches: list[Branch] = field(default_factory=list)


def _unit(x, field_name):
    if not isinstance(x, (int, float)) or isinstance(x, bool) or not 0 <= x <= 1:
        raise ValueError(f"{field_name} must be a number in [0,1], got {x!r}")
    return float(x)


def _row_from_dict(d: dict, node_type: str) -> Row:
    if not isinstance(d, dict) or not isinstance(d.get("signal"), str):
        raise ValueError(f"row must be an object with a signal string: {d!r}")
    if node_type == "score":
        return Row(signal=d["signal"], value=str(d.get("value", "")),
                   weight=_unit(d.get("weight"), "weight"))
    conf = d.get("conf_min")
    return Row(signal=d["signal"], value=str(d.get("value", "")),
               conf_min=None if conf is None else _unit(conf, "conf_min"))


def node_from_dict(d: dict) -> Node:
    if not isinstance(d, dict) or d.get("type") not in NODE_TYPES \
            or not isinstance(d.get("id"), str):
        raise ValueError(f"node must have id and type in {NODE_TYPES}: {d!r}")
    branches = []
    for b in d.get("branches") or []:
        if not isinstance(b, dict) or not isinstance(b.get("id"), str):
            raise ValueError(f"branch must be an object with an id: {b!r}")
        action = b.get("action", "route")
        if d["type"] == "gate" and action not in ACTIONS:
            raise ValueError(f"gate branch action must be in {ACTIONS}")
        min_score = b.get("min_score")
        branches.append(Branch(
            id=b["id"], name=str(b.get("name", "")),
            rows=[_row_from_dict(r, d["type"]) for r in b.get("rows") or []],
            action=action,
            model=b.get("model"),
            min_score=None if min_score is None else _unit(min_score, "min_score")))
    return Node(id=d["id"], type=d["type"], name=str(d.get("name", "")),
                branches=branches)


def node_to_dict(n: Node) -> dict:
    return {"id": n.id, "type": n.type, "name": n.name, "branches": [
        {"id": b.id, "name": b.name, "action": b.action, "model": b.model,
         "min_score": b.min_score,
         "rows": [{"signal": r.signal, "value": r.value, "weight": r.weight,
                   "conf_min": r.conf_min} for r in b.rows]}
        for b in n.branches]}
