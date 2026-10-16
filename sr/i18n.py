"""sr/i18n.py —— 中英文翻译表 + 语言协商（spec §6.3 键表 / §6.6 服务端实现）。

模块职责：
- :data:`MESSAGES` —— zh/en 两张扁平表（``{key: 显示词}``），键集恒相等（测试
  断言）；键 = §6.3 全部语言无关键表：
  ``tag.*`` 17 + ``domain.*`` 14 + 有序档位 ``band.<tag>.<n>`` 16 + 其余标签取值
  ``<tag>.<value>`` 24 + ``pii.*`` 常用 5 型 + 把握度档 ``conf.0..3`` 4 +
  能力标签 ``caps.*`` 10 + 思考档位 ``think.*`` 3 + 操作符 ``op.*`` 5，再加
  ``ui.*`` 控制台用串与 ``block.default`` 拦截消息（§6.5 默认文案）。
- :func:`t` —— 翻译一条消息；key 不存在时原样返回（§6.6 逐字语义）。
- :func:`detect_lang` —— Accept-Language → ``zh`` | ``en``。

与 §6.6 逐字实现的两处出入（控制者裁定，记任务报告）：
- detect_lang 缺省/不可识别回落 **zh**（裁定 7 + §6.1「默认 zh」），不是 §6.6
  字面的「其余 → en」——无头请求的拦截消息从 en 变 zh，属裁定明示的行为口径；
- 标签名（``tag.*``）不另抄一份，直接从 :data:`sr.tags.TAGS` 的 zh/en 字段生成
  ——§6.3 键表与标签注册表同源，避免两处维护漂移（值逐字一致）。

依赖方向：i18n → tags（只读 TAGS 常量）；tags / engine / db 均不 import 本模块，
server 自 Task 9 起 import（内联 detect_lang 迁入，行为按裁定 7）。
"""

from __future__ import annotations

from sr.tags import TAGS

# ─────────────────────────────────────────────────────────────────────────────
# 1) §6.3 键表（zh/en 成对字面量；与 spec 表逐行核对）
# ─────────────────────────────────────────────────────────────────────────────

# 域值（domain 标签取值，14 键，与检查点 labels 一致；§6.3「域值」表逐行）
_DOMAIN: dict[str, tuple[str, str]] = {
    "computer_science": ("计算机", "Computer Science"),
    "math": ("数学", "Math"),
    "physics": ("物理", "Physics"),
    "chemistry": ("化学", "Chemistry"),
    "biology": ("生物", "Biology"),
    "engineering": ("工程", "Engineering"),
    "economics": ("经济", "Economics"),
    "business": ("商科", "Business"),
    "law": ("法律", "Law"),
    "philosophy": ("哲学", "Philosophy"),
    "psychology": ("心理", "Psychology"),
    "health": ("医学", "Health"),
    "history": ("历史", "History"),
    "other": ("其他", "Other"),
}

# 有序档位（存整数索引；§6.3「有序档位」表逐行：complexity 2 档、jailbreak 4 档、
# context 4 档、similarity 3 档、turns 3 档）。键 band.<tag>.<n>（§6.6 示例键形）。
_BANDS: list[tuple[str, int, str, str]] = [
    ("complexity", 0, "简单", "Easy"),
    ("complexity", 1, "难", "Hard"),
    ("jailbreak", 0, "很正常", "Normal"),
    ("jailbreak", 1, "有点可疑", "Slightly"),
    ("jailbreak", 2, "很可疑", "Suspicious"),
    ("jailbreak", 3, "非常可疑", "Very"),
    ("context", 0, "很短", "Very Short"),
    ("context", 1, "一般", "Medium"),
    ("context", 2, "长", "Long"),
    ("context", 3, "超长", "Very Long"),
    ("similarity", 0, "不像", "Dissimilar"),
    ("similarity", 1, "有点像", "Somewhat"),
    ("similarity", 2, "很像", "Similar"),
    ("turns", 0, "首轮", "First Turn"),
    ("turns", 1, "聊了几轮", "A Few Turns"),
    ("turns", 2, "长对话", "Long Conversation"),
]

