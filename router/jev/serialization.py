"""JEV 答案序列化的唯一入口（brief Step 4）。

`POST /v1/chat/completions` 的内存请求记录与 `POST /admin/try` 的试算响应共用
同一形态：完整 answers 只进内存 ring / 试算响应，不进 SQLite。
未知答案类型立即 TypeError，不允许静默丢字段。
"""
from __future__ import annotations

from typing import Union

from router.jev.client import ChoiceAnswer, NoulAnswer, ScoreAnswer

JevAnswer = Union[ChoiceAnswer, NoulAnswer, ScoreAnswer]


def answer_to_dict(answer: JevAnswer) -> dict:
    if isinstance(answer, ChoiceAnswer):
        return {
            "probabilities": dict(answer.probabilities),
            "ranking": list(answer.ranking), "p_top": answer.p_top,
            "margin": answer.margin, "usage": answer.usage,
            "duration_ms": answer.duration_ms,
        }
    if isinstance(answer, NoulAnswer):
        return {"yes": answer.yes, "no": answer.no, "usage": answer.usage,
                "duration_ms": answer.duration_ms}
    if isinstance(answer, ScoreAnswer):
        return {"value": answer.value, "usage": answer.usage,
                "duration_ms": answer.duration_ms}
    raise TypeError(f"unsupported answer type {type(answer).__name__}")
