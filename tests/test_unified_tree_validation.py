"""Two-level validation for the unified routing tree: save (structure) vs enable.

Saving a draft tree only checks node structure so an incomplete disabled tree can
be stored; enabling runs the full runtime-readiness health check (signals, models,
capabilities, reasoning, DecisionRunner, NL candidate count and a guaranteed
fallback).  These tests pin the code names, the conservative fallback rule and
the "structure never judges runtime context" boundary.
"""

from __future__ import annotations

from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Route,
    RoutingTree,
    Weight,
    When,
)
from router.pipeline.validation import (
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    EnableContext,
    Issue,
    has_guaranteed_fallback,
    validate_tree_for_enable,
    validate_tree_structure,
)
from tests.routing_tree_fixtures import mixed_tree

MODEL_IDS = ("qwen3-8b", "qwen3-32b", "deepseek-v3")
CAPABILITIES = {"qwen3-8b": "fast tasks", "qwen3-32b": "deep tasks",
                "deepseek-v3": "reasoning tasks"}


def _declared(*levels: str) -> dict:
    return {"declared": True, "supported": True, "format": "openai",
            "efforts": [{"id": level, "wire": level} for level in levels]}


def context(**overrides) -> EnableContext:
    values = dict(
        known_models=set(MODEL_IDS),
        capabilities=dict(CAPABILITIES),
        model_reasoning={model: _declared("off", "high") for model in MODEL_IDS},
        decision_runner_available=True)
    values.update(overrides)
    return EnableContext(**values)


def tree(root) -> RoutingTree:
    return RoutingTree(id="t", name="tree", enabled=False, root=root)


def route(node_id: str = "r", model: str = "qwen3-8b", **kwargs) -> Route:
    return Route(id=node_id, model=model, **kwargs)


def branch(node_id: str = "root", children=(), otherwise=None, when=None) -> Branch:
    return Branch(id=node_id, children=tuple(children), otherwise=otherwise,
                  when=when)


def nl(node_id: str = "root", options=(), instructions: str = "choose",
       otherwise=None, **kwargs) -> NLChoice:
    return NLChoice(id=node_id, instructions=instructions,
                    options=tuple(options), otherwise=otherwise, **kwargs)


def model_option(parent_id: str, key: str, model: str) -> NLOption:
    return NLOption(key=key, text="",
                    target=Route(id=f"{parent_id}/options/{key}", model=model),
                    capability_model=model)


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def enable(root, **ctx) -> list[Issue]:
    return validate_tree_for_enable(tree(root), context(**ctx))


def healthy_root():
    pick = nl("pick", options=(model_option("pick", "fast", "qwen3-8b"),
                               model_option("pick", "deep", "deepseek-v3")),
              otherwise=route("pick-fallback"))
    office = route("office", "qwen3-32b",
                   when=When("time_window", "work_hours", op="contains"))
    scored = Route("scored", "qwen3-32b",
                   weights=(Weight("complexity", "hard", 0.8),), min_score=0.6)
    return branch(children=(pick, office, scored),
                  otherwise=route("fallback"))


# --------------------------------------------------------------------------
# Step 1 (brief, verbatim): saveable draft vs not-enablable draft
# --------------------------------------------------------------------------

def test_incomplete_disabled_tree_can_save_but_cannot_enable():
    tree = RoutingTree(id="t", name="draft", enabled=False,
                       root=NLChoice(id="root", instructions="", options=()))
    assert validate_tree_structure(tree) == []
    context = EnableContext(known_models=set(), capabilities={},
                            model_reasoning={}, decision_runner_available=False)
    codes = {issue.code for issue in validate_tree_for_enable(tree, context)}
    assert {"missing_instructions", "missing_options", "decision_runner_unavailable", "missing_fallback"} <= codes


# --------------------------------------------------------------------------
# Structure (save) checks
# --------------------------------------------------------------------------

def test_complete_tree_passes_structure_validation():
    assert validate_tree_structure(tree(mixed_tree().root)) == []


def test_healthy_complete_tree_enables_cleanly():
    assert validate_tree_for_enable(tree(healthy_root()), context()) == []


def test_duplicate_node_ids_are_a_structure_error():
    root = branch(children=(route("dup", "qwen3-8b"),
                            route("dup", "qwen3-32b")))
    assert "duplicate_node_id" in codes(validate_tree_structure(tree(root)))


