"""Shared routing runtime: one flow for /v1, /admin/try and eval replay.

``RoutingRuntime`` owns the order that must never drift between call paths:

1. no enabled tree -> ``NoActiveTreeError`` BEFORE any tagging/decision work;
2. prepare the whole required signal pack once (pure-NL trees skip tagging;
   rule-only trees tag with an empty trimmed scope so no model forward runs);
3. strictly validate the pack (missing/malformed -> ``SignalPreparationError``,
   never a normal branch Miss);
4. evaluate the snapshot's tree with ``PreparedSignalReader`` + the runner.

The test fake ``FakeDecisionRunner`` is the Task 4 one (re-used on purpose).
"""

import asyncio
import math

import pytest

from router.decision.runner import ChoiceResult
from router.pipeline.errors import NoActiveTreeError, SignalPreparationError
from router.pipeline.evaluator import Hit, Miss
from router.pipeline.reader import validate_signal_pack
from router.pipeline.runtime import RoutingResult, RoutingRuntime, RuntimeRequest
from router.pipeline.snapshot import build_snapshot, thaw_data
from router.pipeline.tree import (
    Branch,
    NLChoice,
    NLOption,
    Route,
    RoutingTree,
    When,
)
from router.signals.tagger import TagResult
from tests.test_unified_tree_evaluator import FakeDecisionRunner


class FakeTagger:
    def __init__(self, signals=None, *, error=None):
        self.signals = signals or {}
        self.error = error
        self.calls = []

    async def tag(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return TagResult(signals=dict(self.signals))


def runtime_case(tree, results):
    tagger = FakeTagger()
    runner = FakeDecisionRunner(results)
    snapshot = build_snapshot(
        tree=tree, backends={}, questions=None,
        decision_info=runner.info(), jev_trim=True)
    return RoutingRuntime(tagger, runner), snapshot, tagger


def snapshot_of(tree, tagger=None, results=None, **kwargs):
    runner = FakeDecisionRunner(results or [])
    snapshot = build_snapshot(
        tree=tree, backends={}, questions=kwargs.pop("questions", None),
        decision_info=runner.info(), jev_trim=kwargs.pop("jev_trim", True))
    return snapshot, runner


def pure_nl_tree():
    return RoutingTree(
        id="nl", name="NL", enabled=True,
        root=NLChoice(
            id="root", instructions="choose",
            options=(
                NLOption("a", "A", Route("a-route", "m1")),
                NLOption("b", "B", Route("b-route", "m2")),
            ),
            otherwise=Route("fallback", "m1")))


def rule_tree():
    return RoutingTree(
        id="rule", name="Rule", enabled=True,
        root=Branch(
            id="root",
            children=(Route("office", "m1",
                            when=When("time_window", "work_hours",
                                      op="contains")),),
            otherwise=Route("fallback", "m0")))


def mixed_tree():
    return RoutingTree(
        id="mixed", name="Mixed", enabled=True,
        root=Branch(
            id="root",
            children=(
                Route("pii", "m2", when=When("pii", "email_address",
                                             op="contains")),
                NLChoice(id="nl", instructions="choose", options=(
                    NLOption("a", "A", Route("a-route", "m1")),
                    NLOption("b", "B", Route("b-route", "m3")),
                )),
            ),
            otherwise=Route("fallback", "m0")))


def nested_nl_tree():
    inner = NLChoice(id="inner", instructions="inner choose", options=(
        NLOption("x", "X", Route("x-route", "m1")),
        NLOption("y", "Y", Route("y-route", "m2")),
    ))
    return RoutingTree(
        id="nl2", name="NL2", enabled=True,
        root=NLChoice(
            id="root", instructions="outer choose",
            options=(
                NLOption("deep", "deep path", inner),
                NLOption("fast", "fast path", Route("fast-route", "m0")),
            ),
            otherwise=Route("fallback", "m0")))


RULE_SIGNAL = {"time_window": {"value": ["work_hours"], "confidence": 1.0,
                               "is_rule": True}}
PII_SIGNAL = {"pii": {"value": ["email_address"], "confidence": 0.95,
                      "is_rule": False}}


# --------------------------------------------------------------------------- #
# Step 1 (brief): no active tree / pure NL / pure rule / mixed
# --------------------------------------------------------------------------- #
def test_pure_nl_skips_signal_tagger():
    runtime, snapshot, tagger = runtime_case(
        pure_nl_tree(), [ChoiceResult(probabilities={"a": 1.0, "b": 0.0})])
    result = asyncio.run(runtime.route(snapshot, RuntimeRequest(text="hello")))
    assert isinstance(result.outcome, Hit)
    assert tagger.calls == []
    assert result.nl_calls == 1


def test_no_active_tree_fails_before_signal_or_nl():
    runtime, snapshot, tagger = runtime_case(None, [])
    with pytest.raises(NoActiveTreeError):
        asyncio.run(runtime.route(snapshot, RuntimeRequest(text="hello")))
    assert tagger.calls == []


def test_pure_rule_tree_tags_once_with_an_empty_model_scope():
    tagger = FakeTagger(RULE_SIGNAL)
    snapshot, runner = snapshot_of(rule_tree())
    result = asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello")))
    assert isinstance(result.outcome, Hit) and result.outcome.model == "m1"
    assert len(tagger.calls) == 1
    request = tagger.calls[0]
    # the trimmed scope carries no model question: zero model forward passes
    assert request.scope is not None and request.scope.trimmed
    assert request.scope.signals == frozenset()
    assert request.scope.pii_types == frozenset()


def test_mixed_tree_tags_exactly_once():
    tagger = FakeTagger(PII_SIGNAL)
    snapshot, runner = snapshot_of(
        mixed_tree(), results=[ChoiceResult(probabilities={"a": 1.0, "b": 0.0})])
    result = asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello",
                                 headers={"X-SR-Tenant": "t1"},
                                 n_messages=2)))
    assert len(tagger.calls) == 1
    request = tagger.calls[0]
    assert (request.text, request.headers, request.n_messages) == (
        "hello", {"X-SR-Tenant": "t1"}, 2)
    assert request.scope is not None
    assert request.scope.signals == frozenset({"pii"})
    assert isinstance(result.outcome, Hit) and result.outcome.model == "m2"
    assert result.nl_calls == 0               # direct PII hit short-circuits NL


