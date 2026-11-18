"""PolicyEvaluator 端到端场景：单请求不变量、逻辑/文本/时段规则、真实 JevClient 失败链。

所有多问题场景都断言恰好一次 JEV 调用（``len(jev.calls) == 1`` 或真实客户端
只发出一个请求）：一次决策至多一次调用、规则引用后续答案不产生第二请求
（设计 §5.1/§5.3）。真实链路用例复用 wire 契约测试的 MockTransport 形状，
验证 timeout / 非 200 / 额外答案在客户端边界被折成 ``jev_error`` 兜底。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import httpx

from router.jev.client import JevClient
from router.jev.config import JevSettings
from router.routing.conditions import TimePeriod
from router.routing.policy import policy_from_dict
from router.routing.policy.evaluator import PolicyEvaluator
from router.routing.types import RoutingRequest
from tests.policy_harness import FakeJev, choice, noul, run_policy, score
from tests.routing_fixtures import policy_dict

NOW = datetime.fromisoformat("2026-11-18T10:00:00+08:00")

LOW_RISK_ANSWERS = {
    "route": choice("fast", 1.0),
    "risk": noul(0.1),
    "severity": score(1.0),
}


def snapshot_of(raw, *, time_periods=()):
    return SimpleNamespace(
        policy=policy_from_dict(raw),
        jev=JevSettings("http://jev.test", "d1-2B"),
        time_periods=list(time_periods),
        jev_input_tokens=1024,
    )


def evaluate(snapshot, jev_client, *, text="测试请求"):
    return asyncio.run(PolicyEvaluator(jev_client).evaluate(
        snapshot, RoutingRequest(text), now=NOW,
    ))


def test_rule_matching_later_answers_needs_no_second_request():
    outcome, jev = run_policy(policy_dict(), answers={
        "route": choice("strong", 1.0),
        "risk": noul(0.1),
        "severity": score(10.0),
    })
    assert len(jev.calls) == 1
    assert list(jev.calls[0]["questions"]) == ["route", "risk", "severity"]
    assert outcome.target.id == "strong"
    assert outcome.matched_rule_index == 1
    assert outcome.matched_rule.name == "复杂请求"
    assert [e["event"] for e in outcome.trace].count("jev") == 1
    # 第一条命中即停止：规则 2 不再求值。
    assert [e["index"] for e in outcome.trace if e["event"] == "rule"] == [0, 1]


def test_all_any_not_rule_forms_route_end_to_end():
    raw = dict(policy_dict(), rules=[{
        "name": "组合",
        "when": {"all": [
            {"not": {"answer": "risk.yes", "gte": 0.8}},
            {"any": [
                {"answer": "route.selected", "eq": "strong"},
                {"answer": "severity.value", "gte": 5},
            ]},
        ]},
        "target": "strong",
    }])
    outcome, jev = run_policy(raw, answers={
        "route": choice("fast", 1.0),
        "risk": noul(0.1),
        "severity": score(10.0),
    })
    assert len(jev.calls) == 1
    assert outcome.target.id == "strong"
    assert outcome.matched_rule_index == 0
    assert outcome.fallback_reason is None


def test_not_any_negation_falls_through_to_no_rule_matched():
    raw = dict(policy_dict(), rules=[{
        "name": "否定",
        "when": {"not": {"any": [
            {"answer": "route.prob.fast", "gt": 0.5},
            {"answer": "risk.no", "gt": 0.5},
        ]}},
        "target": "strong",
    }])
    outcome, jev = run_policy(raw, answers={
        "route": choice("fast", 0.9),
        "risk": noul(0.1),
        "severity": score(1.0),
    })
    assert len(jev.calls) == 1
    assert outcome.fallback_reason == "no_rule_matched"
    assert outcome.target.id == "safe"


def test_no_rule_matched_keeps_valid_answers_and_full_rule_trace():
    outcome, jev = run_policy(policy_dict(), answers={
        "route": choice("strong", 0.9),
        "risk": noul(0.1),
        "severity": score(1.0),
    })
    assert len(jev.calls) == 1
    assert outcome.fallback_reason == "no_rule_matched"
    assert outcome.target.id == "safe"
    # 答案本身合法：不命中规则也保留，供试算/审计读取。
    assert set(outcome.answers) == {"route", "risk", "severity"}
    rule_events = [e for e in outcome.trace if e["event"] == "rule"]
    assert [(e["index"], e["matched"]) for e in rule_events] == [
        (0, False), (1, False), (2, False),
    ]
    assert outcome.trace[-1] == {
        "event": "final", "kind": "fallback", "target_id": "safe",
        "reason": "no_rule_matched",
    }


def test_text_condition_matches_windowed_request_text():
    raw = dict(policy_dict(), rules=[
        {"name": "电力", "when": {"text_contains_any": ["95598", "客服"]}, "target": "strong"},
    ])
    jev = FakeJev(dict(LOW_RISK_ANSWERS))
    outcome = evaluate(snapshot_of(raw), jev, text="95598 抢修工单如何处理")
    assert len(jev.calls) == 1
    assert outcome.target.id == "strong"
    assert outcome.matched_rule_index == 0
    assert jev.calls[0]["text"] == "95598 抢修工单如何处理"


def test_truncated_window_feeds_jev_and_text_rules_once():
    raw = dict(policy_dict(), rules=[
        {"name": "抢修", "when": {"text_contains_any": ["抢修"]}, "target": "strong"},
    ])
    jev = FakeJev(dict(LOW_RISK_ANSWERS))
    long_text = "背景介绍" * 400 + "抢修"
    outcome = evaluate(snapshot_of(raw), jev, text=long_text)
    assert len(jev.calls) == 1
    # apply_window 只执行一次：JEV 与文本条件用的是同一份窗口化文本。
    assert jev.calls[0]["text"] != long_text
    assert "抢修" in jev.calls[0]["text"]
    window_event = outcome.trace[1]
    assert window_event["event"] == "window"
    assert window_event["truncated"] is True
    assert window_event["tokens_before"] > window_event["tokens_after"]
    assert outcome.target.id == "strong"


def test_period_condition_routes_on_active_periods():
    raw = dict(policy_dict(), rules=[
        {"name": "高峰", "when": {"period_in": ["peak", "night"]}, "target": "strong"},
    ])
    jev = FakeJev(dict(LOW_RISK_ANSWERS))
    periods = [TimePeriod("peak", (2,), "09:00", "12:00")]
    outcome = evaluate(snapshot_of(raw, time_periods=periods), jev)
    assert len(jev.calls) == 1
    assert outcome.target.id == "strong"
    assert outcome.matched_rule_index == 0
    # 2026-11-18 10:00 +08:00 是 Asia/Shanghai 的周三上午：peak 活跃。
    assert outcome.trace[0] == {
        "event": "time", "active_periods": ["peak"], "timezone": "Asia/Shanghai",
    }


def test_period_condition_with_inactive_period_falls_back():
    raw = dict(policy_dict(), rules=[
        {"name": "夜间", "when": {"period_in": ["night"]}, "target": "strong"},
    ])
    jev = FakeJev(dict(LOW_RISK_ANSWERS))
    periods = [TimePeriod("peak", (2,), "09:00", "12:00")]
    outcome = evaluate(snapshot_of(raw, time_periods=periods), jev)
    assert len(jev.calls) == 1
    assert outcome.fallback_reason == "no_rule_matched"
    assert outcome.target.id == "safe"


# ── 真实 JevClient 链路（MockTransport）─────────────────────────────────


def wire_body(mutate=None):
    body = {
        "answers": {
            "route": {"type": "choice", "probabilities": {"轻量": 0.9, "增强": 0.1}},
            "risk": {"type": "noul", "noul": 0.1},
            "severity": {"type": "score", "score": 0.0, "legend": {"0": 1.0}},
        },
        "usage": {"input_tokens": 20},
    }
    if mutate is not None:
        mutate(body)
    return body


def run_real(handler, *, text="测试请求"):
    requests = []

    def recording(request):
        requests.append(request)
        return handler(request)

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(recording)))
    outcome = asyncio.run(PolicyEvaluator(client).evaluate(
        snapshot_of(policy_dict()), RoutingRequest(text), now=NOW,
    ))
    return outcome, requests


def test_real_client_success_routes_first_matching_rule():
    outcome, requests = run_real(lambda request: httpx.Response(200, json=wire_body()))
    assert len(requests) == 1
    assert outcome.target.id == "fast"
    assert outcome.matched_rule_index == 2
    assert outcome.fallback_reason is None
    assert set(outcome.answers) == {"route", "risk", "severity"}
    payload = json.loads(requests[0].content)
    assert list(payload["questions"]) == ["route", "risk", "severity"]


def test_choice_tie_resolves_by_options_order_end_to_end():
    body = wire_body(lambda b: b["answers"]["route"].update(
        probabilities={"轻量": 0.5, "增强": 0.5}))
    outcome, requests = run_real(lambda request: httpx.Response(200, json=body))
    assert len(requests) == 1
    # options 数组序 fast(轻量) 在前：wire 概率并列时按配置序决胜 → 规则 2 命中 fast。
    assert outcome.answers["route"].selected_key == "fast"
    assert outcome.target.id == "fast"
    assert outcome.matched_rule_index == 2


def test_timeout_via_real_client_falls_back_to_jev_error():
    def handler(request):
        raise httpx.TimeoutException("slow upstream")

    outcome, requests = run_real(handler)
    assert len(requests) == 1
    assert outcome.fallback_reason == "jev_error"
    assert "TimeoutException" in outcome.failure_reason
    assert outcome.target.id == "safe"
    assert outcome.answers == {}
    assert outcome.trace[-1] == {
        "event": "final", "kind": "fallback", "target_id": "safe", "reason": "jev_error",
    }


def test_non_200_via_real_client_falls_back_to_jev_error():
    outcome, requests = run_real(lambda request: httpx.Response(500, text="down"))
    assert len(requests) == 1
    assert outcome.fallback_reason == "jev_error"
    assert outcome.failure_reason == "JEV returned HTTP 500"
    assert outcome.answers == {}
    assert outcome.target.id == "safe"


def test_extra_answer_via_real_client_falls_back_to_jev_error():
    body = wire_body(lambda b: b["answers"].update(
        unused={"type": "noul", "noul": 1.0}))
    outcome, requests = run_real(lambda request: httpx.Response(200, json=body))
    assert len(requests) == 1
    assert outcome.fallback_reason == "jev_error"
    assert "answers do not match questions" in outcome.failure_reason
    assert outcome.answers == {}
    assert outcome.target.id == "safe"
