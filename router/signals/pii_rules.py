"""Deterministic PII safety net (regex) shared by every tagger.

Why this exists
---------------
In ``jev`` (and ``checkpoint``) mode the ``pii`` signal comes only from the
model.  The Qwen3.5-0.8B JEV student inherited the RoutePilot PII head's
weakness: ``cn_id_card`` recall is ~1/4, so an obvious ID-card request can slip
past the PII gate (deployment doc: pipeline v10 ``pii.c_0001 >= 0.30`` never
fires on such text because the model emits ``pii=[]``).

This module restores the deterministic rules the heuristic tagger always had —
and extends them to the surface forms that need no semantics — so every tagger
can union regex hits into the model's ``pii`` value.  The union is additive:
model hits keep their own confidence, rule hits get confidence ``1.0``
(deterministic), which is what the pipeline matches on (``conf_by_value``).

Deliberately **not** covered here (needs semantics / too noisy):
person, org, location, occupation, age, gender, date_time, sensitive_attribute,
generic_id, medical_id, vehicle_id, pin_code, routing_number, swift_code.
"""
from __future__ import annotations

import re

#: type -> compiled regex; keys are product-canonical PII values.
PII_RULES: dict[str, re.Pattern] = {
    # 18-digit Chinese ID (17 digits + digit/X checksum), not part of a longer number
    "cn_id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    # mainland mobile number
    "phone_number": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # UnionPay (62…), Visa (4…), MasterCard (51-55…) card numbers
    "cn_bank_card": re.compile(
        r"(?<!\d)(?:62\d{14,17}|4\d{15}|5[1-5]\d{14})(?!\d)"),
    "email_address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    "password": re.compile(r"(密码|口令|password|pwd)\s*[:：为是]\s*\S+",
                           re.IGNORECASE),
    "ip_address": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "url": re.compile(r"https?://[^\s\u4e00-\u9fff]+", re.IGNORECASE),
    # Chinese mainland plate (incl. new-energy 6-char tail and special plates)
    "cn_plate": re.compile(
        r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领]"
        r"[A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]"),
    # Chinese passport (E/G + 8 digits)
    "passport": re.compile(r"(?<!\w)[EG]\d{8}(?!\w)"),
    "us_ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "mac_address": re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"),
}

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def _valid_cn_id(value: str) -> bool:
    """Checksum + birth-date sanity, so 18-digit order numbers do not trip the gate."""
    if len(value) != 18:
        return False
    body, check = value[:17], value[17].upper()
    y, m, d = int(body[6:10]), int(body[10:12]), int(body[12:14])
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return False
    total = sum(int(ch) * w for ch, w in zip(body, _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == check


def _luhn(value: str) -> bool:
    digits = [int(ch) for ch in value if ch.isdigit()]
    if not 16 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


#: patterns that are self-validating (checked separately below)
_CHECKED = ("cn_id_card", "cn_bank_card")
_TYPES = tuple(t for t in PII_RULES if t not in _CHECKED)


def rule_pii_spans(text: str) -> list[tuple[str, int, int]]:
    """Return deterministic ``(type, start_char, end_char)`` hits over the text.

    Unlike :func:`rule_pii_hits` (which only reports the type set) this keeps
    every occurrence and its character offsets, so long-text chunk planning can
    place a segment around a real rule hit (``plan_pii_segments``). Patterns
    that are self-validating (``cn_id_card`` checksum, ``cn_bank_card`` Luhn)
    only report matches that pass validation; the rest are pure regex hits.
    Sorted by ``(start, end, type)`` so the output is deterministic.
    """
    if not text:
        return []
    spans: list[tuple[str, int, int]] = []
    for typ in _TYPES:
        for match in PII_RULES[typ].finditer(text):
            spans.append((typ, match.start(), match.end()))
    for match in PII_RULES["cn_id_card"].finditer(text):
        if _valid_cn_id(match.group(0)):
            spans.append(("cn_id_card", match.start(), match.end()))
    for match in PII_RULES["cn_bank_card"].finditer(text):
        if _luhn(match.group(0)):
            spans.append(("cn_bank_card", match.start(), match.end()))
    spans.sort(key=lambda item: (item[1], item[2], item[0]))
    return spans


def rule_pii_hits(text: str) -> list[str]:
    """Return the sorted canonical PII types whose deterministic pattern hits."""
    return sorted({typ for typ, _, _ in rule_pii_spans(text)})


def merge_rule_pii(signals: dict, text: str) -> dict:
    """Union regex PII hits into ``signals['pii']`` (in place, returns signals).

    Rules are deterministic, so their ``conf_by_value`` entry is ``1.0``; model
    hits keep the confidence they already carry. ``is_rule`` stays ``False``
    when the model contributed values, and ``True`` when the whole value list is
    rule-derived (mostly for audit clarity). No-op when no rule fires.
    """
    hits = rule_pii_hits(text)
    if not hits:
        return signals
    sig = signals.get("pii")
    if not isinstance(sig, dict):
        sig = {}
    model_value = [v for v in (sig.get("value") or []) if isinstance(v, str)]
    conf = dict(sig.get("conf_by_value") or {})
    merged = sorted(set(model_value) | set(hits))
    for typ in hits:
        conf[typ] = 1.0
    signals["pii"] = {
        "value": merged,
        "confidence": 1.0,
        "conf_by_value": conf,
        "is_rule": not model_value,
    }
    return signals


__all__ = ["PII_RULES", "rule_pii_hits", "rule_pii_spans",
           "merge_rule_pii"]