def test_mixed_tree_reaches_the_natural_language_node_once():
    low_confidence_pii = {"pii": {"value": ["email_address"],
                                  "confidence": 0.2, "is_rule": False}}
    tagger = FakeTagger(low_confidence_pii)
    snapshot, runner = snapshot_of(
        mixed_tree(), results=[ChoiceResult(probabilities={"a": 1.0, "b": 0.0})])
    result = asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello")))
    assert len(tagger.calls) == 1
    assert isinstance(result.outcome, Hit) and result.outcome.model == "m1"
    assert result.nl_calls == 1
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- #
# Signal preparation contract
# --------------------------------------------------------------------------- #
def test_missing_required_signal_is_a_preparation_error():
    tagger = FakeTagger({})                    # time_window missing
    snapshot, runner = snapshot_of(rule_tree())
    with pytest.raises(SignalPreparationError):
        asyncio.run(RoutingRuntime(tagger, runner).route(
            snapshot, RuntimeRequest(text="hello")))
    assert len(tagger.calls) == 1


def test_nan_confidence_is_a_preparation_error():
    tagger = FakeTagger({"time_window": {"value": ["work_hours"],
                                         "confidence": float("nan"),
                                         "is_rule": True}})
    snapshot, runner = snapshot_of(rule_tree())
    with pytest.raises(SignalPreparationError):
        asyncio.run(RoutingRuntime(tagger, runner).route(
            snapshot, RuntimeRequest(text="hello")))


def test_tagger_failure_is_a_preparation_error():
    tagger = FakeTagger(error=RuntimeError("model backend down"))
    snapshot, runner = snapshot_of(rule_tree())
    with pytest.raises(SignalPreparationError):
        asyncio.run(RoutingRuntime(tagger, runner).route(
            snapshot, RuntimeRequest(text="hello")))


def test_legal_value_none_is_a_normal_miss_not_a_preparation_error():
    tree = RoutingTree(
        id="null-sig", name="Null signal", enabled=True,
        root=Branch(id="root", children=(
            Route("a", "m1", when=When("jailbreak", "jailbreak")),),
            otherwise=Route("fallback", "m0")))
    tagger = FakeTagger({"jailbreak": {"value": None, "confidence": None,
                                       "is_rule": False}})
    snapshot, runner = snapshot_of(tree)
    result = asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello")))
    # a legal "unknown" is a normal non-match -> the explicit fallback wins
    assert isinstance(result.outcome, Hit) and result.outcome.model == "m0"
    assert result.signals["jailbreak"]["value"] is None


@pytest.mark.parametrize("pack", [
    {"a": "not a dict"},
    {"a": {"confidence": 1.0}},                                  # no value key
    {"a": {"value": "x", "confidence": float("nan")}},
    {"a": {"value": "x", "confidence": True}},
    {"a": {"value": {"nested": 1}}},
    {"a": {"value": ["ok", {"bad": 1}]}},
    {"a": {"value": "ok", "conf_by_value": {"email_address": float("inf")}}},
    {"a": {"value": "ok", "conf_by_value": ["not", "a", "map"]}},
])
def test_validate_signal_pack_rejects_malformed_packs(pack):
    with pytest.raises(SignalPreparationError):
        validate_signal_pack(frozenset({"a"}), pack)


