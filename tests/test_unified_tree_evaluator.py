"""Asynchronous unified-tree evaluator: Hit/Miss, when/otherwise and NL gates.

These tests pin the new engine semantics (2026-10-26 unified routing tree,
Task 4).  ``FakeDecisionRunner`` is deliberately defined here (brief shape) and
re-used by the Task 8 runtime tests:

* a direct hit short-circuits later siblings (a later NL node is never called);
* weighted hits compete by score only when no direct hit was found, ties keep
  the earlier candidate;
* ``nl_choice`` calls the runner exactly once, validates the probability
  distribution, applies the ``min_prob`` / ``min_margin`` gates and never
  backtracks to a second candidate;
* ``when=false`` returns a ``Miss`` without executing that node's ``otherwise``;
* the strict ``PreparedSignalReader`` turns infrastructure failures into
  ``SignalPreparationError`` (a normal ``value=None`` stays a normal miss).
"""

import asyncio

import pytest

from router.decision.runner import ChoiceResult
from router.pipeline.errors import SignalPreparationError
from router.pipeline.evaluator import Hit, Miss, TreeEvaluator
from router.pipeline.reader import PreparedSignalReader
from router.pipeline.tree import (Block, Branch, NLChoice, NLOption, Route,
                                  Weight, When)


class FakeReader:
    def __init__(self, signals):
        self.signals = signals

    def read(self, key):
        return self.signals[key]


class FakeDecisionRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def info(self):
        return {"engine": "fake-decision", "revision": "fake-v1"}

    async def choose(self, request):
        self.calls.append(request)
        if not self.results:
            raise AssertionError("unexpected decision call")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def model_option(owner_id, key, model):
    """A direct ``model`` NL option exactly as the compiler builds it."""
    return NLOption(key=key, text=f"{model} description",
                    target=Route(id=f"{owner_id}/options/{key}", model=model))


def nl(node_id, options, *, otherwise=None, when=None, min_prob=0.45,
       min_margin=0.10):
    return NLChoice(id=node_id, instructions=f"choose {node_id}",
                    options=tuple(options), otherwise=otherwise, when=when,
                    min_prob=min_prob, min_margin=min_margin)


def signal(value, confidence):
    return {"value": value, "confidence": confidence, "is_rule": False}


def evaluate(root, results=None, signals=None):
    runner = FakeDecisionRunner(results or [])
    result = asyncio.run(
        TreeEvaluator(FakeReader(signals or {}), runner).evaluate(root, "text"))
    return result, runner


def nl_event(result):
    return next(event for event in result.trace
                if event.get("event") == "nl_choice")


TWO_OPTIONS = (model_option("root", "a", "m1"), model_option("root", "b", "m2"))
THREE_OPTIONS = TWO_OPTIONS + (model_option("root", "c", "m3"),)


# ── branch: direct hits, weighted competition, short-circuiting ─────────────
def test_direct_hit_short_circuits_before_later_nl():
    later = NLChoice(id="later", instructions="never", options=())
    root = Branch(id="root", children=(
        Route(id="first", model="m1"),
        later,
    ), otherwise=Route(id="fallback", model="m0"))
    runner = FakeDecisionRunner([])
    result = asyncio.run(TreeEvaluator(FakeReader({}), runner).evaluate(root, "text"))
    assert isinstance(result, Hit) and result.model == "m1"
    assert runner.calls == []


def test_weighted_hits_compete_by_score_when_no_direct_hit():
    signals = {"complexity": signal("hard", 0.9), "domain": signal("math", 0.8)}
    root = Branch(id="root", children=(
        Route(id="high", model="m-high", min_score=0.5,
              weights=(Weight("complexity", "hard", 0.5),)),
        Route(id="low", model="m-low", min_score=0.5,
              weights=(Weight("complexity", "hard", 0.3),
                       Weight("domain", "math", 0.7))),
    ), otherwise=Route(id="fallback", model="m0"))
    result, runner = evaluate(root, [], signals)
    assert isinstance(result, Hit) and result.model == "m-high"
    assert result.score == pytest.approx(0.9)
    assert runner.calls == []
    # no direct hit discarded a candidate -> nothing is marked superseded
    assert not any(event.get("superseded") for event in result.trace)


