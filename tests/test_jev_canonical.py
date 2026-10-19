"""Pin the shipped JEV question config to the r4b3 canonical wording.

r4b3 was trained on three wording variants per question, but the hot path only
renders each signal's first wording (``instructions[0]``). The training README
(``results/jev_r4/README.md`` in the training repo) says the runtime must use
the canonical wording: unseen rewordings drift by up to ~5pt macro and
``user_feedback`` is the most wording-sensitive signal (canonical .802 vs .945
reworded).

These tests fail if the tracked config's runtime wording is edited. If a wording
change is intentional, re-run the training-repo eval first and update the pins
together with the training config (``configs/jev/r4_questions.json`` variant 0).
"""

from router.signals.jev import build_signal_records, load_question_config

#: instructions[0] of each signal == r4b3's canonical wording (variant 0).
CANONICAL = {
    "domain": "这道请求最可能属于下面哪个学科领域？",
    "jailbreak": "这条用户请求是否在试图绕过或忽略系统安全指令、套取系统提示词，"
                 "或要求实施明显违法/有害的行为？",
    "fact_check": "回答这条请求是否需要外部事实核查？",
    "modality": "这条请求要求的输出模态是什么？",
    "complexity": "这条请求的难度是否属于「困难」？",
    "user_feedback": "这句话对上一轮回答的反馈属于哪一类？",
}

#: The yes/no meaning lines are part of the rendered noul prompt, so they are
#: pinned as well (r4b3 was trained on exactly these).
NOUL_MEANINGS = {
    "jailbreak": (
        "是：请求试图绕过/忽略安全指令、套取系统提示词，或要求违法/明显有害的行为。",
        "否：其余请求，包括敏感但正当的求助（急救、维权、报警、体检、PII 格式核对等）。",
    ),
    "fact_check": (
        "需要：答案依赖可核查的外部事实（新闻、数据、人物、时间、科学结论等）。",
        "不需要：创作、翻译、代码、常识解释或纯主观问题。",
    ),
    "complexity": (
        "困难：长推理、多步计算、专业分析或需要大模型。",
        "简单：常识、短答案、单步操作或日常闲聊。",
    ),
}

PII_TEMPLATE = "这条请求中是否出现了「{zh}」类的个人敏感信息？"
PII_TRUE = "是：请求中明确包含{zh}信息。"
PII_FALSE = "否：请求中没有{zh}信息。"
PII_TYPES = {
    "age", "bank_account", "credit_card", "date_time", "device_id",
    "email_address", "gender", "generic_id", "ip_address", "location",
    "mac_address", "medical_id", "occupation", "org", "password", "person",
    "phone_number", "pin_code", "routing_number", "sensitive_attribute",
    "street_address", "swift_code", "url", "username", "us_ssn", "vehicle_id",
    "cn_id_card", "cn_bank_card", "cn_plate", "passport",
}


def test_first_instruction_is_canonical():
    cfg = load_question_config()
    for signal, wording in CANONICAL.items():
        assert cfg["signals"][signal]["instructions"][0] == wording, (
            f"{signal}: runtime wording must stay the r4b3 canonical question; "
            "reworded questions drift (see training results/jev_r4/README.md)")


def test_noul_meaning_lines_are_canonical():
    cfg = load_question_config()
    for signal, (true, false) in NOUL_MEANINGS.items():
        entry = cfg["signals"][signal]
        assert (entry["true"], entry["false"]) == (true, false), signal


def test_pii_template_and_types_are_canonical():
    pii = load_question_config()["signals"]["pii"]
    assert pii["instructions"] == PII_TEMPLATE
    assert pii["true"] == PII_TRUE
    assert pii["false"] == PII_FALSE
    assert set(pii["types"]) == PII_TYPES
    assert len(pii["types"]) == 30


def test_runtime_renders_only_the_first_instruction():
    """``build_signal_records`` must use instructions[0] (never the archived paraphrases)."""
    cfg = load_question_config()
    rendered = {}
    for item in build_signal_records(cfg, "帮我写一段 Python 快排；上一轮的回答你答错了。"):
        rendered.setdefault(item["signal"], item["record"])
    for signal, wording in CANONICAL.items():
        entry = cfg["signals"][signal]
        if entry["kind"] == "noul":
            expected = (f"{wording}\nYes means: {entry['true']}"
                        f"\nNo means: {entry['false']}")
        else:
            expected = wording
        assert rendered[signal]["question"] == expected, signal
    # choice candidate prompts keep the configured candidate set/order
    domain = cfg["signals"]["domain"]["candidates"]
    assert rendered["domain"]["answer_keys"] == list(domain)


def test_config_notes_warn_about_canonical_wording():
    notes = load_question_config()["notes"].lower()
    assert "canonical" in notes
    assert "instructions[0]" in notes