def test_duplicate_option_keys_are_a_structure_error():
    root = nl(options=(NLOption("same", "", Route("t1", "qwen3-8b"), "qwen3-8b"),
                       NLOption("same", "", Route("t2", "qwen3-32b"),
                                "qwen3-32b")))
    assert "duplicate_option_key" in codes(validate_tree_structure(tree(root)))


def test_root_condition_is_a_structure_error():
    root = route(when=When("pii", "email_address", op="contains"))
    assert "root_when" in codes(validate_tree_structure(tree(root)))


def test_otherwise_condition_is_a_structure_error():
    when = When("pii", "email_address", op="contains")
    branch_root = branch(children=(route("a"),),
                         otherwise=route("f", when=when))
    nl_root = nl(options=(), otherwise=route("f", when=when))
    for root in (branch_root, nl_root):
        assert "otherwise_when" in codes(validate_tree_structure(tree(root)))


def test_structure_does_not_judge_models_capabilities_or_fallback():
    # Unknown models, empty NL options and a missing fallback are enable-level
    # problems only: a disabled draft tree must still be saveable.
    root = branch(children=(route("a", "ghost"),))
    assert validate_tree_structure(tree(root)) == []


def test_route_without_model_is_a_structure_error():
    assert "missing_model" in codes(validate_tree_structure(tree(route(model=""))))


def test_direct_model_option_must_target_a_route():
    bad = NLOption(key="k", text="", target=branch("sub"),
                   capability_model="qwen3-8b")
    assert "invalid_option" in codes(validate_tree_structure(tree(nl(options=(bad,)))))


def test_option_needs_a_nonempty_key():
    bad = NLOption(key="", text="", target=route("target"))
    assert "invalid_option" in codes(validate_tree_structure(tree(nl(options=(bad,)))))


def test_tree_depth_and_size_are_bounded():
    deep = route("leaf")
    for index in range(MAX_TREE_DEPTH + 1):
        deep = Branch(id=f"b{index}", otherwise=deep)
    assert "tree_too_deep" in codes(validate_tree_structure(tree(deep)))

    wide = branch(children=tuple(route(f"r{i}") for i in range(MAX_TREE_NODES + 1)))
    assert "tree_too_large" in codes(validate_tree_structure(tree(wide)))


def test_missing_root_is_reported_not_crashed():
    broken = RoutingTree(id="t", name="t", enabled=False, root=None)
    assert "missing_root" in codes(validate_tree_structure(broken))
    assert "missing_root" in codes(validate_tree_for_enable(broken, context()))


# --------------------------------------------------------------------------
# Enable (runtime readiness) checks
# --------------------------------------------------------------------------

def test_enable_reports_structure_problems_too():
    root = branch(children=(route("dup"), route("dup")))
    assert "duplicate_node_id" in codes(enable(root))


def test_unknown_signal_is_an_enable_error_not_a_save_error():
    root = branch(children=(route("a", when=When("telepathy", "yes")),))
    saved = tree(root)
    assert validate_tree_structure(saved) == []
    assert "unknown_signal" in codes(validate_tree_for_enable(saved, context()))


def test_condition_operator_must_match_signal_arity():
    single_bad = branch(children=(
        route("a", when=When("domain", "health", op="contains")),))
    multi_bad = branch(children=(
        route("b", when=When("pii", "email_address", op="eq")),))
    assert "invalid_op" in codes(enable(single_bad))
    assert "invalid_op" in codes(enable(multi_bad))

    good = branch(children=(
        route("a", when=When("domain", "health", op="eq")),
        route("b", when=When("pii", "email_address", op="contains")),
    ), otherwise=route("f"))
    assert "invalid_op" not in codes(enable(good))


def test_unknown_signal_value_is_rejected():
    root = branch(children=(
        route("a", when=When("domain", "telepathy", op="eq")),
        route("b", when=When("pii", "not_a_type", op="contains")),
    ), otherwise=route("f"))
    got = [issue.code for issue in enable(root)]
    assert got.count("unknown_signal_value") == 2


def test_condition_value_is_required():
    root = branch(children=(
        route("a", when=When("pii", "", op="contains")),), otherwise=route("f"))
    assert "missing_value" in codes(enable(root))


