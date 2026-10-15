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

import json
import re
from dataclasses import dataclass
from datetime import datetime

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


# ─────────────────────────────────────────────────────────────────────────────
# 4) tag_rules() —— 8 个规则标签（spec §4.5 表逐条；Task 3 追加，不改既有定义）
# ─────────────────────────────────────────────────────────────────────────────

# context 词数四档切点（spec §4.5「按 tag_settings 切四档」，与 sr/db.py
# _SEED_TAG_SETTINGS 的 context 行同值）。裁定：tag_rules 不读库——tag_settings
# 的 context 行留给 admin 展示与 M2；M2 接线时如需跟随库值，由调用方读表后
# 走独立路径，不改本默认常量。
DEFAULT_CONTEXT_THRESHOLDS: list[int] = [20, 100, 400]

# spec §4.5 未写明档位边界归属，取左闭右开 [t_i, t_{i+1})（含等号，与
# conf_band「达到切点个数」同语义）：<20 → 0（很短），20–99 → 1（一般），
# 100–399 → 2（长），≥400 → 3（很长）。

# CJK：基本区 U+4E00–U+9FFF（不含假名/谚文/全角标点，§4.5「CJK 字符」按汉字取）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")

# event 两张正则表（简报 Step 3 给定，逐字采用；英文词不区分大小写）。
# 同一文本同时命中两类时 incident 优先（spec §4.5 未写明优先级，裁定声明）。
_INCIDENT_RE = re.compile(r"故障|宕机|500|5\d\d 错误|incident|down", re.IGNORECASE)
_PAYMENT_RE = re.compile(r"支付|扣款|退款|payment|refund", re.IGNORECASE)


def _language_of(text: str) -> str:
    """§4.5 language：CJK 占比 ≥0.3 → zh；ASCII 字母占比 ≥0.8 → en；否则 other。

    分母口径（spec 未写明，声明）：去除全部空白字符后的字符数，标点计入。
    不扣标点/空白——常规英文句子的空格+标点不至于把字母占比拉下 0.8，
    而把标点也计入可让「中文夹英文」文本稳定落 zh/other 而非误判 en。
    """
    body = re.sub(r"\s+", "", text)
    total = len(body)
    if not total:
        return "other"                      # 空文本无语言信号，按「否则」落 other
    if len(_CJK_RE.findall(body)) / total >= 0.3:
        return "zh"                         # CJK 判定在前：混排且 CJK 达标即 zh
    if len(_ASCII_LETTER_RE.findall(body)) / total >= 0.8:
        return "en"
    return "other"


def _structure_of(text: str) -> str:
    """§4.5 structure：code（``` 围栏或缩进行）→ json（可解析）→ multiline → plain。

    判定顺序按 §4.5 表列序（围栏优先于 JSON：围栏内的 JSON 算代码）。
    multiline 阈值声明：spec 字面「≥ 3 个换行」与简报测试冲突——测试要求
    3 行（2 个换行符）即 multiline，按测试契约取「行数 ≥ 3」，已记任务报告。
    json 声明：仅 dict/list 算结构化（"123"/"true" 等标量虽是合法 JSON，
    作为消息正文没有结构语义，不判 json）。
    """
    if "```" in text:
        return "code"
    lines = text.split("\n")
    if any(line[:1] in (" ", "\t") for line in lines):
        return "code"                       # 缩进代码行（行首空白即算）
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return "json"
    if len(lines) >= 3:
        return "multiline"
    return "plain"


def _context_band(text: str, thresholds: list[int]) -> int:
    """§4.5 context：词数（CJK 按字、其余按空白分词）切四档，左闭右开。"""
    n_words = len(_CJK_RE.findall(text)) + len(_CJK_RE.sub(" ", text).split())
    return sum(1 for t in thresholds if n_words >= t)


def _turns_band(n_messages: int) -> int:
    """§4.5 turns：1 → 0（首轮）；2–9 → 1；≥10 → 2（长对话）。0 条按首轮算。"""
    if n_messages <= 1:
        return 0
    if n_messages < 10:
        return 1
    return 2


def _tenant_of(headers: dict) -> str:
    """§4.5 tenant：请求头 x-sr-tenant 原值（不归一）；缺省/空 → anonymous。

    键匹配不区分大小写（HTTP 头语义）；原值含首尾空白时原样保留（「原值」）。
    """
    for key, value in (headers or {}).items():
        if str(key).lower() == "x-sr-tenant" and value:
            return str(value)
    return "anonymous"


def _time_window_of(now: datetime) -> str:
    """§4.5 time_window：周六日 → weekend；工作日 09:00–18:00 → weekday_day；否则 night。

    只用传入的 now（可测），函数内不取系统时间。边界声明（spec 未写明）：
    白天窗口取左闭右开 [09:00, 18:00)，18:00 整落 night；weekend 全天命中、
    不再看小时（§4.5 周六日一行未限定时段）。
    """
    if now.weekday() >= 5:                  # Monday=0 … Saturday=5, Sunday=6
        return "weekend"
    if 9 <= now.hour < 18:
        return "weekday_day"
    return "night"


def _keyword_hits(text: str, kw_table: dict[str, list[str]] | None) -> list[str]:
    """§4.5 keyword：设置页词表子串命中（大小写不敏感），返回命中词扁平列表。

    按词表迭代序返回；同一词命中多次/多组只收一次（首次出现位置为准）。
    未传 kw_table 或无命中 → []。
    """
    if not kw_table:
        return []
    low = text.lower()
    hits: list[str] = []
    for words in kw_table.values():
        for word in words:
            word = str(word)
            if word and word.lower() in low and word not in hits:
                hits.append(word)
    return hits


def _rule(value) -> dict:
    """规则标签统一形状（§4.5 末行：无把握度，confidence = None）。"""
    return {"value": value, "confidence": None, "is_rule": True}


def tag_rules(text: str, headers: dict, n_messages: int, now: datetime,
              kw_table: dict[str, list[str]] | None = None) -> dict[str, dict]:
    """8 个规则标签打标（spec §4.5 表逐条；Task 3）。

    返回 ``{tag_key: {"value": ..., "confidence": None, "is_rule": True}}``，
    键序随 TAGS 注册表中 rule 标签序：keyword / language / structure / context /
    turns / tenant / time_window / event。规则标签不参与把握度门槛（§4.5 末行）。

    取舍与边界声明（spec 未写明处，均为实现裁定并已记任务报告）：
    - context 阈值用模块常量 DEFAULT_CONTEXT_THRESHOLDS，不读 tag_settings 表；
    - context / time_window 档位边界取左闭右开（含下界、不含上界）；
    - language 占比分母 = 去空白后的字符数；空文本落 other；
    - structure 的 multiline 取「行数 ≥ 3」（测试契约，覆盖 spec「≥3 个换行」字面）；
    - event 双表同中时 incident 优先；时间判定只用传入 now。
    """
    if _INCIDENT_RE.search(text):
        event = "incident"
    elif _PAYMENT_RE.search(text):
        event = "payment_issue"
    else:
        event = "none"
    return {
        "keyword": _rule(_keyword_hits(text, kw_table)),
        "language": _rule(_language_of(text)),
        "structure": _rule(_structure_of(text)),
        "context": _rule(_context_band(text, DEFAULT_CONTEXT_THRESHOLDS)),
        "turns": _rule(_turns_band(n_messages)),
        "tenant": _rule(_tenant_of(headers)),
        "time_window": _rule(_time_window_of(now)),
        "event": _rule(event),
    }
