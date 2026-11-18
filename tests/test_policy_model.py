import pytest

from router.routing.policy.model import (
    AnswerCondition, LogicCondition, policy_from_dict,
    policy_revision_of, policy_to_dict,
)
from tests.routing_fixtures import policy_dict


def test_policy_round_trip_preserves_behavior_document():
    raw = policy_dict()
    policy = policy_from_dict(raw)
    document = policy_to_dict(policy)
    assert policy_to_dict(policy_from_dict(document)) == document
    assert document["questions"]["severity"]["criteria"] == [1.0, 5.0, 10.0]
    assert isinstance(policy.rules[1].when, LogicCondition)
    assert isinstance(policy.rules[0].when, AnswerCondition)
    assert policy.questions["route"].options[0].key == "fast"
    with pytest.raises(AttributeError):
        policy.rules[0].name = "mutated"


def test_revision_ignores_name_and_question_map_order_but_not_rule_order():
    raw = policy_dict()
    base = policy_revision_of(policy_from_dict(raw))
    renamed = dict(raw, name="另一个名字")
    renamed["questions"] = dict(reversed(list(raw["questions"].items())))
    assert policy_revision_of(policy_from_dict(renamed)) == base
    reversed_rules = dict(raw, rules=list(reversed(raw["rules"])))
    assert policy_revision_of(policy_from_dict(reversed_rules)) != base


def test_defaults_are_explicit_in_serialized_document():
    raw = policy_dict()
    del raw["sticky"]
    del raw["timezone"]
    raw["targets"]["fast"] = {"models": [{"model": "fast", "reasoning": None}]}
    policy = policy_from_dict(raw)
    assert policy.sticky.enabled is False
    assert policy.sticky.ttl_seconds == 1800
    assert policy.timezone == "Asia/Shanghai"
    assert policy.targets["fast"].models[0].reasoning is None


def _mutated(mutate):
    raw = policy_dict()
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda raw: raw.update(kind="sr-routing-graph", version=3),
            id="v3-document",
        ),
        pytest.param(lambda raw: raw.update(version=True), id="boolean-version"),
        pytest.param(lambda raw: raw.update(version=4.0), id="float-version"),
        pytest.param(lambda raw: raw.update(extra="nope"), id="unknown-top-level-field"),
        pytest.param(lambda raw: raw.pop("questions"), id="missing-questions"),
        pytest.param(lambda raw: raw.pop("fallback"), id="missing-fallback"),
        pytest.param(
            lambda raw: raw["questions"].update({"ro.ute": raw["questions"].pop("route")}),
            id="dotted-question-id",
        ),
        pytest.param(
            lambda raw: raw["targets"].update({"sa.fe": raw["targets"].pop("safe")}),
            id="dotted-target-id",
        ),
        pytest.param(
            lambda raw: raw["questions"]["route"]["options"].pop(),
            id="single-option-choice",
        ),
        pytest.param(
            lambda raw: raw["questions"]["route"]["options"][1].update(label="轻量"),
            id="duplicate-option-label",
        ),
        pytest.param(
            lambda raw: raw["questions"]["severity"].update(criteria=[1, float("nan")]),
            id="nan-score-criteria",
        ),
        pytest.param(
            lambda raw: raw["questions"]["severity"].update(criteria=[1, float("inf")]),
            id="infinite-score-criteria",
        ),
        pytest.param(
            lambda raw: raw["questions"]["severity"].update(criteria=[1, True]),
            id="boolean-score-criteria",
        ),
        pytest.param(
            lambda raw: raw["questions"]["risk"].update(extra="nope"),
            id="unknown-question-field",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(target="missing"),
            id="undefined-rule-target",
        ),
        pytest.param(
            lambda raw: raw.update(fallback="missing"),
            id="undefined-fallback-target",
        ),
        pytest.param(
            lambda raw: raw["rules"][1].update(
                when={"not": [
                    {"answer": "risk.yes", "gte": 0.8},
                    {"answer": "risk.yes", "gte": 0.8},
                ]},
            ),
            id="not-with-two-children",
        ),
        pytest.param(
            lambda raw: raw["rules"][1].update(when={"all": []}),
            id="empty-all",
        ),
        pytest.param(
            lambda raw: raw["rules"][1].update(when={"any": []}),
            id="empty-any",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "route.selected", "gte": "fast"}),
            id="selected-with-non-eq-op",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "route.selected", "eq": "missing"}),
            id="selected-with-undefined-option",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(
                when={"answer": "route.selected", "eq": "fast", "extra": 1},
            ),
            id="answer-condition-with-extra-field",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "route.prob.missing", "gte": 0.5}),
            id="prob-with-undefined-option",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "route.margin", "gte": 2.0}),
            id="margin-value-out-of-range",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "risk.yes", "gte": True}),
            id="boolean-comparison-value",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "missing.yes", "gte": 0.8}),
            id="undefined-question-path",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"text_contains_any": []}),
            id="empty-text-contains-any",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"period_in": []}),
            id="empty-period-in",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "risk.yes"}),
            id="answer-without-comparator",
        ),
    ],
)
def test_invalid_documents_are_rejected(mutate):
    with pytest.raises(ValueError):
        policy_from_dict(_mutated(mutate))


def test_condition_tree_limits_reject_deep_and_wide_trees():
    def nested(depth):
        node = {"answer": "risk.yes", "gte": 0.8}
        for _ in range(depth):
            node = {"all": [node]}
        return node

    with pytest.raises(ValueError):
        policy_from_dict(
            dict(policy_dict(), rules=[{"when": nested(16), "target": "safe"}])
        )
    deep_but_legal = policy_from_dict(
        dict(policy_dict(), rules=[{"when": nested(15), "target": "safe"}])
    )
    assert len(deep_but_legal.rules[0].when.children) == 1

    wide = dict(
        policy_dict(),
        rules=[{"when": {"any": [{"answer": "risk.yes", "gte": 0.8}] * 400}, "target": "safe"}],
    )
    with pytest.raises(ValueError):
        policy_from_dict(wide)


def test_text_period_and_not_conditions_round_trip():
    raw = policy_dict()
    raw["rules"][0]["when"] = {
        "all": [
            {"text_contains_any": ["95598", "抢修"]},
            {"period_in": ["workday"]},
            {"not": {"answer": "risk.yes", "gte": 0.8}},
        ]
    }
    policy = policy_from_dict(raw)
    document = policy_to_dict(policy)
    assert document["rules"][0]["when"] == raw["rules"][0]["when"]
    assert policy_revision_of(policy_from_dict(document)) == policy_revision_of(policy)


def test_omitted_and_explicit_null_reasoning_share_one_behavior_document():
    omitted = policy_dict()
    explicit = policy_dict()
    explicit["targets"]["fast"] = {"models": [{"model": "fast", "reasoning": None}]}
    omitted_policy = policy_from_dict(omitted)
    explicit_policy = policy_from_dict(explicit)
    assert policy_to_dict(explicit_policy) == policy_to_dict(omitted_policy)
    assert policy_revision_of(explicit_policy) == policy_revision_of(omitted_policy)


def test_empty_questions_and_empty_rules_are_legal():
    raw = policy_dict()
    raw["questions"] = {}
    raw["rules"] = []
    policy = policy_from_dict(raw)
    assert len(policy.questions) == 0
    assert policy.rules == ()
    assert policy.fallback == "safe"
    document = policy_to_dict(policy)
    assert document["questions"] == {}
    assert document["rules"] == []
