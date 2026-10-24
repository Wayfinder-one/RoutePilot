"""Unified evaluation metrics for the offline JEV tooling (pure stdlib).

Every offline report (dump calibration, window A/B, cheap-sufficient, PII layer
analysis) must call this module instead of re-implementing formulas. The
2026-10-24 review found exactly this class of bug: the calibrate script called
``fp/(fp+tp)`` "FPR" while that quantity is the false-discovery rate (FDR).

Definitions used here (binary confusion matrix, gold vs prediction):

* ``FPR = fp / (fp + tn)`` -- false-positive **rate**: share of the negatives
  that were predicted positive (also called false-alarm rate);
* ``FDR = fp / (fp + tp)`` -- false-discovery **rate**: share of the positive
  predictions that were wrong.

They are different quantities and are always exposed separately. A zero
denominator yields ``None`` (undefined), never a fake ``0.0``.

The module imports only ``dataclasses``/``typing``/``math``/``random`` so the
local CPU venv (no numpy/scipy) can run the tests and the CPU scripts.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

#: Metrics exposed by :class:`Confusion` (valid ``macro_average`` names).
METRIC_NAMES = ("precision", "recall", "f1", "fpr", "fdr", "fnr", "accuracy")


def _ratio(numerator: float, denominator: float) -> float | None:
    """``numerator/denominator`` or ``None`` when the denominator is 0."""
    if not denominator:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class Confusion:
    """Binary confusion matrix (gold vs prediction).

    ``tp``/``fp``/``tn``/``fn`` are counts. All metrics are read-only
    properties; ``precision``/``recall``/``f1``/``fpr``/``fdr``/``fnr``/
    ``accuracy`` return ``None`` when their denominator is zero.
    """

    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def __post_init__(self) -> None:
        for name in ("tp", "fp", "tn", "fn"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"Confusion.{name} must be a non-negative int, "
                                 f"got {value!r}")

    # ---- counts -----------------------------------------------------------
    @property
    def n(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def positives(self) -> int:
        return self.tp + self.fn

    @property
    def negatives(self) -> int:
        return self.tn + self.fp

    @property
    def predicted_positives(self) -> int:
        return self.tp + self.fp

    # ---- metrics ----------------------------------------------------------
    @property
    def precision(self) -> float | None:
        """tp/(tp+fp)."""
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        """tp/(tp+fn)."""
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None:
            return None
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    @property
    def fpr(self) -> float | None:
        """False-positive RATE: fp/(fp+tn) (share of negatives flagged)."""
        return _ratio(self.fp, self.fp + self.tn)

    @property
    def fdr(self) -> float | None:
        """False-discovery RATE: fp/(fp+tp) (share of positive predictions wrong)."""
        return _ratio(self.fp, self.fp + self.tp)

    @property
    def fnr(self) -> float | None:
        """False-negative RATE: fn/(fn+tp)."""
        return _ratio(self.fn, self.fn + self.tp)

    @property
    def accuracy(self) -> float | None:
        return _ratio(self.tp + self.tn, self.n)

    def as_dict(self) -> dict[str, int | float | None]:
        """Counts + every metric (handy for JSON reports)."""
        out: dict[str, int | float | None] = {
            "tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn,
            "n": self.n, "positives": self.positives,
            "negatives": self.negatives,
            "predicted_positives": self.predicted_positives,
        }
        for name in METRIC_NAMES:
            out[name] = getattr(self, name)
        return out

    @classmethod
    def from_predictions(cls, gold: Sequence[bool | int],
                         pred: Sequence[bool | int]) -> "Confusion":
        """Build the confusion matrix from per-sample gold/prediction flags."""
        if len(gold) != len(pred):
            raise ValueError(f"gold/pred length mismatch: {len(gold)} vs {len(pred)}")
        tp = fp = tn = fn = 0
        for truth, guess in zip(gold, pred):
            if bool(truth):
                if bool(guess):
                    tp += 1
                else:
                    fn += 1
            elif bool(guess):
                fp += 1
            else:
                tn += 1
        return cls(tp=tp, fp=fp, tn=tn, fn=fn)

    def __add__(self, other: "Confusion") -> "Confusion":
        if not isinstance(other, Confusion):
            return NotImplemented
        return Confusion(tp=self.tp + other.tp, fp=self.fp + other.fp,
                         tn=self.tn + other.tn, fn=self.fn + other.fn)


def micro_confusions(confusions: Iterable[Confusion]) -> Confusion:
    """Sum the counts first, then read the metrics off the summed matrix.

    Use for multi-class/multi-type micro averages: ``micro_confusions(parts)
    .f1`` etc. (``micro_confusion`` is kept as an alias).
    """
    total = Confusion()
    for entry in confusions:
        total = total + entry
    return total


#: Backwards-friendly singular alias.
micro_confusion = micro_confusions


def macro_average(confusions: Iterable[Confusion], metric: str) -> float | None:
    """Mean of one metric over the non-undefined entries (``None`` if none)."""
    if metric not in METRIC_NAMES:
        raise ValueError(f"unknown metric {metric!r}; choose from {METRIC_NAMES}")
    values = [getattr(entry, metric) for entry in confusions]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


# --------------------------------------------------------------------------- #
# coverage-risk / AURC / ECE
# --------------------------------------------------------------------------- #
def risk_coverage(confidences: Sequence[float], errors: Sequence[bool]
                  ) -> list[dict]:
    """Risk/coverage curve: admit the most-confident fraction, measure errors.

    Returns one row per reported admitted count ``k`` (``coverage = k/N``,
    ``risk`` = error rate among the ``k`` most confident samples,
    ``threshold`` = the confidence at that cut).
    """
    if len(confidences) != len(errors):
        raise ValueError("confidences/errors length mismatch")
    pairs = sorted(zip(confidences, errors), key=lambda pair: pair[0],
                   reverse=True)
    total = len(pairs)
    if not total:
        return []
    curve: list[dict] = []
    errors_seen = 0
    stride = max(1, total // 200)
    for i, (score, err) in enumerate(pairs, start=1):
        errors_seen += 1 if err else 0
        if i == total or i % stride == 0 or i <= 20:
            curve.append({"coverage": i / total, "risk": errors_seen / i,
                          "threshold": score, "n": i})
    return curve


def risk_at_coverages(confidences: Sequence[float], errors: Sequence[bool],
                      coverages: Sequence[float] = (1.0, 0.95, 0.9, 0.8, 0.7,
                                                    0.5, 0.3, 0.1)
                      ) -> list[dict]:
    """Risk when admitting exactly the top ``coverage`` fraction."""
    if len(confidences) != len(errors):
        raise ValueError("confidences/errors length mismatch")
    pairs = sorted(zip(confidences, errors), key=lambda pair: pair[0],
                   reverse=True)
    n = len(pairs)
    if not n:
        return []
    out: list[dict] = []
    errors_seen = 0
    wanted = sorted({max(1, min(n, int(round(c * n)))) for c in coverages})
    index = 0
    for k in range(1, n + 1):
        if pairs[k - 1][1]:
            errors_seen += 1
        while index < len(wanted) and wanted[index] == k:
            out.append({"coverage": k / n, "risk": errors_seen / k, "n": k})
            index += 1
    return out


def aurc(confidences: Sequence[float], errors: Sequence[bool]) -> float:
    """Area under the risk-coverage curve (mean risk over admitted prefixes).

    Lower is better; 0.0 means the confidence ranking puts every correct sample
    before every wrong one.
    """
    if len(confidences) != len(errors):
        raise ValueError("confidences/errors length mismatch")
    pairs = sorted(zip(confidences, errors), key=lambda pair: pair[0],
                   reverse=True)
    if not pairs:
        return 0.0
    errors_seen = 0
    risks: list[float] = []
    for i, (_, err) in enumerate(pairs, start=1):
        errors_seen += 1 if err else 0
        risks.append(errors_seen / i)
    return sum(risks) / len(risks)


def ece(confidences: Sequence[float], corrects: Sequence[bool],
        bins: int = 10) -> float:
    """Expected calibration error of the confidence score itself (equal-width bins)."""
    if len(confidences) != len(corrects):
        raise ValueError("confidences/corrects length mismatch")
    if not confidences:
        return 0.0
    bucket = [[0, 0.0, 0] for _ in range(bins)]   # count, sum_conf, n_correct
    for score, ok in zip(confidences, corrects):
        idx = min(bins - 1, max(0, int(score * bins))) if score < 1.0 else bins - 1
        bucket[idx][0] += 1
        bucket[idx][1] += score
        bucket[idx][2] += 1 if ok else 0
    total = len(confidences)
    value = 0.0
    for count, conf_sum, correct_sum in bucket:
        if not count:
            continue
        value += count / total * abs(correct_sum / count - conf_sum / count)
    return value


# --------------------------------------------------------------------------- #
# bootstrap confidence intervals
# --------------------------------------------------------------------------- #
def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (numpy-compatible) for sorted values."""
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * q
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return (sorted_values[lower] * (1.0 - fraction)
            + sorted_values[upper] * fraction)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated ``q`` quantile (0<=q<=1) of unsorted ``values``.

    Same convention as :func:`bootstrap_ci` so p50/p95 latency CIs are
    consistent with the bootstrap bounds. Empty input raises ValueError.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"percentile q must be in [0, 1], got {q}")
    ordered = sorted(float(value) for value in values)
    return _percentile(ordered, q)


