"""Unit tests for the pipeline-aware signal scope (router/pipeline/scope.py).

The scope answers one question: *which JEV questions can this routing tree ever
read?* Everything it lists must be computed (correctness) and everything it
leaves out must be unreadable by ``evaluate()`` (the latency win). These tests
pin the extraction rules, the conservative full-pack fallbacks and the
hot-edit behaviour.
"""

from router.pipeline.model import Station, TreeNode, Weight
from router.pipeline.scope import SignalScope, compute_signal_scope, full_scope
from router.signals.registry import SIGNALS, SignalSpec
from router.store.repositories import default_pipeline


def cond(nid, signal, value, kind="leaf", conf=0.5, model="qwen3-8b",
         weights=None, children=None):
    return TreeNode(id=nid, signal=signal, value=value, conf_min=conf,
                    kind=kind, action="route", model=model,
                    min_score=None, weights=weights or [],
                    children=children or [])


def entry(children):
    return Station(id="s", name="s",
                   root=TreeNode(id="r", signal=None, kind="fork",
                                 children=children))


def fallback():
    return Station(id="f", name="f",
                   root=TreeNode(id="fr", signal=None, action="route",
                                 model="qwen3-8b"))


def test_default_pipeline_scope_is_exactly_the_referenced_model_signals():
    stations = default_pipeline()
    scope = compute_signal_scope(stations)

    def collect(node, out):
        if node.signal and SIGNALS[node.signal].kind == "model":
            out.add(node.signal)
        for w in node.weights:
            if SIGNALS[w.signal].kind == "model":
                out.add(w.signal)
        for child in node.children:
            collect(child, out)

    referenced = set()
    for station in stations:
        collect(station.root, referenced)
    assert scope.trimmed and scope.reason
    assert scope.signals == frozenset(referenced)
    assert referenced == {"jailbreak", "pii", "domain", "complexity"}
    assert scope.pii_types == frozenset({"cn_id_card"})
    # keyword is a rule signal: local, deterministic, zero model work
    assert "keyword" not in scope.signals


def test_union_over_all_nodes_and_weights():
    stations = [
        entry([cond("a", "jailbreak", "jailbreak"),
               cond("b", "keyword", "医疗急症"),            # rule -> ignored
               cond("c", "domain", "health", kind="fork", children=[
                   cond("d", "complexity", "hard", weights=[
                       Weight("modality", "image", 0.5),
                       Weight("fact_check", "needs_check", 0.5)])])]),
        fallback(),
    ]
    scope = compute_signal_scope(stations)
    assert scope.signals == frozenset({"jailbreak", "domain", "complexity",
                                       "modality", "fact_check"})
    assert scope.pii_types == frozenset()


def test_pii_subtypes_are_trimmed_per_referenced_value():
    stations = [entry([cond("a", "pii", "cn_id_card"),
                       cond("b", "pii", "email_address")]), fallback()]
    scope = compute_signal_scope(stations)
    assert scope.signals == frozenset({"pii"})
    assert scope.pii_types == frozenset({"cn_id_card", "email_address"})


def test_rule_only_tree_needs_no_model_questions():
    stations = [entry([cond("a", "keyword", "医疗急症"),
                       cond("b", "context", "1"),
                       cond("c", "language", "zh")]), fallback()]
    scope = compute_signal_scope(stations)
    assert scope.trimmed
    assert scope.signals == frozenset()
    assert scope.pii_types == frozenset()


def test_unknown_signal_falls_back_to_the_full_pack():
    scope = compute_signal_scope([entry([cond("a", "telepathy", "yes")]),
                                  fallback()])
    assert scope.full and "telepathy" in scope.reason
    assert not scope.trimmed


def test_unknown_pii_subtype_falls_back_to_the_full_pack():
    scope = compute_signal_scope([entry([cond("a", "pii", "not_a_real_type")]),
                                  fallback()])
    assert scope.full and "not_a_real_type" in scope.reason


def test_unknown_multi_model_signal_falls_back_to_the_full_pack(monkeypatch):
    spec = SignalSpec("fake_multi", "假", "Fake", "safety", "model",
                      multi=True, values=(("v", "v", "v"),))
    monkeypatch.setitem(SIGNALS, "fake_multi", spec)
    scope = compute_signal_scope([entry([cond("a", "fake_multi", "v")]),
                                  fallback()])
    assert scope.full and "fake_multi" in scope.reason


def test_empty_or_missing_pipeline_falls_back_to_the_full_pack():
    for stations in (None, []):
        scope = compute_signal_scope(stations)
        assert scope.full and scope.reason


def test_full_scope_helper_is_not_trimmed():
    scope = full_scope("because")
    assert scope.full is True
    assert scope.trimmed is False
    assert scope.reason == "because"
    assert isinstance(scope, SignalScope)
