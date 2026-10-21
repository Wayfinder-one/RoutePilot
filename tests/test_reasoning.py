"""推理能力声明 / 归一化 / 上行协议派发（契约 §4）。

覆盖：公开常量契约、profile 校验（9 种 format 的 needs_wire / supports_sre 规则）、
归一化的「绝不抛」语义、以及 9 种 format 的注入 / off / 剥离行为。
"""

import pytest

from router.reasoning import (FORMATS, FORMAT_IDS, LEVELS, ReasoningError,
                              apply_reasoning, level_label, normalize_reasoning,
                              validate_model_profile)


def capability(fmt: str, efforts: dict, **extra) -> dict:
    """A validated + normalized capability object for one format."""
    profile = {"reasoning": {"format": fmt, "efforts": efforts, **extra}}
    normalized = normalize_reasoning(profile)
    assert normalized is not None and normalized["declared"] and normalized["supported"]
    return normalized


# --------------------------------------------------------------------------- #
# 公共契约
# --------------------------------------------------------------------------- #
def test_public_constants_are_the_contract():
    assert LEVELS == ("off", "on", "minimal", "low", "medium", "high",
                      "xhigh", "max")
    assert FORMAT_IDS == ("openai", "deepseek", "qwen", "qwen-chat-template",
                          "openrouter", "zai", "together", "string-thinking",
                          "ant-ling")
    assert tuple(FORMATS) == FORMAT_IDS
    assert {spec.id for spec in FORMATS.values()} == set(FORMAT_IDS)
    for spec in FORMATS.values():
        assert spec.zh and spec.en          # console renders these descriptions
    assert level_label("high") == "高"
    assert level_label("unknown") == "unknown"


# --------------------------------------------------------------------------- #
# validate_model_profile
# --------------------------------------------------------------------------- #
def test_validate_keeps_top_level_and_efforts_order():
    raw = {"domains": ["health"], "tier": "large",
           "reasoning": {"format": "deepseek",
                          "efforts": {"off": None, "high": "high", "max": "max"},
                          "default": "high", "supports_reasoning_effort": True}}
    got = validate_model_profile(raw)
    assert got["domains"] == ["health"] and got["tier"] == "large"
    reasoning = got["reasoning"]
    assert list(reasoning["efforts"]) == ["off", "high", "max"]   # ordered map
    assert reasoning["format"] == "deepseek"
    assert reasoning["default"] == "high"
    assert reasoning["supports_reasoning_effort"] is True


def test_validate_inherits_provider_format_without_materializing_it():
    got = validate_model_profile(
        {"reasoning": {"efforts": {"off": None, "high": "high"}}}, "deepseek")
    assert "format" not in got["reasoning"]        # provider format stays the default
    # ... but validation uses the provider format: openai needs a wire
    with pytest.raises(ReasoningError):
        validate_model_profile(
            {"reasoning": {"efforts": {"off": None, "high": None}}}, "openai")


def test_validate_reasoning_false_and_null():
    assert validate_model_profile({"reasoning": False})["reasoning"] is False
    assert "reasoning" not in validate_model_profile({"reasoning": None})
    assert validate_model_profile({}) == {}
    assert validate_model_profile({"domains": []}) == {"domains": []}


def test_validate_rejects_non_object_profile():
    with pytest.raises(ReasoningError):
        validate_model_profile(["not", "a", "dict"])


@pytest.mark.parametrize("reasoning", [
    True, "yes", 1, [],
    {"format": "nope", "efforts": {"high": "high"}},
    {"efforts": {}},
    {"efforts": {"off": None}},                              # only off -> use false
    {"format": "big", "efforts": {"big": "x"}},              # unknown level id
    {"format": "openai", "efforts": {"off": None, "high": None}},   # needs_wire
    {"format": "openrouter", "efforts": {"high": None}},
    {"format": "string-thinking", "efforts": {"high": None}},
    {"format": "ant-ling", "efforts": {"high": None}},
    {"format": "deepseek", "efforts": {"high": 1}},          # wire must be str|null
    {"format": "deepseek", "efforts": {"high": ""}},         # empty wire
    {"format": "deepseek", "efforts": {"high": "high"}, "default": "max"},
    {"format": "deepseek", "efforts": {"high": "high"}, "bogus": 1},
    {"format": "qwen", "efforts": {"on": None},
     "supports_reasoning_effort": True},                     # only SRE formats allow it
    {"format": "deepseek", "efforts": {"high": "high"},
     "supports_reasoning_effort": "yes"},
])
def test_validate_rejects_illegal_declarations(reasoning):
    with pytest.raises(ReasoningError):
        validate_model_profile({"reasoning": reasoning})


