"""五场景矩阵：通过 tests/fixtures/graph_power.json + graph_scenarios.json 驱动求值层。

期望矩阵（与 mock.js / spec 附录 A 一致）：power / light / deep / deep / light。
本文件只断言图语义（目标/候选/波次/答案/机器 trace）；模型调度断言在
test_routing_service.py。
"""
from __future__ import annotations

import pytest

from router.jev.client import NoulAnswer
from tests.test_graph_evaluator import (
    SCENARIO_BY_ID,
    Candidate,
    WaveTrace,
    run_evaluate,
)


@pytest.mark.parametrize("scenario_id", ["s1", "s2", "s3", "s4", "s5"])
def test_scenario_matrix_matches_fixture_expectations(scenario_id):
    scenario = SCENARIO_BY_ID[scenario_id]
    expected = scenario["expected"]
    outcome, jev, trace = run_evaluate(scenario_id)

    assert outcome.target.id == expected["final"]
    assert len(outcome.candidates) == expected["candidates"]
    assert len(outcome.waves) == expected["waves"]
    # decisionCalls = 每波一次 choose_many。
    assert len(jev.calls) == expected["waves"]
    if expected["status"] == "error":
        assert outcome.selection_reason == "error_fallback"
        assert outcome.fallback_reason == "error_fallback"
        assert outcome.failure_reason is not None
        expected_kind = "error_fallback"
    else:
        assert outcome.selection_reason == "target"
        assert outcome.fallback_reason is None
        expected_kind = "target" if expected["candidates"] else "otherwise"
    final_event = trace[-1]
    assert final_event["event"] == "final"
    assert final_event["kind"] == expected_kind
    assert final_event["target_id"] == expected["final"]
    assert not any(event["event"] in {"schedule", "scheduled"} for event in trace)


def test_five_scenario_final_target_matrix_is_power_light_deep_deep_light():
    finals = [run_evaluate(scenario_id)[0].target.id for scenario_id in ("s1", "s2", "s3", "s4", "s5")]
    assert finals == ["power-target", "light-target", "deep-target", "deep-target", "light-target"]


def test_s1_two_slots_feed_and_power_beats_selected_strong():
    outcome, jev, trace = run_evaluate("s1")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert outcome.waves == (WaveTrace(1, ("capability", "domain"), 12.0, False, None),)
    assert outcome.candidates == (
        Candidate("power-target", 5, "and-power", "in_1", 1),
        Candidate("strong-target", 3, "capability", "in_1", 1),
    )
    assert outcome.target.id == "power-target"
    assert set(outcome.answers) == {"capability", "domain"}
    final_event = next(event for event in trace if event["event"] == "final")
    assert final_event["priority"] == 5
    assert [candidate["target_id"] for candidate in final_event["candidates"]] == ["power-target", "strong-target"]


def test_s2_selected_easy_directs_to_light_and_untouched_clarify_stays_unknown():
    outcome, jev, _trace = run_evaluate("s2")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert outcome.candidates == (Candidate("light-target", 1, "capability", "in_1", 1),)
    assert outcome.target.id == "light-target"
    assert len(outcome.waves) == 1
    assert "clarify" not in outcome.answers


def test_s3_second_wave_only_asks_clarify_then_deep_target_wins():
    outcome, jev, trace = run_evaluate("s3")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"], ["clarify"]]
    assert outcome.waves == (
        WaveTrace(1, ("capability", "domain"), 12.0, False, None),
        WaveTrace(2, ("clarify",), 12.0, False, None),
    )
    assert outcome.candidates == (
        Candidate("deep-target", 4, "clarify", "in_1", 2),
        Candidate("standard-target", 2, "capability", "in_1", 1),
    )
    assert outcome.target.id == "deep-target"
    assert isinstance(outcome.answers["clarify"], NoulAnswer)
    assert outcome.answers["clarify"].yes == 0.74
    assert trace[-1]["event"] == "final"


def test_s4_not_or_follow_up_then_deep_target_wins():
    outcome, jev, trace = run_evaluate("s4")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"], ["clarify"]]
    assert len(outcome.waves) == 2
    assert outcome.candidates == (
        Candidate("deep-target", 4, "clarify", "in_1", 2),
        Candidate("light-target", 1, "capability", "in_1", 1),
    )
    assert outcome.target.id == "deep-target"
    clarify_activations = [
        event for event in trace
        if event["event"] == "activate" and event["node_id"] == "clarify"
    ]
    assert clarify_activations == [
        {"event": "activate", "wave": 1, "node_id": "clarify", "via_port": "in_1"}
    ]


def test_s5_failed_wave_voids_everything_and_uses_error_fallback():
    scenario = SCENARIO_BY_ID["s5"]
    outcome, jev, trace = run_evaluate("s5")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert outcome.candidates == ()
    assert outcome.target.id == "light-target"
    assert outcome.selection_reason == "error_fallback"
    assert outcome.fallback_reason == "error_fallback"
    assert outcome.answers == {}
    assert len(outcome.waves) == 1
    failed_wave = outcome.waves[0]
    assert failed_wave.failed is True
    assert failed_wave.questions == ("capability", "domain")
    assert failed_wave.error == scenario["fail"]["message"]
    assert outcome.failure_reason == scenario["fail"]["message"]

    wave_event = next(event for event in trace if event["event"] == "wave")
    assert wave_event["failed"] is True
    assert wave_event["error"] == scenario["fail"]["message"]
    assert wave_event["questions"] == ["capability", "domain"]
    fallback_event = next(event for event in trace if event["event"] == "fallback")
    assert fallback_event == {
        "event": "fallback",
        "target_id": "light-target",
        "reason": scenario["fail"]["message"],
    }
    final_event = trace[-1]
    assert final_event["kind"] == "error_fallback"
    assert final_event["target_id"] == "light-target"
    assert final_event["priority"] == 1
    assert final_event["candidates"] == []
    # 失败波不回填任何问题端口，也不产生 question 事件。
    assert not [event for event in trace if event["event"] == "question"]
