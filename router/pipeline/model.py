"""Explicit-tree pipeline model.

Each station is an independent ordered tree (2026-10-19 rewrite, replacing the
derived `split` model):

  Station {id, type: gate|score|split, name, root}
  TreeNode {id, condition(signal, value, conf_min) | unconditional,
            kind: leaf|fork, action, model, min_score, weights[], children[]}

A node is either a LEAF (terminal: direct hit, or weighted scoring with
``weights`` + ``min_score``) or a FORK (ordered children; the first child whose
condition matches is followed). Children are ordered and may use different
signals — they are independent branches, not grouped by value.

This module is the trust boundary: anything malformed raises ValueError and the
API layer turns it into a 422. Nothing downstream re-validates shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATION_TYPES = ("gate", "score", "split")
ACTIONS = ("block", "route")
KINDS = ("leaf", "fork")

DEFAULT_CONF_MIN = 0.50
DEFAULT_WEIGHT = 0.50
DEFAULT_MIN_SCORE = 0.50


@dataclass
class Weight:
    """One weighted condition on a leaf (拖拽信号进来的「加权」条件)."""
    signal: str
    value: str
    weight: float = DEFAULT_WEIGHT


@dataclass
class TreeNode:
    id: str
    signal: str | None = None        # None = unconditional (entry / fallback)
    value: str = ""
    conf_min: float | None = None    # condition confidence floor (model signals)
    kind: str = "leaf"               # 'leaf' = terminal, 'fork' = has children
    action: str = "route"            # leaf: 'block' | 'route'
    model: str | None = None         # leaf route target
    min_score: float | None = None   # leaf with weights: attainment floor
    weights: list[Weight] = field(default_factory=list)
    children: list[TreeNode] = field(default_factory=list)


@dataclass
class Station:
    id: str
    type: str = "split"              # 'gate' | 'score' | 'split' (defaults only)
    name: str = ""
    root: TreeNode | None = None


def _unit(x, name: str) -> float:
    if not isinstance(x, (int, float)) or isinstance(x, bool) or not 0 <= x <= 1:
        raise ValueError(f"{name} must be a number in [0,1], got {x!r}")
    return float(x)


def _weight_from_dict(d: dict) -> Weight:
    if not isinstance(d, dict) or not isinstance(d.get("signal"), str):
        raise ValueError(f"weight must be an object with a signal: {d!r}")
    return Weight(signal=d["signal"], value=str(d.get("value", "")),
                  weight=_unit(d.get("weight"), "weight"))


def node_from_dict(d: dict) -> TreeNode:
    if not isinstance(d, dict) or not isinstance(d.get("id"), str):
        raise ValueError(f"node must be an object with an id: {d!r}")
    kind = d.get("kind", "leaf")
    if kind not in KINDS:
        raise ValueError(f"node kind must be in {KINDS}: {d!r}")
    signal = d.get("signal")
    if signal is not None and not isinstance(signal, str):
        raise ValueError(f"node signal must be a string or null: {d!r}")
    action = d.get("action", "route")
    if action not in ACTIONS:
        raise ValueError(f"node action must be in {ACTIONS}: {d!r}")
    conf = d.get("conf_min")
    min_score = d.get("min_score")
    children = [node_from_dict(c) for c in d.get("children") or []]
    weights = [_weight_from_dict(w) for w in d.get("weights") or []]
    if kind == "fork" and not children:
        raise ValueError(f"fork node {d.get('id')} needs at least one child")
    if kind == "fork" and weights:
        raise ValueError(f"fork node {d.get('id')} cannot carry weights")
    if kind == "leaf" and children:
        raise ValueError(f"leaf node {d.get('id')} cannot have children")
    return TreeNode(
        id=d["id"], signal=signal, value=str(d.get("value", "")),
        conf_min=None if conf is None else _unit(conf, "conf_min"),
        kind=kind, action=action, model=d.get("model"),
        min_score=None if min_score is None else _unit(min_score, "min_score"),
        weights=weights, children=children)


def node_to_dict(n: TreeNode) -> dict:
    return {"id": n.id, "signal": n.signal, "value": n.value,
            "conf_min": n.conf_min, "kind": n.kind,
            "action": n.action, "model": n.model, "min_score": n.min_score,
            "weights": [{"signal": w.signal, "value": w.value,
                         "weight": w.weight} for w in n.weights],
            "children": [node_to_dict(c) for c in n.children]}


def station_from_dict(d: dict) -> Station:
    if not isinstance(d, dict) or not isinstance(d.get("id"), str):
        raise ValueError(f"station must be an object with an id: {d!r}")
    if d.get("type") not in STATION_TYPES:
        raise ValueError(f"station type must be in {STATION_TYPES}: {d!r}")
    root = node_from_dict(d["root"]) if d.get("root") is not None else None
    return Station(id=d["id"], type=d["type"], name=str(d.get("name", "")),
                   root=root)


def station_to_dict(s: Station) -> dict:
    return {"id": s.id, "type": s.type, "name": s.name,
            "root": node_to_dict(s.root) if s.root is not None else None}
