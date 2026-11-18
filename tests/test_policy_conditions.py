"""Policy v4 纯条件求值：answer_value 固定分派、比较表、all/any/not、文本和时段。

路径或类型错误属于编程/快照缺陷，必须以 KeyError/ValueError 原样抛出，
绝不转换为 false（设计 §5.3）。所有 evaluate_condition 断言都用 ``is True/False``
钉住"只有布尔值、不存在 UNKNOWN"。
"""
import pytest

from router.routing.policy.conditions import (
    EvaluationContext,
    answer_value,
    evaluate_condition,
)
from router.routing.policy.model import (
    AnswerCondition,
    ChoiceOption,
    LogicCondition,
    PeriodCondition,
    Question,
    TextCondition,
)
from tests.policy_harness import choice, noul, score

QUESTIONS = {
    "route": Question(
        id="route",
        question_type="choice",
        instructions="选择模型",
        options=(
            ChoiceOption(key="fast", label="轻量", capability="简单请求"),
            ChoiceOption(key="strong", label="增强", capability="复杂请求"),
        ),
    ),
    "risk": Question(id="risk", question_type="noul", instructions="是否高风险"),
    "severity": Question(
        id="severity", question_type="score", instructions="评分",
        criteria=(1.0, 5.0, 10.0),
    ),
}


def context(**overrides):
    values = dict(
        questions=QUESTIONS,
        answers={
            "route": choice("fast", 0.8),
            "risk": noul(0.9),
            "severity": score(10.0),
        },
        text="95598 抢修工单如何处理",
        active_periods=["peak"],
    )
    values.update(overrides)
    return EvaluationContext(**values)


def test_answer_value_dispatches_every_validated_output():
    ctx = context()
    assert answer_value("route.selected", ctx) == "fast"
    assert answer_value("route.prob.fast", ctx) == pytest.approx(0.8)
    assert answer_value("route.prob.strong", ctx) == pytest.approx(0.2)
    assert answer_value("route.p_top", ctx) == pytest.approx(0.8)
    assert answer_value("route.margin", ctx) == pytest.approx(0.6)
    assert answer_value("risk.yes", ctx) == pytest.approx(0.9)
    assert answer_value("risk.no", ctx) == pytest.approx(0.1)
    assert answer_value("severity.value", ctx) == 10.0


@pytest.mark.parametrize(
    ["path", "error"],
    [
        ("route", ValueError),  # 只有问题段，没有输出段
        ("route.selected.deep", ValueError),  # 多余段
        ("route.prob", ValueError),  # prob 缺 option key
        ("route.value", ValueError),  # choice 没有 value 输出
        ("risk.selected", ValueError),  # noul 没有 choice 输出
        ("severity.yes", ValueError),  # score 没有 noul 输出
        ("", KeyError),  # 空路径 → 未知问题
        ("missing.yes", KeyError),  # 未定义问题
        ("route.prob.unknown", KeyError),  # 未定义 option key
    ],
)
def test_answer_value_path_or_type_errors_propagate(path, error):
    with pytest.raises(error):
        answer_value(path, context())


def test_missing_answer_key_propagates_as_programming_error():
    ctx = context(answers={"route": choice("fast", 0.8)})
    with pytest.raises(KeyError):
        answer_value("risk.yes", ctx)


@pytest.mark.parametrize(
    ["op", "value", "expected"],
    [
        ("gt", 9.0, True), ("gt", 10.0, False),
        ("gte", 10.0, True), ("gte", 10.5, False),
        ("lt", 11.0, True), ("lt", 10.0, False),
        ("lte", 10.0, True), ("lte", 9.5, False),
        ("eq", 10.0, True), ("eq", 9.0, False),
        ("neq", 9.0, True), ("neq", 10.0, False),
    ],
)
def test_comparison_table(op, value, expected):
    condition = AnswerCondition(path="severity.value", op=op, value=value)
    assert evaluate_condition(condition, context()) is expected


def test_selected_compares_by_exact_string_equality():
    assert evaluate_condition(
        AnswerCondition("route.selected", "eq", "fast"), context()) is True
    assert evaluate_condition(
        AnswerCondition("route.selected", "eq", "strong"), context()) is False
    assert evaluate_condition(
        AnswerCondition("route.selected", "neq", "strong"), context()) is True


def test_unknown_comparison_operator_raises():
    with pytest.raises(ValueError, match="unknown answer comparison"):
        evaluate_condition(AnswerCondition("risk.yes", "approx", 0.9), context())


def test_all_any_not_combinators():
    hi = AnswerCondition("risk.yes", "gte", 0.8)
    lo = AnswerCondition("risk.yes", "lt", 0.2)
    ctx = context()
    assert evaluate_condition(LogicCondition("all", (hi, lo)), ctx) is False
    assert evaluate_condition(LogicCondition("all", (hi,)), ctx) is True
    assert evaluate_condition(LogicCondition("any", (hi, lo)), ctx) is True
    assert evaluate_condition(LogicCondition("any", (lo, lo)), ctx) is False
    assert evaluate_condition(LogicCondition("not", (hi,)), ctx) is False
    assert evaluate_condition(LogicCondition("not", (lo,)), ctx) is True
    nested = LogicCondition("not", (
        LogicCondition("all", (hi, AnswerCondition("route.selected", "eq", "strong"))),
    ))
    assert evaluate_condition(nested, ctx) is True


def test_not_with_multiple_children_is_a_programming_error():
    with pytest.raises(ValueError, match="exactly one child"):
        evaluate_condition(
            LogicCondition("not", (
                AnswerCondition("risk.yes", "gte", 0.8),
                AnswerCondition("risk.yes", "lt", 0.2),
            )),
            context(),
        )


def test_unknown_logic_operator_raises():
    with pytest.raises(ValueError, match="unknown logic operator"):
        evaluate_condition(
            LogicCondition("xor", (AnswerCondition("risk.yes", "gte", 0.8),)),
            context(),
        )


def test_text_condition_matches_any_needle_in_context_text():
    ctx = context()
    assert evaluate_condition(
        TextCondition(contains_any=("95598", "客服")), ctx) is True
    assert evaluate_condition(
        TextCondition(contains_any=("不存在", "客服")), ctx) is False


def test_period_condition_matches_any_active_period_name():
    ctx = context()
    assert evaluate_condition(PeriodCondition(names=("peak", "night")), ctx) is True
    assert evaluate_condition(PeriodCondition(names=("night",)), ctx) is False
    assert evaluate_condition(
        PeriodCondition(names=("peak",)), context(active_periods=[])) is False


def test_unknown_condition_type_raises():
    with pytest.raises(ValueError, match="unknown condition type"):
        evaluate_condition(object(), context())


def test_answer_path_errors_inside_conditions_never_become_false():
    with pytest.raises(KeyError):
        evaluate_condition(AnswerCondition("missing.yes", "gte", 0.5), context())
    with pytest.raises(ValueError):
        evaluate_condition(AnswerCondition("route.unknown", "eq", "fast"), context())


def test_evaluation_context_wraps_mappings_read_only():
    ctx = context()
    with pytest.raises(TypeError):
        ctx.answers["route"] = None
    with pytest.raises(TypeError):
        ctx.questions["extra"] = None
