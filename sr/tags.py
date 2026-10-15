"""标签层静态部分：17 标签注册表、值空间常量、normalize() 与 conf_band()。

依据（spec）：§4.1 标签定义（17 标签 × zh/en/kind/source）、§6.3 语言无关键清单
（17 标签键表、domain 14 键、有序档位表、其余取值、pii 类型）、§6.8 归一化规则
与别名表、§2.2 tags.json 格式与命名差异表、§3.1 conf_settings 默认切点。

约定：
- 所有值一律语言无关键/索引（§3.1 存储约定），中文显示词不进内存数据结构。
- ``normalize`` 对有序（ordered）标签返回档位索引 int（complexity: easy=0/hard=1），
  其余返回规范键 str；multi 类（pii/keyword）只校验/归一单个值，list 组装由调用方做。
- 别名匹配发生在「小写化 + 空白/连字符转下划线」之后，故 :data:`LABEL_ALIASES`
  的键一律是小写机械归一后的拼写。
- ``similarity`` / ``reask`` 的 source 记 ``model``（M2 才真产；M1 启发式也给值）。
- 规则打标（Task 3）与启发式打标（Task 4）往本文件追加，不改既有定义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────────────
# 1) TagDef + TAGS —— 17 标签注册表（spec §4.1 表 + §6.3 键表逐行录入）
# ─────────────────────────────────────────────────────────────────────────────

#: 标签形态（kind）：choice=枚举单选；noul=二值标签；multi=多值（单值校验，list 由
#: 调用方组装）；ordered=有序档位（存档位索引 int，§3.1 tags 表约定）；free=自由串。
Kind = str  # 'choice' | 'noul' | 'multi' | 'ordered' | 'free'
#: 标签来源（source）：model=模型头/嵌入头产出；rule=规则（§4.5/§4.6）产出。
Source = str  # 'model' | 'rule'


@dataclass(frozen=True)
class TagDef:
    """一个产品标签的静态定义（spec §4.1 / §6.3）。"""

    key: str            # 标签键（语言无关，tags.tag / 分流树 cond.tag）
    zh: str             # 中文显示名（§6.3，渲染层用）
    en: str             # 英文显示名（§6.3，渲染层用）
    kind: Kind          # choice / noul / multi / ordered / free
    source: Source      # model / rule
    values: tuple[str, ...] = ()   # 值空间（规范键）；ordered 为档位键序；
                                   # free 与动态词表（keyword）为空 = 不做静态校验


# domain 14 键，顺序 = 检查点 tags.json 的 labels 顺序（spec §2.2，字母序）。
DOMAIN_KEYS: list[str] = [
    "biology", "business", "chemistry", "computer_science", "economics",
    "engineering", "health", "history", "law", "math", "other",
    "philosophy", "physics", "psychology",
]

# pii 类型键，小写规范键。来源 = RoutePilot 注册表（smart-router-train
# src/routepilot/signals.py HEAD_SIGNALS['pii'].labels，spec §4.1 指定的唯一权威来源）
# 的 30 型：26 通用型 + 4 中文型（zh-pii-bench 词汇：cn_id_card/cn_bank_card/cn_plate/passport）。
# 注：spec §4.1/§6.3 行文写「29 型」与其权威注册表实有 30 型不符，此处按注册表全量收录
# （差一会使 tags.json 逐项断言静默失败），已在任务报告记录该出入。
PII_KEYS: list[str] = [
    "age", "bank_account", "credit_card", "date_time", "device_id",
    "email_address", "gender", "generic_id", "ip_address", "location",
    "mac_address", "medical_id", "occupation", "org", "password", "person",
    "phone_number", "pin_code", "routing_number", "sensitive_attribute",
    "street_address", "swift_code", "url", "username", "us_ssn", "vehicle_id",
    "cn_id_card", "cn_bank_card", "cn_plate", "passport",
]

# 有序档位键（spec §6.3「有序档位」表：complexity 2 档、jailbreak 4 档、context 4 档、
# similarity 3 档、turns 3 档）。complexity 的档位键有自然名 easy/hard（检查点 labels）；
# 其余档位无规范枚举名（§6.3 只给显示词），统一用 bandN 占位键，入库/树条件存索引数字。
ORDERED_VALUES: dict[str, list[str]] = {
    "complexity": ["easy", "hard"],
    "jailbreak": ["band0", "band1", "band2", "band3"],
    "context": ["band0", "band1", "band2", "band3"],
    "similarity": ["band0", "band1", "band2"],
    "turns": ["band0", "band1", "band2"],
}

TAGS: dict[str, TagDef] = {
    # 键, zh, en, kind, source, 值空间
    "domain": TagDef(
        "domain", "学科", "Subject", "choice", "model", tuple(DOMAIN_KEYS)),
    "complexity": TagDef(
        "complexity", "难度", "Difficulty", "ordered", "model",
        tuple(ORDERED_VALUES["complexity"])),
    "fact_check": TagDef(
        "fact_check", "需核实", "Fact Check", "noul", "model",
        ("needs_check", "no_check")),
    "similarity": TagDef(
        "similarity", "相似度", "Similarity", "ordered", "model",
        tuple(ORDERED_VALUES["similarity"])),
    "keyword": TagDef(
        "keyword", "关键词", "Keyword", "multi", "rule", ()),  # 值=设置页词表词条，动态
    "modality": TagDef(
        "modality", "要什么", "Modality", "choice", "model",
        ("text", "image", "text_image")),
    "language": TagDef(
        "language", "语言", "Language", "choice", "rule", ("zh", "en", "other")),
    "structure": TagDef(
        "structure", "结构", "Structure", "choice", "rule",
        ("code", "json", "multiline", "plain")),
    "context": TagDef(
        "context", "长度", "Context", "ordered", "rule",
        tuple(ORDERED_VALUES["context"])),
    "turns": TagDef(
        "turns", "轮次", "Turns", "ordered", "rule",
        tuple(ORDERED_VALUES["turns"])),
    "jailbreak": TagDef(
        "jailbreak", "可疑", "Suspicious", "ordered", "model",
        tuple(ORDERED_VALUES["jailbreak"])),
    "pii": TagDef(
        "pii", "敏感信息", "PII", "multi", "model", tuple(PII_KEYS)),
    "tenant": TagDef(
        "tenant", "身份", "Identity", "free", "rule", ()),  # 请求头原值，自由串
    "time_window": TagDef(
        "time_window", "时段", "Time Window", "choice", "rule",
        ("weekday_day", "night", "weekend")),
    "event": TagDef(
        "event", "事件", "Event", "choice", "rule",
        ("incident", "payment_issue", "none")),
    "user_feedback": TagDef(
        "user_feedback", "上轮反馈", "Last Feedback", "choice", "model",
        ("satisfied", "need_clarification", "wrong_answer", "want_different")),
    "reask": TagDef(
        "reask", "重复提问", "Re-ask", "noul", "model", ("yes", "no")),
}

# ─────────────────────────────────────────────────────────────────────────────
# 2) LABEL_ALIASES —— 别名表（spec §6.8 表 + §2.2 差异表逐行）
# ─────────────────────────────────────────────────────────────────────────────

# 结构对齐 routepilot signals.py 的 LABEL_ALIASES（按标签分组）。键是「小写 +
# 空白/连字符转下划线」之后的拼写（§6.8 机械归一先于别名匹配）。
# §6.8 / §2.2 差异表中靠机械归一即可覆盖的行不列于此：
#   domain "computer science" → computer_science（空白转下划线）；
#   user_feedback WRONG_ANSWER/… 与 pii PHONE_NUMBER/CN_ID_CARD/…（小写化）。
LABEL_ALIASES: dict[str, dict[str, str]] = {
    "modality": {
        # 检查点头标签 AR / DIFFUSION / BOTH（§2.2 差异表、§6.8 别名行）
        "ar": "text",
        "diffusion": "image",
        "both": "text_image",
    },
    "fact_check": {
        # FACT_CHECK_NEEDED → needs_check（§6.8 别名行）；
        # no_fact_check_needed → no_check（注册表规范拼写 → 存储键，§2.2 差异表行）
        "fact_check_needed": "needs_check",
        "no_fact_check_needed": "no_check",
    },
    "user_feedback": {
        # SAT → satisfied（§6.8 别名行；其余 NEED_CLARIFICATION 等小写化即达）
        "sat": "satisfied",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# 3) normalize() + conf_band() —— 归一化与把握度切档
# ─────────────────────────────────────────────────────────────────────────────

# §6.8 机械归一：小写；空白与连字符 → 下划线（连续分隔符并作一个）。
_MECH_RE = re.compile(r"[\s\-]+")

# conf_settings 默认切点（spec §3.1；与 sr/db.py _SEED_CONF_SETTINGS 一致）。
DEFAULT_CONF_THRESHOLDS: list[float] = [0.30, 0.60, 0.85]


def _mechanical(value) -> str:
    """§6.8 机械归一：str 化、去首尾空白、小写、空白/连字符转下划线。"""
    return _MECH_RE.sub("_", str(value).strip().lower())


def normalize(tag: str, value) -> str | int:
    """外部拼写 → 规范键 / 档位索引（spec §6.8；§2.3 第 4 步断言共用）。

    步骤：小写 + 空白/连字符转下划线 → 别名表（LABEL_ALIASES）→ 值空间校验。
    - ordered 标签返回档位索引 int（接受档位键 ``"hard"``、``"band2"``、数字
      ``2``/``"2"``）；其余返回规范键 str。
    - multi 类（pii/keyword）只处理单个值，list 组装由调用方做。
    - 不在值空间内抛 ValueError（§6.8「不静默兜底」）；未知标签同样 ValueError。
    - free（tenant）与动态词表（keyword）无静态值空间，只做机械归一。
    """
    spec = TAGS.get(tag)
    if spec is None:
        raise ValueError(
            f"未知标签 {tag!r}（spec §4.1 的 17 标签之外）")
    text = _mechanical(value)
    text = LABEL_ALIASES.get(tag, {}).get(text, text)
    if spec.kind == "ordered":
        if text.isdigit():
            band = int(text)
            if not 0 <= band < len(spec.values):
                raise ValueError(
                    f"{tag} 档位索引越界 {value!r}"
                    f"（0–{len(spec.values) - 1}）")
            return band
        if text in spec.values:
            return spec.values.index(text)
        raise ValueError(
            f"{tag} 不在值空间 {list(spec.values)} 内: {value!r}")
    if not spec.values:          # free / 动态词表（keyword）
        return text
    if text not in spec.values:
        raise ValueError(f"{tag} 不在值空间 {list(spec.values)} 内: {value!r}")
    return text


def conf_band(p: float, thresholds: list[float] | None = None) -> int:
    """把握度 p ∈ [0,1] 按切点分 0–3 档（spec §3.1 conf_settings；§4.2 第 4 步）。

    thresholds 为 None 时用默认切点 [0.30, 0.60, 0.85]（调用方通常从
    conf_settings 表读后显式传入）。档位 = p 达到的切点个数（含等号，与树条件
    ``gte`` 同语义）：0.30 → 1，0.60 → 2，0.85 → 3。
    """
    cuts = DEFAULT_CONF_THRESHOLDS if thresholds is None else thresholds
    return sum(1 for t in cuts if p >= t)
