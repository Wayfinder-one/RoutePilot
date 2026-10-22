"""Shared numeric helpers (router/signals/_math.py).

The two model taggers (JEV and checkpoint) must produce the SAME signal
values for the same model output — otherwise the safety gate behaves
differently depending on which engine is deployed.
"""

import pytest

from router.signals import checkpoint as cp
from router.signals import jev as jev
from router.signals._math import (PII_HIT_THRESHOLD, PII_MIN_CONFIDENCE,
                                 binary_entropy_confidence, entropy_confidence,
                                 l2_normalize, pii_hits, softmax)


def test_both_taggers_use_the_same_helpers():
    """One implementation, two import paths: no drift between the engines."""
    assert cp.entropy_confidence is entropy_confidence
    assert cp.binary_entropy_confidence is binary_entropy_confidence
    assert cp.l2_normalize is l2_normalize
    assert cp.softmax is softmax
    assert jev.entropy_confidence is entropy_confidence
    assert jev.binary_entropy_confidence is binary_entropy_confidence
    assert jev.PII_HIT_THRESHOLD == PII_HIT_THRESHOLD
    assert jev.PII_MIN_CONFIDENCE == PII_MIN_CONFIDENCE


def test_softmax_and_entropy_confidence():
    assert abs(sum(softmax([1.0, 2.0, 3.0])) - 1.0) < 1e-9
    assert entropy_confidence([1.0, 0.0]) == pytest.approx(1.0)
    assert entropy_confidence([0.5, 0.5]) == pytest.approx(0.0)
    assert entropy_confidence([0.25] * 4) == pytest.approx(0.0)
    assert entropy_confidence([1.0]) == 1.0


def test_pii_hit_rule():
    # strictly above the threshold AND confident enough
    hits, conf = pii_hits([0.9, 0.85, 0.5, 0.1], ["a", "b", "c", "d"])
    assert hits == ["a", "b"]
    assert set(conf) == {"a", "b"}
    assert conf["a"] == pytest.approx(binary_entropy_confidence(0.9))
    # a bare 0.51 is above the threshold but carries no confidence
    assert binary_entropy_confidence(0.51) < PII_MIN_CONFIDENCE
    hits, conf = pii_hits([0.51], ["b"])
    assert hits == [] and conf == {}
    # exactly 0.5 is neutral: never a hit
    hits, _ = pii_hits([0.5], ["c"])
    assert hits == []


def test_pii_hits_are_sorted():
    hits, _ = pii_hits([0.99, 0.99, 0.99], ["z", "a", "m"])
    assert hits == ["a", "m", "z"]


def test_l2_normalize():
    assert l2_normalize([3.0, 4.0]) == pytest.approx([0.6, 0.8])
