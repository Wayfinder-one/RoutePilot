import pytest

from router.api.issue_presenter import issues_payload, present_issue
from router.routing.policy.model import policy_from_dict
from router.routing.policy.validate import Issue, validate_policy, validate_policy_for_enable
from tests.routing_fixtures import enable_context, policy_dict


def test_unknown_answer_path_has_exact_json_path():
    raw = policy_dict()
    raw["rules"][0]["when"]["answer"] = "missing.yes"
    issues = validate_policy(policy_from_dict(raw))
    assert [(i.code, i.path) for i in issues] == [
        ("unknown_answer_path", "rules.0.when.answer")
    ]


def test_selected_requires_known_option_key_and_eq():
    raw = policy_dict()
    raw["rules"][0]["when"] = {"answer": "route.selected", "eq": "missing"}
    assert validate_policy(policy_from_dict(raw))[0].code == "unknown_option_key"
    raw["rules"][0]["when"] = {"answer": "route.selected", "gte": "strong"}
    assert validate_policy(policy_from_dict(raw))[0].code == "answer_operator_invalid"


def test_enable_checks_models_periods_and_jev_exactly_when_called():
    raw = policy_dict()
    raw["rules"].append({
        "name": "时段", "when": {"period_in": ["missing"]}, "target": "fast",
    })
    context = enable_context()
    context.known_models.remove("strong")
    context.jev_available = False
    codes = {i.code for i in validate_policy_for_enable(policy_from_dict(raw), context)}
    assert codes == {"unknown_model", "unknown_period", "decision_service_unavailable"}


# ---------------------------------------------------------------------------
# 结构限额：参数化 mutation（设计 §4.3/§4.5）
# ---------------------------------------------------------------------------


def _validated(mutate):
    raw = policy_dict()
    mutate(raw)
    return validate_policy(policy_from_dict(raw))


def _extra_questions(count):
    return {
        f"q{index}": {"type": "noul", "instructions": "补充问题"}
        for index in range(count)
    }


def _extra_rules(count):
    return [
        {"when": {"answer": "risk.yes", "gte": 0.8}, "target": "safe"}
        for _ in range(count)
    ]


def _leaf(answer="risk.yes", op="gte", value=0.8):
    return {"answer": answer, op: value}


def _options(count):
    options = [
        {"key": "fast", "label": "轻量", "capability": "简单请求"},
        {"key": "strong", "label": "增强", "capability": "复杂请求"},
    ]
    options.extend(
        {"key": f"opt{index}", "label": f"选项{index}", "capability": "备用"}
        for index in range(count - 2)
    )
    return options


def _nested(depth):
    node = _leaf()
    for _ in range(depth):
        node = {"all": [node]}
    return node