def confusion_from_pairs(gold: Sequence[bool | int],
                         pred: Sequence[bool | int]) -> Confusion:
    """Module-level alias of :meth:`Confusion.from_predictions`."""
    return Confusion.from_predictions(gold, pred)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


#: Named statistics accepted by :func:`bootstrap_ci` (besides a callable).
NAMED_STATISTICS = {
    "mean": _mean,
    "median": lambda values: float(statistics.median(values)),
}


def _resolve_statistic(statistic) -> Callable[[Sequence[float]], float]:
    if statistic is None:
        return _mean
    if callable(statistic):
        return statistic
    if isinstance(statistic, str):
        try:
            return NAMED_STATISTICS[statistic]
        except KeyError:
            raise ValueError(
                f"unknown statistic {statistic!r}; choose from "
                f"{sorted(NAMED_STATISTICS)} or pass a callable") from None
    raise TypeError(f"statistic must be a callable/str/None, got {type(statistic)}")


def bootstrap_ci(values: Sequence[float], *,
                 statistic: Callable[[Sequence[float]], float] | str | None = None,
                 n_boot: int = 2000, alpha: float = 0.05,
                 confidence: float | None = None, seed: int = 0
                 ) -> tuple[float, float]:
    """Percentile bootstrap CI of ``statistic(values)``.

    ``statistic``: callable, or ``"mean"``/``"median"``, or None (= mean).
    ``confidence`` (e.g. 0.95) overrides ``alpha`` when given. For a per-sample
    0/1 array the default mean is the accuracy. Returns ``(low, high)``.
    """
    values = [float(value) for value in values]
    if not values:
        raise ValueError("bootstrap_ci needs at least one value")
    if confidence is not None:
        alpha = 1.0 - confidence
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    stat = _resolve_statistic(statistic)
    rng = random.Random(seed)
    total = len(values)
    draws: list[float] = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(total)] for _ in range(total)]
        draws.append(float(stat(sample)))
    draws.sort()
    return (_percentile(draws, alpha / 2.0),
            _percentile(draws, 1.0 - alpha / 2.0))


