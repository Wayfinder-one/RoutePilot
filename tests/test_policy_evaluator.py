"""PolicyEvaluator 固定流程：单请求全问题、首条命中、失败兜底和轨迹契约。

轨迹事件集合是设计 §5.5 的机器字段契约：time/window/jev/question/rule/final，
没有任何 wave/Candidate/端口字段；成功与两类兜底的 final 形状固定。
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from router.jev.client import JevError
from router.jev.config import JevSettings
from router.routing.policy import policy_from_dict
from router.routing.policy.evaluator import NoActivePolicyError, PolicyEvaluator
from router.routing.types import RoutingRequest
from tests.policy_harness import FakeJev, choice, noul, run_policy, score
from tests.routing_fixtures import policy_dict

NOW = datetime.fromisoformat("2026-11-18T10:00:00+08:00")

FULL_ANSWERS = {
    "route": choice("fast", 1.0),
    "risk": noul(0.9),
    "severity": score(10.0),
}


def test_all_questions_share_one_call_even_when_first_rule_short_circuits():
    outcome, jev = run_policy(policy_dict(), answers={
        "route": choice("fast", 1.0),
        "risk": noul(0.9),
        "severity": score(10.0),
    })
    assert len(jev.calls) == 1
    assert list(jev.calls[0]["questions"]) == ["route", "risk", "severity"]
    assert outcome.target.id == "safe"
    assert outcome.matched_rule_index == 0
    assert outcome.fallback_reason is None
    assert [e["event"] for e in outcome.trace].count("jev") == 1


def test_bad_answer_falls_back_without_partial_answers():
    outcome, jev = run_policy(policy_dict(), error=JevError("bad risk answer"))
    assert len(jev.calls) == 1
    assert outcome.target.id == "safe"
    assert outcome.answers == {}
    assert outcome.fallback_reason == "jev_error"
    assert outcome.failure_reason == "bad risk answer"


def test_trace_event_sequence_matches_design_contract():
    outcome, _ = run_policy(policy_dict(), answers=dict(FULL_ANSWERS))
    # 命中规则 0 后：规则 1/2 不求值，但仍以 matched=None + conditions=[] 追加。
    assert [e["event"] for e in outcome.trace] == [
        "time", "window", "jev",
        "question", "question", "question",
        "rule", "rule", "rule",
        "final",
    ]
    assert outcome.trace[0] == {
        "event": "time", "active_periods": [], "timezone": "Asia/Shanghai",
    }
    assert outcome.trace[1] == {
        "event": "window", "tokens_before": 4, "tokens_after": 4, "truncated": False,
    }
    assert outcome.trace[2] == {
        "event": "jev",
        "questions": ["route", "risk", "severity"],
        "duration_ms": 12.0,
        "failed": False,
        "error": None,
    }
    assert [e["question_id"] for e in outcome.trace[3:6]] == ["route", "risk", "severity"]
    assert outcome.trace[3]["answer"] == {
        "type": "choice", "selected": "fast",
        "probabilities": {"fast": 1.0, "strong": 0.0},
        "ranking": ["fast", "strong"], "p_top": 1.0, "margin": 1.0,
    }
    assert outcome.trace[4]["answer"] == pytest.approx({"type": "noul", "yes": 0.9, "no": 0.1})
    assert outcome.trace[5]["answer"] == {"type": "score", "value": 10.0}
    assert outcome.trace[6] == {
        "event": "rule", "index": 0, "name": "高风险", "matched": True,
        "conditions": [{
            "path": "when", "kind": "answer", "answer": "risk.yes",
            "op": "gte", "expected": 0.8, "actual": 0.9, "result": True,
        }],
    }
    assert outcome.trace[7] == {
        "event": "rule", "index": 1, "name": "复杂请求",
        "matched": None, "conditions": [],
    }
    assert outcome.trace[8] == {
        "event": "rule", "index": 2, "name": "轻量请求",
        "matched": None, "conditions": [],
    }
    assert outcome.trace[9] == {
        "event": "final", "kind": "target", "target_id": "safe", "reason": None,
    }
    assert outcome.jev_duration_ms == 12.0
    assert outcome.jev_questions == ("route", "risk", "severity")


def test_rule_trace_has_each_logic_child_and_marks_later_rules_skipped():
    raw = policy_dict()
    raw["rules"].append({
        "name": "不应执行", "when": {"answer": "risk.yes", "gte": 0.1}, "target": "safe",
    })
    outcome, _jev = run_policy(raw, answers={
        "route": choice("fast", 0.9), "risk": noul(0.1), "severity": score(1.0),
    })
    rules = [e for e in outcome.trace if e["event"] == "rule"]
    assert [e["matched"] for e in rules] == [False, False, True, None]
    assert [c["path"] for c in rules[1]["conditions"]] == [
        "when.all.0", "when.all.1", "when.all",
    ]
    assert rules[1]["conditions"][0]["actual"] == "fast"
    assert all(e["matched"] is None and e["conditions"] == []
               for e in rules[3:])


def test_trace_sink_receives_the_same_events():
    jev = FakeJev(dict(FULL_ANSWERS))
    snapshot = SimpleNamespace(
        policy=policy_from_dict(policy_dict()),
        jev=JevSettings("http://jev.test", "d1-2B"),
        time_periods=[], jev_input_tokens=1024,
    )
    sink = []
    outcome = asyncio.run(PolicyEvaluator(jev).evaluate(
        snapshot, RoutingRequest("测试请求"), now=NOW, trace_sink=sink,
    ))
    assert list(outcome.trace) == sink
    assert all(event is twin for event, twin in zip(outcome.trace, sink))


def test_answers_are_read_only_and_matched_rule_is_the_hit_rule():
    outcome, _ = run_policy(policy_dict(), answers=dict(FULL_ANSWERS))
    with pytest.raises(TypeError):
        outcome.answers["route"] = None
    assert outcome.matched_rule.name == "高风险"
    assert outcome.matched_rule.target == "safe"
    assert outcome.fallback_reason is None
    assert outcome.failure_reason is None


def test_jev_error_trace_records_failure_and_fallback_final():
    outcome, _ = run_policy(policy_dict(), error=JevError("bad risk answer"))
    assert [e["event"] for e in outcome.trace] == ["time", "window", "jev", "final"]
    failed_event = outcome.trace[2]
    assert failed_event["questions"] == ["route", "risk", "severity"]
    assert failed_event["failed"] is True
    assert failed_event["error"] == "bad risk answer"
    assert failed_event["duration_ms"] == outcome.jev_duration_ms
    assert failed_event["duration_ms"] >= 0.0
    assert outcome.trace[3] == {
        "event": "final", "kind": "fallback", "target_id": "safe", "reason": "jev_error",
    }
    assert outcome.matched_rule is None
    assert outcome.matched_rule_index is None
    assert outcome.jev_questions == ("route", "risk", "severity")


def test_empty_rules_fall_back_without_any_jev_call():
    outcome, jev = run_policy(dict(policy_dict(), rules=[]))
    assert jev.calls == []
    assert outcome.target.id == "safe"
    assert outcome.fallback_reason == "no_rule_matched"
    assert outcome.answers == {}
    assert outcome.jev_duration_ms is None
    assert outcome.jev_questions == ()
    assert [e["event"] for e in outcome.trace] == ["time", "window", "final"]
    assert outcome.trace[2] == {
        "event": "final", "kind": "fallback", "target_id": "safe",
        "reason": "no_rule_matched",
    }
    assert outcome.matched_rule is None and outcome.matched_rule_index is None


def test_static_rules_without_questions_never_call_jev():
    raw = dict(
        policy_dict(), questions={},
        rules=[{"name": "抢修", "when": {"text_contains_any": ["抢修"]}, "target": "strong"}],
    )
    outcome, jev = run_policy(raw, text="95598 抢修工单如何处理")
    assert jev.calls == []
    assert outcome.target.id == "strong"
    assert outcome.matched_rule_index == 0
    assert outcome.fallback_reason is None
    assert outcome.answers == {}
    assert [e["event"] for e in outcome.trace] == ["time", "window", "rule", "final"]


def test_static_rules_without_match_fall_back_no_rule_matched():
    raw = dict(
        policy_dict(), questions={},
        rules=[{"name": "抢修", "when": {"text_contains_any": ["抢修"]}, "target": "strong"}],
    )
    outcome, jev = run_policy(raw, text="普通问候")
    assert jev.calls == []
    assert outcome.target.id == "safe"
    assert outcome.fallback_reason == "no_rule_matched"


def test_no_active_policy_raises_before_any_work():
    snapshot = SimpleNamespace(
        policy=None, jev=JevSettings("http://jev.test", "d1-2B"),
        time_periods=[], jev_input_tokens=1024,
    )
    with pytest.raises(NoActivePolicyError):
        asyncio.run(PolicyEvaluator(FakeJev()).evaluate(
            snapshot, RoutingRequest("请求"), now=NOW,
        ))


def test_missing_jev_configuration_is_a_jev_error_fallback():
    jev = FakeJev()
    snapshot = SimpleNamespace(
        policy=policy_from_dict(policy_dict()), jev=None,
        time_periods=[], jev_input_tokens=1024,
    )
    outcome = asyncio.run(PolicyEvaluator(jev).evaluate(
        snapshot, RoutingRequest("测试请求"), now=NOW,
    ))
    assert jev.calls == []
    assert outcome.fallback_reason == "jev_error"
    assert outcome.failure_reason == "JEV is not configured"
    assert outcome.answers == {}
    assert outcome.target.id == "safe"
