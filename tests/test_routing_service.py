"""RoutingService / ModelScheduler：真实组合根上的粘性、熔断、dry-run 与 inflight。

图语义断言在 test_graph_evaluator.py；本文件覆盖 final → sticky → acquire → put
的调度次序、会话粘性与调度事件契约，以及 preferred 在并发下失效后的降级。
"""
from __future__ import annotations

import asyncio

import pytest

from router.routing.graph.evaluator import GraphEvaluator
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import build_snapshot
from router.routing.types import NoAvailableBackend, RoutingRequest
from tests.test_graph_evaluator import (
    FakeJev,
    MODELS,
    NOW,
    RecordingAffinity,
    RecordingBalancer,
    custom_tree,
    edge,
    make_snapshot,
    node,
    power_tree,
    run_scenario,
)


class _OpenBreaker:
    def __init__(self, *models):
        self.open_models = set(models)

    def is_open(self, model):
        return model in self.open_models


def test_route_composes_evaluator_then_scheduler_and_collects_shared_trace():
    decision, jev, balancer, _affinity = run_scenario("s1")

    assert [call["keys"] for call in jev.calls] == [["capability", "domain"]]
    assert decision.target.id == "power-target"
    assert decision.model == "qwen3.8-flash-sft"
    assert decision.reasoning == "high"
    assert decision.policy_path == "power-target"
    assert decision.sticky_hit is False
    assert decision.selection_reason == "target"
    assert [event["event"] for event in decision.trace[-3:]] == ["final", "schedule", "scheduled"]
    # RoutingService owns the shared list: evaluator events survive even without a caller sink.
    assert decision.trace[0]["event"] == "time"
    assert balancer.calls[-1]["models"] == ["qwen3.8-flash-sft"]
    assert balancer.calls[-1]["preferred"] is None


def test_schedule_trace_uses_actual_selection_and_preserves_event_order():
    decision, _jev, _balancer, _affinity = run_scenario("s1")
    schedule = next(event for event in decision.trace if event["event"] == "schedule")

    assert schedule == {
        "event": "schedule",
        "target_id": "power-target",
        "strategy": "least_inflight",
        "configured_models": ["qwen3.8-flash-sft"],
        "registered_models": ["qwen3.8-flash-sft"],
        "available_models": ["qwen3.8-flash-sft"],
        "unavailable": {},
        "inflight": {"qwen3.8-flash-sft": 0},
        "sticky": {"status": "disabled", "preferred_model": None},
    }


def test_schedule_trace_marks_no_session_and_sticky_miss():
    tree = power_tree(sticky={"enabled": True, "ttl_seconds": 60})
    no_session, *_rest = run_scenario("s1", tree=tree)
    miss, *_rest = run_scenario("s1", tree=tree, session_id="new-session")

    assert next(e for e in no_session.trace if e["event"] == "schedule")["sticky"] == {
        "status": "no_session", "preferred_model": None,
    }
    assert next(e for e in miss.trace if e["event"] == "schedule")["sticky"] == {
        "status": "miss", "preferred_model": None,
    }


def test_second_request_executes_questions_then_applies_sticky():
    tree = power_tree(sticky={"enabled": True, "ttl_seconds": 60})
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    first, _jev1, _balancer1, _affinity1 = run_scenario(
        "s1", tree=tree, affinity=affinity, balancer=balancer, session_id="session-1",
    )
    assert first.selection_reason == "target"
    assert first.sticky_hit is False
    assert affinity.puts and affinity.puts[-1]["policy_path"] == "power-target"
    assert affinity.puts[-1]["target_id"] == "power-target"
    first.release()

    second, jev2, _balancer2, _affinity2 = run_scenario(
        "s1", tree=tree, affinity=affinity, balancer=balancer, session_id="session-1",
    )
    # sticky 不跳过 JEV：第二次仍然执行了被激活的两个问题。
    assert len(jev2.calls) == 1
    assert [event["node_id"] for event in second.trace if event["event"] == "question"] == ["capability", "domain"]
    assert second.sticky_hit is True
    assert second.selection_reason == "sticky"
    assert second.model == first.model == "qwen3.8-flash-sft"
    assert second.policy_path == "power-target"
    assert balancer.calls[-1]["preferred"] == "qwen3.8-flash-sft"
    assert affinity.gets[-1]["touch"] is True
    assert affinity.gets[-1]["policy_path"] == "power-target"
    assert affinity.gets[-1]["allowed_models"] == ["qwen3.8-flash-sft"]
    sticky_events = [event for event in second.trace if event["event"] == "sticky_hit"]
    assert sticky_events == [{"event": "sticky_hit", "model": "qwen3.8-flash-sft"}]
    assert [event["event"] for event in second.trace[-4:]] == ["final", "schedule", "sticky_hit", "scheduled"]
    second.release()