def bootstrap_delta_ci(a: Sequence[float], b: Sequence[float], *,
                       n_boot: int = 2000, alpha: float = 0.05,
                       confidence: float | None = None, seed: int = 0
                       ) -> dict:
    """Paired bootstrap CI of ``mean(a - b)`` (same sample order on both sides)."""
    a = [float(value) for value in a]
    b = [float(value) for value in b]
    if len(a) != len(b):
        raise ValueError(f"bootstrap_delta_ci length mismatch: {len(a)} vs {len(b)}")
    if not a:
        raise ValueError("bootstrap_delta_ci needs at least one pairwise value")
    if confidence is not None:
        alpha = 1.0 - confidence
    rng = random.Random(seed)
    total = len(a)
    diffs = [a[i] - b[i] for i in range(total)]
    draws: list[float] = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(total)] for _ in range(total)]
        draws.append(sum(sample) / total)
    draws.sort()
    return {"delta": sum(diffs) / total,
            "ci_low": _percentile(draws, alpha / 2.0),
            "ci_high": _percentile(draws, 1.0 - alpha / 2.0)}


__all__ = [
    "Confusion", "METRIC_NAMES", "NAMED_STATISTICS", "aurc", "bootstrap_ci",
    "bootstrap_delta_ci", "confusion_from_pairs", "ece", "macro_average",
    "micro_confusion", "micro_confusions", "percentile", "risk_at_coverages",
    "risk_coverage",
]