def test_direct_hit_wins_over_earlier_weighted_hit_and_stops_later_nl():
    signals = {"complexity": signal("hard", 0.9)}
    later = nl("later", TWO_OPTIONS)
    root = Branch(id="root", children=(
        Route(id="weighted", model="m-weighted", min_score=0.5,
              weights=(Weight("complexity", "hard", 0.5),)),
        Route(id="direct", model="m-direct"),
        later,
    ), otherwise=Route(id="fallback", model="m0"))
    result, runner = evaluate(root, [], signals)
    assert isinstance(result, Hit) and result.model == "m-direct"
    assert result.score is None
    assert runner.calls == []


def test_weighted_tie_keeps_the_earlier_candidate():
    signals = {"complexity": signal("hard", 0.8)}
    root = Branch(id="root", children=(
        Route(id="first", model="m-first", min_score=0.5,
              weights=(Weight("complexity", "hard", 1.0),)),
        Route(id="second", model="m-second", min_score=0.5,
              weights=(Weight("complexity", "hard", 1.0),)),
    ))
    result, _ = evaluate(root, [], signals)
    assert result.model == "m-first"


def test_weighted_terminal_below_min_score_misses_to_otherwise():
    signals = {"complexity": signal("easy", 0.9)}
    root = Branch(id="root", children=(
        Route(id="weighted", model="m-weighted", min_score=0.5,
              weights=(Weight("complexity", "hard", 1.0),)),
    ), otherwise=Route(id="fallback", model="m0"))
    result, _ = evaluate(root, [], signals)
    assert isinstance(result, Hit) and result.model == "m0"
    assert any(event.get("reason") == "below_min_score"
               for event in result.trace)


def test_block_terminal_hit_is_a_block_outcome():
    root = Branch(id="root", children=(
        Block(id="deny"),
        Route(id="allow", model="m1"),
    ))
    result, _ = evaluate(root, [])
    assert isinstance(result, Hit) and result.kind == "block"
    assert result.model is None and result.node_id == "deny"


def test_condition_false_skips_one_sibling_and_continues():
    root = Branch(id="root", children=(
        Route(id="gated", model="m-gated", when=When("domain", "math")),
        Route(id="plain", model="m-plain"),
    ))
    result, _ = evaluate(root, [], {"domain": signal("biology", 0.9)})
    assert result.model == "m-plain"
    assert any(event.get("reason") == "condition_false"
               for event in result.trace)


# ── nl_choice: gates (equality boundaries) and ranking order ────────────────
def test_nl_equality_boundaries_pass():
    root = nl("root", THREE_OPTIONS, min_prob=0.5, min_margin=0.25)
    result, runner = evaluate(root, [
        ChoiceResult(probabilities={"a": 0.5, "b": 0.25, "c": 0.25})])
    assert isinstance(result, Hit) and result.model == "m1"
    assert len(runner.calls) == 1
    event = nl_event(result)
    assert event["selected_key"] == "a"
    assert event["p_top"] == 0.5
    assert event["margin"] == 0.25


def test_nl_tie_selects_configuration_order_and_keeps_candidate_order():
    root = nl("root", TWO_OPTIONS, min_prob=0.4, min_margin=0.0)
    result, _ = evaluate(root, [ChoiceResult(probabilities={"b": 0.5, "a": 0.5})])
    assert isinstance(result, Hit) and result.model == "m1"
    event = nl_event(result)
    assert event["selected_key"] == "a"
    assert list(event["probabilities"]) == ["a", "b"]
    assert event["ranking"] == ["a", "b"]