def test_validate_sre_formats_accept_the_compat_switch():
    for fmt in ("openai", "deepseek", "zai", "together"):
        got = validate_model_profile(
            {"reasoning": {"format": fmt, "efforts": {"high": "high"},
                           "supports_reasoning_effort": False}})
        assert got["reasoning"]["supports_reasoning_effort"] is False


# --------------------------------------------------------------------------- #
# normalize_reasoning（绝不抛）
# --------------------------------------------------------------------------- #
def test_normalize_missing_profiles_return_none():
    assert normalize_reasoning(None) is None
    assert normalize_reasoning({}) is None
    assert normalize_reasoning({"domains": ["health"]}) is None
    assert normalize_reasoning({"reasoning": None}) is None


def test_normalize_reasoning_false_shape():
    assert normalize_reasoning({"reasoning": False}) == {
        "declared": True, "supported": False, "format": "openai",
        "efforts": [], "default": None}


def test_normalize_declared_shape():
    got = normalize_reasoning({"reasoning": {
        "format": "deepseek",
        "efforts": {"off": None, "high": "high"},
        "default": "high",
        "supports_reasoning_effort": False}})
    assert got == {
        "declared": True, "supported": True, "format": "deepseek",
        "efforts": [{"id": "off", "wire": None}, {"id": "high", "wire": "high"}],
        "default": "high", "supports_reasoning_effort": False}


def test_normalize_uses_provider_format_when_not_declared():
    got = normalize_reasoning(
        {"reasoning": {"efforts": {"off": None, "high": None}}}, "deepseek")
    assert got["format"] == "deepseek"
    assert got["supports_reasoning_effort"] is True
    # unknown provider format falls back to openai instead of raising
    got = normalize_reasoning({"reasoning": False}, "bogus-format")
    assert got["format"] == "openai"


def test_normalize_invalid_declaration_is_lenient():
    got = normalize_reasoning(
        {"reasoning": {"format": "nope", "efforts": {"high": "high"}}}, "deepseek")
    assert got["declared"] is False and got["supported"] is None
    assert got["format"] == "deepseek" and got["efforts"] == []
    assert got["default"] is None and got["error"]


@pytest.mark.parametrize("profile", [
    None, {}, [], "x", 3, {"reasoning": 3}, {"reasoning": True},
    {"reasoning": {"format": None}}, {"reasoning": {"efforts": 7}},
    {"reasoning": {"efforts": {"high": {"nested": 1}}}},
])
def test_normalize_never_raises(profile):
    got = normalize_reasoning(profile, "openai")
    assert got is None or isinstance(got, dict)


# --------------------------------------------------------------------------- #
# apply_reasoning：9 种 format 的注入（非 off）
# --------------------------------------------------------------------------- #
def test_apply_openai():
    cap = capability("openai", {"off": None, "high": "high", "max": "maximum"})
    body = apply_reasoning(
        {"model": "m", "reasoning_effort": "client", "thinking": "client"}, cap, "max")
    assert body["reasoning_effort"] == "maximum"
    assert "thinking" not in body
    # off with a null wire only strips (no injection)
    assert "reasoning_effort" not in apply_reasoning(
        {"reasoning_effort": "client"}, cap, "off")
    # off with a string wire sends that spelling
    cap2 = capability("openai", {"off": "none", "high": "high"})
    assert apply_reasoning({}, cap2, "off")["reasoning_effort"] == "none"


def test_apply_deepseek():
    cap = capability("deepseek", {"off": None, "high": "high", "max": None})
    got = apply_reasoning({"thinking": "client"}, cap, "max")
    assert got["thinking"] == {"type": "enabled"}
    assert got["reasoning_effort"] == "max"      # null wire falls back to the level id
    assert apply_reasoning({}, cap, "off") == {"thinking": {"type": "disabled"}}
    # supports_reasoning_effort=false drops the extra reasoning_effort
    no_sre = capability("deepseek", {"off": None, "high": "high"},
                        supports_reasoning_effort=False)
    got = apply_reasoning({}, no_sre, "high")
    assert got == {"thinking": {"type": "enabled"}}


