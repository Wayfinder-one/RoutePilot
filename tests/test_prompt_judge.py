"""Unit tests for PromptJudgeTagger (prompt-driven signal feasibility).

Judge calls are mocked with httpx.MockTransport, so these run without a model.
The real accuracy comparison against the trained checkpoint lives in
``scripts/eval_prompt_judge.py`` (run on the GPU host with labeled test splits).
"""

import asyncio
import json

import httpx
import pytest

from router.signals.registry import SIGNALS
from router.signals.prompt_judge import (DEFAULT_PROMPTS_PATH, JUDGED_SIGNALS,
                                         MAX_INPUT_CHARS, PromptJudgeError,
                                         PromptJudgeTagger, build_system_prompt,
                                         load_prompt_config, parse_output)
from router.signals.tagger import TagRequest

VALID = {"signals": {
    "domain": {"value": "computer_science", "confidence": 0.9},
    "jailbreak": {"value": "benign", "confidence": 0.95},
    "fact_check": {"value": "no_check", "confidence": 0.9},
    "user_feedback": {"value": "satisfied", "confidence": 0.8},
    "modality": {"value": "text", "confidence": 0.95},
    "complexity": {"value": "hard", "confidence": 0.8},
    "pii": {"values": ["cn_id_card", "password"],
            "conf_by_value": {"cn_id_card": 0.9, "password": 0.8}},
}}


def chat_response(content: str, usage=None) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 40}})


def tagger(handler) -> PromptJudgeTagger:
    return PromptJudgeTagger(
        base_url="http://judge.test/v1", model="judge-model",
        transport=httpx.MockTransport(handler))


def tag(t: PromptJudgeTagger, text="用 Python 写快速排序", n_messages=1):
    return asyncio.run(t.tag(TagRequest(text=text, n_messages=n_messages)))


def test_default_config_and_system_prompt_covers_every_judged_signal():
    cfg = load_prompt_config()
    prompt = build_system_prompt(cfg)
    assert JUDGED_SIGNALS == ("jailbreak", "pii", "domain", "fact_check",
                              "modality", "complexity", "user_feedback")
    for key in JUDGED_SIGNALS:
        assert key in prompt
    # allowed value keys come from the registry, including all PII types
    assert "computer_science" in prompt and "needs_check" in prompt
    assert "cn_id_card" in prompt


def test_prompt_config_rejects_missing_signal(tmp_path):
    good = json.loads(DEFAULT_PROMPTS_PATH.read_text(encoding="utf-8"))
    good["signals"].pop("pii")
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(good))
    with pytest.raises(PromptJudgeError):
        load_prompt_config(p)


def test_parse_output_handles_think_and_code_fences():
    text = ("<think>让我想一想</think>\n```json\n"
            + json.dumps(VALID) + "\n```")
    signals = parse_output(text)
    assert signals["domain"]["value"] == "computer_science"
    assert signals["pii"]["value"] == ["cn_id_card", "password"]
    assert signals["pii"]["confidence"] == pytest.approx(0.9)


def test_parse_output_rejects_unknown_value():
    broken = json.loads(json.dumps(VALID))
    broken["signals"]["domain"]["value"] = "astrology"
    with pytest.raises(PromptJudgeError):
        parse_output(json.dumps(broken))


def test_parse_output_rejects_missing_signal_and_bad_confidence():
    missing = json.loads(json.dumps(VALID))
    missing["signals"].pop("modality")
    with pytest.raises(PromptJudgeError):
        parse_output(json.dumps(missing))
    bad_conf = json.loads(json.dumps(VALID))
    bad_conf["signals"]["domain"]["confidence"] = "high"
    with pytest.raises(PromptJudgeError):
        parse_output(json.dumps(bad_conf))


def test_tag_returns_full_pack_with_rule_signals_and_no_embedding():
    t = tagger(lambda req: chat_response(json.dumps(VALID)))
    result = tag(t)
    signals = result.signals
    # capability_route comes from the JEV decision model, not from this judge.
    assert set(signals) == set(SIGNALS) - {"capability_route"}
    assert signals["domain"] == {"value": "computer_science",
                                 "confidence": 0.9, "is_rule": False}
    assert signals["pii"]["value"] == ["cn_id_card", "password"]
    assert signals["pii"]["conf_by_value"]["password"] == pytest.approx(0.8)
    assert signals["language"]["is_rule"] is True        # rules still local
    assert signals["user_feedback"]["value"] is None     # single turn
    assert result.vec is None
    assert t.stats["calls"] == 1 and t.stats["errors"] == 0
    assert t.stats["prompt_tokens"] == 100


def test_tag_keeps_user_feedback_with_previous_turn():
    t = tagger(lambda req: chat_response(json.dumps(VALID)))
    signals = asyncio.run(t.tag(TagRequest(text="谢谢，正是我想要的",
                                           n_messages=3))).signals
    assert signals["user_feedback"]["value"] == "satisfied"


def test_tag_retries_once_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return chat_response("这不是 JSON")
        return chat_response(json.dumps(VALID))

    t = tagger(handler)
    signals = tag(t).signals
    assert signals["domain"]["value"] == "computer_science"
    assert t.stats["retries"] == 1 and t.stats["errors"] == 0
    assert calls["n"] == 2


def test_tag_raises_after_unusable_output():
    t = tagger(lambda req: chat_response("not json at all"))
    with pytest.raises(PromptJudgeError):
        tag(t)
    assert t.stats["errors"] == 1


def test_tag_truncates_judge_input_and_reports_char_window():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["messages"][1]["content"])
        return chat_response(json.dumps(VALID))

    t = tagger(handler)
    long_text = "写代码" * 3000                     # 9000 chars
    result = tag(t, text=long_text)
    assert seen[0] == long_text[:MAX_INPUT_CHARS]
    # the JEV key names, but counts are characters on this path
    assert result.input == {"input_tokens": len(long_text),
                            "input_visible_tokens": MAX_INPUT_CHARS,
                            "input_truncated": True}
    # rules still see the full text (context bucket by word count)
    assert result.signals["context"]["value"] == 3


def test_tag_short_input_is_not_marked_truncated():
    t = tagger(lambda req: chat_response(json.dumps(VALID)))
    result = tag(t)
    assert result.input == {"input_tokens": len("用 Python 写快速排序"),
                            "input_visible_tokens": len("用 Python 写快速排序"),
                            "input_truncated": False}


def test_failed_request_counts_one_error():
    t = tagger(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(PromptJudgeError):
        tag(t)
    assert t.stats["calls"] == 2 and t.stats["retries"] == 1
    assert t.stats["errors"] == 1
