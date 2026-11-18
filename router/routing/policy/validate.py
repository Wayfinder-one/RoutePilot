"""Routing Policy v4 的保存时结构校验与启用校验（设计 §7.1/§7.2）。

纯函数：不读数据库、不发 HTTP、不打印、不抛业务异常。``policy_from_dict``
已把形状错误折叠为 ``ValueError``，本层只对解析成功的 Policy 做数量上限、
时区标识、引用完整性、答案路径与比较符/取值范围的语义校验，产出机器可读的 ``Issue``
列表（空列表 = 无问题）；中英文案统一由 ``router.api.issue_presenter`` 生成。

- 限额常量唯一来源于 ``router.routing.policy.model``（MAX_*）。
- 条件遍历使用显式栈，不依赖 Python 递归上限；条件节点预算是全策略聚合
  （设计 §4.5：最多 400 个条件节点，含组合节点和叶子，根也计入），嵌套
  层数从根节点的第 1 层开始计算，超深子树不再展开。
- ``Issue.path`` 指向 v4 文档里的 JSON 定位：answer 条件的问题指向
  ``<condition>.answer``，操作符/比较值问题指向 ``<condition>.<op>``
  （如 ``rules.1.when.any.0.gte``），与设计 §6.1 的示例同构。
- ``_finish`` 按 (level, code, path, params) 稳定排序并去重，同一策略不同
  输入顺序得到相同 Issue 列表。
- ``validate_policy_for_enable`` 先跑结构校验，无结构错误时才扫描目标模型、
  reasoning 档位、``period_in`` 时段引用与 JEV 配置存在性（不做在线探测）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from router.routing.policy.model import (
    MAX_CONDITION_DEPTH,
    MAX_CONDITION_NODES,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    MAX_RULES,
    AnswerCondition,
    LogicCondition,
    PeriodCondition,
    Policy,
)

__all__ = [
    "EnableContext",
    "Issue",
    "validate_policy",
    "validate_policy_for_enable",
]

#: 概率族输出（prob/p_top/yes/no）的比较值区间（设计 §4.5）。
_PROBABILITY_BOUNDS = (0, 1)
#: margin 输出的比较值区间（设计 §4.5）。
_MARGIN_BOUNDS = (-1, 1)
#: score 的 value 输出：任意有限数值都合法，没有区间限制。
_UNBOUNDED = None


@dataclass(frozen=True)
class Issue:
    """一条校验问题：机器字段 + 文案参数，展示统一由 api.issue_presenter 生成。

    ``path`` 是 v4 文档里的 JSON 定位（如 ``rules.0.when.answer``）；
    ``params`` 只携带生成中英文案所需的插值数据，本身不是 UI 文案。
    """

    code: str
    path: str = ""
    level: str = "hard"
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass
class EnableContext:
    """启用校验上下文：模型目录、推理档位、时段定义与 JEV 配置存在性（不做在线探测）。

    非 frozen：调用方（测试/控制面）允许原地调整单个维度，例如移除某个
    已登记模型再复核启用结果。
    """

    known_models: set[str] = field(default_factory=set)
    model_reasoning: Mapping[str, Mapping[str, object] | None] = field(default_factory=dict)
    known_periods: set[str] = field(default_factory=set)
    jev_available: bool = False


def _issue_key(issue: Issue) -> tuple:
    return (issue.level, issue.code, issue.path, tuple(sorted((issue.params or {}).items())))


def _finish(issues: list[Issue]) -> list[Issue]:
    issues.sort(key=_issue_key)
    deduped: list[Issue] = []
    for issue in issues:
        if deduped and _issue_key(deduped[-1]) == _issue_key(issue):
            continue
        deduped.append(issue)
    return deduped


def _validate_answer_condition(
    policy: Policy, condition: AnswerCondition, path: str
) -> list[Issue]:
    """answer 条件语义校验（设计 §4.5 答案路径表）。

    ``condition.path`` 按 ``.`` 切分后必须与已定义问题和输出完整匹配：
    ``q.selected`` 两段、``q.prob.<option-key>`` 三段、
    ``q.p_top/q.margin/q.yes/q.no/q.value`` 两段；不接受额外段或任意 key。
    """
    segments = condition.path.split(".")
    field_path = f"{path}.{condition.op}"
    if len(segments) == 2:
        question = policy.questions.get(segments[0])
        if question is None:
            return [_unknown_path_issue(condition, path)]
        output = segments[1]
        if question.question_type == "choice":
            if output == "selected":
                if condition.op != "eq":
                    return [Issue(
                        "answer_operator_invalid", field_path,
                        params={"answer": condition.path, "op": condition.op},
                    )]
                if not isinstance(condition.value, str):
                    return [Issue(
                        "answer_value_invalid", field_path,
                        params={"answer": condition.path, "value": condition.value},
                    )]
                if condition.value not in {option.key for option in question.options}:
                    return [Issue(
                        "unknown_option_key", field_path,
                        params={"answer": condition.path, "option": condition.value},
                    )]
                return []
            if output == "p_top":
                return _numeric_issues(condition, field_path, _PROBABILITY_BOUNDS)
            if output == "margin":
                return _numeric_issues(condition, field_path, _MARGIN_BOUNDS)
            return [_unknown_path_issue(condition, path)]
        if question.question_type == "noul" and output in ("yes", "no"):
            return _numeric_issues(condition, field_path, _PROBABILITY_BOUNDS)
        if question.question_type == "score" and output == "value":
            return _numeric_issues(condition, field_path, _UNBOUNDED)
        return [_unknown_path_issue(condition, path)]
    if len(segments) == 3 and segments[1] == "prob":
        question = policy.questions.get(segments[0])
        if question is None or question.question_type != "choice":
            return [_unknown_path_issue(condition, path)]
        option_key = segments[2]
        if option_key not in {option.key for option in question.options}:
            return [Issue(
                "unknown_option_key", field_path,
                params={"answer": condition.path, "option": option_key},
            )]
        return _numeric_issues(condition, field_path, _PROBABILITY_BOUNDS)
    return [_unknown_path_issue(condition, path)]


def _unknown_path_issue(condition: AnswerCondition, path: str) -> Issue:
    return Issue(
        "unknown_answer_path", f"{path}.answer", params={"answer": condition.path}
    )


def _numeric_issues(
    condition: AnswerCondition, field_path: str, bounds: tuple[float, float] | None
) -> list[Issue]:
    """数值输出的比较值类型与区间检查（解析层只保证 str 或有限数值）。"""
    value = condition.value
    if isinstance(value, str):
        return [Issue(
            "answer_value_invalid", field_path,
            params={"answer": condition.path, "value": value},
        )]
    if bounds is not None and not bounds[0] <= value <= bounds[1]:
        return [Issue(
            "answer_value_out_of_range", field_path,
            params={
                "answer": condition.path, "value": value,
                "min": bounds[0], "max": bounds[1],
            },
        )]
    return []


def validate_policy(policy: Policy) -> list[Issue]:
    """结构校验（保存/试算，设计 §7.1）：限额、引用、时区、答案路径与取值范围。"""
    issues: list[Issue] = []
    if len(policy.questions) > MAX_QUESTIONS:
        issues.append(Issue("limit_exceeded", "questions", params={"limit": MAX_QUESTIONS}))
    if len(policy.rules) > MAX_RULES:
        issues.append(Issue("limit_exceeded", "rules", params={"limit": MAX_RULES}))
    # 时区必须是 ZoneInfo 可解析的标识（v3 的 invalid_timezone 语义回归）：
    # 否则求值期 ZoneInfo(policy.timezone) 才失败，线上每次 auto 请求 503。
    try:
        ZoneInfo(policy.timezone)
    except (KeyError, ValueError, OSError):
        issues.append(Issue(
            "invalid_timezone", "timezone", params={"timezone": policy.timezone}))
    if policy.fallback not in policy.targets:
        issues.append(Issue("unknown_target", "fallback", params={"target": policy.fallback}))
    for tid, target in policy.targets.items():
        if not target.models:
            issues.append(Issue("empty_models", f"targets.{tid}.models", params={"target": tid}))
    for qid, question in policy.questions.items():
        if question.question_type == "choice" and len(question.options) > MAX_OPTIONS:
            issues.append(Issue(
                "limit_exceeded", f"questions.{qid}.options",
                params={"kind": "options", "limit": MAX_OPTIONS, "count": len(question.options)},
            ))
    condition_count = 0
    for index, rule in enumerate(policy.rules):
        base = f"rules.{index}"
        if rule.target not in policy.targets:
            issues.append(Issue("unknown_target", base + ".target", params={"target": rule.target}))
        stack = [(rule.when, base + ".when", 1)]
        while stack:
            condition, path, depth = stack.pop()
            condition_count += 1
            if condition_count > MAX_CONDITION_NODES:
                issues.append(Issue(
                    "limit_exceeded", "rules",
                    params={"kind": "conditions", "limit": MAX_CONDITION_NODES},
                ))
                return _finish(issues)
            if depth > MAX_CONDITION_DEPTH:
                issues.append(Issue(
                    "condition_depth_exceeded", path, params={"limit": MAX_CONDITION_DEPTH},
                ))
                continue
            if isinstance(condition, LogicCondition):
                stack.extend(
                    (child, f"{path}.{condition.op}.{child_index}", depth + 1)
                    for child_index, child in enumerate(condition.children)
                )
            elif isinstance(condition, AnswerCondition):
                issues.extend(_validate_answer_condition(policy, condition, path))
    return _finish(issues)


def _condition_locations(policy: Policy):
    """产出规则条件树里的 (condition, path)；结构校验已通过，遍历有界。"""
    for index, rule in enumerate(policy.rules):
        stack = [(rule.when, f"rules.{index}.when")]
        while stack:
            condition, path = stack.pop()
            yield condition, path
            if isinstance(condition, LogicCondition):
                stack.extend(
                    (child, f"{path}.{condition.op}.{child_index}")
                    for child_index, child in enumerate(condition.children)
                )


def validate_policy_for_enable(
    policy: Policy, context: EnableContext
) -> list[Issue]:
    """启用校验（设计 §7.2）：结构校验通过后，再查模型目录、时段与 JEV 配置。"""
    if not isinstance(context, EnableContext):
        raise TypeError(
            f"enable validation expects an EnableContext, got {type(context).__name__}"
        )
    issues = validate_policy(policy)
    if issues:
        return issues
    for tid, target in policy.targets.items():
        for model_index, item in enumerate(target.models):
            model_path = f"targets.{tid}.models.{model_index}"
            if item.model not in context.known_models:
                issues.append(Issue("unknown_model", model_path, params={"model": item.model}))
                continue
            supported = (context.model_reasoning or {}).get(item.model)
            efforts = {
                entry.get("id")
                for entry in (supported or {}).get("efforts", [])
                if isinstance(entry, dict)
            } if isinstance(supported, dict) else set()
            if item.reasoning is not None and (
                not isinstance(supported, dict) or not supported.get("supported")
                or item.reasoning not in efforts
            ):
                issues.append(Issue(
                    "reasoning_unsupported", model_path,
                    params={
                        "model": item.model, "reasoning": item.reasoning,
                        "efforts": sorted(efforts),
                    },
                ))
    for condition, path in _condition_locations(policy):
        if isinstance(condition, PeriodCondition):
            for name in sorted(set(condition.names)):
                if name not in context.known_periods:
                    issues.append(Issue(
                        "unknown_period", f"{path}.period_in", params={"period": name},
                    ))
    if policy.rules and policy.questions and not context.jev_available:
        issues.append(Issue("decision_service_unavailable"))
    return _finish(issues)