def test_nl_low_probability_uses_otherwise():
    root = nl("root", TWO_OPTIONS, otherwise=Route(id="safe", model="m-safe"),
              min_prob=0.8)
    result, runner = evaluate(root, [
        ChoiceResult(probabilities={"a": 0.3, "b": 0.7})])
    assert isinstance(result, Hit) and result.model == "m-safe"
    assert any(event.get("reason") == "nl_low_probability"
               for event in result.trace)
    assert result.trace[-1] == {"node_id": "safe", "event": "otherwise_hit"}
    assert len(runner.calls) == 1


def test_nl_ambiguous_uses_otherwise():
    root = nl("root", THREE_OPTIONS, otherwise=Route(id="safe", model="m-safe"),
              min_prob=0.4, min_margin=0.1)
    result, runner = evaluate(root, [
        ChoiceResult(probabilities={"a": 0.45, "b": 0.44, "c": 0.11})])
    assert isinstance(result, Hit) and result.model == "m-safe"
    assert any(event.get("reason") == "nl_ambiguous" for event in result.trace)
    assert len(runner.calls) == 1


def test_nl_runner_error_uses_otherwise():
    root = nl("root", TWO_OPTIONS, otherwise=Route(id="safe", model="m-safe"))
    result, runner = evaluate(root, [RuntimeError("model crashed")])
    assert isinstance(result, Hit) and result.model == "m-safe"
    events = [event for event in result.trace if event.get("nl_call") is True]
    assert len(events) == 1
    assert events[0]["reason"] == "nl_error"
    assert "model crashed" in events[0]["error"]
    assert len(runner.calls) == 1


@pytest.mark.parametrize("probabilities", [
    {"a": 0.9, "c": 0.1},                 # unknown key, missing candidate
    {"a": 1.0},                           # missing candidate
    {"a": float("nan"), "b": 0.5},        # non-finite probability
    {"a": float("inf"), "b": -float("inf")},
    {"a": 1.2, "b": -0.2},                # negative probability
    {"a": 0.7, "b": 0.7},                 # not a normalised distribution
    {"a": "0.9", "b": 0.1},               # non-numeric probability
    "not-a-mapping",
])
def test_nl_invalid_result_is_nl_error_and_uses_otherwise(probabilities):
    root = nl("root", TWO_OPTIONS, otherwise=Route(id="safe", model="m-safe"))
    result, runner = evaluate(root, [
        ChoiceResult(probabilities=probabilities)])
    assert isinstance(result, Hit) and result.model == "m-safe"
    assert any(event.get("reason") == "nl_error" for event in result.trace)
    assert len(runner.calls) == 1


def test_nl_choice_without_otherwise_returns_a_miss():
    root = nl("root", TWO_OPTIONS, min_prob=0.8)
    result, runner = evaluate(root, [
        ChoiceResult(probabilities={"a": 0.3, "b": 0.7})])
    assert isinstance(result, Miss) and result.reason == "nl_low_probability"
    assert len(runner.calls) == 1


def test_nl_call_event_carries_runner_evidence_once():
    root = nl("root", TWO_OPTIONS, min_prob=0.4)
    result, _ = evaluate(root, [ChoiceResult(
        probabilities={"a": 0.8, "b": 0.2}, input={"input_window": "2:0"},
        model_info={"engine": "fake-jev"}, duration_ms=12.5)])
    events = [event for event in result.trace if event.get("nl_call") is True]
    assert len(events) == 1
    event = events[0]
    assert event["node_id"] == "root" and event["event"] == "nl_choice"
    assert event["duration_ms"] == 12.5
    assert event["input"] == {"input_window": "2:0"}
    assert event["model_info"] == {"engine": "fake-jev"}
    assert event["probabilities"] == {"a": 0.8, "b": 0.2}
    assert event["candidates"] == ["a", "b"]
    assert event["selected_key"] == "a"


