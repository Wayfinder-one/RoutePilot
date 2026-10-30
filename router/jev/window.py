"""JEV 输入窗口：token 估算与首尾保留截断。

决策模型（llama.cpp + d1-2B）要求整个 prompt 一次送入，输入超长会直接 400/500，
而且决策延迟随长度超线性增长（4K≈200ms、16K≈2.7s）。因此 router 侧必须给送给 JEV
的 state 文本设一个 token 上限。

这里不做精确分词（避免引入 tokenizer 依赖或额外 HTTP 往返），用保守估算：
- 中日韩字符按 1 token/字；
- 其他字符（ASCII/标点/空格）按 4 字符/token。

估算偏高一点是安全的（宁可多截一点，也不要打到决策服务报错）。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONTEXT_TOKENS = 4096
DEFAULT_INPUT_TOKENS = 1024
MIN_INPUT_TOKENS = 256
MAX_TOKENS = 131072

# 截断时首尾各保留多少（比例）；开头通常是背景，结尾通常是诉求
HEAD_RATIO = 0.7

_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0x3400, 0x4DBF),   # 扩展 A
    (0x3000, 0x303F),   # CJK 标点
    (0xFF00, 0xFFEF),   # 全角字符
    (0x3040, 0x30FF),   # 日文假名
    (0xAC00, 0xD7AF),   # 韩文
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """保守估算 token 数：CJK 1 token/字，其他 4 字符/token（向上取整）。"""
    if not text:
        return 0
    cost = 0.0
    for ch in text:
        cost += 1.0 if _is_cjk(ch) else 0.25
    return int(cost) + (1 if cost % 1 else 0)


@dataclass(frozen=True)
class WindowResult:
    text: str
    tokens_before: int
    tokens_after: int
    truncated: bool


def _take_head(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    cost = 0.0
    end = 0
    for i, ch in enumerate(text):
        step = 1.0 if _is_cjk(ch) else 0.25
        if cost + step > budget:
            break
        cost += step
        end = i + 1
    return text[:end]


def _take_tail(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    cost = 0.0
    start = len(text)
    for i in range(len(text) - 1, -1, -1):
        step = 1.0 if _is_cjk(text[i]) else 0.25
        if cost + step > budget:
            break
        cost += step
        start = i
    return text[start:]


def apply_window(text: str, input_tokens: int) -> WindowResult:
    """把 text 截断到 input_tokens 估算 token 以内（首尾保留）。"""
    if input_tokens < MIN_INPUT_TOKENS:
        input_tokens = MIN_INPUT_TOKENS
    before = estimate_tokens(text)
    if before <= input_tokens:
        return WindowResult(text=text, tokens_before=before, tokens_after=before, truncated=False)

    marker = "\n…（已截断 {n} 字）…\n"
    reserve = estimate_tokens(marker.format(n=len(text))) or 8
    budget = max(1, input_tokens - reserve)
    head_budget = int(budget * HEAD_RATIO)
    tail_budget = budget - head_budget
    head = _take_head(text, head_budget)
    tail = _take_tail(text[len(head):], tail_budget)
    removed = len(text) - len(head) - len(tail)
    out = head + marker.format(n=max(removed, 0)) + tail

    # 估算误差兜底：必要时再收一次
    guard = 0
    while estimate_tokens(out) > input_tokens and guard < 4:
        budget = max(1, int(budget * 0.8))
        head = _take_head(text, int(budget * HEAD_RATIO))
        tail = _take_tail(text[len(head):], budget - int(budget * HEAD_RATIO))
        removed = len(text) - len(head) - len(tail)
        out = head + marker.format(n=max(removed, 0)) + tail
        guard += 1

    return WindowResult(text=out, tokens_before=before, tokens_after=estimate_tokens(out), truncated=True)
