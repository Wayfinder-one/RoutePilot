"""Score-node evaluator (design doc section 3.3).

Branch score = sum(w*c*m) / sum(w): the weighted attainment rate, always in
[0,1] because each term w*c*m <= w. The winning branch is the highest scorer
at or above its min_score; ties keep branch order (deterministic, replayable).
"""

from __future__ import annotations

from router.pipeline.matching import match_row
from router.pipeline.model import Branch, Node


def branch_score(branch: Branch, signals: dict, rows=None) -> dict:
    """Weighted attainment over ``rows`` (defaults to the branch's own rows).

    Split leaves are scored only on their remaining conditions (SPEC-TREE-4
    section 4.2), so the split evaluator passes the subset explicitly.
    """
    lines = []
    for r in (branch.rows if rows is None else rows):
        m, c = match_row(r.signal, r.value, signals)
        w = r.weight or 0.0
        lines.append({"signal": r.signal, "value": r.value,
                      "w": w, "c": c, "m": m, "contrib": w * c * m})
    sum_w = sum(l["w"] for l in lines)
    total = sum(l["contrib"] for l in lines)
    score = total / sum_w if sum_w > 0 else 0.0
    min_score = branch.min_score if branch.min_score is not None else 0.0
    return {"branch_id": branch.id, "name": branch.name, "model": branch.model,
            "lines": lines, "sum_w": sum_w, "score": score,
            "min_score": min_score,
            "passed": sum_w > 0 and score >= min_score, "winner": False}


def eval_score(node: Node, signals: dict) -> dict:
    board = [branch_score(b, signals) for b in node.branches]
    win = None
    for bs in board:
        if bs["passed"] and (win is None or bs["score"] > win["score"]):
            win = bs                     # strictly greater => first wins ties
    decision = None
    if win is not None:
        win["winner"] = True
        decision = {"kind": "route", "model": win["model"], "node_id": node.id,
                    "branch_id": win["branch_id"], "score": win["score"]}
    return {"decision": decision,
            "trace": {"node_id": node.id, "name": node.name, "type": "score",
                      "result": "hit" if win else "miss",
                      "branch": None, "board": board}}
