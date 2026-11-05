from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from router.jev.config import JevSettings

QUESTION_TYPES = ("choice", "noul", "score")


class JevError(RuntimeError):
    pass


@dataclass(frozen=True)
class JevCandidate:
    key: str
    label: str
    capability: str


def _finite_number(value, what: str) -> float:
    """拒绝 bool 与非有限数值，返回 float（用于 DecisionQuestion 构造期校验）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{what} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number")
    return number


@dataclass(frozen=True)
class DecisionQuestion:
    """单个画布节点对应的 JEV 问题。

    - choice：options 为 JevCandidate(key,label,capability) 元组，criteria 必须为 None；
    - noul：无 options/criteria；
    - score：options 必须为空，criteria 为 2–10 个有限数值档位。
    """

    question_type: str
    instructions: str
    options: tuple[JevCandidate, ...] = ()
    criteria: tuple[float, ...] | None = None

    def __post_init__(self):
        if self.question_type not in QUESTION_TYPES:
            raise ValueError("question_type must be one of choice/noul/score")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("instructions must be nonempty")
        if isinstance(self.options, list):
            object.__setattr__(self, "options", tuple(self.options))
        if self.question_type == "choice":
            if self.criteria is not None:
                raise ValueError("choice questions must not carry numeric criteria")
            candidates = self.options
            if not all(isinstance(candidate, JevCandidate) for candidate in candidates):
                raise ValueError("choice options must be JevCandidate values")
            if not 2 <= len(candidates) <= 255:
                raise ValueError("JEV choice needs 2–255 candidates")
            keys = [candidate.key for candidate in candidates]
            labels = [candidate.label for candidate in candidates]
            if len(set(keys)) != len(keys) or len(set(labels)) != len(labels):
                raise ValueError("JEV candidate keys and labels must be unique")
            if any(not candidate.label.strip() or not candidate.capability.strip() for candidate in candidates):
                raise ValueError("JEV candidates need labels and capabilities")
        elif self.question_type == "noul":
            if self.options:
                raise ValueError("noul questions take no options")
            if self.criteria is not None:
                raise ValueError("noul questions take no criteria")
        else:  # score
            if self.options:
                raise ValueError("score questions take no options")
            if isinstance(self.criteria, list):
                object.__setattr__(self, "criteria", tuple(self.criteria))
            if not isinstance(self.criteria, tuple) or not 2 <= len(self.criteria) <= 10:
                raise ValueError("score questions need 2–10 criteria levels")
            values = tuple(_finite_number(value, "score criteria") for value in self.criteria)
            object.__setattr__(self, "criteria", values)


@dataclass(frozen=True)
class ChoiceAnswer:
    probabilities: dict[str, float]
    ranking: list[str]
    p_top: float
    margin: float
    usage: dict
    duration_ms: float

    @property
    def selected_key(self) -> str:
        return self.ranking[0]


@dataclass(frozen=True)
class NoulAnswer:
    yes: float
    no: float
    usage: dict
    duration_ms: float


@dataclass(frozen=True)
class ScoreAnswer:
    value: float
    usage: dict
    duration_ms: float


JevAnswer = ChoiceAnswer | NoulAnswer | ScoreAnswer


def _wire_question(question: DecisionQuestion) -> dict:
    if question.question_type == "choice":
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": {candidate.label: candidate.capability for candidate in question.options},
        }
    if question.question_type == "noul":
        return {"type": "noul", "instructions": question.instructions}
    return {"type": "score", "instructions": question.instructions, "criteria": list(question.criteria)}


def _usage(body) -> dict:
    usage = body.get("usage") if isinstance(body, dict) else {}
    return usage if isinstance(usage, dict) else {}


def _wire_number(value, what: str) -> float:
    """wire 数值：拒绝 bool，要求 int/float 且有限。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError(f"{what} must be a finite number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise JevError(f"{what} must be a finite number") from exc
    if not math.isfinite(number):
        raise JevError(f"{what} must be a finite number")
    return number


def _probabilities(raw, options: tuple[JevCandidate, ...]) -> dict[str, float]:
    """label 严格集合校验 → 非负有限 → 1e-3 绝对容差 → 归一化并映射回 option key。"""
    if not isinstance(raw, dict):
        raise JevError("JEV probabilities must be an object")
    labels = [candidate.label for candidate in options]
    if set(raw) != set(labels) or len(raw) != len(labels):
        raise JevError("JEV probabilities do not cover exactly the configured candidates")
    values = []
    for label in labels:
        value = raw[label]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JevError("JEV probabilities must be numbers")
        try:
            number = float(value)
        except OverflowError as exc:
            raise JevError("JEV probabilities must be finite and nonnegative") from exc
        if not math.isfinite(number) or number < 0:
            raise JevError("JEV probabilities must be finite and nonnegative")
        values.append(number)
    total = sum(values)
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-3) or total <= 0:
        raise JevError(f"JEV probabilities must sum to 1, got {total}")
    return {
        candidate.key: value / total
        for candidate, value in zip(options, values)
    }


