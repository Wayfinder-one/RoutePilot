"""Natural-language capability routing: pure logic (no model required)."""

import json

import pytest

from router.decision.capability import (CapabilityConfigError, build_choice_record,
                                        decide, fingerprint, load_capability_config,
                                        rank, score_with_tagger,
                                        validate_capability_config)

CANDIDATES = {
    "qwen3-8b": "适合短翻译、字段抽取、事实问答和基础算术",
    "qwen3-32b": "适合带约束的算法实现、SQL 编写和局部代码调试",
    "deepseek-v3": "适合复杂推理、带测试的并发代码和分布式架构设计",
}


def cfg(**decision):
    raw = {"candidates": dict(CANDIDATES)}
    if decision:
        raw["decision"] = decision
    return validate_capability_config(raw)


def test_validate_defaults():
    out = cfg()
    assert out["version"] == 1
    assert out["decision"]["min_prob"] == pytest.approx(0.45)
    assert out["decision"]["min_margin"] == pytest.approx(0.10)
    assert out["decision"]["fallback_model"] is None
    assert out["decision"]["instructions"]
    assert out["candidates"] == CANDIDATES


def test_validate_requires_two_candidates():
    with pytest.raises(CapabilityConfigError):
        validate_capability_config({"candidates": {"only": "x"}})


def test_validate_rejects_empty_capability():
    with pytest.raises(CapabilityConfigError):
        validate_capability_config({"candidates": {"a": "x", "b": "  "}})


def test_validate_rejects_out_of_range_threshold():
    with pytest.raises(CapabilityConfigError):
        cfg(min_prob=1.5)


def test_validate_rejects_bool_version():
    with pytest.raises(CapabilityConfigError):
        validate_capability_config({"version": True, "candidates": dict(CANDIDATES)})


def test_load_config_from_file(tmp_path):
    path = tmp_path / "cap.json"
    path.write_text(json.dumps({"decision": {"fallback_model": "deepseek-v3"},
                                "candidates": dict(CANDIDATES)}), encoding="utf-8")
    out = load_capability_config(path)
    assert out["decision"]["fallback_model"] == "deepseek-v3"
    assert out["source"] == str(path)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(CapabilityConfigError):
        load_capability_config(tmp_path / "nope.json")


def test_build_choice_record_matches_jev_render_contract():
    record = build_choice_record("把 Good morning 翻译成中文", cfg())
    assert record["kind"] == "choice"
    assert record["state"] == "把 Good morning 翻译成中文"
    assert record["question"] == cfg()["decision"]["instructions"]
    assert record["answer_keys"] == list(CANDIDATES)
    assert record["options"] == [
        f"{model}: {capability}" for model, capability in CANDIDATES.items()]


def test_rank_descending():
    ranked = rank({"a": 0.2, "b": 0.7, "c": 0.1})
    assert [row["model"] for row in ranked] == ["b", "a", "c"]
    assert ranked[0]["p"] == pytest.approx(0.7)


def test_decide_picks_top_when_confident():
    out = decide(cfg(), {"qwen3-8b": 0.9, "qwen3-32b": 0.08, "deepseek-v3": 0.02})
    assert out["model"] == "qwen3-8b"
    assert out["fallback_used"] is False
    assert out["reason"] == "decision_model"
    assert out["runner_up"] == "qwen3-32b"
    assert out["margin"] == pytest.approx(0.82)


def test_decide_low_probability_falls_back():
    out = decide(cfg(min_prob=0.8, fallback_model="deepseek-v3"),
                 {"qwen3-8b": 0.5, "qwen3-32b": 0.3, "deepseek-v3": 0.2})
    assert out["model"] == "deepseek-v3"
    assert out["fallback_used"] is True
    assert out["reason"] == "low_confidence"
    assert out["top_model"] == "qwen3-8b"


def test_decide_ambiguous_margin_falls_back():
    out = decide(cfg(min_prob=0.3, min_margin=0.2,
                     fallback_model="deepseek-v3"),
                 {"qwen3-8b": 0.42, "qwen3-32b": 0.40, "deepseek-v3": 0.18})
    assert out["model"] == "deepseek-v3"
    assert out["reason"] == "ambiguous"
    assert out["margin"] == pytest.approx(0.02)


def test_decide_without_fallback_keeps_top():
    out = decide(cfg(), {"qwen3-8b": 0.34, "qwen3-32b": 0.33, "deepseek-v3": 0.33})
    assert out["model"] == "qwen3-8b"
    assert out["fallback_used"] is False


def test_decide_score_failure_uses_fallback():
    out = decide(cfg(fallback_model="deepseek-v3"), None,
                 error=RuntimeError("gpu oom"))
    assert out["model"] == "deepseek-v3"
    assert out["reason"] == "score_failed"
    assert "gpu oom" in out["error"]
    assert out["ranking"] == []


def test_fingerprint_tracks_capability_and_ignores_dict_order():
    left = validate_capability_config({"candidates": {"a": "x", "b": "y", "c": "z"}})
    right = validate_capability_config({"candidates": {"c": "z", "a": "x", "b": "y"}})
    assert fingerprint(left) == fingerprint(right)
    changed = validate_capability_config({"candidates": {"a": "x", "b": "y2", "c": "z"}})
    assert fingerprint(left) != fingerprint(changed)


class FakeTagger:
    def __init__(self, probabilities=None, exc=None):
        self.probabilities = probabilities
        self.exc = exc
        self.calls = []

    def score_choice(self, state, instructions, candidates):
        self.calls.append((state, instructions, dict(candidates)))
        if self.exc is not None:
            raise self.exc
        return {"probabilities": self.probabilities,
                "input": {"input_tokens": 10, "input_truncated": False}}


def test_score_with_tagger_passes_operator_config():
    tagger = FakeTagger({"qwen3-8b": 0.8, "qwen3-32b": 0.1, "deepseek-v3": 0.1})
    config = cfg(fallback_model="qwen3-32b")
    out = score_with_tagger(tagger, config, "hello")
    assert out["model"] == "qwen3-8b"
    assert out["input"] == {"input_tokens": 10, "input_truncated": False}
    (state, instructions, candidates), = tagger.calls
    assert state == "hello"
    assert instructions == config["decision"]["instructions"]
    assert candidates == config["candidates"]


def test_score_with_tagger_fail_open_on_exception():
    tagger = FakeTagger(exc=RuntimeError("model crashed"))
    out = score_with_tagger(tagger, cfg(fallback_model="deepseek-v3"), "hello")
    assert out["model"] == "deepseek-v3"
    assert out["fallback_used"] is True
    assert out["reason"] == "score_failed"
    assert "model crashed" in out["error"]
