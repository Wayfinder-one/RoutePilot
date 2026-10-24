"""Tests for the opt-in long-text PII chunking (2026-10-24 P2-A).

Covers the pure planner / aggregation helpers, the pii_rules character spans, the
calibration file format, and the end-to-end tagger path with a fake tokenizer and
scorer (no torch). The central regression promise: with chunking off (the
default) or a short request, signals and ``TagResult.input`` are byte-identical
to the pre-chunk single-window tagger.
"""

import asyncio
import json
import math

import pytest

from router.pipeline.scope import SignalScope
from router.signals.jev import (CHUNK_AGGS, CHUNK_OVERLAP, JevTaggerError,
                                aggregate_presence, load_chunk_calibration,
                                plan_pii_segments)
from router.signals.pii_rules import rule_pii_hits, rule_pii_spans
from router.signals.tagger import TagRequest
from tests.test_jev import (FakeTokenizer, probs_for, real_tagger,
                            stub_tagger)

#: A checksum-valid Chinese ID card used as the middle-of-text needle.
NEEDLE = "11010519491231002X"
VALID_CJK_ID = "11010519491231002X"


def _long_text(prefix=100, suffix=100, needle=NEEDLE):
    filler = "".join(chr(0x4E00 + (i % 500)) for i in range(prefix))
    tail = "".join(chr(0x4E00 + (i % 500)) for i in range(suffix))
    return filler + needle + tail


def _no_rule_long_text(total=200):
    """A long text with no PII regex hits (so only model evidence changes)."""
    return "".join(chr(0x4E00 + (i % 500)) for i in range(total))


# --------------------------------------------------------------------------- #
# pii_rules.rule_pii_spans
# --------------------------------------------------------------------------- #
def test_rule_pii_spans_returns_character_offsets():
    text = "联系我 someone@example.com，身份证号 " + VALID_CJK_ID + "，密码: abc"
    spans = rule_pii_spans(text)
    types = {typ for typ, _, _ in spans}
    assert {"email_address", "cn_id_card", "password"} <= types
    for typ, start, end in spans:
        assert 0 <= start < end <= len(text)
        assert typ in types
    # deterministic ordering by (start, end, type)
    assert spans == sorted(spans, key=lambda item: (item[1], item[2], item[0]))
    assert rule_pii_spans("") == []
    # the hit-type view is unchanged and derived from the same matches
    assert set(rule_pii_hits(text)) == types


def test_rule_pii_spans_only_report_checksum_valid_ids():
    invalid = "123456789012345678"
    valid = VALID_CJK_ID
    assert [typ for typ, _, _ in rule_pii_spans(invalid)
            if typ == "cn_id_card"] == []
    vs = [typ for typ, _, _ in rule_pii_spans("身份证 " + valid)
          if typ == "cn_id_card"]
    assert vs == ["cn_id_card"]
    # multiple occurrences are all reported (planner wants every hit)
    both = rule_pii_spans("a " + valid + " b " + valid)
    assert sum(1 for typ, _, _ in both if typ == "cn_id_card") == 2


# --------------------------------------------------------------------------- #
# plan_pii_segments
# --------------------------------------------------------------------------- #
def test_plan_head_tail_and_uniform_segments():
    assert CHUNK_OVERLAP == 0.25
    tokenizer = FakeTokenizer()
    text = _no_rule_long_text(1000)
    segments = plan_pii_segments(text, tokenizer, size=100, max_segments=4)
    assert len(segments) == 4
    reasons = [segment["reason"] for segment in segments]
    assert reasons.count("head") == 1
    assert reasons.count("tail") == 1
    assert reasons.count("uniform") == 2
    assert segments[0]["start"] == 0 and segments[0]["end"] == 100
    assert segments[-1]["start"] == 900 and segments[-1]["end"] == 1000
    # sorted by start, within budget, no empty segment
    starts = [segment["start"] for segment in segments]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))
    for segment in segments:
        assert 0 <= segment["start"] < segment["end"] <= 1000
        assert segment["end"] - segment["start"] <= 100
    # deterministic
    assert plan_pii_segments(text, tokenizer, size=100, max_segments=4) == segments


def test_plan_rule_span_gets_a_covering_segment():
    tokenizer = FakeTokenizer()
    needle_pos = 500
    text = _no_rule_long_text(1000)
    rule_spans = [("email_address", needle_pos, needle_pos + 20)]
    segments = plan_pii_segments(text, tokenizer, size=100, max_segments=6,
                                 rule_spans=rule_spans)
    rule_segments = [s for s in segments if s["reason"].startswith("rule:")]
    assert rule_segments
    assert any(s["start"] <= needle_pos < s["end"] for s in rule_segments)
    # an auto-detected rule (rule_spans=None) also plans a rule segment
    auto = plan_pii_segments("aa " + VALID_CJK_ID + " bb",
                             tokenizer, size=10, max_segments=4)
    assert any(s["reason"] == "rule:cn_id_card" for s in auto)