def test_weight_condition_signals_are_validated():
    root = branch(children=(
        Route("a", "qwen3-8b",
              weights=(Weight("telepathy", "yes", 0.5),), min_score=0.5),
        Route("b", "qwen3-8b",
              weights=(Weight("complexity", "not_a_level", 0.5),), min_score=0.5),
        Route("c", "qwen3-8b", weights=(Weight("pii", "", 0.5),),
              min_score=0.5),
    ), otherwise=route("f"))
    got = codes(enable(root))
    assert {"unknown_signal", "unknown_signal_value", "missing_value"} <= got


def test_unknown_model_is_rejected():
    root = branch(children=(route("a", "ghost"),), otherwise=route("f"))
    assert "unknown_model" in codes(enable(root))


def test_direct_model_options_need_known_models_and_capabilities():
    no_capability = nl(options=(model_option("root", "a", "qwen3-8b"),
                                model_option("root", "b", "qwen3-32b")),
                       otherwise=route("f"))
    got = codes(enable(no_capability,
                       capabilities={"qwen3-8b": "fast tasks"}))
    assert "missing_capability" in got and "unknown_model" not in got

    unknown = nl(options=(model_option("root", "a", "ghost"),
                          model_option("root", "b", "qwen3-8b")),
                 otherwise=route("f"))
    assert "unknown_model" in codes(enable(unknown))


def test_child_option_needs_a_description():
    child = NLOption(key="a", text="", target=Route("sub", "qwen3-8b"))
    root = nl(options=(child, model_option("root", "b", "qwen3-8b")),
              otherwise=route("f"))
    assert "missing_option_description" in codes(enable(root))


def test_route_reasoning_must_be_declared_by_the_model():
    unsupported = branch(children=(
        route("a", "qwen3-8b", reasoning="max"),), otherwise=route("f"))
    assert "reasoning_unsupported" in codes(enable(unsupported))

    undeclared = branch(children=(
        route("a", "qwen3-32b", reasoning="high"),), otherwise=route("f"))
    assert "reasoning_unsupported" in codes(enable(
        undeclared, model_reasoning={"qwen3-32b": None}))

    supported = branch(children=(
        route("a", "qwen3-8b", reasoning="high"),), otherwise=route("f"))
    assert "reasoning_unsupported" not in codes(enable(supported))


def test_direct_model_option_reasoning_is_validated_like_a_route():
    bad = NLOption(key="a", text="",
                   target=Route("root/options/a", "qwen3-8b", reasoning="max"),
                   capability_model="qwen3-8b")
    root = nl(options=(bad, model_option("root", "b", "qwen3-32b")),
              otherwise=route("f"))
    hits = [issue for issue in enable(root)
            if issue.code == "reasoning_unsupported"]
    assert hits and hits[0].node_id == "root"

    good = NLOption(key="a", text="",
                    target=Route("root/options/a", "qwen3-8b", reasoning="high"),
                    capability_model="qwen3-8b")
    ok_root = nl(options=(good, model_option("root", "b", "qwen3-32b")),
                 otherwise=route("f"))
    assert "reasoning_unsupported" not in codes(enable(ok_root))


def test_nl_choice_needs_instructions():
    for instructions in ("", "   "):
        root = nl(instructions=instructions,
                  options=(model_option("root", "a", "qwen3-8b"),
                           model_option("root", "b", "qwen3-32b")),
                  otherwise=route("f"))
        assert "missing_instructions" in codes(enable(root))


def test_nl_choice_needs_two_to_255_options():
    empty = nl(options=(), otherwise=route("f"))
    assert "missing_options" in codes(enable(empty))

    single = nl(options=(model_option("root", "a", "qwen3-8b"),),
                otherwise=route("f"))
    assert "too_few_options" in codes(enable(single))

    many = tuple(model_option("root", f"k{index}", "qwen3-8b")
                 for index in range(256))
    assert "too_many_options" in codes(enable(nl(options=many,
                                                otherwise=route("f"))))


def test_nl_thresholds_must_be_in_unit_interval():
    root = nl(options=(model_option("root", "a", "qwen3-8b"),
                       model_option("root", "b", "qwen3-32b")),
              otherwise=route("f"), min_prob=1.2)
    assert "invalid_threshold" in codes(enable(root))


