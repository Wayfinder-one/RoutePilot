import pytest

from router.routing.scheduler import LoadBalancer
from router.routing.types import NoAvailableBackend
from router.upstream.breaker import CircuitBreaker
from router.upstream.runtime_keys import runtime_model_key, tree_scope


class Breaker:
    def __init__(self, open_models=()):
        self.open_models = set(open_models)

    def is_open(self, model):
        return model in self.open_models


class FixedRng:
    """Deterministic stand-in for ``random.Random``: always picks one model."""

    def __init__(self, model):
        self.model = model
        self.candidates = []

    def choice(self, models):
        self.candidates.append(list(models))
        return self.model


def test_multi_model_pick_is_random_and_ignores_inflight_counts():
    rng = FixedRng("a")
    balancer = LoadBalancer(rng=rng)
    first = balancer.acquire(["a", "b"])
    second = balancer.acquire(["a", "b"])

    # 最少在途请求会改选 b；随机调度必须继续按随机源给出的结果选 a。
    assert (first.model, second.model) == ("a", "a")
    assert rng.candidates == [["a", "b"], ["a", "b"]]
    assert balancer.inflight("a") == 2
    assert balancer.inflight("b") == 0
    first.release()
    second.release()
    assert balancer.inflight("a") == 0


def test_release_is_idempotent_and_dry_run_never_changes_counts():
    balancer = LoadBalancer(rng=FixedRng("a"))
    lease = balancer.acquire(["a"])
    lease.release()
    lease.release()
    assert balancer.inflight("a") == 0
    preview = balancer.acquire(["a", "b"], dry_run=True)
    preview.release()
    assert balancer.inflight("a") == 0
    assert balancer.inflight("b") == 0


def test_circuit_open_models_are_filtered():
    balancer = LoadBalancer(Breaker({"a"}), rng=FixedRng("b"))
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
    balancer = LoadBalancer(Breaker({"a"}), rng=FixedRng("b"))
    with pytest.raises(NoAvailableBackend):
        balancer.acquire(["a", "b"], preferred="a")
    lease = balancer.acquire(["a", "b"], preferred="b")
    assert lease.model == "b"
    lease.release()


def test_selection_snapshot_is_taken_before_increment():
    balancer = LoadBalancer(rng=FixedRng("a"))
    held = balancer.acquire(["a", "b"])
    preview = balancer.acquire(["a", "b"], dry_run=True)

    assert held.model == "a"
    assert held.selection == {
        "available_models": ["a", "b"],
        "unavailable": {},
        "inflight": {"a": 0, "b": 0},
    }
    assert preview.selection == {
        "available_models": ["a", "b"],
        "unavailable": {},
        "inflight": {"a": 1, "b": 0},
    }
    assert balancer.inflight("a") == 1
    assert balancer.inflight("b") == 0
    preview.release()
    held.release()


def test_selection_snapshot_is_available_when_acquire_fails():
    balancer = LoadBalancer(Breaker({"a"}))
    with pytest.raises(NoAvailableBackend) as caught:
        balancer.acquire(["a", "b"], preferred="a")

    assert caught.value.selection == {
        "available_models": ["b"],
        "unavailable": {"a": "breaker_open"},
        "inflight": {"a": 0, "b": 0},
    }


def test_selection_probe_uses_the_same_lock_and_filter_rules():
    balancer = LoadBalancer(Breaker({"a"}))
    assert balancer.selection(["a", "b", "b"]) == {
        "available_models": ["b"],
        "unavailable": {"a": "breaker_open"},
        "inflight": {"a": 0, "b": 0},
    }


# ── 按图作用域隔离：同一逻辑模型在不同图里互不影响 ──────────────────────


def test_same_model_has_independent_breaker_and_inflight_per_tree():
    breaker = CircuitBreaker(threshold=1)
    balancer = LoadBalancer(breaker)
    main = tree_scope('main')
    finance = tree_scope('finance')

    first = balancer.acquire(['shared'], scope=main)
    second = balancer.acquire(['shared'], scope=finance)
    assert balancer.inflight('shared', scope=main) == 1
    assert balancer.inflight('shared', scope=finance) == 1
    first.release()
    assert balancer.inflight('shared', scope=main) == 0
    assert balancer.inflight('shared', scope=finance) == 1
    second.release()

    breaker.failure(runtime_model_key(main, 'shared'))
    assert balancer.available(['shared'], scope=main) == []
    assert balancer.available(['shared'], scope=finance) == ['shared']
    assert balancer.available(['shared']) == ['shared'], 'fixed direct must stay independent'


def test_tree_scope_rejects_an_empty_or_non_string_tree_id():
    for value in ('', None, 0):
        with pytest.raises(ValueError, match='tree_id must be nonempty'):
            tree_scope(value)


def test_runtime_model_key_keeps_the_bare_model_for_the_empty_scope():
    assert runtime_model_key('', 'shared') == 'shared'
    assert runtime_model_key(tree_scope('main'), 'shared') == 'tree:main:shared'
