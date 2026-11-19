"""RoutingService / ModelScheduler 在 Policy v4 组合根上的调度行为。

策略求值语义（JEV 单调用、首条命中、失败兜底、轨迹契约）在
test_policy_evaluator.py；本文件覆盖 evaluator → sticky → acquire → put 的
调度次序、会话粘性（粘性命中不跳过 JEV）、dry-run 纯度、无可用后端
fail-closed、preferred 失效后的 least_inflight 降级与无启用策略的错误。
"""
from __future__ import annotations

import asyncio

import pytest

from router.jev.config import JevSettings
from router.routing.affinity import SessionAffinity
from router.routing.policy import (
    PolicyResource,
    policy_from_dict,
    policy_revision_of,
)
from router.routing.policy.evaluator import PolicyEvaluator
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import build_snapshot
from router.routing.types import (
    NoActivePolicyError,
    NoAvailableBackend,
    RoutingRequest,
)
from router.upstream.breaker import CircuitBreaker
from router.upstream.catalog import Backend
from tests.policy_harness import FakeJev, choice, noul, score
from tests.routing_fixtures import policy_dict


BACKENDS = {
    model: Backend(
        url=f"http://{model}.test", headers={}, upstream_name=model,
        provider="local", reasoning={},
    )
    for model in ("fast", "strong")
}


def route_policy(*, session_id="", fallback=None):
    raw = policy_dict()
    if fallback == "no_rule_matched":
        raw["rules"] = []
    resource = PolicyResource(
        id="main", policy=policy_from_dict(raw), enabled=True,
        policy_revision=policy_revision_of(policy_from_dict(raw)),
    )
    snapshot = build_snapshot(
        resource=resource, backends=BACKENDS, time_periods=[],
        jev=JevSettings("http://jev.test", "d1-2B"), capabilities={},
    )
    answers = None if raw["rules"] == [] else {
        "route": choice("fast", 0.9), "risk": noul(0.1), "severity": score(1.0),
    }
    jev = FakeJev(answers)
    service = RoutingService(
        PolicyEvaluator(jev),
        ModelScheduler(SessionAffinity(), LoadBalancer(CircuitBreaker())),
    )
    decision = asyncio.run(service.route(snapshot, RoutingRequest("测试", session_id)))
    return decision, jev


def policy_snapshot(raw):
    """与 route_policy 同一组合根的快照工厂，供进阶场景复用。"""
    resource = PolicyResource(
        id="main", policy=policy_from_dict(raw), enabled=True,
        policy_revision=policy_revision_of(policy_from_dict(raw)),
    )
    return build_snapshot(
        resource=resource, backends=BACKENDS, time_periods=[],
        jev=JevSettings("http://jev.test", "d1-2B"), capabilities={},
    )


def full_answers():
    return {
        "route": choice("fast", 0.9), "risk": noul(0.1), "severity": score(1.0),
    }


def route_raw(raw, *, session_id="", answers=None, jev=None, affinity=None,
              balancer=None, dry_run=False, trace_sink=None):
    snapshot = policy_snapshot(raw)
    jev = FakeJev(full_answers() if answers is None else answers) if jev is None else jev
    affinity = SessionAffinity() if affinity is None else affinity
    balancer = LoadBalancer(CircuitBreaker()) if balancer is None else balancer
    service = RoutingService(
        PolicyEvaluator(jev), ModelScheduler(affinity, balancer),
    )
    decision = asyncio.run(service.route(
        snapshot, RoutingRequest("测试", session_id),
        dry_run=dry_run, trace_sink=trace_sink,
    ))
    return decision, jev, affinity, balancer


class _OpenBreaker:
    def __init__(self, *models):
        self.open_models = set(models)

    def is_open(self, model):
        return model in self.open_models


class RecordingBalancer(LoadBalancer):
    """记录 acquire 的 models/preferred/dry_run，其余调度语义复用真实实现。"""

    def __init__(self, breaker=None):
        super().__init__(breaker)
        self.calls = []

    def acquire(self, models, *, preferred=None, dry_run=False):
        self.calls.append({"models": list(models), "preferred": preferred, "dry_run": dry_run})
        return super().acquire(models, preferred=preferred, dry_run=dry_run)