# ── nl_choice: selection, child miss and no backtracking ───────────────────
def test_selected_child_miss_uses_otherwise_without_second_choice():
    root = NLChoice(
        id="root", instructions="choose", min_prob=0.4, min_margin=0.1,
        options=(
            NLOption(key="a", text="A", target=Branch(id="a-branch")),
            NLOption(key="b", text="B", target=Route(id="b-route", model="m2")),
        ),
        otherwise=Route(id="safe", model="m1"))
    runner = FakeDecisionRunner([ChoiceResult(probabilities={"a": 0.8, "b": 0.2})])
    result = asyncio.run(TreeEvaluator(FakeReader({}), runner).evaluate(root, "text"))
    assert isinstance(result, Hit) and result.model == "m1"
    assert len(runner.calls) == 1
    assert any(event.get("reason") == "selected_child_miss" for event in result.trace)


def test_two_level_nl_chain_calls_the_runner_once_per_reached_node():
    child = nl("child", (model_option("child", "x", "mx"),
                         model_option("child", "y", "my")))
    root = nl("root", (NLOption(key="deep", text="DEEP", target=child),
                       model_option("root", "b", "m-b")),
              min_prob=0.4)
    result, runner = evaluate(root, [
        ChoiceResult(probabilities={"deep": 0.8, "b": 0.2}),
        ChoiceResult(probabilities={"x": 0.9, "y": 0.1})])
    assert isinstance(result, Hit) and result.model == "mx"
    assert len(runner.calls) == 2
    first, second = runner.calls
    assert first.state == "text" and first.instructions == "choose root"
    assert [option.key for option in first.options] == ["deep", "b"]
    assert first.options[0].text == "DEEP"
    assert second.instructions == "choose child"
    assert [option.key for option in second.options] == ["x", "y"]


def test_nl_selected_weighted_hit_bubbles_up_with_its_score():
    weighted = Route(id="w", model="m-weighted", min_score=0.5,
                     weights=(Weight("complexity", "hard", 1.0),))
    root = nl("root", (NLOption(key="weighted", text="WEIGHTED", target=weighted),
                       model_option("root", "flat", "m-flat")))
    result, _ = evaluate(
        root, [ChoiceResult(probabilities={"weighted": 0.9, "flat": 0.1})],
        {"complexity": signal("hard", 0.9)})
    assert isinstance(result, Hit) and result.model == "m-weighted"
    assert result.node_id == "w"
    assert result.score == pytest.approx(0.9)


def test_nl_weighted_hit_competes_in_the_parent_branch():
    weighted = Route(id="w", model="m-weighted", min_score=0.5,
                     weights=(Weight("complexity", "hard", 1.0),))
    root = Branch(id="root", children=(
        nl("root", (NLOption(key="weighted", text="WEIGHTED", target=weighted),
                    model_option("root", "flat", "m-flat"))),
        Route(id="other", model="m-other", min_score=0.5,
              weights=(Weight("complexity", "hard", 0.5),
                       Weight("domain", "math", 0.5))),
    ), otherwise=Route(id="fallback", model="m0"))
    result, _ = evaluate(
        root, [ChoiceResult(probabilities={"weighted": 0.9, "flat": 0.1})],
        {"complexity": signal("hard", 0.9), "domain": signal("math", 0.8)})
    assert isinstance(result, Hit) and result.model == "m-weighted"


# ── when / confidence floors ────────────────────────────────────────────────
def test_when_false_does_not_execute_the_nodes_own_otherwise():
    root = NLChoice(
        id="root", instructions="choose", when=When("domain", "math"),
        options=TWO_OPTIONS, otherwise=Route(id="safe", model="m-safe"))
    result, runner = evaluate(root, [], {"domain": signal("biology", 0.9)})
    assert isinstance(result, Miss) and result.reason == "condition_false"
    assert runner.calls == []
    assert not any(event.get("event") == "otherwise_hit"
                   for event in result.trace)


def test_model_signal_below_conf_min_is_skipped():
    root = nl("root", TWO_OPTIONS, when=When("domain", "math", conf_min=0.9))
    result, runner = evaluate(root, [], {"domain": signal("math", 0.5)})
    assert isinstance(result, Miss) and result.reason == "below_confidence"
    assert runner.calls == []