@pytest.mark.parametrize(
    ("mutate", "codes"),
    [
        pytest.param(
            lambda raw: raw["questions"].update(_extra_questions(30)),
            {"limit_exceeded"}, id="thirty-three-questions",
        ),
        pytest.param(
            lambda raw: raw["rules"].extend(_extra_rules(198)),
            {"limit_exceeded"}, id="two-hundred-one-rules",
        ),
        pytest.param(
            lambda raw: raw.update(rules=[{"when": {"all": [_leaf() for _ in range(400)]}, "target": "safe"}]),
            {"limit_exceeded"}, id="four-hundred-one-condition-nodes-in-one-rule",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_nested(16)),
            {"condition_depth_exceeded"}, id="seventeen-level-nesting",
        ),
        pytest.param(
            lambda raw: raw["questions"]["route"].update(options=_options(256)),
            {"limit_exceeded"}, id="two-hundred-fifty-six-options",
        ),
        pytest.param(
            lambda raw: raw.update(targets={}),
            {"unknown_target"}, id="empty-targets",
        ),
        pytest.param(
            lambda raw: raw["targets"].update(strong={"models": []}),
            {"empty_models"}, id="target-without-models",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(target="missing"),
            {"unknown_target"}, id="unknown-rule-target",
        ),
        pytest.param(
            lambda raw: raw.update(fallback="missing"),
            {"unknown_target"}, id="unknown-fallback",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="risk.yes", op="gte", value=1.5)),
            {"answer_value_out_of_range"}, id="yes-threshold-above-one",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="risk.no", op="lt", value=-0.5)),
            {"answer_value_out_of_range"}, id="no-threshold-below-zero",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.prob.fast", op="gte", value=1.01)),
            {"answer_value_out_of_range"}, id="prob-threshold-above-one",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.p_top", op="eq", value=2)),
            {"answer_value_out_of_range"}, id="p-top-threshold-above-one",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.margin", op="gte", value=1.5)),
            {"answer_value_out_of_range"}, id="margin-threshold-above-one",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.margin", op="lt", value=-1.5)),
            {"answer_value_out_of_range"}, id="margin-threshold-below-minus-one",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.prob.missing", op="gte", value=0.5)),
            {"unknown_option_key"}, id="prob-unknown-option-key",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when={"answer": "route.selected", "eq": 3}),
            {"answer_value_invalid"}, id="numeric-value-on-eq-path",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="risk.yes", op="gte", value="0.8")),
            {"answer_value_invalid"}, id="string-value-on-numeric-path",
        ),
    ],
)
def test_structure_mutations_are_flagged(mutate, codes):
    issues = _validated(mutate)
    assert {issue.code for issue in issues} == codes


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda raw: raw["questions"].update(_extra_questions(29)),
            id="thirty-two-questions",
        ),
        pytest.param(
            lambda raw: raw["rules"].extend(_extra_rules(197)),
            id="two-hundred-rules",
        ),
        pytest.param(
            lambda raw: raw.update(rules=[{"when": {"all": [_leaf() for _ in range(399)]}, "target": "safe"}]),
            id="four-hundred-condition-nodes",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_nested(15)),
            id="sixteen-level-nesting",
        ),
        pytest.param(
            lambda raw: raw["questions"]["route"].update(options=_options(255)),
            id="two-hundred-fifty-five-options",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="severity.value", op="gte", value=1e9)),
            id="score-value-with-finite-non-interval-threshold",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="severity.value", op="lt", value=-1e9)),
            id="score-value-with-large-negative-threshold",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.prob.fast", op="gte", value=0.25)),
            id="prob-threshold-in-range",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.p_top", op="gte", value=0.9)),
            id="p-top-threshold-in-range",
        ),
        pytest.param(
            lambda raw: raw["rules"][0].update(when=_leaf(answer="route.margin", op="lte", value=0.5)),
            id="margin-threshold-in-range",
        ),
    ],
)
def test_boundary_structures_stay_legal(mutate):
    assert _validated(mutate) == []


def test_limit_issues_carry_exact_paths_and_params():
    questions = _validated(lambda raw: raw["questions"].update(_extra_questions(30)))
    assert [(i.code, i.path) for i in questions] == [("limit_exceeded", "questions")]
    assert questions[0].params == {"limit": 32}

    rules = _validated(lambda raw: raw["rules"].extend(_extra_rules(198)))
    assert [(i.code, i.path) for i in rules] == [("limit_exceeded", "rules")]
    assert rules[0].params == {"limit": 200}

    conditions = _validated(
        lambda raw: raw.update(rules=[{"when": {"all": [_leaf() for _ in range(400)]}, "target": "safe"}])
    )
    assert [(i.code, i.path) for i in conditions] == [("limit_exceeded", "rules")]
    assert conditions[0].params == {"kind": "conditions", "limit": 400}

    options = _validated(lambda raw: raw["questions"]["route"].update(options=_options(256)))
    assert [(i.code, i.path) for i in options] == [("limit_exceeded", "questions.route.options")]
    assert options[0].params == {"kind": "options", "limit": 255, "count": 256}