def test_validate_signal_pack_requires_every_key():
    with pytest.raises(SignalPreparationError):
        validate_signal_pack(frozenset({"a", "b"}), {"a": {"value": "x"}})


def test_validate_signal_pack_accepts_legal_packs():
    validate_signal_pack(frozenset(), {})
    validate_signal_pack(frozenset({"a", "b", "c", "d"}), {
        "a": {"value": None, "confidence": None},
        "b": {"value": ["x", "y"], "confidence": 0.5,
              "conf_by_value": {"x": 0.4, "y": 0.1}},
        "c": {"value": 3, "confidence": 1.0, "is_rule": True},
        "d": {"value": "text", "confidence": 0.0, "is_rule": False},
    })


# --------------------------------------------------------------------------- #
# Snapshot questions / scope / evidence
# --------------------------------------------------------------------------- #
def test_runtime_passes_thawed_snapshot_questions_to_the_tagger():
    questions = {"pii": {"types": {"email_address": "邮箱"}, "list": [1, 2]}}
    tagger = FakeTagger(RULE_SIGNAL)
    snapshot, runner = snapshot_of(rule_tree(), questions=questions)
    asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello")))
    sent = tagger.calls[0].questions
    assert sent == questions
    assert isinstance(sent, dict) and isinstance(sent["pii"]["list"], list)
    sent["pii"]["list"].append(3)              # request owns its deep copy
    assert thaw_data(snapshot.questions) == questions


def test_runtime_passes_no_scope_when_trimming_is_disabled():
    tagger = FakeTagger(RULE_SIGNAL)
    snapshot, runner = snapshot_of(rule_tree(), jev_trim=False)
    asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello")))
    assert tagger.calls[0].scope is None


def test_result_aggregates_nl_evidence_from_the_trace():
    snapshot, runner = snapshot_of(
        nested_nl_tree(),
        results=[ChoiceResult(probabilities={"deep": 1.0, "fast": 0.0},
                              duration_ms=7.5),
                 ChoiceResult(probabilities={"x": 1.0, "y": 0.0},
                              duration_ms=3.25)])
    tagger = FakeTagger()
    result = asyncio.run(RoutingRuntime(tagger, runner).route(
        snapshot, RuntimeRequest(text="hello")))
    assert isinstance(result, RoutingResult)
    assert isinstance(result.outcome, Hit) and result.outcome.model == "m1"
    assert tagger.calls == []                  # pure NL: no tagging at all
    assert result.nl_calls == 2
    assert result.decision_ms == pytest.approx(10.75)
    assert result.trace == result.outcome.trace
    assert result.signals == {}
    assert result.input == {}
    assert result.questions_sha256 is None
    assert result.tagger_info == {}
    assert result.snapshot_revision == snapshot.snapshot_revision
    assert math.isfinite(result.signal_ms) and result.signal_ms >= 0.0


def test_runner_exception_becomes_the_nl_error_miss_not_a_runtime_error():
    snapshot, runner = snapshot_of(pure_nl_tree(),
                                   results=[RuntimeError("decision down")])
    result = asyncio.run(RoutingRuntime(FakeTagger(), runner).route(
        snapshot, RuntimeRequest(text="hello")))
    # the evaluator turns the failure into a local nl_error -> the node's
    # explicit otherwise handles it; the runtime never re-raises it.
    assert isinstance(result.outcome, Hit) and result.outcome.model == "m1"
    assert result.nl_calls == 1                # the failed call is still traced
    assert len(runner.calls) == 1
    assert any(event.get("event") == "nl_choice"
               and event.get("reason") == "nl_error"
               for event in result.trace)


def test_nl_low_probability_goes_to_otherwise_with_one_traced_call():
    gated = RoutingTree(
        id="gated", name="Gated", enabled=True,
        root=NLChoice(id="root", instructions="choose", min_prob=0.9,
                      options=(
                          NLOption("a", "A", Route("a-route", "m1")),
                          NLOption("b", "B", Route("b-route", "m2")),
                      ), otherwise=Route("fallback", "m0")))
    snapshot, runner = snapshot_of(gated, results=[
        ChoiceResult(probabilities={"a": 0.6, "b": 0.4}, duration_ms=2.0)])
    result = asyncio.run(RoutingRuntime(FakeTagger(), runner).route(
        snapshot, RuntimeRequest(text="hello")))
    assert isinstance(result.outcome, Hit)
    assert result.outcome.model == "m0"          # the explicit otherwise
    assert result.nl_calls == 1
    assert result.decision_ms == pytest.approx(2.0)
