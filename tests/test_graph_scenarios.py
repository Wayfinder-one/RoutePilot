"""五场景矩阵：通过 tests/fixtures/graph_power.json + graph_scenarios.json 驱动引擎。

期望矩阵（与 mock.js / spec 附录 A 一致）：power / light / deep / deep / light。
"""
from __future__ import annotations

import pytest

from router.jev.client import NoulAnswer
from tests.test_graph_engine import (
    SCENARIO_BY_ID,
    Candidate,
    WaveTrace,
    run_scenario,
)


_DISPLAY_FIELDS = {"phase", "label", "cell", "summary", "facts", "tone"}


def _machine_event(event):
    return {key: value for key, value in event.items() if key not in _DISPLAY_FIELDS}


@pytest.mark.parametrize("scenario_id", ["s1", "s2", "s3", "s4", "s5"])
def test_scenario_matrix_matches_fixture_expectations(scenario_id):
    scenario = SCENARIO_BY_ID[scenario_id]
    expected = scenario["expected"]
    decision, jev, _balancer, _affinity = run_scenario(scenario_id)

    assert decision.target.id == expected["final"]
    assert len(decision.candidates) == expected["candidates"]
    assert len(decision.waves) == expected["waves"]
    # decisionCalls = 每波一次 choose_many。
    assert len(jev.calls) == expected["waves"]
    if expected["status"] == "error":
        assert decision.selection_reason == "error_fallback"
        assert decision.fallback_reason == "error_fallback"
    else:
        assert decision.selection_reason == "target"
        assert decision.fallback_reason is None
    assert decision.trace[-1]["event"] == "scheduled"
    # Task 2 起引擎在 final 与 scheduled 之间补发 schedule 事件；本矩阵无 session_id，
    # sticky 读被跳过，尾序恒为 final → schedule → scheduled。
    assert [event["event"] for event in decision.trace[-3:]] == ["final", "schedule", "scheduled"]


def test_five_scenario_final_target_matrix_is_power_light_deep_deep_light():
    finals = [run_scenario(scenario_id)[0].target.id for scenario_id in ("s1", "s2", "s3", "s4", "s5")]
    assert finals == ["power-target", "light-target", "deep-target", "deep-target", "light-target"]


def test_s1_two_slots_feed_and_power_beats_selected_strong():
    decision, jev, balancer, _affinity = run_scenario("s1")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert decision.waves == (WaveTrace(1, ("capability", "domain"), 12.0, False, None),)
    assert decision.candidates == (
        Candidate("power-target", 5, "and-power", "in_1", 1),
        Candidate("strong-target", 3, "capability", "in_1", 1),
    )
    assert decision.target.id == "power-target"
    assert decision.model == "qwen3.8-flash-sft"
    assert decision.reasoning == "high"
    assert decision.policy_path == "power-target"
    assert set(decision.answers) == {"capability", "domain"}
    assert balancer.calls[-1]["models"] == ["qwen3.8-flash-sft"]
    assert balancer.calls[-1]["preferred"] is None
    assert decision.sticky_hit is False
    final_event = next(event for event in decision.trace if event["event"] == "final")
    assert final_event["priority"] == 5
    assert [candidate["target_id"] for candidate in final_event["candidates"]] == ["power-target", "strong-target"]


def test_s2_selected_easy_directs_to_light_and_untouched_clarify_stays_unknown():
    decision, jev, balancer, _affinity = run_scenario("s2")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert decision.candidates == (Candidate("light-target", 1, "capability", "in_1", 1),)
    assert decision.target.id == "light-target"
    assert decision.model == "qwen3-8b"
    assert decision.reasoning == "low"
    assert len(decision.waves) == 1
    assert "clarify" not in decision.answers
    assert balancer.calls[-1]["models"] == ["qwen3-8b"]


def test_s3_second_wave_only_asks_clarify_then_deep_target_wins():
    decision, jev, _balancer, _affinity = run_scenario("s3")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"], ["clarify"]]
    assert decision.waves == (
        WaveTrace(1, ("capability", "domain"), 12.0, False, None),
        WaveTrace(2, ("clarify",), 12.0, False, None),
    )
    assert decision.candidates == (
        Candidate("deep-target", 4, "clarify", "in_1", 2),
        Candidate("standard-target", 2, "capability", "in_1", 1),
    )
    assert decision.target.id == "deep-target"
    assert decision.model == "deepseek-v3"
    assert decision.reasoning == "high"
    assert isinstance(decision.answers["clarify"], NoulAnswer)
    assert decision.answers["clarify"].yes == 0.74
    assert _machine_event(decision.trace[-1]) == {
        "event": "scheduled", "model": "deepseek-v3", "reason": "target", "dry_run": False,
    }


def test_s4_not_or_follow_up_then_deep_target_wins():
    decision, jev, _balancer, _affinity = run_scenario("s4")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"], ["clarify"]]
    assert len(decision.waves) == 2
    assert decision.candidates == (
        Candidate("deep-target", 4, "clarify", "in_1", 2),
        Candidate("light-target", 1, "capability", "in_1", 1),
    )
    assert decision.target.id == "deep-target"
    assert decision.model == "deepseek-v3"
    clarify_activations = [
        event for event in decision.trace
        if event["event"] == "activate" and event["node_id"] == "clarify"
    ]
    assert [_machine_event(event) for event in clarify_activations] == [
        {"event": "activate", "wave": 1, "node_id": "clarify", "via_port": "in_1"}
    ]


def test_s5_failed_wave_voids_everything_and_uses_error_fallback():
    scenario = SCENARIO_BY_ID["s5"]
    decision, jev, _balancer, _affinity = run_scenario("s5")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert decision.candidates == ()
    assert decision.target.id == "light-target"
    assert decision.model == "qwen3-8b"
    assert decision.selection_reason == "error_fallback"
    assert decision.fallback_reason == "error_fallback"
    assert decision.answers == {}
    assert len(decision.waves) == 1
    failed_wave = decision.waves[0]
    assert failed_wave.failed is True
    assert failed_wave.questions == ("capability", "domain")
    assert failed_wave.error == scenario["fail"]["message"]

    wave_event = next(event for event in decision.trace if event["event"] == "wave")
    assert wave_event["failed"] is True
    assert wave_event["error"] == scenario["fail"]["message"]
    assert wave_event["questions"] == ["capability", "domain"]
    fallback_event = next(event for event in decision.trace if event["event"] == "fallback")
    assert _machine_event(fallback_event) == {
        "event": "fallback",
        "target_id": "light-target",
        "reason": scenario["fail"]["message"],
    }
    final_event = next(event for event in decision.trace if event["event"] == "final")
    assert final_event["kind"] == "error_fallback"
    assert final_event["target_id"] == "light-target"
    assert final_event["priority"] == 1
    assert final_event["candidates"] == []
    # 失败波不回填任何问题端口，也不产生 question 事件。
    assert not [event for event in decision.trace if event["event"] == "question"]
    assert _machine_event(decision.trace[-1]) == {
        "event": "scheduled", "model": "qwen3-8b", "reason": "error_fallback", "dry_run": False,
    }
