"""Signal registry: the single source of truth for every routing signal.

Both the console palette and the pipeline evaluators derive from this
registry. Adding a signal = one SignalSpec here plus tagger output.
Label spaces follow the training repo (smart-router-train
src/routepilot/signals.py) verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUPS = ("safety", "domain", "difficulty", "context", "session", "rule")


@dataclass(frozen=True)
class SignalSpec:
    key: str
    zh: str
    en: str
    group: str
    kind: str                  # 'model' | 'rule' (rule => deterministic, c == 1)
    multi: bool = False
    values: tuple = ()         # ((value_key, zh, en), ...); empty = free-form


_DOMAIN = (
    ("biology", "生物", "Biology"), ("business", "商业", "Business"),
    ("chemistry", "化学", "Chemistry"),
    ("computer_science", "计算机", "Computer Science"),
    ("economics", "经济", "Economics"), ("engineering", "工程", "Engineering"),
    ("health", "医疗", "Health"), ("history", "历史", "History"),
    ("law", "法律", "Law"), ("math", "数学", "Math"), ("other", "其他", "Other"),
    ("philosophy", "哲学", "Philosophy"), ("physics", "物理", "Physics"),
    ("psychology", "心理", "Psychology"),
)

_PII = (
    ("age", "年龄", "Age"), ("bank_account", "银行账户", "Bank Account"),
    ("credit_card", "信用卡", "Credit Card"), ("date_time", "日期时间", "Date/Time"),
    ("device_id", "设备ID", "Device ID"), ("email_address", "邮箱", "Email"),
    ("gender", "性别", "Gender"), ("generic_id", "通用ID", "Generic ID"),
    ("ip_address", "IP地址", "IP Address"), ("location", "位置", "Location"),
    ("mac_address", "MAC地址", "MAC Address"), ("medical_id", "医疗ID", "Medical ID"),
    ("occupation", "职业", "Occupation"), ("org", "组织", "Organization"),
    ("password", "密码", "Password"), ("person", "姓名", "Person"),
    ("phone_number", "手机号", "Phone Number"), ("pin_code", "PIN码", "PIN Code"),
    ("routing_number", "路由号码", "Routing Number"),
    ("sensitive_attribute", "敏感属性", "Sensitive Attribute"),
    ("street_address", "街道地址", "Street Address"),
    ("swift_code", "SWIFT码", "SWIFT Code"), ("url", "URL", "URL"),
    ("username", "用户名", "Username"), ("us_ssn", "美国社保号", "US SSN"),
    ("vehicle_id", "车辆ID", "Vehicle ID"),
    ("cn_id_card", "身份证号", "CN ID Card"),
    ("cn_bank_card", "银行卡号", "CN Bank Card"),
    ("cn_plate", "车牌号", "CN Plate"), ("passport", "护照", "Passport"),
)

_SPECS = (
    SignalSpec("jailbreak", "越狱可疑", "Jailbreak", "safety", "model",
               values=(("jailbreak", "可疑", "Jailbreak"), ("benign", "正常", "Benign"))),
    SignalSpec("pii", "敏感信息", "PII", "safety", "model", multi=True, values=_PII),
    SignalSpec("domain", "学科", "Subject", "domain", "model", values=_DOMAIN),
    SignalSpec("fact_check", "需核实", "Fact Check", "domain", "model",
               values=(("needs_check", "需要核实", "Needs Check"),
                       ("no_check", "无需核实", "No Check"))),
    SignalSpec("modality", "要什么", "Modality", "domain", "model",
               values=(("text", "文字", "Text"), ("image", "图片", "Image"),
                       ("text_image", "图文", "Text+Image"))),
    SignalSpec("complexity", "难度", "Difficulty", "difficulty", "model",
               values=(("easy", "简单", "Easy"), ("hard", "困难", "Hard"))),
    SignalSpec("context", "长度", "Context", "context", "rule",
               values=(("0", "很短", "Tiny"), ("1", "一般", "Short"),
                       ("2", "长", "Long"), ("3", "很长", "Huge"))),
    SignalSpec("turns", "轮次", "Turns", "context", "rule",
               values=(("0", "首轮", "First"), ("1", "多轮", "Multi"),
                       ("2", "长对话", "Long"))),
    SignalSpec("language", "语言", "Language", "context", "rule",
               values=(("zh", "中文", "Chinese"), ("en", "英文", "English"),
                       ("other", "其他", "Other"))),
    SignalSpec("structure", "结构", "Structure", "context", "rule",
               values=(("code", "代码", "Code"), ("json", "JSON", "JSON"),
                       ("multiline", "多行", "Multiline"), ("plain", "纯文本", "Plain"))),
    SignalSpec("similarity", "相似度", "Similarity", "session", "model",
               values=(("2", "很高", "High"), ("1", "一般", "Medium"),
                       ("0", "很低", "Low"))),
    SignalSpec("reask", "重复提问", "Re-ask", "session", "model",
               values=(("yes", "是", "Yes"), ("no", "否", "No"))),
    SignalSpec("user_feedback", "上轮反馈", "Last Feedback", "session", "model",
               values=(("satisfied", "满意", "Satisfied"),
                       ("need_clarification", "需澄清", "Need Clarification"),
                       ("wrong_answer", "答错了", "Wrong Answer"),
                       ("want_different", "想换个", "Want Different"))),
    SignalSpec("keyword", "关键词", "Keyword", "rule", "rule", multi=True),  # values = lexicon groups, dynamic
    SignalSpec("tenant", "身份", "Tenant", "rule", "rule"),                  # header value, free-form
    SignalSpec("time_window", "时段", "Time Window", "rule", "rule",
               values=(("weekday_day", "白天", "Day"), ("night", "夜间", "Night"),
                       ("weekend", "周末", "Weekend"))),
    SignalSpec("event", "事件", "Event", "rule", "rule",
               values=(("incident", "故障", "Incident"),
                       ("payment_issue", "支付问题", "Payment Issue"),
                       ("none", "无", "None"))),
)

SIGNALS: dict[str, SignalSpec] = {s.key: s for s in _SPECS}