def test_plan_dedupes_near_identical_windows():
    tokenizer = FakeTokenizer()
    text = _no_rule_long_text(1000)
    # a rule at the very start produces the same window as the head segment
    segments = plan_pii_segments(text, tokenizer, size=100, max_segments=4,
                                 rule_spans=[("email_address", 0, 5)])
    assert len(segments) == 4
    assert sum(1 for s in segments if s["start"] == 0) == 1
    assert all(s["reason"] != "rule:email_address" for s in segments)


def test_plan_budget_and_edge_cases():
    tokenizer = FakeTokenizer()
    text = _no_rule_long_text(400)
    # budget honoured
    for budget in (1, 2, 3, 5):
        segments = plan_pii_segments(text, tokenizer, size=50,
                                     max_segments=budget)
        assert 1 <= len(segments) <= budget
        assert segments[0]["reason"] == "head"
        assert all(s["end"] - s["start"] <= 50 for s in segments)
    # zero size / zero budget / empty text
    assert plan_pii_segments(text, tokenizer, size=0, max_segments=4) == []
    assert plan_pii_segments(text, tokenizer, size=50, max_segments=0) == []
    assert plan_pii_segments("", tokenizer, size=50, max_segments=4) == []
    # supplied ids are equivalent to tokenising inside the planner
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    assert plan_pii_segments(text, tokenizer, size=50, max_segments=3,
                             ids=ids) == plan_pii_segments(
        text, tokenizer, size=50, max_segments=3)
    # a text no longer than one window: head only (tail is the same window)
    short = plan_pii_segments(_no_rule_long_text(10), tokenizer, size=100,
                              max_segments=4)
    assert len(short) == 1 and short[0]["reason"] == "head"


# --------------------------------------------------------------------------- #
# aggregate_presence
# --------------------------------------------------------------------------- #
def test_aggregate_presence_noisy_or():
    assert aggregate_presence([0.5, 0.5]) == pytest.approx(0.75)
    assert aggregate_presence([0.9]) == pytest.approx(0.9)
    assert aggregate_presence([0.0, 0.0]) == 0.0
    assert aggregate_presence([1.0]) == 1.0
    assert aggregate_presence([]) == 0.0
    # values are clamped into [0, 1]
    assert aggregate_presence([1.5, -0.5]) == pytest.approx(1.0)


def test_aggregate_presence_logsumexp_and_max():
    # single segment logsumexp reduces to the probability itself
    assert aggregate_presence([0.8], method="logsumexp") == pytest.approx(0.8)
    # identical segments: the -log(n) normalisation keeps the same probability
    assert aggregate_presence([0.8, 0.8], method="logsumexp") == pytest.approx(0.8)
    # mixed evidence follows the documented formula
    probs = [0.9, 0.6]
    logits = [math.log(p / (1 - p)) for p in probs]
    top = max(logits)
    lse = top + math.log(sum(math.exp(value - top) for value in logits))
    expected = 1.0 / (1.0 + math.exp(-(lse - math.log(len(probs)))))
    assert aggregate_presence(probs, method="logsumexp") == pytest.approx(expected)
    # max is the control arm: it cannot exceed the strongest segment
    assert aggregate_presence([0.2, 0.9, 0.4], method="max") == pytest.approx(0.9)
    assert aggregate_presence([], method="max") == 0.0
    with pytest.raises(ValueError):
        aggregate_presence([0.9], method="softmax")


def test_aggregate_presence_bias_scale_monotonic():
    probs = [0.4, 0.6]
    for method in CHUNK_AGGS:
        low = aggregate_presence(probs, method=method, bias=-2.0)
        mid = aggregate_presence(probs, method=method)
        high = aggregate_presence(probs, method=method, bias=2.0)
        assert low < mid < high
    # scale sharpens the calibration (probability above .5 moves up)
    plain = aggregate_presence([0.9], method="noisy_or")
    sharp = aggregate_presence([0.9], method="noisy_or", scale=2.0)
    assert sharp > plain
    # the documented logsumexp bias lives inside the sigmoid
    with_bias = aggregate_presence([0.8, 0.8], method="logsumexp", bias=1.5)
    assert with_bias > aggregate_presence([0.8, 0.8], method="logsumexp")


