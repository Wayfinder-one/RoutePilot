"""Routing Policy v4 的单请求策略执行器（设计 §5）。

一次 ``evaluate`` 至多一次 JEV 调用：所有问题一次构造、一个
``choose_many``，全部答案就绪后按序求规则，第一条命中即停止（§5.1/§5.3）。
规则为空直接兜底，没有问题只算静态规则，都不触发 JEV。只有 ``JevError``
转换为 ``jev_error`` 兜底；编程/快照缺陷（KeyError/TypeError/ValueError 等）
必须继续抛出（§5.4）。快照只读 ``policy``/``jev``/``time_periods``/
``jev_input_tokens`` 四个字段。

轨迹只含机器字段（time/window/jev/question/rule/final，§5.5），不含
wave/Candidate/端口写回事件；展示文案由前端生成。成功 final 为
``kind=target``，两类兜底 final 为 ``kind=fallback`` + ``jev_error``/
``no_rule_matched``。
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

from router.jev.client import (
    ChoiceAnswer,
    DecisionQuestion,
    JevAnswer,
    JevCandidate,
    JevError,
    NoulAnswer,
    ScoreAnswer,
)
from router.jev.window import apply_window
from router.routing.conditions import matching_periods
from router.routing.policy.conditions import EvaluationContext, evaluate_condition
from router.routing.policy.model import Policy, Rule, Target
from router.routing.types import (
    NoActivePolicyError,
    RoutingMiss,
    RoutingRequest,
)

__all__ = [
    "NoActivePolicyError",
    "PolicyEvaluator",
    "PolicyOutcome",
]


@dataclass(frozen=True)
class PolicyOutcome:
    """策略决定：目标、答案、命中规则与兜底原因。

    ``fallback_reason`` 是对外可见值（``jev_error``/``no_rule_matched``）；
    ``failure_reason`` 仅作诊断（JEV 阶段的错误文本）。兜底时
    ``matched_rule``/``matched_rule_index`` 为 None，``answers`` 为空映射。
    """

    target: Target
    answers: Mapping[str, JevAnswer]
    matched_rule: Rule | None
    matched_rule_index: int | None
    fallback_reason: str | None
    failure_reason: str | None
    jev_duration_ms: float | None
    jev_questions: tuple[str, ...]
    trace: tuple[dict, ...]


class PolicyEvaluator:
    """单请求策略执行器：一次 JevClient 边界调用 + 本地规则求值。"""

    def __init__(self, jev_client):
        self.jev_client = jev_client

    async def evaluate(
        self,
        snapshot,
        request: RoutingRequest,
        *,
        now: datetime | None = None,
        trace_sink: list[dict] | None = None,
    ) -> PolicyOutcome:
        events: list[dict] = []

        def emit(event: dict) -> None:
            events.append(event)
            if trace_sink is not None:
                trace_sink.append(event)

        policy = snapshot.policy
        if policy is None:
            raise NoActivePolicyError("no routing policy is enabled")

        # 1. 时区/活跃时段计算一次；apply_window 只跑一次（§5.1）。
        timezone = ZoneInfo(policy.timezone)
        if now is None:
            local_now = datetime.now(timezone)
        elif now.tzinfo is None:
            local_now = now.replace(tzinfo=timezone)
        else:
            local_now = now.astimezone(timezone)
        active_periods = matching_periods(local_now, list(snapshot.time_periods))
        emit({"event": "time", "active_periods": active_periods, "timezone": policy.timezone})
        window = apply_window(request.text, snapshot.jev_input_tokens)
        emit({
            "event": "window",
            "tokens_before": window.tokens_before,
            "tokens_after": window.tokens_after,
            "truncated": window.truncated,
        })

        answers: dict[str, JevAnswer] = {}
        jev_failed = False
        failure_reason: str | None = None
        jev_duration_ms: float | None = None
        jev_questions: tuple[str, ...] = ()

        # 2. 规则非空且问题非空：一次构造全部 DecisionQuestion，一个 choose_many。
        if policy.rules and policy.questions:
            questions = {
                question_id: DecisionQuestion(
                    question.question_type,
                    question.instructions,
                    tuple(JevCandidate(o.key, o.label, o.capability) for o in question.options),
                    question.criteria,
                )
                for question_id, question in snapshot.policy.questions.items()
            }
            jev_questions = tuple(questions)
            started = time.monotonic()
            try:
                if snapshot.jev is None:
                    raise JevError("JEV is not configured")
                raw_answers = await self.jev_client.choose_many(
                    snapshot.jev, window.text, questions,
                )
            except JevError as exc:  # 只有 JEV 失败走兜底；编程缺陷继续抛出
                jev_failed = True
                failure_reason = str(exc) or type(exc).__name__
                jev_duration_ms = (time.monotonic() - started) * 1000
                emit({
                    "event": "jev",
                    "questions": list(questions),
                    "duration_ms": jev_duration_ms,
                    "failed": True,
                    "error": failure_reason,
                })
            else:
                answers = dict(raw_answers)
                jev_duration_ms = float(answers[next(iter(policy.questions))].duration_ms)
                emit({
                    "event": "jev",
                    "questions": list(questions),
                    "duration_ms": jev_duration_ms,
                    "failed": False,
                    "error": None,
                })
                for question_id in policy.questions:
                    emit(_question_event(question_id, answers[question_id]))

        # 3. 全部答案就绪后按序求规则；第一条命中即停止，后续规则不求值。
        matched_rule: Rule | None = None
        matched_rule_index: int | None = None
        if policy.rules and not jev_failed:
            context = EvaluationContext(
                questions=policy.questions,
                answers=answers,
                text=window.text,
                active_periods=active_periods,
            )
            for index, rule in enumerate(policy.rules):
                matched = evaluate_condition(rule.when, context)
                emit({"event": "rule", "index": index, "name": rule.name, "matched": matched})
                if matched:
                    matched_rule = rule
                    matched_rule_index = index
                    break

        # 4. 目标确定：命中规则 → 规则目标；失败/未命中 → fallback 目标。
        if matched_rule is not None:
            target = _required_target(policy, matched_rule.target, "rule")
            fallback_reason: str | None = None
        else:
            target = _required_target(policy, policy.fallback, "fallback")
            fallback_reason = "jev_error" if jev_failed else "no_rule_matched"
        if fallback_reason is None:
            emit({"event": "final", "kind": "target", "target_id": target.id, "reason": None})
        else:
            emit({
                "event": "final", "kind": "fallback",
                "target_id": target.id, "reason": fallback_reason,
            })

        return PolicyOutcome(
            target=target,
            answers=MappingProxyType(dict(answers)),
            matched_rule=matched_rule,
            matched_rule_index=matched_rule_index,
            fallback_reason=fallback_reason,
            failure_reason=failure_reason,
            jev_duration_ms=jev_duration_ms,
            jev_questions=jev_questions,
            trace=tuple(events),
        )


def _question_event(question_id: str, answer: JevAnswer) -> dict:
    """question 轨迹：该问题规范化后的答案（§5.5）。"""
    event = {"event": "question", "question_id": question_id}
    if isinstance(answer, ChoiceAnswer):
        event["answer"] = {
            "type": "choice",
            "selected": answer.selected_key,
            "probabilities": dict(answer.probabilities),
            "ranking": list(answer.ranking),
            "p_top": answer.p_top,
            "margin": answer.margin,
        }
    elif isinstance(answer, NoulAnswer):
        event["answer"] = {"type": "noul", "yes": answer.yes, "no": answer.no}
    elif isinstance(answer, ScoreAnswer):
        event["answer"] = {"type": "score", "value": answer.value}
    else:
        raise ValueError(f"unsupported JEV answer type {type(answer).__name__}")
    return event


def _required_target(policy: Policy, target_id: str, label: str) -> Target:
    target = policy.targets.get(target_id) if target_id else None
    if target is None:
        raise RoutingMiss(f"{label} target {target_id!r} is not defined in the policy")
    return target