def test_apply_qwen_and_chat_template():
    cap = capability("qwen", {"off": None, "on": None})
    assert apply_reasoning({}, cap, "on") == {"enable_thinking": True}
    assert apply_reasoning({}, cap, "off") == {"enable_thinking": False}
    assert apply_reasoning({"enable_thinking": False}, cap, "on")[
        "enable_thinking"] is True

    tpl = capability("qwen-chat-template", {"off": None, "on": None})
    got = apply_reasoning(
        {"chat_template_kwargs": {"enable_thinking": False, "foo": 1}}, tpl, "on")
    assert got["chat_template_kwargs"] == {"foo": 1, "enable_thinking": True}
    got = apply_reasoning({"chat_template_kwargs": {"foo": 1}}, tpl, "off")
    assert got["chat_template_kwargs"] == {"foo": 1, "enable_thinking": False}
    # other formats strip the sub-key and drop an empty object entirely
    openai_cap = capability("openai", {"off": None, "high": "high"})
    got = apply_reasoning(
        {"chat_template_kwargs": {"enable_thinking": True}}, openai_cap, "high")
    assert "chat_template_kwargs" not in got


def test_apply_openrouter():
    cap = capability("openrouter", {"off": None, "medium": "medium"})
    assert apply_reasoning({}, cap, "medium")["reasoning"] == {"effort": "medium"}
    assert apply_reasoning({}, cap, "off")["reasoning"] == {"effort": "none"}
    # an explicit off wire wins
    cap2 = capability("openrouter", {"off": "none", "medium": "medium"})
    assert apply_reasoning({}, cap2, "off")["reasoning"] == {"effort": "none"}


def test_apply_zai():
    cap = capability("zai", {"off": None, "high": "high"})
    got = apply_reasoning({}, cap, "high")
    assert got["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert got["reasoning_effort"] == "high"
    assert apply_reasoning({}, cap, "off")["thinking"] == {"type": "disabled"}


def test_apply_together():
    cap = capability("together", {"off": None, "on": None, "high": "high"})
    got = apply_reasoning({}, cap, "high")
    assert got["reasoning"] == {"enabled": True}
    assert got["reasoning_effort"] == "high"
    assert apply_reasoning({}, cap, "off")["reasoning"] == {"enabled": False}


def test_apply_string_thinking():
    cap = capability("string-thinking", {"off": None, "high": "high"})
    assert apply_reasoning({}, cap, "high")["thinking"] == "high"
    assert apply_reasoning({}, cap, "off")["thinking"] == "none"


def test_apply_ant_ling():
    cap = capability("ant-ling", {"off": None, "high": "high"})
    got = apply_reasoning({"thinking": "client"}, cap, "high")
    assert got["reasoning"] == {"effort": "high"}
    assert "thinking" not in got
    off = apply_reasoning({"reasoning": {"effort": "client"}}, cap, "off")
    assert "reasoning" not in off and "thinking" not in off


# --------------------------------------------------------------------------- #
# apply_reasoning：剥离 / 校验 / 不可变
# --------------------------------------------------------------------------- #
def test_apply_strips_every_heterogeneous_client_field():
    cap = capability("openai", {"off": None, "high": "high"})
    body = {"model": "m", "reasoning_effort": "low",
            "reasoning": {"effort": "low"}, "thinking": {"type": "enabled"},
            "enable_thinking": True,
            "chat_template_kwargs": {"enable_thinking": True, "foo": 2},
            "messages": [{"role": "user", "content": "x"}]}
    got = apply_reasoning(body, cap, "high")
    assert got["reasoning_effort"] == "high"
    assert "reasoning" not in got and "thinking" not in got
    assert "enable_thinking" not in got
    assert got["chat_template_kwargs"] == {"foo": 2}
    assert got["messages"] == body["messages"]
    # input is not mutated (deep copy contract)
    assert body["reasoning_effort"] == "low"
    assert body["thinking"] == {"type": "enabled"}
    assert body["chat_template_kwargs"]["enable_thinking"] is True


def test_apply_rejects_illegal_level_and_unknown_capability():
    cap = capability("openai", {"off": None, "high": "high"})
    with pytest.raises(ReasoningError):
        apply_reasoning({}, cap, "auto")
    with pytest.raises(ReasoningError):
        apply_reasoning({}, None, "high")
    with pytest.raises(ReasoningError):
        apply_reasoning({}, {"declared": True, "supported": False,
                             "format": "openai", "efforts": []}, "high")
    with pytest.raises(ReasoningError):
        apply_reasoning({}, cap, "max")          # level exists but undeclared
    with pytest.raises(ReasoningError):          # unknown format
        apply_reasoning({}, {"declared": True, "supported": True,
                             "format": "nope", "efforts": [{"id": "high",
                                                            "wire": "high"}]}, "high")


def test_apply_defensively_rejects_missing_wire():
    """A hand-built capability that skipped validation must not send null effort."""
    cap = {"declared": True, "supported": True, "format": "openai",
           "efforts": [{"id": "off", "wire": None},
                       {"id": "high", "wire": None}]}
    with pytest.raises(ReasoningError):
        apply_reasoning({}, cap, "high")