def test_error_fallback_sticky_keeps_fallback_reason():
    tree = power_tree(sticky={"enabled": True, "ttl_seconds": 60})
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    first, _jev, _balancer, _affinity = run_scenario(
        "s5", tree=tree, affinity=affinity, balancer=balancer, session_id="fallback-session",
    )
    assert first.selection_reason == "error_fallback"
    assert first.fallback_reason == "error_fallback"
    assert first.target.id == "light-target"
    first.release()

    second, _jev2, _balancer2, _affinity2 = run_scenario(
        "s5", tree=tree, affinity=affinity, balancer=balancer, session_id="fallback-session",
    )
    assert second.selection_reason == "sticky"
    assert second.sticky_hit is True
    assert second.fallback_reason == "error_fallback"
    assert second.target.id == "light-target"
    assert second.model == "qwen3-8b"
    second.release()


def test_no_candidate_fallback_sticky_keeps_cause():
    nodes = [
        node("start", "start"),
        node("t-safe", "target", priority=1, models=[{"model": "deepseek-v3"}]),
    ]
    tree_sticky = {"enabled": True, "ttl_seconds": 60}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    first, jev, _balancer, _affinity = run_scenario(
        "s1", tree=custom_tree(nodes, [], error_fallback="t-safe", sticky=tree_sticky),
        session_id="otherwise-session", affinity=affinity, balancer=balancer,
    )
    assert jev.calls == []
    assert first.selection_reason == "error_fallback"
    assert first.fallback_reason == "error_fallback"
    assert first.candidates == ()
    final_event = next(event for event in first.trace if event["event"] == "final")
    assert final_event["kind"] == "otherwise"
    assert final_event["candidates"] == []
    first.release()

    second, _jev2, _balancer2, _affinity2 = run_scenario(
        "s1", tree=custom_tree(nodes, [], error_fallback="t-safe", sticky=tree_sticky),
        session_id="otherwise-session", affinity=affinity, balancer=balancer,
    )
    assert second.selection_reason == "sticky"
    assert second.sticky_hit is True
    assert second.fallback_reason == "error_fallback"
    assert second.model == "deepseek-v3"
    second.release()