def test_condition_budget_is_policy_wide_across_rules():
    # 两条规则各 201 个条件节点：单条合法，聚合 402 > 400 必须在规则边界外被拒绝。
    raw = policy_dict()
    raw["rules"] = [
        {"when": {"all": [_leaf() for _ in range(200)]}, "target": "safe"},
        {"when": {"all": [_leaf() for _ in range(200)]}, "target": "fast"},
    ]
    issues = validate_policy(policy_from_dict(raw))
    assert [(i.code, i.path) for i in issues] == [("limit_exceeded", "rules")]
    assert issues[0].params == {"kind": "conditions", "limit": 400}


def test_depth_issue_points_at_the_overdeep_node_and_its_params():
    issues = _validated(lambda raw: raw["rules"][0].update(when=_nested(16)))
    assert [(i.code, i.path) for i in issues] == [
        ("condition_depth_exceeded", "rules.0.when" + ".all.0" * 16)
    ]
    assert issues[0].params == {"limit": 16}


def test_empty_models_issue_points_at_the_targets_models_path():
    issues = _validated(lambda raw: raw["targets"].update(strong={"models": []}))
    assert [(i.code, i.path) for i in issues] == [("empty_models", "targets.strong.models")]
    assert issues[0].params == {"target": "strong"}


def test_unknown_target_issues_point_at_fallback_and_rule_target():
    fallback = _validated(lambda raw: raw.update(fallback="missing"))
    assert [(i.code, i.path) for i in fallback] == [("unknown_target", "fallback")]
    assert fallback[0].params == {"target": "missing"}

    rule = _validated(lambda raw: raw["rules"][0].update(target="missing"))
    assert [(i.code, i.path) for i in rule] == [("unknown_target", "rules.0.target")]
    assert rule[0].params == {"target": "missing"}


def test_out_of_range_issue_points_at_the_comparison_field():
    issues = _validated(
        lambda raw: raw["rules"][0].update(when=_leaf(answer="risk.yes", op="gte", value=1.5))
    )
    assert [(i.code, i.path) for i in issues] == [
        ("answer_value_out_of_range", "rules.0.when.gte")
    ]
    assert issues[0].params == {"answer": "risk.yes", "value": 1.5, "min": 0, "max": 1}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("missing.yes", id="unknown-question"),
        pytest.param("route.value", id="choice-has-no-value-output"),
        pytest.param("risk.selected", id="noul-has-no-selected-output"),
        pytest.param("severity.selected", id="score-has-no-selected-output"),
        pytest.param("route.p_top.extra", id="p-top-with-extra-segment"),
        pytest.param("route.selected.extra", id="selected-with-extra-segment"),
        pytest.param("route.prob", id="prob-without-option-segment"),
        pytest.param("route.prob.fast.slow", id="prob-with-extra-segment"),
        pytest.param("a.b.c.d", id="four-segment-path"),
    ],
)
def test_unknown_answer_paths_all_point_at_the_answer_field(answer):
    issues = _validated(lambda raw: raw["rules"][0].update(when=_leaf(answer=answer)))
    assert [(i.code, i.path) for i in issues] == [
        ("unknown_answer_path", "rules.0.when.answer")
    ]
    assert issues[0].params == {"answer": answer}


def test_finish_sorts_and_dedups_issues_stably():
    # 结构级：多种错误码按 (level, code, path) 稳定输出。
    raw = policy_dict()
    raw["fallback"] = "missing"
    raw["rules"][0]["target"] = "missing"
    raw["questions"].update(_extra_questions(30))
    issues = validate_policy(policy_from_dict(raw))
    assert [(i.code, i.path) for i in issues] == [
        ("limit_exceeded", "questions"),
        ("unknown_target", "fallback"),
        ("unknown_target", "rules.0.target"),
    ]

    # 启用级：同一 period 名重复引用只报一次，多码按 (level, code, path) 排序。
    raw = policy_dict()
    raw["rules"].append({
        "name": "时段", "when": {"period_in": ["missing", "missing"]}, "target": "fast",
    })
    context = enable_context()
    context.known_models.remove("strong")
    context.jev_available = False
    issues = validate_policy_for_enable(policy_from_dict(raw), context)
    assert [(i.code, i.path) for i in issues] == [
        ("decision_service_unavailable", ""),
        ("unknown_model", "targets.strong.models.0"),
        ("unknown_period", "rules.3.when.period_in"),
    ]
    assert issues[2].params == {"period": "missing"}


