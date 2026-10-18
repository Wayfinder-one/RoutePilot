"""Deterministic PII safety net (router/signals/pii_rules.py)."""

import pytest

from router.pipeline.matching import match_row
from router.signals.pii_rules import PII_RULES, merge_rule_pii, rule_pii_hits
from router.signals.registry import SIGNALS


def test_rule_hits_cover_high_confidence_formats():
    cases = {
        "cn_id_card": "我的身份证号 11010519491231002X",
        "phone_number": "联系人 13812345678",
        "cn_bank_card": "卡号 4111111111111111",
        "email_address": "发到 test@example.com",
        "ip_address": "服务器 192.168.1.1",
        "url": "参考 https://example.com/a/b",
        "cn_plate": "车牌 京A12345",
        "passport": "护照 E12345678",
        "us_ssn": "ssn 123-45-6789",
        "mac_address": "mac 00:1A:2B:3C:4D:5E",
        "password": "密码：abc123",
    }
    for typ, text in cases.items():
        assert typ in rule_pii_hits(text), (typ, text)


def test_plain_text_and_invalid_checksums_do_not_trip():
    assert rule_pii_hits("今天天气不错，帮我写一首诗") == []
    # 18 digits but invalid ID checksum / implausible birth date
    assert rule_pii_hits("订单号 202610181234567890") == []
    assert rule_pii_hits("工号 123456789012345678") == []
    # 16 digits but fails Luhn
    assert rule_pii_hits("编号 4111111111111112") == []


def test_rules_are_a_subset_of_registry_values():
    allowed = {v[0] for v in SIGNALS["pii"].values}
    assert set(PII_RULES) <= allowed


def test_merge_unions_model_values_and_keeps_confidences():
    signals = {"pii": {"value": ["date_time"], "confidence": 0.7,
                       "conf_by_value": {"date_time": 0.7}, "is_rule": False}}
    merge_rule_pii(signals, "身份证号 11010519491231002X，邮箱 a@b.com")
    pii = signals["pii"]
    assert pii["value"] == ["cn_id_card", "date_time", "email_address"]
    assert pii["conf_by_value"]["cn_id_card"] == 1.0
    assert pii["conf_by_value"]["email_address"] == 1.0
    assert pii["conf_by_value"]["date_time"] == 0.7
    assert pii["confidence"] == 1.0
    assert pii["is_rule"] is False          # the model contributed values


def test_merge_without_model_value_marks_signal_as_rule():
    signals = {"pii": {"value": [], "confidence": None,
                       "conf_by_value": {}, "is_rule": False}}
    merge_rule_pii(signals, "电话 13812345678")
    assert signals["pii"]["value"] == ["phone_number"]
    assert signals["pii"]["is_rule"] is True
    assert rule_pii_hits("没有敏感信息的文本") == []
    # no-op keeps the original dict untouched
    same = {"pii": {"value": [], "confidence": None, "conf_by_value": {},
                    "is_rule": False}}
    assert merge_rule_pii(same, "没有敏感信息的文本") is same
    assert same["pii"]["value"] == []


def test_match_row_uses_rule_confidence_for_gates():
    signals = {"pii": {"value": ["cn_id_card"], "confidence": 1.0,
                       "conf_by_value": {"cn_id_card": 1.0}, "is_rule": True}}
    assert match_row("pii", "cn_id_card", signals) == (1, 1.0)
    assert match_row("pii", "email_address", signals)[0] == 0
