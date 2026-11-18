"""Routing Policy v4 的不可变模型、严格结构解析和 canonical 序列化。

约定（对应设计文档 §4）：

- 所有 dataclass 一律 frozen；``questions``/``targets`` 在构造时用
  ``MappingProxyType(dict(...))`` 包装，其余集合全部使用 tuple。
- ``policy_from_dict`` 只做结构与形状解析：kind/version、字段白名单、ID 形状、
  非空白文本、条件形状和有限数值；任何形状错误抛 ``ValueError``，绝不返回部分
  Policy。数量上限、问题/选项/目标/规则引用完整性、答案路径与输出/比较符/取值
  范围等语义校验属于保存时校验（``router.routing.policy.validate``，设计 §7.1），
  解析层不重复强制，保证校验器能拿到完整 Policy 并产出机器可读的 issue 列表。
- ``policy_to_dict`` 输出唯一的 canonical 行为文档：默认值显式写回，值为
  ``None`` 的 model reasoning 省略，使省略与显式 ``null`` 得到同一文档。
- 行为版本（``policy_revision_of``）是去掉 ``name`` 后 canonical JSON 的
  SHA-256；对象 key 排序，questions/targets 的 key 顺序无行为意义，rules 数组
  顺序保持语义顺序不排序。

错误消息沿用 codec 的字段导向英文短句；这里没有 UI 文案。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from router.routing.codec import (
    finite_number,
    optional_text_field,
    reject_unknown_fields,
    text_field,
)

__all__ = [
    "KIND",
    "VERSION",
    "MAX_CONDITION_DEPTH",
    "MAX_CONDITION_NODES",
    "MAX_CRITERIA",
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MAX_RULES",
    "MAX_STICKY_TTL_SECONDS",
    "MIN_CRITERIA",
    "MIN_OPTIONS",
    "AnswerCondition",
    "ChoiceOption",
    "Condition",
    "LogicCondition",
    "ModelTarget",
    "PeriodCondition",
    "Policy",
    "PolicyResource",
    "Question",
    "Rule",
    "Sticky",
    "Target",
    "TextCondition",
    "canonical_json",
    "policy_behavior_dict",
    "policy_from_dict",
    "policy_revision_of",
    "policy_to_dict",
]

KIND = "sr-routing-policy"
VERSION = 4

DEFAULT_TIMEZONE = "Asia/Shanghai"

# 语义限额（设计 §4.3/§4.5），validate 层的唯一常量来源：除 sticky 区间、
# options 下限（MIN_OPTIONS）与 criteria 2–10 外，policy_from_dict 不在解析期
# 强制这些上限/引用/范围语义——数量上限、问题/选项/目标/规则引用完整性、答案
# 路径与输出/比较符/取值范围的强制属于 router.routing.policy.validate 的
# 保存时校验（设计 §7.1，Task 2）。
MAX_STICKY_TTL_SECONDS = 31536000
MAX_QUESTIONS = 32
MIN_OPTIONS = 2
MAX_OPTIONS = 255
MIN_CRITERIA = 2
MAX_CRITERIA = 10
MAX_RULES = 200
MAX_CONDITION_NODES = 400
MAX_CONDITION_DEPTH = 16

#: 解析期唯一的递归防护：远高于 validate 的 16 层语义上限、远低于 C 栈极限，
#: 保证任意嵌套输入只会得到 ValueError，不会打爆 Python 栈。
_MAX_PARSE_DEPTH = 100

#: 问题ID/目标ID/选项 key 的唯一合法形状（设计 §4.3）：不允许点号，保证答案
#: 路径可以无歧义地按 ``.`` 切分。
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

_TOP_LEVEL_FIELDS = frozenset(
    {"kind", "version", "name", "timezone", "sticky", "questions", "targets", "rules", "fallback"}
)
_REQUIRED_TOP_LEVEL_FIELDS = frozenset({"kind", "version", "questions", "targets", "rules", "fallback"})
_QUESTION_TYPES = ("choice", "noul", "score")
_ANSWER_OPS = ("gt", "gte", "lt", "lte", "eq", "neq")


# ---------------------------------------------------------------------------
# 不可变类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sticky:
    enabled: bool = False
    ttl_seconds: int = 1800


@dataclass(frozen=True)
class ChoiceOption:
    key: str
    label: str
    capability: str


@dataclass(frozen=True)
class Question:
    id: str
    question_type: str
    instructions: str
    options: tuple[ChoiceOption, ...] = ()
    criteria: tuple[float, ...] | None = None


@dataclass(frozen=True)
class ModelTarget:
    model: str
    reasoning: str | None = None


@dataclass(frozen=True)
class Target:
    id: str
    models: tuple[ModelTarget, ...]

    @property
    def strategy(self) -> str:
        return "least_inflight"


@dataclass(frozen=True)
class AnswerCondition:
    path: str
    op: str
    value: str | float


@dataclass(frozen=True)
class LogicCondition:
    op: str
    children: tuple[Condition, ...]


@dataclass(frozen=True)
class TextCondition:
    contains_any: tuple[str, ...]


@dataclass(frozen=True)
class PeriodCondition:
    names: tuple[str, ...]


Condition = AnswerCondition | LogicCondition | TextCondition | PeriodCondition


@dataclass(frozen=True)
class Rule:
    when: Condition
    target: str
    name: str = ""


@dataclass(frozen=True)
class Policy:
    name: str
    timezone: str
    sticky: Sticky
    questions: Mapping[str, Question]
    targets: Mapping[str, Target]
    rules: tuple[Rule, ...]
    fallback: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", MappingProxyType(dict(self.questions)))
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))


@dataclass(frozen=True)
class PolicyResource:
    id: str
    policy: Policy
    enabled: bool = False
    policy_revision: str = ""
    updated_at: str = ""
    document_sha256: str = ""


# ---------------------------------------------------------------------------
# 严格解析
# ---------------------------------------------------------------------------


def _require_object(raw: object, kind: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{kind} must be an object")
    return raw


def _validated_id(value: object, kind: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{kind} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}} without dots"
        )
    return value


def _sticky_from_dict(raw: object) -> Sticky:
    raw = _require_object(raw, "sticky")
    reject_unknown_fields("sticky", raw, {"enabled", "ttl_seconds"})
    enabled = raw.get("enabled", False)
    ttl_seconds = raw.get("ttl_seconds", 1800)
    if not isinstance(enabled, bool):
        raise ValueError("sticky.enabled must be a boolean")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= MAX_STICKY_TTL_SECONDS
    ):
        raise ValueError(
            f"sticky.ttl_seconds must be an integer in [1,{MAX_STICKY_TTL_SECONDS}]"
        )
    return Sticky(enabled=enabled, ttl_seconds=ttl_seconds)


def _options_from_dict(qid: str, raw: object) -> tuple[ChoiceOption, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"question {qid!r}.options must be an array")
    if len(raw) < MIN_OPTIONS:
        raise ValueError(f"question {qid!r}.options must hold at least {MIN_OPTIONS} options")
    options: list[ChoiceOption] = []
    keys: set[str] = set()
    labels: set[str] = set()
    for item in raw:
        item = _require_object(item, f"question {qid!r} option")
        reject_unknown_fields(f"question {qid!r} option", item, {"key", "label", "capability"})
        key = _validated_id(item.get("key"), f"question {qid!r} option key")
        label = text_field(item.get("label"), f"question {qid!r} option label")
        capability = text_field(item.get("capability"), f"question {qid!r} option capability")
        if key in keys:
            raise ValueError(f"question {qid!r} has duplicate option key {key!r}")
        if label in labels:
            raise ValueError(f"question {qid!r} has duplicate option label {label!r}")
        keys.add(key)
        labels.add(label)
        options.append(ChoiceOption(key=key, label=label, capability=capability))
    return tuple(options)


def _criteria_from_dict(qid: str, raw: object) -> tuple[float, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"question {qid!r}.criteria must be an array")
    if not MIN_CRITERIA <= len(raw) <= MAX_CRITERIA:
        raise ValueError(
            f"question {qid!r}.criteria must hold {MIN_CRITERIA}-{MAX_CRITERIA} finite numbers"
        )
    return tuple(
        finite_number(value, f"question {qid!r}.criteria entry") for value in raw
    )


def _question_from_dict(qid: str, raw: object) -> Question:
    raw = _require_object(raw, f"question {qid!r}")
    question_type = raw.get("type")
    if question_type not in _QUESTION_TYPES:
        raise ValueError(
            f"question {qid!r}.type must be one of {', '.join(_QUESTION_TYPES)}"
        )
    if question_type == "choice":
        reject_unknown_fields(
            f"question {qid!r}", raw, {"type", "instructions", "options"}
        )
        instructions = text_field(raw.get("instructions"), f"question {qid!r}.instructions")
        options = _options_from_dict(qid, raw.get("options"))
        return Question(id=qid, question_type="choice", instructions=instructions, options=options)
    if question_type == "noul":
        reject_unknown_fields(f"question {qid!r}", raw, {"type", "instructions"})
        instructions = text_field(raw.get("instructions"), f"question {qid!r}.instructions")
        return Question(id=qid, question_type="noul", instructions=instructions)
    reject_unknown_fields(
        f"question {qid!r}", raw, {"type", "instructions", "criteria"}
    )
    instructions = text_field(raw.get("instructions"), f"question {qid!r}.instructions")
    criteria = _criteria_from_dict(qid, raw.get("criteria"))
    return Question(id=qid, question_type="score", instructions=instructions, criteria=criteria)


def _questions_from_dict(raw: object) -> dict[str, Question]:
    raw = _require_object(raw, "questions")
    questions: dict[str, Question] = {}
    for qid, item in raw.items():
        questions[_validated_id(qid, "question id")] = _question_from_dict(qid, item)
    return questions


def _model_target_from_dict(tid: str, raw: object) -> ModelTarget:
    raw = _require_object(raw, f"target {tid!r} model")
    reject_unknown_fields(f"target {tid!r} model", raw, {"model", "reasoning"})
    model = text_field(raw.get("model"), f"target {tid!r} model name")
    reasoning = raw.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError(f"target {tid!r} model reasoning must be a string or null")
    return ModelTarget(model=model, reasoning=reasoning)


def _target_from_dict(tid: str, raw: object) -> Target:
    raw = _require_object(raw, f"target {tid!r}")
    reject_unknown_fields(f"target {tid!r}", raw, {"models"})
    models_raw = raw.get("models")
    if not isinstance(models_raw, list):
        raise ValueError(f"target {tid!r}.models must be an array")
    models = tuple(_model_target_from_dict(tid, item) for item in models_raw)
    return Target(id=tid, models=models)


def _targets_from_dict(raw: object) -> dict[str, Target]:
    raw = _require_object(raw, "targets")
    targets: dict[str, Target] = {}
    for tid, item in raw.items():
        targets[_validated_id(tid, "target id")] = _target_from_dict(tid, item)
    return targets


def _answer_condition_from_dict(path_raw: object, op: str, value_raw: object) -> AnswerCondition:
    path = text_field(path_raw, "answer path")
    if isinstance(value_raw, bool):
        raise ValueError("answer condition value must be a string or finite number")
    value = (
        value_raw
        if isinstance(value_raw, str)
        else finite_number(value_raw, "answer condition value")
    )
    return AnswerCondition(path=path, op=op, value=value)


def _string_tuple(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} must hold a nonempty array of strings")
    return tuple(text_field(value, f"{field} entry") for value in raw)


def _condition_from_dict(raw: object, depth: int) -> Condition:
    if depth > _MAX_PARSE_DEPTH:
        raise ValueError(
            f"condition tree must not nest deeper than {_MAX_PARSE_DEPTH} levels during parsing"
        )
    raw = _require_object(raw, "condition")
    keys = set(raw)
    if keys in ({"all"}, {"any"}):
        op = "all" if keys == {"all"} else "any"
        children_raw = raw[op]
        if not isinstance(children_raw, list) or not children_raw:
            raise ValueError(f"{op} must hold a nonempty array of conditions")
        children = tuple(
            _condition_from_dict(child_raw, depth + 1)
            for child_raw in children_raw
        )
        return LogicCondition(op=op, children=children)
    if keys == {"not"}:
        child = _condition_from_dict(raw["not"], depth + 1)
        return LogicCondition(op="not", children=(child,))
    if "answer" in keys:
        ops = keys - {"answer"}
        if len(keys) != 2 or len(ops) != 1 or next(iter(ops)) not in _ANSWER_OPS:
            raise ValueError(
                "answer condition must hold 'answer' plus exactly one of "
                + "/".join(_ANSWER_OPS)
            )
        return _answer_condition_from_dict(
            raw["answer"], next(iter(ops)), raw[next(iter(ops))]
        )
    if keys == {"text_contains_any"}:
        return TextCondition(
            contains_any=_string_tuple(raw["text_contains_any"], "text_contains_any")
        )
    if keys == {"period_in"}:
        return PeriodCondition(names=_string_tuple(raw["period_in"], "period_in"))
    raise ValueError(
        "condition must be an all/any/not node, answer comparison, "
        "text_contains_any or period_in object"
    )


def _rules_from_dict(raw: object) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        raise ValueError("rules must be an array")
    rules: list[Rule] = []
    for item in raw:
        item = _require_object(item, "rule")
        reject_unknown_fields("rule", item, {"name", "when", "target"})
        name = optional_text_field(item.get("name", ""), "rule name")
        if "when" not in item:
            raise ValueError("rule requires a 'when' condition")
        when = _condition_from_dict(item["when"], depth=1)
        target = text_field(item.get("target"), "rule target")
        rules.append(Rule(when=when, target=target, name=name))
    return tuple(rules)


def policy_from_dict(raw: object) -> Policy:
    raw = _require_object(raw, "policy document")
    if raw.get("kind") != KIND:
        raise ValueError(f"policy kind must be {KIND!r}")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != VERSION:
        raise ValueError(f"policy version must be the integer {VERSION}")
    missing = sorted(_REQUIRED_TOP_LEVEL_FIELDS - set(raw))
    if missing:
        raise ValueError(f"policy document is missing fields: {', '.join(missing)}")
    reject_unknown_fields("policy document", raw, _TOP_LEVEL_FIELDS)
    name = optional_text_field(raw.get("name", ""), "name")
    timezone = text_field(raw.get("timezone", DEFAULT_TIMEZONE), "timezone")
    sticky = _sticky_from_dict(raw["sticky"]) if "sticky" in raw else Sticky()
    questions = _questions_from_dict(raw["questions"])
    targets = _targets_from_dict(raw["targets"])
    fallback = text_field(raw["fallback"], "fallback")
    rules = _rules_from_dict(raw["rules"])
    return Policy(
        name=name,
        timezone=timezone,
        sticky=sticky,
        questions=questions,
        targets=targets,
        rules=rules,
        fallback=fallback,
    )


# ---------------------------------------------------------------------------
# canonical 序列化
# ---------------------------------------------------------------------------


def _question_to_dict(question: Question) -> dict:
    if question.question_type == "choice":
        return {
            "type": "choice",
            "instructions": question.instructions,
            "options": [
                {"key": option.key, "label": option.label, "capability": option.capability}
                for option in question.options
            ],
        }
    if question.question_type == "noul":
        return {"type": "noul", "instructions": question.instructions}
    return {
        "type": "score",
        "instructions": question.instructions,
        "criteria": list(question.criteria or ()),
    }


def _target_to_dict(target: Target) -> dict:
    models = []
    for model_target in target.models:
        entry = {"model": model_target.model}
        if model_target.reasoning is not None:
            entry["reasoning"] = model_target.reasoning
        models.append(entry)
    return {"models": models}


def _condition_to_dict(condition: Condition) -> dict:
    if isinstance(condition, AnswerCondition):
        return {"answer": condition.path, condition.op: condition.value}
    if isinstance(condition, LogicCondition):
        if condition.op == "not":
            return {"not": _condition_to_dict(condition.children[0])}
        return {
            condition.op: [_condition_to_dict(child) for child in condition.children]
        }
    if isinstance(condition, TextCondition):
        return {"text_contains_any": list(condition.contains_any)}
    return {"period_in": list(condition.names)}


def policy_to_dict(policy: Policy) -> dict:
    return {
        "kind": KIND,
        "version": VERSION,
        "name": policy.name,
        "timezone": policy.timezone,
        "sticky": {
            "enabled": policy.sticky.enabled,
            "ttl_seconds": policy.sticky.ttl_seconds,
        },
        "questions": {
            qid: _question_to_dict(question)
            for qid, question in policy.questions.items()
        },
        "targets": {
            tid: _target_to_dict(target) for tid, target in policy.targets.items()
        },
        "rules": [
            {
                "name": rule.name,
                "when": _condition_to_dict(rule.when),
                "target": rule.target,
            }
            for rule in policy.rules
        ],
        "fallback": policy.fallback,
    }


def canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def policy_behavior_dict(policy: Policy) -> dict:
    """去掉 ``name`` 的 canonical 行为文档：快照 content 与行为版本共用此序列化。"""
    document = policy_to_dict(policy)
    document.pop("name", None)
    return document


def policy_revision_of(policy: Policy) -> str:
    return hashlib.sha256(
        canonical_json(policy_behavior_dict(policy)).encode("utf-8")
    ).hexdigest()