def test_decision_runner_is_required_only_for_nl_trees():
    nl_root = nl(options=(model_option("root", "a", "qwen3-8b"),
                          model_option("root", "b", "qwen3-32b")),
                 otherwise=route("f"))
    assert "decision_runner_unavailable" in codes(
        enable(nl_root, decision_runner_available=False))
    assert "decision_runner_unavailable" not in codes(enable(nl_root))

    signal_root = branch(children=(route("a", "qwen3-8b"),),
                         otherwise=route("f"))
    assert "decision_runner_unavailable" not in codes(
        enable(signal_root, decision_runner_available=False))


def test_threshold_ranges_are_validated():
    bad_floor = branch(children=(
        route("a", when=When("pii", "email_address", op="contains",
                             conf_min=1.5)),))
    assert "invalid_threshold" in codes(enable(bad_floor))

    bad_weight = branch(children=(
        Route("a", "qwen3-8b",
              weights=(Weight("complexity", "hard", 1.5),), min_score=0.5),))
    assert "invalid_threshold" in codes(enable(bad_weight))

    bad_min_score = branch(children=(
        Route("a", "qwen3-8b",
              weights=(Weight("complexity", "hard", 0.5),), min_score=-0.1),))
    assert "invalid_threshold" in codes(enable(bad_min_score))


def test_rule_signal_confidence_floor_is_only_a_warning():
    root = branch(children=(
        route("a", when=When("time_window", "work_hours", op="contains",
                             conf_min=0.7)),
    ), otherwise=route("f"))
    issues = enable(root)
    floors = [issue for issue in issues if issue.code == "rule_signal_floor"]
    assert floors and all(issue.level == "warn" for issue in floors)


def test_missing_fallback_cases():
    cases = {
        "branch_without_otherwise": branch(children=(route("a"),)),
        "weighted_root_route": Route(
            "r", "qwen3-8b", weights=(Weight("complexity", "hard", 0.5),),
            min_score=0.5),
        "weighted_otherwise_tail": branch(
            children=(route("a"),),
            otherwise=Route("f", "qwen3-8b",
                            weights=(Weight("complexity", "hard", 0.5),),
                            min_score=0.5)),
    }
    for name, root in cases.items():
        assert "missing_fallback" in codes(enable(root)), name


def test_guaranteed_fallback_cases():
    assert has_guaranteed_fallback(route("r")) is True
    assert has_guaranteed_fallback(Block("b")) is True
    assert has_guaranteed_fallback(route("r"), is_root=False) is False
    assert has_guaranteed_fallback(
        route("r", weights=(Weight("complexity", "hard", 0.5),))) is False
    assert has_guaranteed_fallback(
        branch(children=(route("a"),), otherwise=route("f"))) is True
    assert has_guaranteed_fallback(
        branch(children=(route("a"),),
               otherwise=branch("mid", otherwise=route("f")))) is True
    assert has_guaranteed_fallback(branch(children=(route("a"),))) is False
    assert has_guaranteed_fallback(
        nl(options=(), otherwise=route("f"))) is True

    assert "missing_fallback" not in codes(enable(route("r", "qwen3-8b")))
    assert "missing_fallback" not in codes(enable(
        branch(children=(route("a"),), otherwise=route("f"))))


def test_issue_and_enable_context_shapes():
    issue = Issue("code", "中文", "English", node_id="n1", level="warn")
    assert (issue.code, issue.zh, issue.en, issue.node_id, issue.level) == (
        "code", "中文", "English", "n1", "warn")
    empty = EnableContext()
    assert not empty.known_models
    assert empty.decision_runner_available is False


def test_enable_does_not_recurse_into_an_unbounded_tree():
    deep = route("leaf")
    for index in range(1200):
        deep = Branch(id=f"b{index}", otherwise=deep)
    issues = validate_tree_for_enable(tree(deep), context())
    assert "tree_too_deep" in codes(issues)


def test_shared_mixed_tree_fixture_enables_cleanly():
    # The Task 2 fixtures are the shared input for engine/runtime/API tests, so
    # the complete mixed tree must pass the full enable health check as built.
    assert validate_tree_for_enable(mixed_tree(), context()) == []
