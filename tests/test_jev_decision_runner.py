"""JevDecisionRunner: async adapter over the JEV ``choice`` tagger.

The runner is the only place a routing-tree ``nl_choice`` node touches the
model: one :class:`ChoiceRequest` in, one :class:`ChoiceResult` out, with a
serial gate so one request's NL nodes never overlap on the GPU, and a real
wall-clock duration for the trace.  ``JevTagger.score_choice`` itself is not
modified; this adapter only maps the DTOs and moves the blocking call off the
event loop.

``tests/test_choice.py::FakeChoiceTagger`` models a tagger only for the
**unbound** ``JevTagger.score_choice(tagger, ...)`` call used by Task 1, so it
has no bound ``score_choice`` method.  ``BoundChoiceTagger`` (local subclass)
adds exactly that bound method, which is what the runner calls.
"""

import asyncio
import contextlib
import threading
import time

import pytest

from router.decision.jev_runner import JevDecisionRunner
from router.decision.runner import ChoiceOption, ChoiceRequest, ChoiceResult
from router.signals.jev import JevTagger
from tests.test_choice import FakeChoiceTagger


class BoundChoiceTagger(FakeChoiceTagger):
    """Task 1's fake + the bound ``score_choice`` a tagger must expose."""

    def score_choice(self, state, instructions, candidates):
        return JevTagger.score_choice(self, state, instructions, candidates)


TWO_OPTIONS = (ChoiceOption("b", "second"), ChoiceOption("a", "first"))


def make_request(options=TWO_OPTIONS):
    return ChoiceRequest(state="request", instructions="choose",
                         options=options)


# ── Step 1: candidate order, evidence and real duration ────────────────────
def test_jev_runner_preserves_option_order_and_evidence():
    tagger = BoundChoiceTagger()
    runner = JevDecisionRunner(tagger)
    request = ChoiceRequest(
        state="request", instructions="choose",
        options=(ChoiceOption("b", "second"), ChoiceOption("a", "first")))
    result = asyncio.run(runner.choose(request))
    assert list(result.probabilities) == ["b", "a"]
    assert result.input == {"input_window": "2:0"}
    assert result.duration_ms >= 0
    assert result.model_info["engine"] == "fake-jev"
    assert isinstance(result, ChoiceResult)
    # the tagger received the state/instructions and the untouched order
    assert tagger.records[0]["state"] == "window:request"
    assert tagger.records[0]["question"] == "choose"
    assert tagger.records[0]["answer_keys"] == ["b", "a"]
    assert tagger.records[0]["options"] == ["b: second", "a: first"]


def test_info_is_merged_into_model_info_with_temperature():
    runner = JevDecisionRunner(BoundChoiceTagger())
    assert runner.info() == {"engine": "fake-jev"}
    result = asyncio.run(runner.choose(make_request()))
    assert result.model_info == {"engine": "fake-jev", "temperature": 0.7}


def test_missing_input_and_temperature_default_to_safe_values():
    class BareTagger:
        def score_choice(self, state, instructions, candidates):
            return {"probabilities": {"a": 0.6, "b": 0.4}}

        def info(self):
            return {"engine": "bare"}

    result = asyncio.run(
        JevDecisionRunner(BareTagger()).choose(make_request()))
    assert result.input == {}
    assert result.model_info == {"engine": "bare", "temperature": None}


def test_info_falls_back_to_empty_mapping_without_tagger_info():
    class NoInfoTagger:
        def score_choice(self, state, instructions, candidates):
            return {"probabilities": {"a": 0.6, "b": 0.4}}

    assert JevDecisionRunner(NoInfoTagger()).info() == {}


def test_duration_ms_is_a_real_wall_clock_measurement():
    class SlowTagger:
        def score_choice(self, state, instructions, candidates):
            time.sleep(0.02)
            return {"probabilities": {"a": 0.6, "b": 0.4}, "input": {},
                    "temperature": 0.7}

        def info(self):
            return {"engine": "slow-fake"}

    result = asyncio.run(
        JevDecisionRunner(SlowTagger()).choose(make_request()))
    assert result.duration_ms >= 10.0


# ── Step 1: adapter errors surface unchanged (engine maps them to nl_error) ─
def test_tagger_timeout_error_is_raised_unchanged():
    class TimeoutTagger:
        def score_choice(self, state, instructions, candidates):
            raise TimeoutError("jev forward timed out")

        def info(self):
            return {"engine": "timeout-fake"}

    with pytest.raises(TimeoutError) as excinfo:
        asyncio.run(JevDecisionRunner(TimeoutTagger()).choose(make_request()))
    assert "jev forward timed out" in str(excinfo.value)

    # a configured (but not yet reached) runner timeout must not mask it
    runner = JevDecisionRunner(TimeoutTagger(), timeout=5)
    with pytest.raises(TimeoutError) as excinfo:
        asyncio.run(runner.choose(make_request()))
    assert "jev forward timed out" in str(excinfo.value)


# ── Step 4: the serial gate survives a caller-visible timeout ──────────────
def test_timeout_keeps_serial_gate_until_thread_finishes():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class SlowTagger:
        def score_choice(self, state, instructions, candidates):
            calls.append(state)
            if len(calls) == 1:
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("test thread was not released")
            return {"probabilities": {"a": 0.6, "b": 0.4},
                    "input": {}, "temperature": 0.7}

        def info(self):
            return {"engine": "slow-fake"}

    async def scenario():
        runner = JevDecisionRunner(SlowTagger(), timeout=0.01)
        request = ChoiceRequest(
            state="request", instructions="choose",
            options=(ChoiceOption("a", "A"), ChoiceOption("b", "B")))
        first = asyncio.create_task(runner.choose(request))
        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(TimeoutError):
            await first
        runner._timeout = 1
        second = asyncio.create_task(runner.choose(request))
        try:
            await asyncio.wait_for(asyncio.shield(second), timeout=0.02)
            raise AssertionError("second call passed the gate before release")
        except TimeoutError:
            pass
        assert calls == ["request"]
        release.set()
        await asyncio.wait_for(second, timeout=1)
        assert calls == ["request", "request"]

    try:
        asyncio.run(scenario())
    finally:
        release.set()


# ── Step 3: the blocking JEV call runs off the event loop ──────────────────
def test_blocking_score_choice_runs_off_the_event_loop():
    class SlowTagger:
        def score_choice(self, state, instructions, candidates):
            time.sleep(0.03)
            return {"probabilities": {"a": 0.6, "b": 0.4}}

        def info(self):
            return {"engine": "slow-fake"}

    ticks = []

    async def scenario():
        runner = JevDecisionRunner(SlowTagger())
        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)
        ticker_task = asyncio.create_task(ticker())
        try:
            await runner.choose(make_request())
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task

    asyncio.run(scenario())
    assert len(ticks) >= 3
