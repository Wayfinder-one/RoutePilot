"""Routing Policy v4 的纯条件求值（设计 §5.3）。

求值只产生布尔 ``True/False``，不存在 UNKNOWN。答案读取是固定分派表
（choice: ``selected``/``prob.<key>``/``p_top``/``margin``；noul: ``yes``/``no``；
score: ``value``），不用 getattr/eval。路径或类型错误属于编程/快照缺陷
（保存时校验已拦截的文档不会走到这些分支），``KeyError``/``TypeError``/
``ValueError`` 必须原样抛出，绝不转换为 false。

文本条件匹配经过输入窗口处理后的请求文本（设计 §4.4），时段条件匹配
本次请求计算出的活跃时段名集合。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from router.jev.client import JevAnswer
from router.routing.policy.model import (
    AnswerCondition,
    Condition,
    LogicCondition,
    PeriodCondition,
    Question,
    TextCondition,
)

__all__ = ["EvaluationContext", "answer_value", "evaluate_condition"]


@dataclass(frozen=True)
class EvaluationContext:
    """规则求值输入：问题定义、已验证答案、窗口化请求文本和活跃时段名。"""

    questions: Mapping[str, Question]
    answers: Mapping[str, JevAnswer]
    text: str
    active_periods: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", MappingProxyType(dict(self.questions)))
        object.__setattr__(self, "answers", MappingProxyType(dict(self.answers)))


def answer_value(path: str, context: EvaluationContext):
    parts = path.split(".")
    question = context.questions[parts[0]]
    answer = context.answers[question.id]
    if question.question_type == "choice" and parts[1:] == ["selected"]:
        return answer.selected_key
    if question.question_type == "choice" and len(parts) == 3 and parts[1] == "prob":
        return answer.probabilities[parts[2]]
    if question.question_type == "choice" and parts[1:] == ["p_top"]:
        return answer.p_top
    if question.question_type == "choice" and parts[1:] == ["margin"]:
        return answer.margin
    if question.question_type == "noul" and parts[1:] == ["yes"]:
        return answer.yes
    if question.question_type == "noul" and parts[1:] == ["no"]:
        return answer.no
    if question.question_type == "score" and parts[1:] == ["value"]:
        return answer.value
    raise ValueError(f"invalid validated answer path {path!r}")


def _compare(op: str, left, right) -> bool:
    """固定比较表（设计 §5.3）：数值比较用已校验的有限数值，字符串用精确相等。"""
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "eq":
        return left == right
    if op == "neq":
        return left != right
    raise ValueError(f"unknown answer comparison operator {op!r}")


def evaluate_condition(condition: Condition, context: EvaluationContext) -> bool:
    if isinstance(condition, AnswerCondition):
        return _compare(condition.op, answer_value(condition.path, context), condition.value)
    if isinstance(condition, LogicCondition):
        if condition.op == "all":
            return all(evaluate_condition(child, context) for child in condition.children)
        if condition.op == "any":
            return any(evaluate_condition(child, context) for child in condition.children)
        if condition.op == "not":
            if len(condition.children) != 1:
                raise ValueError(
                    f"'not' must hold exactly one child, got {len(condition.children)}"
                )
            return not evaluate_condition(condition.children[0], context)
        raise ValueError(f"unknown logic operator {condition.op!r}")
    if isinstance(condition, TextCondition):
        return any(needle in context.text for needle in condition.contains_any)
    if isinstance(condition, PeriodCondition):
        return any(name in context.active_periods for name in condition.names)
    raise ValueError(f"unknown condition type {type(condition).__name__}")
