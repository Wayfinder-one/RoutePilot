"""Routing Policy v4 的纯条件求值（设计 §5.3）。

求值只产生布尔 ``True/False``，不存在 UNKNOWN。答案读取是固定分派表
（choice: ``selected``/``prob.<key>``/``p_top``/``margin``；noul: ``yes``/``no``；
score: ``value``），不用 getattr/eval。路径或类型错误属于编程/快照缺陷
（保存时校验已拦截的文档不会走到这些分支），``KeyError``/``TypeError``/
``ValueError`` 必须原样抛出，绝不转换为 false。

同一个 ``evaluate_condition`` 同时负责布尔结果和条件轨迹（Task 3）：
``trace_sink`` 非空时按 ``path`` 追加扁平机器事件，布尔结果与事件里的
``result`` 来自同一次计算，不另写第二套比较逻辑。``all/any`` 先求全部
child 再调用组合（最多 400 个本地节点，不做 JEV 短路），保证逻辑门下的
每个条件都有结果；``trace_sink`` 为 None 时不构造任何事件，热路径零开销。

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


def _child_path(path: str, op: str, child_index: int) -> str:
    """子条件定位：与 validate.py 的定位语法完全一致（``when.all.0`` 这类）。"""
    return f"{path}.{op}.{child_index}"


def evaluate_condition(
    condition: Condition,
    context: EvaluationContext,
    *,
    path: str = "when",
    trace_sink: list[dict] | None = None,
) -> bool:
    """求值一个条件子树；``trace_sink`` 非空时逐节点追加扁平机器事件。"""
    if isinstance(condition, AnswerCondition):
        actual = answer_value(condition.path, context)
        result = _compare(condition.op, actual, condition.value)
        if trace_sink is not None:
            trace_sink.append({
                "path": path, "kind": "answer", "answer": condition.path,
                "op": condition.op, "expected": condition.value,
                "actual": actual, "result": result,
            })
        return result
    if isinstance(condition, LogicCondition):
        if condition.op in ("all", "any"):
            # 先求全部 child 再调用 all/any：让逻辑门下的每个条件都有结果，
            # 不做布尔短路（≤400 个本地节点，省下的是可读 trace，不是 JEV 调用）。
            child_results = [
                evaluate_condition(
                    child, context,
                    path=_child_path(path, condition.op, child_index),
                    trace_sink=trace_sink,
                )
                for child_index, child in enumerate(condition.children)
            ]
            result = all(child_results) if condition.op == "all" else any(child_results)
        elif condition.op == "not":
            if len(condition.children) != 1:
                raise ValueError(
                    f"'not' must hold exactly one child, got {len(condition.children)}"
                )
            child_results = [evaluate_condition(
                condition.children[0], context,
                path=_child_path(path, "not", 0),
                trace_sink=trace_sink,
            )]
            result = not child_results[0]
        else:
            raise ValueError(f"unknown logic operator {condition.op!r}")
        if trace_sink is not None:
            # 组合节点事件使用「本层集合」的完整路径：根 all 在 when 时为
            # "when.all"，其 child 是 "when.all.0"（计划 Task 3 的固定语法）。
            trace_sink.append({
                "path": f"{path}.{condition.op}",
                "kind": condition.op, "result": result,
            })
        return result
    if isinstance(condition, TextCondition):
        result = any(needle in context.text for needle in condition.contains_any)
        if trace_sink is not None:
            trace_sink.append({
                "path": path, "kind": "text_contains_any",
                "contains_any": list(condition.contains_any), "result": result,
            })
        return result
    if isinstance(condition, PeriodCondition):
        result = any(name in context.active_periods for name in condition.names)
        if trace_sink is not None:
            trace_sink.append({
                "path": path, "kind": "period_in",
                "names": list(condition.names), "result": result,
            })
        return result
    raise ValueError(f"unknown condition type {type(condition).__name__}")