def _typed_answer(question: DecisionQuestion, raw, usage: dict, duration_ms: float) -> JevAnswer:
    if not isinstance(raw, dict):
        raise JevError("answer must be an object")
    answer_type = raw.get("type")
    if answer_type is not None and answer_type != question.question_type:
        raise JevError(f"answer type {answer_type!r} does not match requested {question.question_type!r}")
    if question.question_type == "choice":
        probabilities = _probabilities(raw.get("probabilities"), question.options)
        ranking = [
            candidate.key for _, candidate in sorted(
                enumerate(question.options),
                key=lambda item: (-probabilities[item[1].key], item[0]),
            )
        ]
        p_top = probabilities[ranking[0]]
        margin = p_top - probabilities[ranking[1]]
        return ChoiceAnswer(
            probabilities=probabilities,
            ranking=ranking,
            p_top=p_top,
            margin=margin,
            usage=usage,
            duration_ms=duration_ms,
        )
    if question.question_type == "noul":
        yes = _wire_number(raw.get("noul"), "noul probability")
        if not 0.0 <= yes <= 1.0:
            raise JevError(f"noul probability must be within [0,1], got {yes}")
        return NoulAnswer(yes=yes, no=1.0 - yes, usage=usage, duration_ms=duration_ms)
    index = _wire_number(raw.get("score"), "selected score")
    level = str(int(round(index)))
    legend = raw.get("legend")
    if not isinstance(legend, dict):
        raise JevError("score legend must be an object")
    if level not in legend:
        raise JevError(f"selected score index {level} is missing from the legend")
    value = _wire_number(legend[level], f"legend value for index {level}")
    return ScoreAnswer(value=value, usage=usage, duration_ms=duration_ms)


def parse_answer(key: str, question: DecisionQuestion, body, duration_ms: float) -> JevAnswer:
    """从 body["answers"][key] 读取并解析单个答案；任何失败都以含 key 的 JevError 抛出。"""
    try:
        raw = body["answers"][key]
    except (KeyError, TypeError) as exc:
        raise JevError(f"JEV response is missing answer for {key!r}") from exc
    try:
        return _typed_answer(question, raw, _usage(body), duration_ms)
    except JevError as exc:
        raise JevError(f"JEV answer for {key!r}: {exc}") from exc


class JevClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def choose_many(
        self,
        settings: JevSettings,
        text: str,
        questions: Mapping[str, DecisionQuestion],
    ) -> dict[str, JevAnswer]:
        if not isinstance(questions, Mapping) or not questions:
            raise JevError("JEV needs at least one question")
        payload = {
            "model": settings.model,
            "state": settings.state_template.format(request=text),
            "questions": {key: _wire_question(question) for key, question in questions.items()},
        }
        api_key = settings.api_key or os.environ.get("ROUTER_JEV_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        started = time.monotonic()
        try:
            response = await self.http.post(
                f"{settings.base_url}/v1/systemone",
                json=payload,
                headers=headers,
                timeout=settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise JevError("JEV transport failed: " + type(exc).__name__) from exc
        duration_ms = (time.monotonic() - started) * 1000
        if response.status_code != 200:
            raise JevError(f"JEV returned HTTP {response.status_code}")
        try:
            body = response.json()
            answers = {key: parse_answer(key, questions[key], body, duration_ms) for key in questions}
        except (ValueError, KeyError, TypeError) as exc:
            raise JevError("JEV returned an invalid response: " + str(exc)) from exc
        return answers