def test_dry_run_writes_nothing_and_trace_matches_decision():
    tree = power_tree(sticky={"enabled": True, "ttl_seconds": 60})
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    decision, jev, _balancer, _affinity = run_scenario(
        "s1", tree=tree, dry_run=True, session_id="dry-session",
        affinity=affinity, balancer=balancer,
    )
    assert len(jev.calls) == 1
    assert affinity.puts == []
    assert affinity.gets and affinity.gets[-1]["touch"] is False
    assert balancer.calls[-1]["dry_run"] is True
    assert all(balancer.inflight(model) == 0 for model in MODELS)
    scheduled = [event for event in decision.trace if event["event"] == "scheduled"]
    assert scheduled[-1]["dry_run"] is True

    final_event = next(event for event in decision.trace if event["event"] == "final")
    assert final_event["candidates"] == [
        {
            "target_id": candidate.target_id,
            "priority": candidate.priority,
            "via_node": candidate.via_node,
            "via_port": candidate.via_port,
            "wave": candidate.wave,
        }
        for candidate in decision.candidates
    ]
    wave_events = [event for event in decision.trace if event["event"] == "wave"]
    assert [
        (event["wave"], tuple(event["questions"]), event["failed"], event["error"])
        for event in wave_events
    ] == [
        (wave.wave, wave.questions, wave.failed, wave.error)
        for wave in decision.waves
    ]
    assert [(wave.wave, wave.questions) for wave in decision.waves] == [(1, ("capability", "domain"))]
    assert {(event["wave"], event["node_id"], event["via_port"])
            for event in decision.trace if event["event"] == "activate"} == {
        (0, "capability", "in_1"),
        (0, "domain", "in_1"),
        (1, "power-target", "in_1"),
        (1, "strong-target", "in_1"),
    }
    decision.release()
    decision.release()
    assert all(balancer.inflight(model) == 0 for model in MODELS)


def test_preferred_target_circuit_open_does_not_fall_to_next_priority():
    balancer = RecordingBalancer(_OpenBreaker("qwen3.8-flash-sft"))
    with pytest.raises(NoAvailableBackend):
        run_scenario("s1", balancer=balancer)
    # 只在最终 target 内调度；没有任何 acquire 尝试，更不会改选 strong-target(p3)。
    assert balancer.calls == []


def test_fallback_model_without_backend_fails_closed():
    nodes = [
        node("start", "start"),
        node("t-safe", "target", priority=1, models=[{"model": "unregistered-model"}]),
    ]
    balancer = RecordingBalancer()
    with pytest.raises(NoAvailableBackend):
        run_scenario(
            "s1", tree=custom_tree(nodes, [], error_fallback="t-safe"), balancer=balancer,
        )
    assert balancer.calls == []


def test_no_available_backend_keeps_schedule_trace_without_acquire_or_affinity_read():
    trace = []
    balancer = RecordingBalancer(_OpenBreaker("qwen3.8-flash-sft"))
    affinity = RecordingAffinity()
    tree = power_tree(sticky={"enabled": True, "ttl_seconds": 60})

    with pytest.raises(NoAvailableBackend):
        run_scenario(
            "s1", tree=tree, session_id="session-1", balancer=balancer,
            affinity=affinity, trace_sink=trace,
        )

    assert balancer.calls == []
    assert affinity.gets == []
    assert trace[-1]["event"] == "schedule"
    assert trace[-1]["available_models"] == []
    assert trace[-1]["unavailable"] == {"qwen3.8-flash-sft": "breaker_open"}
    assert trace[-1]["sticky"] == {"status": "not_checked", "preferred_model": None}


def test_sticky_shared_model_keeps_original_target_reasoning():
    nodes = [
        node("start", "start"),
        node("q", "question", question_type="choice", instructions="选择能力",
             options=[
                 {"key": "easy", "label": "EASY", "capability": "easy capability"},
                 {"key": "strong", "label": "STRONG", "capability": "strong capability"},
             ]),
        node("t-low", "target", priority=1,
             models=[{"model": "qwen3-8b", "reasoning": "low"}]),
        node("t-high", "target", priority=5,
             models=[{"model": "qwen3-8b", "reasoning": "high"}]),
    ]
    edges = [
        edge("e-start-q", "start", "out", "q", "in_1"),
        edge("e-q-low", "q", "selected.easy", "t-low", "in_1"),
        edge("e-q-high", "q", "selected.strong", "t-high", "in_1"),
    ]
    scenario = {"answers": {"q": {"probs": {"easy": 0.1, "strong": 0.9}}}}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()
    tree = custom_tree(nodes, edges, error_fallback="t-low",
                       sticky={"enabled": True, "ttl_seconds": 60})

    first, _jev1, _balancer1, _affinity1 = run_scenario(
        "s1", tree=tree, scenario=scenario, session_id="shared-model",
        affinity=affinity, balancer=balancer, text="请求", now=NOW,
    )
    assert first.target.id == "t-high"
    assert first.model == "qwen3-8b"
    assert first.reasoning == "high"
    assert affinity.puts[-1]["target_id"] == "t-high"
    first.release()

    second, _jev2, _balancer2, _affinity2 = run_scenario(
        "s1", tree=tree, scenario=scenario, session_id="shared-model",
        affinity=affinity, balancer=balancer, text="请求", now=NOW,
    )
    assert second.target.id == "t-high"
    assert second.model == "qwen3-8b"
    assert second.reasoning == "high"
    assert second.sticky_hit is True
    assert second.selection_reason == "sticky"
    assert balancer.calls[-1]["preferred"] == "qwen3-8b"
    second.release()