def test_rule_signal_ignores_the_confidence_floor():
    root = Route(id="r", model="m1", when=When("language", "zh"))
    result, _ = evaluate(root, [], {"language": {"value": "zh",
                                                "confidence": 0.1}})
    assert isinstance(result, Hit) and result.model == "m1"


# ── trace: passing `when` gates are recorded (design §10 entered/skipped) ──
def test_passing_when_is_traced_before_the_hit():
    root = Route(id="root", model="m1",
                 when=When("domain", "math", conf_min=0.4))
    result, _ = evaluate(root, [], {"domain": signal("math", 0.9)})
    assert isinstance(result, Hit) and result.model == "m1"
    assert [event["event"] for event in result.trace] == ["condition", "hit"]
    assert result.trace[0] == {
        "node_id": "root", "event": "condition", "signal": "domain",
        "op": "eq", "value": "math", "m": 1, "conf": 0.9, "conf_min": 0.4,
        "pass": True, "reason": None}
    assert result.trace[1]["node_id"] == "root"
    assert result.trace[1]["event"] == "hit"


def test_passing_branch_child_condition_is_traced_before_its_hit():
    root = Branch(id="root", children=(
        Route(id="child", model="m1",
              when=When("domain", "math", conf_min=0.4)),
        Route(id="later", model="m2"),
    ), otherwise=Route(id="fallback", model="m0"))
    result, _ = evaluate(root, [], {"domain": signal("math", 0.9)})
    assert isinstance(result, Hit) and result.model == "m1"
    assert [event["event"] for event in result.trace] == [
        "entered", "condition", "hit"]
    assert result.trace[0] == {"node_id": "root", "event": "entered"}
    assert result.trace[1]["node_id"] == "child"
    assert result.trace[1]["pass"] is True
    assert result.trace[2]["node_id"] == "child"
    assert result.trace[2]["event"] == "hit"


def test_passing_when_on_a_container_node_is_traced_once():
    root = Branch(id="root", when=When("domain", "math", conf_min=0.4),
                  children=(Route(id="child", model="m1"),
                            Route(id="later", model="m2")))
    result, _ = evaluate(root, [], {"domain": signal("math", 0.9)})
    assert isinstance(result, Hit) and result.model == "m1"
    assert [event["event"] for event in result.trace] == [
        "entered", "condition", "hit"]
    assert result.trace[0] == {"node_id": "root", "event": "entered"}
    assert result.trace[1]["node_id"] == "root"
    assert result.trace[1]["pass"] is True


def test_passing_when_on_an_nl_node_precedes_the_nl_call_event():
    root = nl("root", (model_option("root", "a", "m1"),
                       model_option("root", "b", "m2")),
              when=When("domain", "math", conf_min=0.4))
    result, _ = evaluate(
        root, [ChoiceResult(probabilities={"a": 1.0, "b": 0.0})],
        {"domain": signal("math", 0.9)})
    assert isinstance(result, Hit) and result.model == "m1"
    assert [event["event"] for event in result.trace] == [
        "entered", "condition", "nl_choice", "hit"]
    assert result.trace[0] == {"node_id": "root", "event": "entered"}
    assert result.trace[1]["node_id"] == "root"
    assert result.trace[1]["pass"] is True


def test_failing_when_trace_stays_a_single_condition_event():
    root = Route(id="root", model="m1",
                 when=When("domain", "math", conf_min=0.9))
    result, _ = evaluate(root, [], {"domain": signal("math", 0.5)})
    assert isinstance(result, Miss) and result.reason == "below_confidence"
    assert [event["event"] for event in result.trace] == ["condition"]
    assert result.trace[0]["pass"] is False
    assert result.trace[0]["reason"] == "below_confidence"


# ── trace: container `entered` events + superseded weighted hits (M-6) ────
def test_container_entered_event_is_traced_before_its_children():
    root = Branch(id="root", children=(Route(id="child", model="m1"),))
    result, _ = evaluate(root, [])
    assert isinstance(result, Hit) and result.model == "m1"
    assert [event["event"] for event in result.trace] == ["entered", "hit"]
    assert result.trace[0] == {"node_id": "root", "event": "entered"}


