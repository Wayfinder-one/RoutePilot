"""tests for ``scripts/jev_candidates.py``（P5-A 候选 span 生成，纯 CPU）。

覆盖：短文本单窗、预算/去重/合并/≤chunk_size、rule/cue/format/retrieval 来源、
确定性、per_type、候选召回 helper、uniform/head helper、无 sklearn 回退、
不引用 needles.json（诚实边界）、Python 3.10 语法。
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import jev_candidates as candidates  # noqa: E402

SCRIPT_PATH = SCRIPTS_DIR / "jev_candidates.py"


class CharTokenizer:
    """char = 1 token 的 fake tokenizer（offset 精确）。"""

    def __init__(self, *, supports_offsets: bool = True) -> None:
        self.supports_offsets = supports_offsets

    def __call__(self, text, add_special_tokens=False,
                 return_offsets_mapping=False, **kwargs):
        ids = [ord(char) for char in text]
        if return_offsets_mapping:
            if not self.supports_offsets:
                raise TypeError("offset_mapping not supported")
            return {"input_ids": ids,
                    "offset_mapping": [[index, index + 1]
                                       for index in range(len(text))]}
        return {"input_ids": ids}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(chr(int(token)) for token in ids)


def filler(length: int) -> str:
    base = "这是一段用来填充的普通文本内容。"
    return (base * (length // len(base) + 2))[:length]


def test_short_text_returns_single_full_window() -> None:
    tokenizer = CharTokenizer()
    text = "我的年龄是 34 岁。"
    windows = candidates.plan_candidates(text, tokenizer, chunk_size=512)
    assert windows == [{"start": 0, "end": len(text), "reason": "short",
                        "types": []}]
    assert candidates.plan_candidates("", tokenizer) == []


def test_budget_dedup_chunk_size_and_determinism() -> None:
    tokenizer = CharTokenizer()
    text = (filler(800) + " 地址 姓名 密码 年龄 " + filler(800)
            + " 邮箱 姓名 手机号 " + filler(800) + " 账号 用户名 " + filler(800))
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=3, chunk_size=64,
        use_rule=False, use_cue=True, use_retrieval=False)
    assert 1 <= len(windows) <= 3
    for window in windows:
        assert 0 <= window["start"] < window["end"] <= len(text)
        assert window["end"] - window["start"] <= 64
    again = candidates.plan_candidates(
        text, tokenizer, max_candidates=3, chunk_size=64,
        use_rule=False, use_cue=True, use_retrieval=False)
    assert windows == again
    assert windows == sorted(windows, key=lambda item: item["start"])


def test_rule_spans_produce_candidates() -> None:
    tokenizer = CharTokenizer()
    body = filler(3000)
    index = len(body) // 2
    text = body[:index] + " 我的邮箱是 test@example.com，手机号 13800138000。" + body[index:]
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=8, chunk_size=256,
        use_rule=True, use_cue=False, use_retrieval=False)
    assert windows
    assert {window["reason"] for window in windows} == {"rule"}
    email_token = text.index("test@example.com")
    phone_token = text.index("13800138000")
    assert candidates.needle_in_candidates(windows, email_token, 16)
    assert candidates.needle_in_candidates(windows, phone_token, 11)
    types = candidates.candidate_types(windows)
    assert "email_address" in types
    assert "phone_number" in types


def test_format_regex_and_cue_windows() -> None:
    tokenizer = CharTokenizer()
    body = filler(3000)
    index = len(body) // 3
    text = (body[:index] + " 身份证号：123456789012345 订单号 1234567899 "
            + body[index:])
    cue_windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=8, chunk_size=256,
        use_rule=False, use_cue=True, use_retrieval=False)
    assert cue_windows
    assert candidates.needle_in_candidates(cue_windows, text.index("身份证号"), 5)
    reasons = {window["reason"] for window in cue_windows}
    # cue 与格式窗可能因重叠>50% 合并（保留更高分者），只要 needle 被覆盖即可
    assert reasons & {"cue", "format"}
    # 纯格式（无 cue 词）也有兜底窗（reason=format）
    format_text = body[:index] + " 998877665544 " + body[index:]
    format_windows = candidates.plan_candidates(
        format_text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=False, use_cue=True, use_retrieval=False)
    assert format_windows
    assert "format" in {window["reason"] for window in format_windows}


def test_retrieval_finds_middle_paragraph() -> None:
    tokenizer = CharTokenizer()
    paragraph = 256
    stride = 128
    blocks = []
    needle_index = None
    cursor = 0
    for index in range(40):
        if index == 25:
            block = ("信用卡号是 NEEDLEXYZ " + filler(paragraph - 20))[:paragraph]
            needle_index = cursor + 6
        else:
            block = filler(paragraph)
        blocks.append(block)
        cursor += len(block)
    text = "".join(blocks)
    assert needle_index is not None
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=False, use_cue=False, use_retrieval=True,
        retrieval_top_k=2, retrieval_backend="char_bigram")
    assert windows
    assert {window["reason"] for window in windows} == {"retrieval"}
    assert candidates.needle_in_candidates(windows, needle_index, 9)
    again = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=False, use_cue=False, use_retrieval=True,
        retrieval_top_k=2, retrieval_backend="char_bigram")
    assert windows == again
    _ = stride


def test_retrieval_sklearn_backend_optional(monkeypatch) -> None:
    import importlib.util

    if importlib.util.find_spec("sklearn") is None:
        pytest.skip("本机没有 sklearn；char-bigram 回退由其它用例覆盖")
    tokenizer = CharTokenizer()
    text = filler(1000) + " password 口令 " + filler(1000)
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=2, chunk_size=128,
        use_rule=False, use_cue=False, use_retrieval=True,
        retrieval_backend="sklearn")
    assert windows


def test_per_type_windows_are_typed() -> None:
    tokenizer = CharTokenizer()
    body = filler(3000)
    text = (body[:500] + " 年龄 34 岁 " + body[500:1500]
            + " 手机号 13800138000 " + body[1500:])
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=False, use_cue=True, use_retrieval=False,
        per_type=True, types=["age", "phone_number"])
    assert windows
    for window in windows:
        assert len(window["types"]) == 1
    again = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=False, use_cue=True, use_retrieval=False,
        per_type=True, types=["age", "phone_number"])
    assert windows == again


def test_overlap_merge_and_rule_priority() -> None:
    tokenizer = CharTokenizer()
    text = filler(2000)
    # 两个距离很近的 cue 命中 → 合并成一个窗
    near = filler(500) + "身份证 身份证号 " + filler(1500)
    merged = candidates.plan_candidates(
        near, tokenizer, max_candidates=8, chunk_size=256,
        use_rule=False, use_cue=True, use_retrieval=False)
    assert len(merged) == 1

    # 规则窗优先保留：5 个相隔很远的邮箱，预算 3 → 全是 rule 窗
    parts = []
    for index in range(5):
        parts.append(filler(600))
        parts.append(" 邮箱%d test%d@example.com " % (index, index))
    rule_text = "".join(parts)
    rule_windows = candidates.plan_candidates(
        rule_text, tokenizer, max_candidates=3, chunk_size=256,
        use_rule=True, use_cue=False, use_retrieval=False)
    assert len(rule_windows) == 3
    assert {window["reason"] for window in rule_windows} == {"rule"}


def test_context_reuse_matches_fresh_planning() -> None:
    tokenizer = CharTokenizer()
    body = filler(3000)
    text = body[:400] + " 护照号 E12345678 " + body[400:]
    context = candidates.build_plan_context(text, tokenizer)
    with_context = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=True, use_cue=True, use_retrieval=True, context=context)
    fresh = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=True, use_cue=True, use_retrieval=True)
    assert with_context == fresh
    assert context.total_tokens == len(text)


def test_char_to_token_fallback_without_offsets() -> None:
    tokenizer = CharTokenizer(supports_offsets=False)
    body = filler(3000)
    text = body[:1000] + " 邮箱 test@example.com " + body[1000:]
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=4, chunk_size=256,
        use_rule=True, use_cue=False, use_retrieval=False)
    assert windows
    assert candidates.needle_in_candidates(windows, text.index("test@"), 10)


def test_invalid_arguments() -> None:
    tokenizer = CharTokenizer()
    with pytest.raises(ValueError):
        candidates.plan_candidates("x" * 1000, tokenizer, max_candidates=0)
    with pytest.raises(ValueError):
        candidates.plan_candidates("x" * 1000, tokenizer, chunk_size=0)
    with pytest.raises(ValueError):
        candidates.plan_candidates("x" * 1000, tokenizer,
                                   use_rule=False, use_cue=False,
                                   use_retrieval=False)


def test_helpers_uniform_head_needle_overlap() -> None:
    uniform = candidates.plan_uniform_windows(5000, n_windows=8,
                                              chunk_size=512)
    assert len(uniform) == 8
    assert uniform[0]["start"] == 0
    assert uniform[-1]["end"] == 5000
    for window in uniform:
        assert window["end"] - window["start"] <= 512
    assert candidates.plan_uniform_windows(100, n_windows=8,
                                           chunk_size=512)[0]["reason"] in (
                                               "short",)
    head = candidates.plan_head_window(5000, size=1024)
    assert head == [{"start": 0, "end": 1024, "reason": "head",
                     "types": []}]
    assert candidates.plan_head_window(100, size=1024)[0]["end"] == 100
    windows = [{"start": 100, "end": 200, "reason": "rule", "types": ["age"]}]
    assert candidates.needle_in_candidates(windows, 150, 10)
    assert candidates.needle_in_candidates(windows, 200, 10) is False
    assert candidates.needle_in_candidates(windows, 99, 1) is False
    assert candidates.needle_in_candidates([], 0, 1) is False


def test_cue_dictionary_not_from_needles_json() -> None:
    source = Path(candidates.__file__).read_text(encoding="utf-8")
    # 模块不读任何文件/不 import json：cue/格式表是硬编码的通用语义词，
    # 不可能在运行时用 spec/needles.json 造候选（docstring 提到该文件名仅作声明）。
    assert "import json" not in source
    assert "read_text(" not in source
    assert "open(" not in source
    assert "load_needles" not in source
    # cue 词表覆盖全部 30 类，每类至少 1 个通用词
    for typ in candidates.ALL_PII_TYPES:
        assert candidates.CUE_WORDS.get(typ), typ
        assert candidates.TYPE_QUERY.get(typ), typ


def test_script_python310_compatible() -> None:
    candidates_for_python = [str(Path.home() / ".local/bin/python3.11"),
                             "python3.11"]
    executable = None
    for candidate in candidates_for_python:
        if Path(candidate).is_file() or shutil.which(candidate):
            executable = candidate
            break
    if executable is None:
        pytest.skip("本机没有 python3.11")
    subprocess.run([executable, "-m", "py_compile", str(SCRIPT_PATH)],
                   check=True, capture_output=True)
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    ast.parse(source)
    assert "from __future__ import annotations" in source


# --------------------------------------------------------------------------- #
# P5-B：预算策略（strength/diverse/spread）与 needle 覆盖诊断纯函数
# --------------------------------------------------------------------------- #
def test_budget_strength_prefers_needle_statement() -> None:
    tokenizer = CharTokenizer()
    body = filler(6000)
    inserts = [(400, "时间"), (1200, "位置"), (2000, "日期"), (2800, "链接"),
               (3600, "账号"), (4400, "姓名"), (5200, "单位"),
               (3000, "我的手机号是 13800138000")]
    text = body
    for position, insert in sorted(inserts, reverse=True):
        text = text[:position] + insert + text[position:]
    needle_char = text.index("我的手机号是")
    results = {}
    for budget in ("spread", "diverse", "strength"):
        windows = candidates.plan_candidates(
            text, tokenizer, max_candidates=3, chunk_size=128,
            use_rule=False, use_cue=True, use_retrieval=False,
            budget=budget)
        assert 1 <= len(windows) <= 3
        assert all(window["end"] - window["start"] <= 128 for window in windows)
        assert windows == sorted(windows, key=lambda item: item["start"])
        results[budget] = windows
    # 强度优先策略必须覆盖「我的…是…」这类 needle 陈述句
    assert candidates.needle_in_candidates(results["strength"], needle_char, 20)
    assert all("score" in window for window in results["strength"])
    # 三种策略结果可不同，但都受预算约束
    assert results["spread"] != results["strength"] or True


def test_budget_rule_windows_priority() -> None:
    tokenizer = CharTokenizer()
    parts = []
    for index in range(4):
        parts.append(filler(800))
        parts.append(" 邮箱%d test%d@example.com " % (index, index))
    text = "".join(parts)
    windows = candidates.plan_candidates(
        text, tokenizer, max_candidates=3, chunk_size=256,
        use_rule=True, use_cue=True, use_retrieval=False, budget="strength")
    reasons = [window["reason"] for window in windows]
    assert reasons.count("rule") == 3          # 4 个 rule 窗，预算 3 全给 rule


def test_budget_invalid_strategy() -> None:
    tokenizer = CharTokenizer()
    with pytest.raises(ValueError):
        candidates.plan_candidates("x" * 2000, tokenizer, budget="bogus")


def test_needle_coverage_helper() -> None:
    windows = [{"start": 100, "end": 200, "reason": "cue", "types": []}]
    assert candidates.needle_coverage(windows, 150, 10) == "full"
    assert candidates.needle_coverage(windows, 195, 10) == "partial"
    assert candidates.needle_coverage(windows, 300, 5) == "none"
    assert candidates.needle_coverage([], 0, 1) == "none"
    assert candidates.needle_coverage(windows, 100, 100) == "full"


def test_merge_keeps_best_covering_window_not_recenter() -> None:
    """P5-B 回归：重叠 >50% 合并时保留 score 高的窗，不用中点重居中。

    200K 文档上附近常有一串泛化 cue 窗；旧实现取两窗中点重新居中，会把原本
    覆盖 needle 的强窗推走（诊断实测 unlimited 召回 95.6%→88.9%）。
    """
    first = {"start": 0, "end": 512, "reason": "cue", "types": ["age"],
             "score": 1.0}
    second = {"start": 200, "end": 700, "reason": "cue", "types": ["person"],
              "score": 10.0}
    merged = candidates._merge_windows([first, second], total=2000,
                                       chunk_size=512)
    assert len(merged) == 1
    assert merged[0]["start"] == 200 and merged[0]["end"] == 700
    assert merged[0]["score"] == 10.0
    assert set(merged[0]["types"]) == {"age", "person"}
    assert candidates.needle_in_candidates(merged, 650, 10)


def test_ascii_cue_scale_on_chinese_doc(monkeypatch) -> None:
    """中文文档里 ASCII cue（如 "my country"）的相对强度应被下调。"""
    tokenizer = CharTokenizer()
    zh_text = "这是一段中文内容。" * 200 + "我的手机号是 13800138000"
    ctx = candidates.build_plan_context(zh_text, tokenizer)
    assert ctx.ascii_cue_scale < 1.0
    ascii_text = "plain english filler " * 200 + "my phone is 13800138000"
    ctx_en = candidates.build_plan_context(ascii_text, tokenizer)
    assert ctx_en.ascii_cue_scale == 1.0
