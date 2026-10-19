"""Leaf scoring formula (weighted attainment)."""

from router.pipeline.model import Weight
from router.pipeline.scoring import leaf_score

SIG = {"domain": {"value": "health", "confidence": 0.9, "is_rule": False},
       "complexity": {"value": "hard", "confidence": 0.8, "is_rule": False}}


def test_weighted_attainment():
    out = leaf_score([Weight("domain", "health", 0.5),
                      Weight("complexity", "hard", 0.5)], SIG, 0.7)
    assert abs(out["score"] - 0.85) < 1e-9     # (0.45 + 0.40) / 1.0
    assert out["passed"] is True


def test_zero_weight_scores_zero_and_fails():
    out = leaf_score([], SIG, 0.1)
    assert out["score"] == 0.0 and out["passed"] is False
    out2 = leaf_score([Weight("domain", "health", 0.0)], SIG, 0.0)
    assert out2["passed"] is False


def test_min_score_is_respected():
    weights = [Weight("domain", "health", 1.0)]
    assert leaf_score(weights, SIG, 0.95)["passed"] is False
    assert leaf_score(weights, SIG, 0.85)["passed"] is True


def test_lines_carry_c_and_m():
    out = leaf_score([Weight("domain", "health", 1.0),
                      Weight("complexity", "easy", 1.0)], SIG, 0.0)
    lines = {line["signal"]: line for line in out["lines"]}
    assert lines["domain"]["m"] == 1 and abs(lines["domain"]["c"] - 0.9) < 1e-9
    assert lines["complexity"]["m"] == 0
