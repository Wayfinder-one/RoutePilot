"""Leaf scoring for the unified routing tree.

A leaf with weighted conditions scores
``score = sum(w*c*m) / sum(w)`` — the weighted attainment rate, always in
[0,1] because every term w*c*m <= w. ``passed`` requires at least one weight
and ``score >= min_score`` (leaf-level; unconfigured leaves fall back to
``DEFAULT_MIN_SCORE`` = 0.5, matching the console's editor default — a 0.0
fallback would let a weighted leaf pass with zero attainment).
Leaves without weights are direct hits and never call this module.
"""

from __future__ import annotations

from router.pipeline.matching import match_row
from router.pipeline.tree import Weight

#: Fallback attainment floor when a weighted terminal does not configure
#: ``min_score`` (matches the console editor default; a 0.0 floor would let a
#: weighted leaf pass with zero attainment).  Owned here with the formula.
DEFAULT_MIN_SCORE = 0.50


def leaf_score(weights: list[Weight], signals: dict,
               min_score: float | None) -> dict:
    lines = []
    for w in weights:
        m, c = match_row(w.signal, w.value, signals)
        lines.append({"signal": w.signal, "value": w.value,
                      "weight": w.weight, "c": c, "m": m,
                      "contrib": w.weight * c * m})
    sum_w = sum(line["weight"] for line in lines)
    total = sum(line["contrib"] for line in lines)
    score = total / sum_w if sum_w > 0 else 0.0
    floor = DEFAULT_MIN_SCORE if min_score is None else min_score
    return {"lines": lines, "sum_w": sum_w, "score": score,
            "min_score": floor,
            "passed": sum_w > 0 and score >= floor}
