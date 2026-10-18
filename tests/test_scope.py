"""Unit tests for the pipeline-aware signal scope (router/pipeline/scope.py).

The scope answers one question: *which JEV questions can this routing tree ever
read?*  Everything it lists must be computed (correctness) and everything it
leaves out must be unreadable by ``evaluate()`` (the latency win).  These tests
pin the extraction rules, the conservative full-pack fallbacks and the
hot-edit behaviour.
"""

from router.pipeline.model import Branch, Node, Row
from router.pipeline.scope import SignalScope, compute_signal_scope, full_scope
from router.signals.registry import SIGNALS, SignalSpec
from router.store.repositories import default_pipeline


def gate(rows: list[Row], node_id: str = "g") -> Node:
    return Node(id=node_id, type="gate",
                branches=[Branch(id=node_id + "-b", rows=rows, action="route",
                                model="qwen3-8b")])


def score(rows: list[Row], node_id: str = "s") -> Node:
    return Node(id=node_id, type="score",
                branches=[Branch(id=node_id + "-b", rows=rows, model="qwen3-8b",
                                min_score=0.5)])


def test_default_pipeline_scope_is_exactly_the_referenced_model_signals():
    nodes = default_pipeline("checkpoint")
    scope = compute_signal_scope(nodes)
    referenced = {row.signal for node in nodes for branch in node.branches
                  for row in branch.rows}
    model_refs = {key for key in referenced if SIGNALS[key].kind == "model"}
    assert scope.trimmed and scope.reason
    assert scope.signals == frozenset(model_refs)
    assert model_refs == {"jailbreak", "pii", "domain", "complexity"}
    assert scope.pii_types == frozenset({"cn_id_card"})
    # keyword is a rule signal: local, deterministic, zero model work
    assert "keyword" not in scope.signals


def test_union_over_all_nodes_and_branches():
    nodes = [
        gate([Row("jailbreak", "jailbreak", conf_min=0.35)]),
        gate([Row("keyword", "医疗急症", conf_min=0.5)]),      # rule -> ignored
        score([Row("domain", "health", weight=0.5),
               Row("complexity", "hard", weight=0.5)]),
        score([Row("modality", "image", weight=0.5),
               Row("fact_check", "needs_check", weight=0.5)]),
    ]
    scope = compute_signal_scope(nodes)
    assert scope.signals == frozenset({"jailbreak", "domain", "complexity",
                                       "modality", "fact_check"})
    assert scope.pii_types == frozenset()


def test_pii_subtypes_are_trimmed_per_referenced_value():
    nodes = [gate([Row("pii", "cn_id_card", conf_min=0.3),
                   Row("pii", "email_address", conf_min=0.3)])]
    scope = compute_signal_scope(nodes)
    assert scope.signals == frozenset({"pii"})
    assert scope.pii_types == frozenset({"cn_id_card", "email_address"})


def test_rule_only_tree_needs_no_model_questions():
    nodes = [gate([Row("keyword", "医疗急症", conf_min=0.5),
                   Row("context", "1", conf_min=0.0),
                   Row("language", "zh", conf_min=0.0)]),
             gate([], node_id="fallback")]
    scope = compute_signal_scope(nodes)
    assert scope.trimmed
    assert scope.signals == frozenset()
    assert scope.pii_types == frozenset()


def test_unknown_signal_falls_back_to_the_full_pack():
    scope = compute_signal_scope([gate([Row("telepathy", "yes")])])
    assert scope.full and "telepathy" in scope.reason
    assert not scope.trimmed


def test_unknown_pii_subtype_falls_back_to_the_full_pack():
    scope = compute_signal_scope([gate([Row("pii", "not_a_real_type")])])
    assert scope.full and "not_a_real_type" in scope.reason


def test_unknown_multi_model_signal_falls_back_to_the_full_pack(monkeypatch):
    spec = SignalSpec("fake_multi", "假", "Fake", "safety", "model",
                      multi=True, values=(("v", "v", "v"),))
    monkeypatch.setitem(SIGNALS, "fake_multi", spec)
    scope = compute_signal_scope([gate([Row("fake_multi", "v")])])
    assert scope.full and "fake_multi" in scope.reason


def test_empty_or_missing_pipeline_falls_back_to_the_full_pack():
    for nodes in (None, []):
        scope = compute_signal_scope(nodes)
        assert scope.full and scope.reason


def test_full_scope_helper_is_not_trimmed():
    scope = full_scope("because")
    assert scope.full is True
    assert scope.trimmed is False
    assert scope.reason == "because"
    assert isinstance(scope, SignalScope)
