from router.pipeline.matching import match_row


def sig(**kw):
    return kw


def test_model_signal_uses_confidence():
    s = {"domain": {"value": "health", "confidence": 0.9, "is_rule": False}}
    assert match_row("domain", "health", s) == (1, 0.9)
    assert match_row("domain", "law", s) == (0, 0.9)


def test_multi_signal_membership():
    s = {"pii": {"value": ["cn_id_card"], "confidence": 0.8, "is_rule": False}}
    assert match_row("pii", "cn_id_card", s) == (1, 0.8)
    assert match_row("pii", "password", s) == (0, 0.8)


def test_rule_signal_confidence_is_one():
    s = {"language": {"value": "zh", "confidence": 1.0, "is_rule": True}}
    assert match_row("language", "zh", s) == (1, 1.0)


def test_missing_or_null_signal():
    assert match_row("domain", "health", {}) == (0, 0.0)
    s = {"user_feedback": {"value": None, "confidence": None, "is_rule": False}}
    assert match_row("user_feedback", "satisfied", s) == (0, 0.0)
