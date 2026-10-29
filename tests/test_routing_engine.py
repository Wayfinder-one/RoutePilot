import asyncio
from datetime import datetime

import pytest

from router.api.upstream import Backend
from router.jev.client import JevAnswer, JevError
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.routing.conditions import TimePeriod
from router.routing.engine import RoutingEngine
from router.routing.model import tree_from_dict
from router.routing.snapshot import build_snapshot
from router.routing.types import NoActiveTreeError, RoutingRequest
from tests.routing_fixtures import jev_dict, target_dict, tree_dict


CAPABILITIES = {"fast": "简单请求", "strong-a": "复杂请求", "strong-b": "复杂请求", "standard": "通用请求"}
JEV_SETTINGS = JevSettings("http://jev.test", "NeoHorse-Jev-4B", "", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE)


class Lease:
    def __init__(self, model):
        self.model = model
        self.releases = 0

    def release(self):
        self.releases += 1


class Balancer:
    def __init__(self):
        self.calls = []

    def acquire(self, models, *, preferred=None, dry_run=False):
        self.calls.append({"models": list(models), "preferred": preferred, "dry_run": dry_run})
        return Lease(preferred or models[0])


class Affinity:
    def __init__(self, sticky=None):
        self.sticky = sticky
        self.gets = []
        self.puts = []

    def get(self, session_id, *, tree_revision, policy_path, allowed_models):
        self.gets.append({"session_id": session_id, "tree_revision": tree_revision, "policy_path": policy_path, "allowed_models": list(allowed_models)})
        return self.sticky

    def put(self, session_id, model, *, tree_revision, policy_path, ttl_seconds):
        self.puts.append({"session_id": session_id, "model": model, "tree_revision": tree_revision, "policy_path": policy_path, "ttl_seconds": ttl_seconds})


class Jev:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    async def choose(self, settings, text, candidates, *, instructions=None):
        self.calls.append({"text": text, "candidates": candidates, "instructions": instructions})
        if self.error:
            raise self.error
        return self.answer


def answer(fast=0.2, strong=0.8):
    return JevAnswer({"fast": fast, "strong": strong}, ["strong", "fast"], strong, strong - fast, {"input_tokens": 20}, 12.0)


def snapshot(raw=None, *, jev=JEV_SETTINGS):
    backends = {model: Backend(url=f"http://{model}.test", headers={}, upstream_name=model) for model in CAPABILITIES}
    return build_snapshot(
        tree=tree_from_dict(raw or tree_dict()),
        backends=backends,
        time_periods=[TimePeriod("夜间", (0, 1, 2, 3, 4, 5, 6), "18:00", "09:00")],
        jev=jev,
        capabilities=CAPABILITIES,
    )


def route(engine, snap, *, session="s1", dry_run=False, now=None):
    return asyncio.run(engine.route(snap, RoutingRequest("请求", session_id=session), dry_run=dry_run, now=now))


def test_direct_target_skips_jev_and_releases_lease():
    jev = Jev(answer())
    balancer = Balancer()
    engine = RoutingEngine(jev, Affinity(), balancer)
    decision = route(engine, snapshot(tree_dict(root=target_dict("direct", ("standard",)))))
    assert decision.model == "standard"
    assert decision.selection_reason == "direct"
    assert jev.calls == []
    lease = decision.lease
    decision.release()
    decision.release()
    assert lease.releases == 1


def test_jev_selects_target_and_records_probability_evidence():
    jev = Jev(answer())
    affinity = Affinity()
    engine = RoutingEngine(jev, affinity, Balancer())
    decision = route(engine, snapshot())
    assert decision.model == "strong-a"
    assert decision.target.id == "strong-target"
    assert decision.selection_reason == "jev"
    assert decision.jev_answer.selected_key == "strong"
    assert decision.trace[-1]["event"] == "scheduled"
    assert len(jev.calls) == 1
    assert affinity.puts[0]["model"] == "strong-a"


def test_low_probability_and_jev_failure_use_fallback_without_second_choice():
    raw = tree_dict()
    raw["root"]["min_prob"] = 0.9
    jev = Jev(answer())
    decision = route(RoutingEngine(jev, Affinity(), Balancer()), snapshot(raw))
    assert decision.model == "standard"
    assert decision.selection_reason == "fallback"
    assert decision.fallback_reason == "jev_low_probability"
    assert len(jev.calls) == 1

    jev = Jev(error=JevError("down"))
    decision = route(RoutingEngine(jev, Affinity(), Balancer()), snapshot())
    assert decision.model == "standard"
    assert decision.fallback_reason == "jev_error"
    assert len(jev.calls) == 1


def test_valid_sticky_model_skips_jev_and_selects_its_target():
    jev = Jev(answer())
    affinity = Affinity("strong-b")
    decision = route(RoutingEngine(jev, affinity, Balancer()), snapshot())
    assert decision.model == "strong-b"
    assert decision.target.id == "strong-target"
    assert decision.sticky_hit is True
    assert decision.selection_reason == "sticky"
    assert jev.calls == []
    assert affinity.gets[0]["policy_path"] == "choose"


def test_circuit_open_sticky_model_is_recomputed_instead_of_reused():
    from router.routing.scheduler import LoadBalancer

    class Breaker:
        def is_open(self, model):
            return model == "strong-b"

    jev = Jev(answer())
    affinity = Affinity("strong-b")
    decision = route(RoutingEngine(jev, affinity, LoadBalancer(Breaker())), snapshot())
    assert decision.model == "strong-a"
    assert decision.sticky_hit is False
    assert decision.selection_reason == "jev"
    assert len(jev.calls) == 1


def test_time_branch_runs_before_sticky_and_jev():
    root = {
        "id": "root", "type": "branch",
        "children": [{**jev_dict("night-choice"), "when": {"periods": ["夜间"]}}],
        "otherwise": target_dict("day", ("standard",)),
    }
    snap = snapshot(tree_dict(root=root))
    jev = Jev(answer())
    engine = RoutingEngine(jev, Affinity("strong-a"), Balancer())
    day = route(engine, snap, now=datetime(2026, 10, 29, 12, 0))
    assert day.model == "standard"
    assert day.sticky_hit is False
    night = route(engine, snap, now=datetime(2026, 10, 29, 23, 0))
    assert night.model == "strong-a"
    assert night.sticky_hit is True
    assert night.policy_path == "root/night-choice"
    assert jev.calls == []


def test_dry_run_does_not_write_affinity_or_acquire_inflight():
    affinity = Affinity()
    balancer = Balancer()
    decision = route(RoutingEngine(Jev(answer()), affinity, balancer), snapshot(), dry_run=True)
    assert decision.model == "strong-a"
    assert affinity.puts == []
    assert balancer.calls[0]["dry_run"] is True
    decision.release()


def test_no_active_tree_fails_before_jev():
    snap = build_snapshot(tree=None, backends={}, time_periods=[], jev=None, capabilities={})
    jev = Jev(answer())
    with pytest.raises(NoActiveTreeError):
        route(RoutingEngine(jev, Affinity(), Balancer()), snap)
    assert jev.calls == []
