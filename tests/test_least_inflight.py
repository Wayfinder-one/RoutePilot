import pytest

from router.routing.scheduler import LoadBalancer
from router.routing.types import NoAvailableBackend


class Breaker:
    def __init__(self, open_models=()):
        self.open_models = set(open_models)

    def is_open(self, model):
        return model in self.open_models


def test_least_inflight_uses_counts_and_stable_tie_order():
    balancer = LoadBalancer()
    first = balancer.acquire(["a", "b"])
    second = balancer.acquire(["a", "b"])
    assert {first.model, second.model} == {"a", "b"}
    first.release()
    third = balancer.acquire(["a", "b"])
    assert third.model == "a"
    third.release()
    second.release()
    fourth = balancer.acquire(["a", "b"])
    assert fourth.model == "a"
    fourth.release()


def test_release_is_idempotent_and_dry_run_never_changes_counts():
    balancer = LoadBalancer()
    lease = balancer.acquire(["a"])
    lease.release()
    lease.release()
    assert balancer.inflight("a") == 0
    preview = balancer.acquire(["a", "b"], dry_run=True)
    preview.release()
    assert balancer.inflight("a") == 0
    assert balancer.inflight("b") == 0


def test_circuit_open_models_are_filtered():
    balancer = LoadBalancer(Breaker({"a"}))
    lease = balancer.acquire(["a", "b"])
    assert lease.model == "b"
    lease.release()
    assert balancer.available(["a", "b"]) == ["b"]


def test_all_unavailable_or_empty_targets_fail_closed():
    with pytest.raises(NoAvailableBackend):
        LoadBalancer().acquire([])
    with pytest.raises(NoAvailableBackend):
        LoadBalancer(Breaker({"a", "b"})).acquire(["a", "b"])


def test_preferred_model_must_still_be_available():
    balancer = LoadBalancer(Breaker({"a"}))
    with pytest.raises(NoAvailableBackend):
        balancer.acquire(["a", "b"], preferred="a")
    lease = balancer.acquire(["a", "b"], preferred="b")
    assert lease.model == "b"
    lease.release()
