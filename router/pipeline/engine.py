"""Pipeline engine: walk nodes in order; the first node that produces a
decision wins. Evaluators are registered per node type (EVALUATORS is the
extension point: a new node type = one entry here plus a UI fragment).
Evaluators are pure functions; all side effects live at the API boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.gates import eval_gate
from router.pipeline.model import Node
from router.pipeline.scoring import eval_score
from router.pipeline.splits import eval_split

EVALUATORS = {"gate": eval_gate, "score": eval_score, "split": eval_split}


@dataclass
class Decision:
    kind: str                       # 'route' | 'block' | 'none'
    model: str | None = None
    node_id: str = ""
    branch_id: str = ""
    score: float | None = None


def decision_to_dict(d: Decision) -> dict:
    return {"kind": d.kind, "model": d.model, "node_id": d.node_id,
            "branch_id": d.branch_id, "score": d.score}


def evaluate(nodes: list[Node], signals: dict) -> tuple[Decision, list[dict]]:
    trace: list[dict] = []
    for node in nodes:
        evaluator = EVALUATORS[node.type]   # unknown type is a health-check error
        out = evaluator(node, signals)
        trace.append(out["trace"])
        if out["decision"] is not None:
            d = out["decision"]
            return Decision(kind=d["kind"], model=d.get("model"),
                            node_id=d["node_id"], branch_id=d["branch_id"],
                            score=d.get("score")), trace
    return Decision(kind="none"), trace     # no fallback: health check prevents it
