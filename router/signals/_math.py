"""Shared numeric helpers for the signal taggers.

Both the JEV tagger and the checkpoint tagger turn model outputs into signal
values with the same formulas. One copy keeps the two engines comparable: the
PII hit rule in particular is shared, so both report the same ``pii`` value for
the same input (a difference here used to mean "the safety gate behaves
differently depending on the engine").

Nothing in this module imports torch: it is pure math, unit-testable without
a model.
"""

from __future__ import annotations

import math

#: PII hit rule (shared by every tagger):
#:
#: * the per-type probability must be strictly above the threshold — at
#:   exactly 0.5 the model has no evidence either way, and the positive class
#:   is the intercepting one for the safety gate;
#: * the binary entropy confidence must be at least PII_MIN_CONFIDENCE, so a
#:   barely-positive probability does not enter the signal with a ~0
#:   confidence (such a row can never pass a condition floor anyway).
#:
#: Deterministic regex hits (``merge_rule_pii``) enter at confidence 1.0.
PII_HIT_THRESHOLD = 0.5
PII_MIN_CONFIDENCE = 0.2


def softmax(logits) -> list[float]:
    """Numerically stable softmax (max shifted out)."""
    m = max(logits)
    exps = [math.exp(v - m) for v in logits]
    total = sum(exps)
    return [e / total for e in exps]


def entropy_confidence(probs: list[float]) -> float:
    """1 - H(p)/log K, clamped to [0,1].

    ``K <= 1`` returns **0.0**: a choice signal with fewer than two candidates
    has no distribution to compare, so it carries no confidence evidence. The
    old 1.0 convention let a misconfigured (single-candidate) choice pass any
    confidence floor; the question-config validator now rejects K < 2 and this
    helper reports "no evidence" for anything that still reaches it. K >= 2 is
    unchanged bit-for-bit (checkpoint heads / JEV choices).
    """
    k = len(probs)
    if k <= 1:
        return 0.0
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return max(0.0, min(1.0, 1.0 - h / math.log(k)))


def binary_entropy_confidence(p: float) -> float:
    """1 - H2(p)/log 2 for a per-type presence probability."""
    p = max(0.0, min(1.0, p))
    if p <= 0.0 or p >= 1.0:
        return 1.0
    h2 = -(p * math.log(p) + (1 - p) * math.log(1 - p)) / math.log(2)
    return max(0.0, min(1.0, 1.0 - h2))


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def pii_hits(probs: list[float], labels: list[str]
             ) -> tuple[list[str], dict[str, float]]:
    """Apply the shared PII hit rule.

    Returns ``(hits, conf_by_value)``: the hit types (sorted by label, so
    every tagger reports the same order) and the binary entropy confidence
    per hit.
    """
    hits: list[str] = []
    conf_by_value: dict[str, float] = {}
    for label, p in zip(labels, probs):
        conf = binary_entropy_confidence(p)
        if p > PII_HIT_THRESHOLD and conf >= PII_MIN_CONFIDENCE:
            hits.append(label)
            conf_by_value[label] = conf
    hits.sort()
    return hits, {label: conf_by_value[label] for label in hits}
