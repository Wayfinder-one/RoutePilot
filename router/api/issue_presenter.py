"""Bilingual console copy for policy validation ``Issue`` values.

Validators are machine-only: an ``Issue`` carries ``code`` + ``path`` +
``params`` and no human text.  This module is the single place that renders
that machine fact into the ``zh``/``en`` pair the console (and the 422 payload)
has always received, so validators and domain code never maintain UI copy.
"""
from __future__ import annotations

from router.routing.policy.validate import Issue


def _text(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _param(issue: Issue, name: str, default=""):
    return (issue.params or {}).get(name, default)


def present_issue(issue: Issue) -> dict[str, str]:
    """Render one machine ``Issue`` into the console's bilingual message."""
    code = issue.code
    kind = _param(issue, "kind")

    if code == "limit_exceeded":
        limit = _param(issue, "limit")
        count = _param(issue, "count")
        if kind == "options":
            return _text(f"选项数量超过上限 {limit}（当前 {count} 个）",
                         f"Too many options: limit is {limit} (found {count})")
        if kind == "conditions":
            return _text(f"条件节点总数超过上限 {limit}",
                         f"Too many condition nodes: the policy-wide limit is {limit}")
        if issue.path == "questions":
            return _text(f"问题数量超过上限 {limit}",
                         f"Too many questions: limit is {limit}")
        return _text(f"规则数量超过上限 {limit}",
                     f"Too many rules: limit is {limit}")
    if code == "unknown_target":
        target = _param(issue, "target")
        return _text(f"引用了未定义的目标：{target}",
                     f"References an undefined target: {target}")
    if code == "empty_models":
        target = _param(issue, "target")
        return _text(f"目标未配置任何模型：{target}",
                     f"Target has no models configured: {target}")
    if code == "condition_depth_exceeded":
        limit = _param(issue, "limit")
        return _text(f"条件嵌套超过 {limit} 层",
                     f"Condition nesting exceeds the {limit}-level limit")
    if code == "unknown_answer_path":
        answer = _param(issue, "answer")
        return _text(f"答案路径未定义：{answer}",
                     f"Unknown answer path: {answer}")
    if code == "unknown_option_key":
        option = _param(issue, "option")
        answer = _param(issue, "answer")
        return _text(f"选项 key 未定义：{option}（{answer}）",
                     f"Unknown option key: {option} ({answer})")
    if code == "answer_operator_invalid":
        answer = _param(issue, "answer")
        operator = _param(issue, "op")
        return _text(f"selected 只允许 eq 比较符：{answer} 使用了 {operator}",
                     f"selected only allows the eq operator: {answer} uses {operator}")
    if code == "answer_value_invalid":
        answer = _param(issue, "answer")
        value = _param(issue, "value")
        return _text(f"比较值类型非法：{answer} 的比较值 {value!r}",
                     f"Invalid comparison value: {answer} compares against {value!r}")
    if code == "answer_value_out_of_range":
        answer = _param(issue, "answer")
        value = _param(issue, "value")
        lower = _param(issue, "min")
        upper = _param(issue, "max")
        return _text(f"比较值越界：{answer} 的比较值 {value!r} 必须在 [{lower}, {upper}]",
                     f"Comparison value out of range: {answer} compares against {value!r}, "
                     f"which must be within [{lower}, {upper}]")
    if code == "unknown_model":
        model = _param(issue, "model")
        return _text(f"目标引用了未登记模型：{model}",
                     f"Target references an unknown model: {model}")
    if code == "reasoning_unsupported":
        model = _param(issue, "model")
        efforts = _param(issue, "efforts")
        reasoning = _param(issue, "reasoning")
        return _text(
            f"模型不支持所选推理档位：{model} 支持 {efforts!r}，目标配置 {reasoning!r}",
            f"Model does not support the selected reasoning tier: {model} supports {efforts!r}, "
            f"target uses {reasoning!r}",
        )
    if code == "unknown_period":
        period = _param(issue, "period")
        return _text(f"条件引用未定义时段：{period}",
                     f"Condition references an undefined period: {period}")
    if code == "decision_service_unavailable":
        return _text(
            "策略包含问题与规则，但决策服务（JEV）未配置或不可用",
            "The policy has questions and rules but the decision service (JEV) "
            "is not configured or unavailable",
        )
    # Defensive: an unmapped code must still render deterministically.
    return _text(code, code)


def issues_payload(issues) -> list[dict]:
    """Issue 列表 → 422 载荷（双语字段 + 机器定位 ``path``）。

    Issue 本身只存机器字段；中文/英文在这里由 issue_presenter 统一生成，
    ``path`` 直接来自校验器，控制台用它定位文档位置。
    """
    payload = []
    for issue in issues:
        message = present_issue(issue)
        payload.append({
            "code": issue.code,
            "zh": message["zh"],
            "en": message["en"],
            "path": issue.path,
            "level": issue.level,
        })
    return payload