# 其余标签取值（§6.3「其余标签取值」表逐行；键 <tag>.<value>）
_TAG_VALUES: list[tuple[str, str, str, str]] = [
    ("fact_check", "needs_check", "需要核实", "Needs Check"),
    ("fact_check", "no_check", "不用核实", "No Check"),
    ("modality", "text", "文字", "Text"),
    ("modality", "image", "图片", "Image"),
    ("modality", "text_image", "文字加图片", "Text + Image"),
    ("language", "zh", "中文", "Chinese"),
    ("language", "en", "英文", "English"),
    ("language", "other", "其他", "Other"),
    ("structure", "code", "有代码", "Code"),
    ("structure", "json", "有 JSON", "JSON"),
    ("structure", "multiline", "多行文字", "Multi-line"),
    ("structure", "plain", "普通文本", "Plain"),
    ("time_window", "weekday_day", "工作日白天", "Weekday daytime"),
    ("time_window", "night", "夜间", "Night"),
    ("time_window", "weekend", "周末", "Weekend"),
    ("event", "incident", "故障报告", "Incident"),
    ("event", "payment_issue", "支付异常", "Payment Issue"),
    ("event", "none", "没有", "None"),
    ("user_feedback", "satisfied", "满意", "Satisfied"),
    ("user_feedback", "need_clarification", "想让我说清楚", "Need Clarification"),
    ("user_feedback", "wrong_answer", "答错了", "Wrong Answer"),
    ("user_feedback", "want_different", "想换个说法", "Want Different"),
    ("reask", "yes", "是", "Yes"),
    ("reask", "no", "否", "No"),
]

# 敏感信息类型（§6.3 常用 5 型；完整 30 型以 sr/tags.PII_KEYS / tags.json 为准，
# 控制台「＋ 自定义」引用时缺键按 t() 语义回显键本身）
_PII: list[tuple[str, str, str]] = [
    ("phone_number", "手机号", "Phone"),
    ("cn_id_card", "身份证号", "ID number"),
    ("cn_bank_card", "银行卡号", "Bank card"),
    ("email_address", "邮箱", "Email"),
    ("password", "密码", "Password"),
]

# 把握度档位（全局共用，§6.3「把握度档位」表逐行）
_CONF_BANDS: list[tuple[int, str, str]] = [
    (0, "很不确定", "Very Uncertain"),
    (1, "不太确定", "Somewhat Uncertain"),
    (2, "比较确定", "Fairly Certain"),
    (3, "非常确定", "Very Certain"),
]

# 能力标签（models.caps，§6.3「能力标签」表逐行）
_CAPS: list[tuple[str, str, str]] = [
    ("text", "文字", "Text"),
    ("image", "图片", "Image"),
    ("long_ctx", "长上下文", "Long Context"),
    ("very_long_ctx", "超长", "Very Long Context"),
    ("cheap", "便宜", "Cheap"),
    ("medium", "中等", "Medium"),
    ("expensive", "贵", "Expensive"),
    ("fast", "快", "Fast"),
    ("medium_speed", "中速", "Medium Speed"),
    ("slow", "慢", "Slow"),
]

# 思考档位（action.think，§6.3「思考档位」表逐行）
_THINK: list[tuple[str, str, str]] = [
    ("off", "关闭", "Off"),
    ("light", "轻度", "Light"),
    ("deep", "深度", "Deep"),
]

# 操作符（cond.op，§6.3「操作符」表逐行）
_OPS: list[tuple[str, str, str]] = [
    ("is", "是", "is"),
    ("in", "属于", "in"),
    ("gte", "至少", "at least"),
    ("lte", "至多", "at most"),
    ("contains", "含有", "contains"),
]

