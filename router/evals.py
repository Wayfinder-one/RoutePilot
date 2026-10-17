"""Evaluation (M3, design doc section 8): replay and labeled-set reports.

Replay rebuilds the signal pack from ``request_signals`` and re-runs the current
pipeline against the destination recorded at the time. Labeled sets tag a jsonl
of ``{text, expect_model}`` and compare. Both funnel into :func:`summarize`,
which produces the accuracy, a confusion matrix and the mismatching samples.
"""

from __future__ import annotations


def summarize(mode: str, pairs: list[tuple], mismatches: list[dict],
              **extra) -> dict:
    """pairs = [(expect, got)] -> accuracy + confusion matrix + mismatches."""
    total = len(pairs)
    matched = sum(1 for expect, got in pairs if expect == got)
    counts: dict[tuple, int] = {}
    for expect, got in pairs:
        counts[(expect, got)] = counts.get((expect, got), 0) + 1
    matrix = [{"expect": e, "got": g, "count": c}
              for (e, g), c in sorted(counts.items(), key=lambda kv: -kv[1])]
    return {"mode": mode, "total": total, "matched": matched,
            "rate": (matched / total) if total else 0.0,
            "matrix": matrix, "mismatches": mismatches, **extra}