class RecordingAffinity(SessionAffinity):
    """记录 get/put 的粘性参数（只接受 policy_revision）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gets = []
        self.puts = []

    def get(self, session_id, *, policy_revision, policy_path, allowed_models, touch=True):
        self.gets.append({
            "session_id": session_id,
            "policy_revision": policy_revision,
            "policy_path": policy_path,
            "allowed_models": list(allowed_models),
            "touch": touch,
        })
        return super().get(
            session_id, policy_revision=policy_revision, policy_path=policy_path,
            allowed_models=allowed_models, touch=touch,
        )

    def put(self, session_id, model, *, target_id, policy_revision, policy_path, ttl_seconds):
        self.puts.append({
            "session_id": session_id,
            "model": model,
            "target_id": target_id,
            "policy_revision": policy_revision,
            "policy_path": policy_path,
            "ttl_seconds": ttl_seconds,
        })
        return super().put(
            session_id, model, target_id=target_id, policy_revision=policy_revision,
            policy_path=policy_path, ttl_seconds=ttl_seconds,
        )


def test_snapshot_uses_policy_revision_and_layout_cannot_change_it():
    resource = PolicyResource(
        id="main", policy=policy_from_dict(policy_dict()), enabled=True,
        policy_revision="",
    )
    snapshot = build_snapshot(
        resource=resource, backends=BACKENDS, time_periods=[],
        jev=JevSettings("http://jev.test", "d1-2B"), capabilities={},
    )
    assert snapshot.policy_id == "main"
    assert snapshot.policy_revision == policy_revision_of(resource.policy)
    assert snapshot.content.find("layout") == -1
    assert snapshot.content.find("tree_revision") == -1


def test_scheduler_keeps_rule_and_fallback_cause():
    decision, _jev = route_policy(session_id="s", fallback="no_rule_matched")
    assert decision.fallback_reason == "no_rule_matched"
    assert decision.matched_rule is None
    assert decision.matched_rule_index is None
    assert not hasattr(decision, "waves")
    assert decision.target.id == "safe"


def test_rule_hit_keeps_matched_rule_and_jev_evidence():
    decision, jev = route_policy(session_id="s")
    assert decision.target.id == "fast"
    assert decision.model == "fast"
    assert decision.matched_rule == "轻量请求"
    assert decision.matched_rule_index == 2
    assert decision.fallback_reason is None
    assert decision.selection_reason == "target"
    assert decision.jev_questions == ("route", "risk", "severity")
    assert decision.jev_duration_ms == 12.0
    assert decision.answers["route"].selected_key == "fast"
    assert len(jev.calls) == 1


def test_route_composes_evaluator_then_scheduler_in_one_trace():
    decision, _jev, _affinity, _balancer = route_raw(policy_dict(), session_id="t")
    assert [event["event"] for event in decision.trace] == [
        "time", "window", "jev",
        "question", "question", "question",
        "rule", "rule", "rule",
        "final", "schedule", "scheduled",
    ]
    # RoutingService owns the shared list: evaluator events survive even
    # without a caller sink, and scheduling continues the same trace.
    assert decision.trace[0]["event"] == "time"


def test_second_request_runs_jev_then_applies_sticky():
    raw = policy_dict()
    raw["sticky"] = {"enabled": True, "ttl_seconds": 60}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    first, _jev1, _affinity, _balancer = route_raw(
        raw, session_id="session-1", affinity=affinity, balancer=balancer,
    )
    assert first.target.id == "fast"
    assert first.sticky_hit is False
    assert first.selection_reason == "target"
    assert affinity.puts[-1]["policy_path"] == "fast"
    assert affinity.puts[-1]["target_id"] == "fast"
    assert affinity.puts[-1]["policy_revision"] == policy_revision_of(policy_from_dict(raw))
    first.release()

    second, jev2, _affinity2, _balancer2 = route_raw(
        raw, session_id="session-1", affinity=affinity, balancer=balancer,
    )
    # sticky 命中不跳过 JEV：第二次仍然单次调用全部三个问题。
    assert len(jev2.calls) == 1
    assert [event["question_id"] for event in second.trace if event["event"] == "question"] == [
        "route", "risk", "severity",
    ]
    assert second.sticky_hit is True
    assert second.selection_reason == "sticky"
    assert second.model == first.model == "fast"
    assert second.matched_rule == "轻量请求"
    assert balancer.calls[-1]["preferred"] == "fast"
    assert affinity.gets[-1]["touch"] is True
    sticky_events = [event for event in second.trace if event["event"] == "sticky_hit"]
    assert sticky_events == [{"event": "sticky_hit", "model": "fast"}]
    assert [event["event"] for event in second.trace[-4:]] == [
        "final", "schedule", "sticky_hit", "scheduled",
    ]
    second.release()


def test_sticky_hit_keeps_fallback_cause_and_absent_rule():
    raw = policy_dict()
    raw["rules"] = []
    raw["sticky"] = {"enabled": True, "ttl_seconds": 60}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    first, jev1, _affinity, _balancer = route_raw(
        raw, session_id="fallback-session", affinity=affinity, balancer=balancer,
    )
    assert jev1.calls == []  # 规则为空不调用 JEV
    assert first.selection_reason == "fallback"
    assert first.fallback_reason == "no_rule_matched"
    assert first.matched_rule is None
    assert first.target.id == "safe"
    first.release()

    second, jev2, _affinity2, _balancer2 = route_raw(
        raw, session_id="fallback-session", affinity=affinity, balancer=balancer,
    )
    assert jev2.calls == []
    assert second.sticky_hit is True
    assert second.selection_reason == "sticky"
    assert second.fallback_reason == "no_rule_matched"
    assert second.matched_rule is None
    assert second.target.id == "safe"
    assert second.model == "fast"
    second.release()


def test_dry_run_writes_no_sticky_and_no_inflight():
    raw = policy_dict()
    raw["sticky"] = {"enabled": True, "ttl_seconds": 60}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    decision, jev, _affinity, _balancer = route_raw(
        raw, session_id="dry-session", affinity=affinity, balancer=balancer,
        dry_run=True,
    )
    assert len(jev.calls) == 1
    assert affinity.puts == []
    assert affinity.gets and affinity.gets[-1]["touch"] is False
    assert balancer.calls[-1]["dry_run"] is True
    assert balancer.inflight(decision.model) == 0
    scheduled = [event for event in decision.trace if event["event"] == "scheduled"]
    assert scheduled[-1]["dry_run"] is True
    decision.release()
    assert balancer.inflight(decision.model) == 0


def test_route_without_active_policy_raises_and_never_touches_scheduler_state():
    snapshot = build_snapshot(
        resource=None, backends=BACKENDS, time_periods=[],
        jev=JevSettings("http://jev.test", "d1-2B"), capabilities={},
    )
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()
    service = RoutingService(
        PolicyEvaluator(FakeJev(full_answers())),
        ModelScheduler(affinity, balancer),
    )
    with pytest.raises(NoActivePolicyError):
        asyncio.run(service.route(snapshot, RoutingRequest("测试", session_id="s")))
    assert affinity.gets == []
    assert affinity.puts == []
    assert balancer.calls == []


def test_fallback_target_without_backend_fails_closed():
    raw = policy_dict()
    raw["rules"] = []
    raw["targets"] = {"safe": {"models": [{"model": "unregistered-model"}]}}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer()

    with pytest.raises(NoAvailableBackend):
        route_raw(raw, session_id="closed-session", affinity=affinity, balancer=balancer)
    assert balancer.calls == []
    assert affinity.gets == []
    assert affinity.puts == []


def test_no_available_backend_keeps_schedule_trace_without_sticky_read():
    raw = policy_dict()
    raw["sticky"] = {"enabled": True, "ttl_seconds": 60}
    affinity = RecordingAffinity()
    balancer = RecordingBalancer(_OpenBreaker("fast"))
    trace: list[dict] = []

    with pytest.raises(NoAvailableBackend):
        route_raw(
            raw, session_id="session-1", affinity=affinity, balancer=balancer,
            trace_sink=trace,
        )

    assert balancer.calls == []
    assert affinity.gets == []
    assert trace[-1]["event"] == "schedule"
    assert trace[-1]["available_models"] == []
    assert trace[-1]["unavailable"] == {"fast": "breaker_open"}
    assert trace[-1]["sticky"] == {"status": "not_checked", "preferred_model": None}


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
    raw = policy_dict()
    raw["rules"] = []
    raw["sticky"] = {"enabled": True, "ttl_seconds": 60}
    raw["targets"] = {"safe": {"models": [{"model": "strong"}, {"model": "fast"}]}}
    snapshot = policy_snapshot(raw)
    affinity = RecordingAffinity()
    # 旧请求留下的粘性指向一个即将失效的后端。
    affinity.put(
        "dead-session", "fast", target_id="safe",
        policy_revision=snapshot.policy_revision, policy_path="safe", ttl_seconds=60,
    )
    balancer = DeadPreferredBalancer("fast")
    service = RoutingService(
        PolicyEvaluator(FakeJev(None)),
        ModelScheduler(affinity, balancer),
    )
    trace: list[dict] = []

    decision = asyncio.run(service.route(
        snapshot, RoutingRequest("测试", session_id="dead-session"), trace_sink=trace,
    ))

    assert decision.model == "strong"
    assert decision.sticky_hit is False
    assert decision.selection_reason == "fallback"
    assert decision.fallback_reason == "no_rule_matched"
    assert [call["preferred"] for call in balancer.calls] == ["fast", None]
    assert not [event for event in trace if event["event"] == "sticky_hit"]
    schedule = next(event for event in trace if event["event"] == "schedule")
    assert schedule["sticky"] == {"status": "unavailable", "preferred_model": None}
    assert schedule["available_models"] == ["strong", "fast"]
    assert affinity.puts[-1]["model"] == "strong"
    decision.release()
    assert balancer.inflight("strong") == 0


def test_release_is_idempotent_and_lease_is_the_only_release_point():
    decision, _jev, _affinity, balancer = route_raw(
        policy_dict(), session_id="release-session", balancer=RecordingBalancer(),
    )
    assert not hasattr(decision, "_released")
    assert balancer.inflight(decision.model) == 1

    decision.release()
    decision.release()
    assert balancer.inflight(decision.model) == 0
