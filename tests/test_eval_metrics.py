"""Tests for the unified offline metrics library (router/eval_metrics.py).

The 2026-10-24 review found the reports mixing up FPR and FDR; these tests pin
the two definitions (and the zero-denominator ``None`` convention), the micro
aggregation, the coverage-risk/AURC/ECE diagnostics and the bootstrap CIs.
Pure stdlib: no numpy/scipy.
"""

import math

import pytest

from router.eval_metrics import (Confusion, METRIC_NAMES, aurc, bootstrap_ci,
                                 bootstrap_delta_ci, confusion_from_pairs, ece,
                                 macro_average, micro_confusion,
                                 micro_confusions, percentile, risk_at_coverages,
                                 risk_coverage)


def test_confusion_metrics_and_fpr_is_not_fdr():
    c = Confusion(tp=10, fp=5, tn=20, fn=2)
    assert c.n == 37
    assert c.positives == 12 and c.negatives == 25
    assert c.predicted_positives == 15
    assert c.precision == pytest.approx(10 / 15)
    assert c.recall == pytest.approx(10 / 12)
    assert c.f1 == pytest.approx(2 * (10 / 15) * (10 / 12)
                                 / ((10 / 15) + (10 / 12)))
    # THE point of P1-A: false-positive RATE vs false-discovery RATE
    assert c.fpr == pytest.approx(5 / 25)          # fp/(fp+tn) = 0.20
    assert c.fdr == pytest.approx(5 / 15)          # fp/(fp+tp) = 0.333...
    assert c.fpr != c.fdr
    assert c.fnr == pytest.approx(2 / 12)
    assert c.accuracy == pytest.approx(30 / 37)
    as_dict = c.as_dict()
    assert as_dict["tp"] == 10 and as_dict["tn"] == 20
    assert as_dict["fpr"] == pytest.approx(0.2)
    assert as_dict["fdr"] == pytest.approx(1 / 3)


def test_confusion_zero_denominators_are_none_not_zero():
    empty = Confusion()
    assert empty.n == 0
    for metric in ("precision", "recall", "f1", "fpr", "fdr", "fnr", "accuracy"):
        assert getattr(empty, metric) is None
    # TN = 0 with flagged negatives: every negative was flagged -> FPR = 1.0
    c = Confusion(tp=3, fp=2, tn=0, fn=1)
    assert c.fpr == 1.0                      # fp/(fp+0) = 2/2
    assert c.fdr == pytest.approx(2 / 5)     # fp/(fp+tp) is a different number
    assert c.precision == pytest.approx(3 / 5)
    assert c.recall == pytest.approx(3 / 4)
    # no negatives at all -> FPR undefined; FDR/precision defined
    all_positive = Confusion(tp=5, fp=0, tn=0, fn=0)
    assert all_positive.fpr is None
    assert all_positive.fdr == 0.0
    assert all_positive.precision == 1.0
    # no negatives and no positive predictions -> both FPR and FDR undefined
    no_negatives_missed = Confusion(tp=0, fp=0, tn=0, fn=5)
    assert no_negatives_missed.fpr is None and no_negatives_missed.fdr is None
    assert no_negatives_missed.recall == 0.0
    # no predicted positives -> precision/FDR undefined, recall defined
    c2 = Confusion(tp=0, fp=0, tn=5, fn=2)
    assert c2.precision is None and c2.fdr is None
    assert c2.recall == 0.0 and c2.fpr == 0.0
    # negative/float counts are rejected
    with pytest.raises(ValueError):
        Confusion(tp=-1)
    with pytest.raises(ValueError):
        Confusion(fp=1.5)                     # type: ignore[arg-type]


def test_confusion_from_predictions_and_add():
    gold = [1, 1, 0, 0, 1, 0]
    pred = [1, 0, 1, 0, 1, 1]
    c = Confusion.from_predictions(gold, pred)
    assert (c.tp, c.fp, c.tn, c.fn) == (2, 2, 1, 1)
    assert confusion_from_pairs(gold, pred) == c
    with pytest.raises(ValueError):
        Confusion.from_predictions([1], [])
    assert (c + Confusion(tp=1)).tp == 3


def test_micro_and_macro_aggregation():
    parts = [Confusion(tp=2, fp=1, tn=3, fn=1),
             Confusion(tp=1, fp=0, tn=1, fn=3)]
    micro = micro_confusions(parts)
    assert (micro.tp, micro.fp, micro.tn, micro.fn) == (3, 1, 4, 4)
    assert micro.recall == pytest.approx(3 / 7)
    assert micro.fpr == pytest.approx(1 / 5)     # fp/(fp+tn), not FDR
    assert micro.fdr == pytest.approx(1 / 4)     # fp/(fp+tp)
    assert micro_confusion(parts) == micro       # singular alias
    assert macro_average(parts, "recall") == pytest.approx(
        ((2 / 3) + (1 / 4)) / 2)
    # empty inputs: micro is an empty matrix, macro is None
    assert micro_confusions([]) == Confusion()
    assert macro_average([], "f1") is None
    with pytest.raises(ValueError):
        macro_average(parts, "auc")
    assert set(METRIC_NAMES) >= {"precision", "recall", "f1", "fpr", "fdr",
                                 "accuracy"}


