"""Split-node evaluator (SPEC-TREE-4 section 4).

Walk the derived tree: at every layer exactly one group may admit the request
(m == 1, plus ``confidence >= node.floor`` for model signals; rule signals are
deterministic and skip the floor). No group admits it -> the node misses and
the request moves to the next node. At the leaf set, remaining conditions
score per ``scoring.py``; a fully determined path (zero remaining conditions)
hits outright without scoring or ``min_score`` (section 4.2).

The trace carries the layer ``path`` plus the score-shaped per-branch
``board``, so the console renders both node kinds with one renderer.
"""

from __future__ import annotations

from router.pipeline.matching import match_row
from router.pipeline.model import Branch, Node
from router.pipeline.scoring import branch_score
from router.pipeline.tree import derive, is_rule_signal, node_floor, \
    remaining_rows


def _layer_step(key: str, value: str | None, signals: dict,
                floor: float) -> dict | None:
    """Layer record per section 4.4. None when the group does not match at all;
    ``pass=False`` when it matches but sits below the entry floor. Rule signals
    record conf=1.0 / floor=null and never take the floor gate (section 3.5)."""
    m, c = match_row(key, value, signals)
    if value is None or m != 1:
        return None
    rule = is_rule_signal(key)
    step = {"signal": key, "value": value,
            "conf": 1.0 if rule else c,
            "floor": None if rule else floor, "pass": True}
    if not rule and c < floor:
        step["pass"] = False
    return step


def _decide(node: Node, branch: Branch, score: float | None) -> dict:
    kind = "block" if branch.action == "block" else "route"
    return {"kind": kind, "model": None if kind == "block" else branch.model,
            "node_id": node.id, "branch_id": branch.id, "score": score}


def _trace(node: Node, result: str, path: list[dict], board: list) -> dict:
    return {"node_id": node.id, "name": node.name, "type": "split",
            "result": result, "path": path, "board": board}


def eval_split(node: Node, signals: dict) -> dict:
    floor = node_floor(node)
    level = derive(node, node.branches, set())
    used: set[str] = set()
    path: list[dict] = []

    while "groups" in level:                      # descend layer by layer
        key = level["key"]
        chosen = None
        blocked = None                            # matched but below the floor
        for g in level["groups"]:
            step = _layer_step(key, g["value"], signals, floor)
            if step is None:
                continue
            if step["pass"]:
                chosen = g
                break
            blocked = step
        if chosen is None:
            if blocked is not None:
                path.append(blocked)              # show where the floor cut it
            return {"decision": None, "trace": _trace(node, "miss", path, [])}
        path.append(_layer_step(key, chosen["value"], signals, floor))
        used.add(key)
        level = chosen["child"]

    leaves: list[Branch] = level["leaves"]
    # Fully determined path: first leaf with no remaining conditions hits
    # outright — skip scoring and min_score (section 4.2).
    for b in leaves:
        if not remaining_rows(b, used):
            decision = _decide(node, b, None)
            return {"decision": decision, "trace": _trace(node, "hit", path, [])}

    board = [branch_score(b, signals, remaining_rows(b, used))
             for b in leaves]
    win = None
    for bs in board:
        if bs["passed"] and (win is None or bs["score"] > win["score"]):
            win = bs                              # strictly greater: first wins
    if win is None:
        return {"decision": None,
                "trace": _trace(node, "miss", path, board)}
    win["winner"] = True
    branch = next(b for b in leaves if b.id == win["branch_id"])
    decision = _decide(node, branch, win["score"])
    return {"decision": decision, "trace": _trace(node, "hit", path, board)}
