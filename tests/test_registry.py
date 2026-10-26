from router.signals.registry import GROUPS, SIGNALS


def test_registry_shape():
    # The retired decision-only candidate signal is gone with the unified tree:
    # 15 signals = 7 model + 8 rule, and no empty "decision" palette group.
    assert len(SIGNALS) == 15
    assert sum(1 for s in SIGNALS.values() if s.kind == "model") == 7
    assert sum(1 for s in SIGNALS.values() if s.kind == "rule") == 8
    assert set(GROUPS) == {"safety", "domain", "difficulty", "context",
                           "session", "rule"}
    for spec in SIGNALS.values():
        assert spec.kind in ("model", "rule")
        assert spec.group in GROUPS


def test_key_signals():
    assert len(SIGNALS["domain"].values) == 14
    assert len(SIGNALS["pii"].values) == 30
    assert SIGNALS["pii"].multi and SIGNALS["keyword"].multi
    assert SIGNALS["keyword"].kind == "rule"
    assert SIGNALS["jailbreak"].group == "safety"
