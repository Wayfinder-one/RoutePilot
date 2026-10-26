"""Unit tests for the unified-tree signal scope (router/pipeline/scope.py).

The scope answers one question: *which JEV model questions can this routing tree
ever read?*  Everything it lists must be computed (correctness) and everything it
leaves out must be unreadable by the evaluator (the latency win).  These tests pin
the extraction rules, PII sub-type trimming, the decision-group exclusion and the
defensive full-pack fallbacks (validation rejects unknown references before a
tree can be enabled, so a full pack is only a stale-DB safety net).
"""

from __future__ import annotations

import pytest

from router.pipeline.scope import (
    SignalScope,
    compute_tree_scope,
    full_scope,
    required_signal_keys,
)
from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Route,
    Weight,
    When,
)
from router.signals.registry import SIGNALS, SignalSpec
from tests import routing_tree_fixtures


@pytest.fixture()
def mixed_tree():
    return routing_tree_fixtures.mixed_tree()


def test_scope_visits_nl_children_and_otherwise(mixed_tree):
    scope = compute_tree_scope(mixed_tree.root)
    assert {"pii", "complexity"} <= scope.signals
    assert "time_window" in required_signal_keys(mixed_tree.root)
    assert not scope.full


def test_mixed_tree_scope_is_exactly_the_referenced_model_signals(mixed_tree):
    scope = compute_tree_scope(mixed_tree.root)
    assert scope.signals == frozenset({"pii", "complexity"})
    assert scope.pii_types == frozenset({"email_address"})
    assert scope.trimmed and scope.reason
    # time_window is a rule signal: computed locally, no model forward
    assert "time_window" not in scope.signals
    assert required_signal_keys(mixed_tree.root) == frozenset(
        {"pii", "time_window", "complexity"})


def test_pure_nl_tree_needs_no_signals():
    root = NLChoice(
        id="root", instructions="choose",
        options=(NLOption("a", "", Route("root/options/a", "m1"), "m1"),
                 NLOption("b", "", Route("root/options/b", "m2"), "m2")),
        otherwise=Route("fallback", "m1"))
    assert required_signal_keys(root) == frozenset()
    scope = compute_tree_scope(root)
    assert scope.signals == frozenset()
    assert scope.pii_types == frozenset()
    assert scope.trimmed and not scope.full


def test_rule_only_tree_needs_no_model_questions():
    root = Branch(
        id="root",
        children=(Route("a", "m1", when=When("time_window", "work_hours",
                                             op="contains")),
                  Route("b", "m1", when=When("context", "1"))),
        otherwise=Route("f", "m1"))
    assert required_signal_keys(root) == frozenset({"time_window", "context"})
    scope = compute_tree_scope(root)
    assert scope.signals == frozenset()
    assert scope.pii_types == frozenset()
    assert scope.trimmed and not scope.full


def test_pii_is_trimmed_to_the_referenced_subtypes():
    root = Branch(
        id="root",
        children=(Route("a", "m1", when=When("pii", "cn_id_card",
                                            op="contains")),
                  Route("b", "m1", when=When("pii", "email_address",
                                            op="contains"))),
        otherwise=Route("f", "m1"))
    scope = compute_tree_scope(root)
    assert scope.signals == frozenset({"pii"})
    assert scope.pii_types == frozenset({"cn_id_card", "email_address"})


def test_terminal_weights_are_scoped():
    root = Branch(
        id="root",
        children=(
            Route("a", "m1", weights=(Weight("complexity", "hard", 0.5),),
                  min_score=0.5),
            Block("b", weights=(Weight("jailbreak", "jailbreak", 0.5),),
                  min_score=0.5),
        ),
        otherwise=Route("f", "m1"))
    assert required_signal_keys(root) == frozenset({"complexity", "jailbreak"})
    assert compute_tree_scope(root).signals == frozenset(
        {"complexity", "jailbreak"})


def test_decision_group_signals_are_not_model_questions(monkeypatch):
    spec = SignalSpec("fake_decision", "假决策", "Fake Decision",
                      "decision", "model")
    monkeypatch.setitem(SIGNALS, "fake_decision", spec)
    root = Branch(
        id="root",
        children=(Route("a", "m1", when=When("fake_decision", "m1")),),
        otherwise=Route("f", "m1"))
    scope = compute_tree_scope(root)
    assert not scope.full
    assert scope.signals == frozenset()
    # ... but the static signal requirement still knows the tree reads it
    assert "fake_decision" in required_signal_keys(root)


def test_unknown_signal_falls_back_to_full_defensively():
    root = Branch(
        id="root",
        children=(Route("a", "m1", when=When("telepathy", "yes")),),
        otherwise=Route("f", "m1"))
    scope = compute_tree_scope(root)
    assert scope.full and "telepathy" in scope.reason
    assert "telepathy" in required_signal_keys(root)


def test_unknown_pii_subtype_falls_back_to_full():
    root = Branch(
        id="root",
        children=(Route("a", "m1", when=When("pii", "not_a_real_type",
                                            op="contains")),),
        otherwise=Route("f", "m1"))
    scope = compute_tree_scope(root)
    assert scope.full and "not_a_real_type" in scope.reason


def test_unknown_multi_model_signal_falls_back_to_full(monkeypatch):
    spec = SignalSpec("fake_multi", "假", "Fake", "safety", "model",
                      multi=True, values=(("v", "v", "v"),))
    monkeypatch.setitem(SIGNALS, "fake_multi", spec)
    root = Branch(
        id="root",
        children=(Route("a", "m1", when=When("fake_multi", "v")),),
        otherwise=Route("f", "m1"))
    scope = compute_tree_scope(root)
    assert scope.full and "fake_multi" in scope.reason


def test_invalid_root_falls_back_to_full():
    scope = compute_tree_scope(None)
    assert scope.full and scope.reason


def test_full_scope_helper_is_not_trimmed():
    scope = full_scope("because")
    assert scope.full is True
    assert scope.trimmed is False
    assert scope.reason == "because"
    assert isinstance(scope, SignalScope)