# ui.* 控制台用串（§6.6 示例 ui.add_node + 控制台顶栏/表单常用少量；§9.2 出现
# 的控制台概念都收在此处，后续任务按需追加，键集 zh/en 恒同步）
_UI: list[tuple[str, str, str]] = [
    ("add_node", "新增节点", "Add Node"),
    ("save", "保存", "Save"),
    ("cancel", "取消", "Cancel"),
    ("delete", "删除", "Delete"),
    ("demo_mode", "演示模式", "Demo Mode"),
    ("block_msg", "拦截消息", "Block Message"),
    ("block_msg_zh", "拦截消息（中文）", "Block Message (Chinese)"),
    ("block_msg_en", "拦截消息（英文）", "Block Message (English)"),
    # audit step 4：模型接入 / 供应商表单的串（控制台 §9 引导第 4 步）
    ("add_model", "接入模型", "Add Model"),
    ("provider", "供应商", "Provider"),
    ("new_provider", "新建供应商", "New Provider"),
    ("provider_name", "供应商名称", "Provider Name"),
    ("base_url", "基址 URL", "Base URL"),
    ("key_env", "密钥来源（环境变量名；密钥本身不入库）",
                "Key Source (env var name; the key itself is never stored)"),
    ("upstream_model", "上游模型名", "Upstream Model"),
    ("provider_summary", "绑定摘要（只读）", "Binding Summary (read-only)"),
]

# block_msg 相关（§6.5 拦截消息；block.default = engine 种子树 _BLOCK_MSG 同文，
# 树外兜底文案与前端回退显示共用）
_BLOCK: list[tuple[str, str, str]] = [
    ("block.default", "请求被安全策略拦截", "Blocked by safety policy"),
]


# ─────────────────────────────────────────────────────────────────────────────
# 2) MESSAGES 组装（zh/en 两表同源成对写入，键集恒相等）
# ─────────────────────────────────────────────────────────────────────────────


def _build() -> dict[str, dict[str, str]]:
    """按 §6.3 各表成对写入，返回 ``{"zh": {...}, "en": {...}}``。"""
    zh: dict[str, str] = {}
    en: dict[str, str] = {}

    def add(triples) -> None:
        for key, z, e in triples:
            zh[key] = z
            en[key] = e

    # 标签名 17：唯一权威 = sr.tags.TAGS（§6.3 与注册表同源，防漂移）
    add((f"tag.{k}", d.zh, d.en) for k, d in TAGS.items())
    add((f"domain.{k}", z, e) for k, (z, e) in _DOMAIN.items())
    add((f"band.{tag}.{n}", z, e) for tag, n, z, e in _BANDS)
    add((f"{tag}.{v}", z, e) for tag, v, z, e in _TAG_VALUES)
    add((f"pii.{k}", z, e) for k, z, e in _PII)
    add((f"conf.{n}", z, e) for n, z, e in _CONF_BANDS)
    add((f"caps.{k}", z, e) for k, z, e in _CAPS)
    add((f"think.{k}", z, e) for k, z, e in _THINK)
    add((f"op.{k}", z, e) for k, z, e in _OPS)
    add((f"ui.{k}", z, e) for k, z, e in _UI)
    add(_BLOCK)
    return {"zh": zh, "en": en}


MESSAGES: dict[str, dict[str, str]] = _build()


def t(key: str, lang: str = "zh") -> str:
    """翻译一条消息；key 不存在时原样返回（§6.6 逐字语义）。"""
    return MESSAGES.get(lang, MESSAGES["zh"]).get(key, key)


def detect_lang(accept_language: str | None) -> str:
    """Accept-Language → 'zh' | 'en'（裁定 7：zh/zh-CN → zh，en → en，缺省 zh）。

    与 §6.6 字面实现（其余 → en）的差异是控制者裁定 + §6.1「默认 zh」：
    请求未带该头或语言不可识别时回落 zh。
    """
    h = (accept_language or "").lower()
    if "zh" in h:
        return "zh"
    if "en" in h:
        return "en"
    return "zh"