# ---------------------------------------------------------------------------
# 启用校验（设计 §7.2）
# ---------------------------------------------------------------------------


def test_enable_skips_catalog_scan_when_structure_is_broken():
    raw = policy_dict()
    raw["rules"][0]["when"]["answer"] = "missing.yes"
    context = enable_context()
    context.known_models.remove("strong")
    issues = validate_policy_for_enable(policy_from_dict(raw), context)
    assert [(i.code, i.path) for i in issues] == [
        ("unknown_answer_path", "rules.0.when.answer")
    ]


def test_enable_flags_unknown_model_with_targets_models_path():
    raw = policy_dict()
    context = enable_context()
    context.known_models.remove("strong")
    issues = validate_policy_for_enable(policy_from_dict(raw), context)
    assert [(i.code, i.path) for i in issues] == [("unknown_model", "targets.strong.models.0")]
    assert issues[0].params == {"model": "strong"}


def test_enable_reasoning_contract_follows_catalog_capabilities():
    def _with_reasoning(reasoning):
        raw = policy_dict()
        raw["targets"]["fast"] = {"models": [{"model": "fast", "reasoning": reasoning}]}
        return raw

    supported = {"fast": {"supported": True, "efforts": [{"id": "high"}]}}
    context = enable_context()
    context.model_reasoning = supported
    assert validate_policy_for_enable(policy_from_dict(_with_reasoning("high")), context) == []

    context = enable_context()
    context.model_reasoning = supported
    issues = validate_policy_for_enable(policy_from_dict(_with_reasoning("medium")), context)
    assert [(i.code, i.path) for i in issues] == [
        ("reasoning_unsupported", "targets.fast.models.0")
    ]
    assert issues[0].params == {"model": "fast", "reasoning": "medium", "efforts": ["high"]}

    context = enable_context()  # model_reasoning 默认 {"fast": None}：不支持任何档位
    issues = validate_policy_for_enable(policy_from_dict(_with_reasoning("high")), context)
    assert [(i.code, i.path) for i in issues] == [
        ("reasoning_unsupported", "targets.fast.models.0")
    ]


def test_enable_period_issue_points_at_period_in_field():
    raw = policy_dict()
    raw["rules"].append({
        "name": "时段", "when": {"period_in": ["missing"]}, "target": "fast",
    })
    issues = validate_policy_for_enable(policy_from_dict(raw), enable_context())
    assert [(i.code, i.path) for i in issues] == [
        ("unknown_period", "rules.3.when.period_in")
    ]
    assert issues[0].params == {"period": "missing"}


def test_enable_requires_jev_only_when_both_rules_and_questions_exist():
    def _static_policy():
        raw = policy_dict()
        raw["questions"] = {}
        raw["rules"] = [{"when": {"text_contains_any": ["95598"]}, "target": "fast"}]
        return raw

    context = enable_context()
    context.jev_available = False
    # 空 questions + 静态规则：不调用 JEV，不要求配置存在。
    assert validate_policy_for_enable(policy_from_dict(_static_policy()), context) == []

    raw = policy_dict()
    raw["rules"] = []
    # 空 rules：同样不调用 JEV。
    assert validate_policy_for_enable(policy_from_dict(raw), context) == []

    raw = policy_dict()
    issues = validate_policy_for_enable(policy_from_dict(raw), context)
    assert [(i.code, i.path) for i in issues] == [("decision_service_unavailable", "")]


