from router.routing.model import tree_from_dict
from router.routing.validation import EnableContext, validate_tree_for_enable
from tests.routing_fixtures import jev_dict, target_dict, tree_dict


MODELS = {"fast", "strong-a", "strong-b", "standard"}
CAPABILITIES = {"fast": "简单请求", "strong-a": "复杂请求", "strong-b": "复杂请求", "standard": "通用请求"}
REASONING = {model: {"declared": True, "supported": True, "efforts": [{"id": "off"}, {"id": "high"}]} for model in MODELS}


def context(**overrides):
    values = {
        "known_models": MODELS,
        "capabilities": CAPABILITIES,
        "model_reasoning": REASONING,
        "known_periods": {"白天", "夜间"},
        "jev_available": True,
    }
    values.update(overrides)
    return EnableContext(**values)


def codes(tree):
    return {issue.code for issue in validate_tree_for_enable(tree, context())}


def test_valid_jev_tree_has_no_issues():
    assert validate_tree_for_enable(tree_from_dict(tree_dict()), context()) == []


def test_enable_requires_fallback_models_periods_and_capabilities():
    raw = tree_dict()
    del raw["root"]["fallback"]
    raw["root"]["options"][0]["target"]["when"] = {"periods": ["不存在"]}
    raw["root"]["options"][0]["target"]["models"] = []
    raw["root"]["options"][1]["target"]["capability"] = ""
    tree = tree_from_dict(raw)
    assert {"fallback_required", "unknown_period", "target_models_required", "capability_required"} <= codes(tree)


def test_enable_checks_reasoning_and_jev_availability():
    raw = tree_dict(root=target_dict("target", ("fast",), reasoning="max"))
    assert "reasoning_unsupported" in codes(tree_from_dict(raw))
    tree = tree_from_dict(tree_dict(root=jev_dict()))
    assert "jev_unavailable" in {
        issue.code for issue in validate_tree_for_enable(tree, context(jev_available=False))
    }


def test_duplicate_node_ids_and_candidate_labels_are_rejected():
    raw = tree_dict()
    raw["root"]["options"][1]["target"]["id"] = "fast-target"
    raw["root"]["options"][1]["label"] = "轻量模型"
    assert {"duplicate_node_id", "duplicate_option_label"} <= codes(tree_from_dict(raw))


def test_option_count_and_invalid_timezone_are_checked_at_enable():
    raw = tree_dict()
    raw["root"]["options"] = raw["root"]["options"][:1]
    raw["timezone"] = "Not/AZone"
    assert {"option_count", "invalid_timezone"} <= codes(tree_from_dict(raw))