# --------------------------------------------------------------------------- #
# calibration file format
# --------------------------------------------------------------------------- #
def test_load_chunk_calibration_accepts_dicts_and_files(tmp_path):
    assert load_chunk_calibration(None) == ({}, "none")
    assert load_chunk_calibration("") == ({}, "none")
    assert load_chunk_calibration({}) == ({}, "none")
    mapping, desc = load_chunk_calibration(
        {"*": {"bias": 0.5}, "cn_id_card": {"bias": 1, "scale": 2.0}})
    assert desc == "inline"
    assert mapping["*"]["bias"] == pytest.approx(0.5)       # missing scale -> 1
    assert mapping["*"]["scale"] == pytest.approx(1.0)
    assert mapping["cn_id_card"] == {"bias": 1.0, "scale": 2.0}
    path = tmp_path / "calib.json"
    path.write_text(json.dumps({"cn_id_card": {"bias": -1.0}}), encoding="utf-8")
    mapping, desc = load_chunk_calibration(str(path))
    assert desc == str(path)
    assert mapping["cn_id_card"] == {"bias": -1.0, "scale": 1.0}


def test_load_chunk_calibration_rejects_bad_input(tmp_path):
    with pytest.raises(JevTaggerError):
        load_chunk_calibration(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(JevTaggerError):
        load_chunk_calibration(str(bad))
    with pytest.raises(JevTaggerError):
        load_chunk_calibration([1, 2])
    with pytest.raises(JevTaggerError):
        load_chunk_calibration({"cn_id_card": 1.0})
    with pytest.raises(JevTaggerError):
        load_chunk_calibration({"cn_id_card": {"bias": "oops"}})


# --------------------------------------------------------------------------- #
# end-to-end tagger path (fake tokenizer + fake scorer)
# --------------------------------------------------------------------------- #
def _chunk_scorer(records):
    """Scorer for the chunk integration tests.

    PII cn_id_card is positive only when the segment state contains the needle;
    every other signal gets a deterministic flat distribution.
    """
    out = []
    for record in records:
        if record["kind"] == "choice":
            n = len(record["answer_keys"])
            out.append([1.0 / n] * n)
        elif "身份证号" in record["question"]:
            out.append([0.1, 0.9] if NEEDLE in record["state"] else [0.9, 0.1])
        else:
            out.append([0.9, 0.1])
    return out


def _long_head_only_scorer(records):
    """Scorer that only ever sees the windowed head (chunk off comparison)."""
    return _chunk_scorer(records)


def test_chunk_finds_the_middle_needle_head_only_misses_it(monkeypatch):
    long_text = _long_text(100, 100)
    # head-only (chunk off): the needle is outside the 32-token head window
    head_only = stub_tagger(max_length=32, chunk_segments=0)
    monkeypatch.setattr(head_only, "_score", _long_head_only_scorer)
    head_result = asyncio.run(head_only.tag(TagRequest(text=long_text,
                                                       n_messages=1)))
    assert head_result.signals["pii"]["model_hits"] == []
    assert head_result.signals["pii"]["model_p_by_type"]["cn_id_card"] < 0.5
    assert "chunk_segments" not in head_result.input

    # chunked: the rule span plans a segment around the middle needle
    chunked = stub_tagger(max_length=32, chunk_segments=4, chunk_size=24,
                          chunk_agg="noisy_or")
    monkeypatch.setattr(chunked, "_score", _long_head_only_scorer)
    chunk_result = asyncio.run(chunked.tag(TagRequest(text=long_text,
                                                      n_messages=1)))
    pii = chunk_result.signals["pii"]
    assert "cn_id_card" in pii["model_hits"]
    assert pii["model_p_by_type"]["cn_id_card"] > 0.5
    assert "cn_id_card" in pii["value"]            # model + rule union
    # chunk stats are reported in the request input
    for key in ("chunk_segments", "chunk_scored_segments",
                "chunk_prompt_tokens", "chunk_strategy"):
        assert key in chunk_result.input
    assert chunk_result.input["chunk_segments"] >= 4
    assert chunk_result.input["chunk_scored_segments"] == \
        chunk_result.input["chunk_segments"]
    assert chunk_result.input["chunk_prompt_tokens"] > 0
    assert chunk_result.input["chunk_strategy"].endswith(":" + "noisy_or")
    # the window stats still describe the single window for the other signals
    assert chunk_result.input["input_window"] == "32:0"
    # non-PII signals are identical between the two paths
    for key, value in head_result.signals.items():
        if key != "pii":
            assert chunk_result.signals[key] == value


def test_chunk_short_text_is_byte_identical_to_chunk_off(monkeypatch):
    short_text = "你好，帮我写一段 Python 快排。"
    off = stub_tagger(max_length=1024, chunk_segments=0)
    on = stub_tagger(max_length=1024, chunk_segments=4, chunk_size=24)
    monkeypatch.setattr(off, "_score", probs_for)
    monkeypatch.setattr(on, "_score", probs_for)
    off_result = asyncio.run(off.tag(TagRequest(text=short_text, n_messages=1)))
    on_result = asyncio.run(on.tag(TagRequest(text=short_text, n_messages=1)))
    assert on_result.signals == off_result.signals
    assert on_result.input == off_result.input          # no chunk keys
    assert "chunk_segments" not in on_result.input


def test_chunk_only_asks_the_scope_pii_types(monkeypatch):
    long_text = _long_text(100, 100)
    tagger = stub_tagger(max_length=32, chunk_segments=4, chunk_size=24)
    calls: list[list[dict]] = []

    def capture(records):
        calls.append(list(records))
        return _chunk_scorer(records)

    monkeypatch.setattr(tagger, "_score", capture)
    scope = SignalScope(signals=frozenset({"pii"}),
                        pii_types=frozenset({"cn_id_card"}),
                        reason="test")
    result = asyncio.run(tagger.tag(TagRequest(text=long_text, n_messages=1,
                                               scope=scope)))
    # call 0 = the six non-PII signals (here: none), call 1 = staged PII records
    assert len(calls) == 2
    staged = calls[1]
    segments = result.input["chunk_segments"]
    assert len(staged) == segments                       # one type per segment
    assert all("身份证号" in record["question"] for record in staged)
    assert all(record["state"] != long_text for record in staged)
    assert "cn_id_card" in result.signals["pii"]["model_hits"]


def test_chunk_does_not_run_when_pii_is_out_of_scope(monkeypatch):
    long_text = _no_rule_long_text(200)
    tagger = stub_tagger(max_length=32, chunk_segments=4, chunk_size=24)
    calls: list[int] = []

    def capture(records):
        calls.append(len(records))
        return probs_for(records)

    monkeypatch.setattr(tagger, "_score", capture)
    scope = SignalScope(signals=frozenset({"domain"}),
                        pii_types=frozenset(), reason="test")
    result = asyncio.run(tagger.tag(TagRequest(text=long_text, n_messages=1,
                                               scope=scope)))
    assert calls == [1]                                  # only the window pass
    assert "chunk_segments" not in result.input
    assert "pii" not in result.signals


def test_chunk_calibration_bias_lifts_logsumexp_probability(monkeypatch):
    long_text = _long_text(100, 100)

    def low_scorer(records):
        out = []
        for record in records:
            n = len(record["answer_keys"])
            out.append([1.0 / n] * n if record["kind"] == "choice"
                       else [0.7, 0.3])
        return out

    def aggregate_p(tagger):
        monkeypatch.setattr(tagger, "_score", low_scorer)
        result = asyncio.run(tagger.tag(TagRequest(text=long_text,
                                                   n_messages=1)))
        return result.signals["pii"]["model_p_by_type"]["cn_id_card"]

    plain = stub_tagger(max_length=32, chunk_segments=4, chunk_size=24,
                        chunk_agg="logsumexp")
    biased = stub_tagger(max_length=32, chunk_segments=4, chunk_size=24,
                         chunk_agg="logsumexp",
                         chunk_calibration={"*": {"bias": 2.0}})
    plain_p = aggregate_p(plain)
    biased_p = aggregate_p(biased)
    assert biased_p > plain_p
    assert 0.0 <= plain_p <= 1.0 and 0.0 <= biased_p <= 1.0
    # the calibration config is part of the tagger version snapshot
    assert biased.info()["chunk_calibration"] == "inline"
    assert plain.info()["chunk_calibration"] == "none"


def test_chunk_aggregation_max_is_available_as_control(monkeypatch):
    long_text = _long_text(100, 100)
    tagger = stub_tagger(max_length=32, chunk_segments=4, chunk_size=24,
                         chunk_agg="max")
    monkeypatch.setattr(tagger, "_score", _chunk_scorer)
    result = asyncio.run(tagger.tag(TagRequest(text=long_text, n_messages=1)))
    assert "cn_id_card" in result.signals["pii"]["model_hits"]
    assert result.input["chunk_strategy"].endswith(":max")


def test_tagger_rejects_invalid_chunk_config(tmp_path, monkeypatch):
    with pytest.raises(JevTaggerError):
        real_tagger(tmp_path, monkeypatch, chunk_segments=-1)
    with pytest.raises(JevTaggerError):
        real_tagger(tmp_path, monkeypatch, chunk_segments=4, chunk_size=0)
    with pytest.raises(JevTaggerError):
        # chunk_size > max_length is only invalid when chunking is enabled
        real_tagger(tmp_path, monkeypatch, max_length=64, chunk_segments=4,
                    chunk_size=128)
    with pytest.raises(JevTaggerError):
        real_tagger(tmp_path, monkeypatch, chunk_segments=4, chunk_agg="softmax")
    with pytest.raises(JevTaggerError):
        real_tagger(tmp_path, monkeypatch, chunk_segments=4,
                    chunk_calibration={"cn_id_card": {"bias": "x"}})
    # disabled chunking must NOT fail on a chunk_size larger than max_length
    tagger = real_tagger(tmp_path, monkeypatch, max_length=64,
                         chunk_segments=0, chunk_size=128)
    assert tagger.chunk_enabled is False
    assert tagger.info()["chunk_agg"] == "off"


def test_settings_chunk_env_contract(monkeypatch):
    """A1: ROUTER_JEV_CHUNK_* env -> Settings fields (0/512/noisy_or/"" default)."""
    from router.config import Settings

    for env in ("ROUTER_JEV_CHUNK_SEGMENTS", "ROUTER_JEV_CHUNK_SIZE",
                "ROUTER_JEV_CHUNK_AGG", "ROUTER_JEV_CHUNK_CALIBRATION"):
        monkeypatch.delenv(env, raising=False)
    defaults = Settings.from_env()
    assert defaults.jev_chunk_segments == 0
    assert defaults.jev_chunk_size == 512
    assert defaults.jev_chunk_agg == "noisy_or"
    assert defaults.jev_chunk_calibration == ""

    monkeypatch.setenv("ROUTER_JEV_CHUNK_SEGMENTS", "4")
    monkeypatch.setenv("ROUTER_JEV_CHUNK_SIZE", "256")
    monkeypatch.setenv("ROUTER_JEV_CHUNK_AGG", "logsumexp")
    monkeypatch.setenv("ROUTER_JEV_CHUNK_CALIBRATION", "/tmp/calib.json")
    configured = Settings.from_env()
    assert configured.jev_chunk_segments == 4
    assert configured.jev_chunk_size == 256
    assert configured.jev_chunk_agg == "logsumexp"
    assert configured.jev_chunk_calibration == "/tmp/calib.json"


def test_real_constructor_info_records_enabled_chunk_config(tmp_path, monkeypatch):
    tagger = real_tagger(tmp_path, monkeypatch, max_length=64,
                         chunk_segments=4, chunk_size=32,
                         chunk_agg="logsumexp",
                         chunk_calibration={"*": {"bias": 0.5}})
    info = tagger.info()
    assert info["chunk_segments"] == 4
    assert info["chunk_size"] == 32
    assert info["chunk_agg"] == "logsumexp"
    assert info["chunk_overlap"] == 0.25
    assert info["chunk_calibration"] == "inline"
    assert tagger.chunk_enabled is True


def test_app_passes_chunk_settings_to_the_tagger(monkeypatch):
    """A1: Settings -> app._make_tagger -> JevTagger constructor kwargs."""
    import router.signals.jev as jev_module
    from router.api import app as app_module
    from router.config import Settings

    captured: dict = {}

    class _FakeJevTagger:
        def __init__(self, model_dir, **kwargs):
            captured["model_dir"] = model_dir
            captured.update(kwargs)

    monkeypatch.setattr(jev_module, "JevTagger", _FakeJevTagger)
    settings = Settings(tagger="jev", jev_model="/fake/model",
                        jev_chunk_segments=4, jev_chunk_size=256,
                        jev_chunk_agg="logsumexp",
                        jev_chunk_calibration="/tmp/calib.json")
    app_module._make_tagger(settings, {})
    assert captured["model_dir"] == "/fake/model"
    assert captured["chunk_segments"] == 4
    assert captured["chunk_size"] == 256
    assert captured["chunk_agg"] == "logsumexp"
    assert captured["chunk_calibration"] == "/tmp/calib.json"

    # chunking off + empty calibration path -> None calibration, defaults kept
    captured.clear()
    app_module._make_tagger(Settings(tagger="jev", jev_model="/fake/model"), {})
    assert captured["chunk_segments"] == 0
    assert captured["chunk_size"] == 512
    assert captured["chunk_agg"] == "noisy_or"
    assert captured["chunk_calibration"] is None