def test_enable_context_is_mutable_in_place():
    context = enable_context()
    context.known_models.remove("strong")
    context.known_models.add("extra")
    context.known_periods.discard("peak")
    context.jev_available = False
    assert context.known_models == {"fast", "extra"}
    assert context.known_periods == {"normal", "offpeak"}
    assert context.jev_available is False


# ---------------------------------------------------------------------------
# Issue 展示：每个新错误码都有中英文案，载荷附 path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "path", "params"),
    [
        pytest.param("limit_exceeded", "questions", {"limit": 32}, id="limit-questions"),
        pytest.param("limit_exceeded", "rules", {"limit": 200}, id="limit-rules"),
        pytest.param(
            "limit_exceeded", "questions.route.options",
            {"kind": "options", "limit": 255, "count": 256}, id="limit-options",
        ),
        pytest.param(
            "limit_exceeded", "rules",
            {"kind": "conditions", "limit": 400}, id="limit-conditions",
        ),
        pytest.param("unknown_target", "fallback", {"target": "missing"}, id="unknown-target"),
        pytest.param("empty_models", "targets.strong.models", {"target": "strong"}, id="empty-models"),
        pytest.param("condition_depth_exceeded", "rules.0.when.all.0", {"limit": 16}, id="depth"),
        pytest.param(
            "unknown_answer_path", "rules.0.when.answer", {"answer": "missing.yes"},
            id="unknown-answer-path",
        ),
        pytest.param(
            "unknown_option_key", "rules.0.when.eq",
            {"answer": "route.selected", "option": "missing"}, id="unknown-option-key",
        ),
        pytest.param(
            "answer_operator_invalid", "rules.0.when.gte",
            {"answer": "route.selected", "op": "gte"}, id="operator-invalid",
        ),
        pytest.param(
            "answer_value_invalid", "rules.0.when.eq",
            {"answer": "route.selected", "value": 3}, id="value-invalid",
        ),
        pytest.param(
            "answer_value_out_of_range", "rules.0.when.gte",
            {"answer": "risk.yes", "value": 1.5, "min": 0, "max": 1}, id="value-out-of-range",
        ),
        pytest.param("unknown_model", "targets.strong.models.0", {"model": "strong"}, id="unknown-model"),
        pytest.param(
            "reasoning_unsupported", "targets.fast.models.0",
            {"model": "fast", "reasoning": "high", "efforts": []}, id="reasoning-unsupported",
        ),
        pytest.param("unknown_period", "rules.3.when.period_in", {"period": "missing"}, id="unknown-period"),
        pytest.param("decision_service_unavailable", "", {}, id="jev-unavailable"),
    ],
)
def test_presenter_renders_every_new_code_bilingually(code, path, params):
    message = present_issue(Issue(code, path, params=params))
    # 未映射的码会渲染成 (code, code) 防御兜底：中英文都必须是真实文案。
    assert message["zh"] != code
    assert message["en"] != code
    assert message["zh"]
    assert message["en"]


def test_issues_payload_attaches_path_and_keeps_no_graph_locators():
    issues = [
        Issue("unknown_answer_path", "rules.0.when.answer", params={"answer": "missing.yes"}),
        Issue("limit_exceeded", "questions", params={"limit": 32}),
    ]
    payload = issues_payload(issues)
    assert payload == [
        {
            "code": "unknown_answer_path",
            "zh": "答案路径未定义：missing.yes",
            "en": "Unknown answer path: missing.yes",
            "path": "rules.0.when.answer",
            "level": "hard",
        },
        {
            "code": "limit_exceeded",
            "zh": "问题数量超过上限 32",
            "en": "Too many questions: limit is 32",
            "path": "questions",
            "level": "hard",
        },
    ]
    assert "node_id" not in payload[0] and "edge_id" not in payload[0] and "port" not in payload[0]