def test_nested_container_entered_events_keep_the_parent_first():
    inner = Branch(id="inner", children=(Route(id="leaf", model="m1"),))
    root = Branch(id="root", children=(inner,))
    result, _ = evaluate(root, [])
    assert [event["event"] for event in result.trace] == [
        "entered", "entered", "hit"]
    assert [event["node_id"] for event in result.trace[:2]] == [
        "root", "inner"]


def test_nl_container_entered_event_precedes_its_call():
    root = nl("root", TWO_OPTIONS)
    result, _ = evaluate(root, [ChoiceResult(probabilities={"a": 1.0,
                                                           "b": 0.0})])
    assert [event["event"] for event in result.trace] == [
        "entered", "nl_choice", "hit"]
    assert result.trace[0] == {"node_id": "root", "event": "entered"}


def test_direct_hit_marks_discarded_weighted_hits_superseded():
    signals = {"complexity": signal("hard", 0.9)}
    root = Branch(id="root", children=(
        Route(id="weighted", model="m-weighted", min_score=0.5,
              weights=(Weight("complexity", "hard", 0.5),)),
        Route(id="direct", model="m-direct"),
    ), otherwise=Route(id="fallback", model="m0"))
    result, _ = evaluate(root, [], signals)
    assert isinstance(result, Hit) and result.model == "m-direct"
    assert result.score is None
    hits = [event for event in result.trace if event.get("event") == "hit"]
    weighted_hit = next(event for event in hits
                        if event["node_id"] == "weighted")
    direct_hit = next(event for event in hits if event["node_id"] == "direct")
    assert weighted_hit["superseded"] is True
    assert "superseded" not in direct_hit
    # only the terminal hit events are marked, not their condition evidence
    assert all("superseded" not in event for event in result.trace
               if event.get("event") != "hit")


# ── strict signal reader ────────────────────────────────────────────────────
def test_prepared_signal_reader_is_strict():
    reader = PreparedSignalReader({"domain": {"value": "math",
                                              "confidence": 0.9}})
    assert reader.read("domain")["value"] == "math"
    cases = [
        ({}, "domain"),                                        # missing key
        ({"domain": None}, "domain"),                          # not a dict
        ({"domain": {"confidence": 0.9}}, "domain"),           # no value key
        ({"domain": {"value": "math", "confidence": float("nan")}}, "domain"),
        ({"domain": {"value": "math", "confidence": float("inf")}}, "domain"),
        ({"domain": {"value": "math", "confidence": "high"}}, "domain"),
    ]
    for signals, key in cases:
        with pytest.raises(SignalPreparationError):
            PreparedSignalReader(signals).read(key)


def test_prepared_signal_reader_rejects_a_non_mapping_pack():
    with pytest.raises(SignalPreparationError):
        PreparedSignalReader(None)


def test_prepared_signal_reader_allows_null_value_and_missing_confidence():
    reader = PreparedSignalReader({"user_feedback": {"value": None}})
    assert reader.read("user_feedback")["value"] is None


def test_null_signal_value_is_a_normal_condition_miss():
    root = Route(id="r", model="m1", when=When("user_feedback", "satisfied"))
    reader = PreparedSignalReader({"user_feedback": {"value": None,
                                                     "confidence": None}})
    result = asyncio.run(
        TreeEvaluator(reader, FakeDecisionRunner([])).evaluate(root, "text"))
    assert isinstance(result, Miss) and result.reason == "condition_false"


def test_signal_preparation_error_propagates_out_of_evaluate():
    root = Route(id="r", model="m1", when=When("domain", "math"))
    with pytest.raises(SignalPreparationError):
        asyncio.run(TreeEvaluator(PreparedSignalReader({}),
                                  FakeDecisionRunner([])).evaluate(root, "text"))
