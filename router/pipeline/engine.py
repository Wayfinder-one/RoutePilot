"""Explicit-tree pipeline engine.

Walk the stations in order; the first station that produces a decision wins.
Inside a station, walk the explicit tree:

* a node's condition must match (m == 1, and confidence >= conf_min for model
  signals; rule signals are deterministic and skip the confidence floor);
* a LEAF is terminal — without weights it hits directly (route/block), with
  weights it is scored and must reach ``min_score``;
* a FORK tries its ordered children and follows the first one that produces a
  decision. When several matching children end in weighted leaves, the highest
  score wins (siblings under the same parent compete).

Evaluators are pure functions; the trace carries the matched path plus the
scored-leaf board for the console.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.matching import is_rule_signal, match_row
from router.pipeline.model import (DEFAULT_CONF_MIN, Station, TreeNode,
                                   Weight)
from router.pipeline.scoring import leaf_score


@dataclass
class Decision:
    kind: str                       # 'route' | 'block' | 'none'
    model: str | None = None
    station_id: str = ""
    node_id: str = ""               # leaf node id
    score: float | None = None
    path: list | None = None        # matched condition steps


def decision_to_dict(d: Decision) -> dict:
    return {"kind": d.kind, "model": d.model, "station_id": d.station_id,
            "node_id": d.node_id, "score": d.score}


def _floor(node: TreeNode) -> float:
    if node.conf_min is not None:
        return node.conf_min
    return DEFAULT_CONF_MIN


def _step(node: TreeNode, signals: dict) -> dict | None:
    """Trace step for one condition; None for unconditional nodes.

    Rule signals record conf=1.0 / conf_min=null (deterministic, no floor)."""
    if node.signal is None:
        return None
    m, c = match_row(node.signal, node.value, signals)
    rule = is_rule_signal(node.signal)
    floor = None if rule else _floor(node)
    passed = m == 1 and (rule or c >= floor)
    return {"signal": node.signal, "value": node.value,
            "conf": 1.0 if rule else c,
            "conf_min": floor, "m": m, "pass": passed}


def _condition_ok(node: TreeNode, signals: dict
                  ) -> tuple[bool, dict | None, bool]:
    """(matches, trace step, matched-but-below-floor)."""
    step = _step(node, signals)
    if step is None:                 # unconditional node: always matches
        return True, None, False
    return bool(step["pass"]), step, bool(step["m"] == 1 and not step["pass"])


def _leaf_decision(station: Station, leaf: TreeNode, signals: dict,
                   path: list, board: list) -> Decision | None:
    if not leaf.weights:
        kind = "block" if leaf.action == "block" else "route"
        return Decision(kind=kind,
                        model=None if kind == "block" else leaf.model,
                        station_id=station.id, node_id=leaf.id,
                        path=list(path))
    result = leaf_score(leaf.weights, signals, leaf.min_score)
    result.update({"node_id": leaf.id, "winner": False})
    board.append(result)
    if not result["passed"]:
        return None
    return Decision(kind="block" if leaf.action == "block" else "route",
                    model=None if leaf.action == "block" else leaf.model,
                    station_id=station.id, node_id=leaf.id,
                    score=result["score"], path=list(path))


def _eval_node(station: Station, node: TreeNode, signals: dict,
               path: list, board: list, miss: dict) -> Decision | None:
    """Evaluate a node whose own condition already matched.

    ``miss`` records where a request was cut (no child matched, or a matched
    child sat below its confidence floor) so the trace can show it."""
    if node.kind == "leaf" or not node.children:
        return _leaf_decision(station, node, signals, path, board)

    matched = []
    for child in node.children:
        ok, step, cut = _condition_ok(child, signals)
        if not ok:
            if cut and "path" not in miss:
                miss["path"] = path + [step]
            continue
        child_path = path + [s for s in (step,) if s]
        matched.append((child, child_path))
    if not matched:
        return None

    # Ordered traversal: the first matching child that yields a decision wins.
    # Weighted leaves under the same parent compete by score instead.
    decisions = []
    for child, child_path in matched:
        d = _eval_node(station, child, signals, child_path, board, miss)
        if d is not None:
            decisions.append(d)
    if not decisions:
        return None
    scored = [d for d in decisions if d.score is not None]
    if scored:
        return max(scored, key=lambda d: d.score)   # ties keep the earlier
    return decisions[0]


def _eval_station(station: Station, signals: dict) -> tuple[Decision | None, dict]:
    path: list = []
    board: list = []
    miss: dict = {}
    root = station.root
    if root is None:
        return None, {"station_id": station.id, "name": station.name,
                      "type": station.type, "result": "miss",
                      "path": [], "board": []}
    ok, step, cut = _condition_ok(root, signals)
    if step is not None:
        path.append(step)
    if not ok:
        return None, {"station_id": station.id, "name": station.name,
                      "type": station.type, "result": "miss",
                      "path": path, "board": []}
    decision = _eval_node(station, root, signals, path, board, miss)
    if decision is not None:
        # mark the winning board entry, if this decision came from a score
        for entry in board:
            if entry["node_id"] == decision.node_id:
                entry["winner"] = True
    miss_path = miss.get("path") if not decision else None
    return decision, {"station_id": station.id, "name": station.name,
                      "type": station.type,
                      "result": "hit" if decision else "miss",
                      "path": decision.path if decision else (miss_path or path),
                      "board": board}


def evaluate(stations: list[Station], signals: dict) -> tuple[Decision, list[dict]]:
    trace: list[dict] = []
    for station in stations:
        decision, entry = _eval_station(station, signals)
        trace.append(entry)
        if decision is not None:
            return decision, trace
    return Decision(kind="none"), trace     # no fallback: health check prevents it