def test_release_is_idempotent_and_lease_is_the_only_release_point():
    decision, _jev, balancer, _affinity = run_scenario("s1")
    assert not hasattr(decision, "_released")
    assert balancer.inflight(decision.model) == 1

    decision.release()
    decision.release()
    assert balancer.inflight(decision.model) == 0


class DeadPreferredBalancer(RecordingBalancer):
    """第一次 preferred 获取时模拟该后端在并发下消失。"""

    def __init__(self, dead_model):
        super().__init__()
        self.dead_model = dead_model
        self.rejected = False

    def acquire(self, models, *, preferred=None, dry_run=False):
        self.calls.append({"models": list(models), "preferred": preferred, "dry_run": dry_run})
        if preferred == self.dead_model and not self.rejected:
            self.rejected = True
            selection = self.selection(models)
            selection["available_models"] = [
                model for model in selection["available_models"] if model != self.dead_model
            ]
            selection["unavailable"] = {**selection["unavailable"], self.dead_model: "breaker_open"}
            raise NoAvailableBackend("preferred backend went away", selection=selection)
        return LoadBalancer.acquire(self, models, preferred=preferred, dry_run=dry_run)


def test_dead_sticky_backend_is_rescheduled_by_least_inflight():
    """回归：sticky/preferred 在并发下失效必须改用其他可用后端，而不是 503。"""
    nodes = [
        node("start", "start"),
        node("t", "target", priority=1, models=[
            {"model": "deepseek-v3", "reasoning": None},
            {"model": "qwen3-8b", "reasoning": None},
        ]),
    ]
    tree = custom_tree(
        nodes, [edge("e-start-t", "start", "out", "t", "in_1")],
        error_fallback="t", sticky={"enabled": True, "ttl_seconds": 60},
    )
    snapshot = make_snapshot(tree, periods=[])
    affinity = RecordingAffinity()
    # 旧请求留下的粘性指向一个即将失效的后端。
    affinity.put(
        "dead-session", "qwen3-8b", target_id="t",
        tree_revision=snapshot.tree_revision, policy_path="t", ttl_seconds=60,
    )
    balancer = DeadPreferredBalancer("qwen3-8b")
    service = RoutingService(
        GraphEvaluator(FakeJev({"answers": {}})),
        ModelScheduler(affinity, balancer),
    )
    trace: list[dict] = []

    decision = asyncio.run(service.route(
        snapshot, RoutingRequest("", session_id="dead-session"), now=NOW,
        trace_sink=trace,
    ))

    assert decision.model == "deepseek-v3"
    assert decision.sticky_hit is False
    assert decision.selection_reason == "target"
    assert [call["preferred"] for call in balancer.calls] == ["qwen3-8b", None]
    assert not [event for event in trace if event["event"] == "sticky_hit"]
    schedule = next(event for event in trace if event["event"] == "schedule")
    assert schedule["sticky"] == {"status": "unavailable", "preferred_model": None}
    assert schedule["available_models"] == ["deepseek-v3", "qwen3-8b"]
    assert affinity.puts[-1]["model"] == "deepseek-v3"
    decision.release()
    assert balancer.inflight("deepseek-v3") == 0