def test_risk_coverage_aurc_and_ece():
    # The most confident sample is wrong, then two correct ones.
    conf = [0.9, 0.8, 0.7]
    error = [True, False, False]
    curve = risk_coverage(conf, error)
    assert curve[-1]["coverage"] == 1.0
    assert curve[-1]["risk"] == pytest.approx(1 / 3)
    assert curve[0]["risk"] == 1.0
    assert aurc(conf, error) == pytest.approx((1.0 + 0.5 + 1 / 3) / 3)
    # a perfect ranking has a lower AURC than the reversed one
    assert aurc([0.9, 0.8, 0.7], [False, False, True]) < aurc(
        [0.9, 0.8, 0.7], [True, False, False])
    assert ece([0.8] * 10, [True] * 8 + [False] * 2) == pytest.approx(0.0)
    assert ece([0.8] * 10, [True] * 10) == pytest.approx(0.2)
    assert ece([], []) == 0.0
    assert risk_at_coverages(conf, error, coverages=(0.5, 1.0)) == [
        {"coverage": pytest.approx(2 / 3), "risk": 0.5, "n": 2},
        {"coverage": 1.0, "risk": pytest.approx(1 / 3), "n": 3},
    ]


def test_percentile():
    values = [4.0, 1.0, 3.0, 2.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 0.5) == pytest.approx(2.5)
    assert percentile(values, 1.0) == 4.0
    assert percentile([7.0], 0.5) == 7.0
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile(values, 1.5)


def test_bootstrap_ci_mean_and_median():
    values = [1, 0, 1, 1, 0, 1, 1, 0, 1, 1]           # mean 0.7
    low, high = bootstrap_ci(values, n_boot=1000, seed=3)
    assert low <= 0.7 <= high
    assert 0.0 <= low < high <= 1.0
    # deterministic for a fixed seed, and repeated calls agree
    assert bootstrap_ci(values, n_boot=500, seed=3) == (
        bootstrap_ci(values, n_boot=500, seed=3))
    # constant sample -> degenerate CI at the constant
    assert bootstrap_ci([0.5] * 10, n_boot=200, seed=0) == (0.5, 0.5)
    # named statistics and callable statistics both work
    low_med, high_med = bootstrap_ci(values, statistic="median", n_boot=500,
                                     confidence=0.95, seed=1)
    assert low_med <= 1.0 and high_med <= 1.0 and low_med <= high_med
    assert bootstrap_ci(values, statistic=lambda xs: max(xs), n_boot=100,
                        seed=0) == (1.0, 1.0)
    # confidence overrides alpha; wider confidence -> wider interval
    narrow = bootstrap_ci(values, n_boot=1000, alpha=0.20, seed=5)
    wide = bootstrap_ci(values, n_boot=1000, confidence=0.99, seed=5)
    assert (wide[1] - wide[0]) >= (narrow[1] - narrow[0])
    with pytest.raises(ValueError):
        bootstrap_ci([])
    with pytest.raises(ValueError):
        bootstrap_ci(values, statistic="mode")


def test_bootstrap_delta_ci():
    a = [1, 1, 0, 1, 0, 1, 1, 0, 1, 1]
    b = [1, 0, 0, 1, 0, 0, 1, 0, 0, 1]
    result = bootstrap_delta_ci(a, b, n_boot=1000, seed=2, confidence=0.95)
    assert result["delta"] == pytest.approx(0.3)
    assert result["ci_low"] <= result["delta"] <= result["ci_high"]
    assert set(result) == {"delta", "ci_low", "ci_high"}
    with pytest.raises(ValueError):
        bootstrap_delta_ci([1], [1, 0])
    with pytest.raises(ValueError):
        bootstrap_delta_ci([], [])


def test_metrics_match_the_shared_formulas_exactly():
    """Same numbers as the pipeline's ``entropy_confidence`` PII hit rule is not
    re-derived here, but precision/recall must agree with the plain formulas."""
    c = Confusion(tp=152, fp=23, tn=98, fn=32)
    precision = 152 / (152 + 23)
    recall = 152 / (152 + 32)
    assert c.precision == pytest.approx(precision)
    assert c.recall == pytest.approx(recall)
    assert c.f1 == pytest.approx(2 * precision * recall / (precision + recall))
    # the review's FPR bug: old code reported fp/(fp+tp)=FDR for ip_address
    assert c.fpr == pytest.approx(23 / (23 + 98))     # 0.1901...
    assert c.fdr == pytest.approx(23 / (23 + 152))    # 0.1314...
    assert not math.isclose(c.fpr, c.fdr)
