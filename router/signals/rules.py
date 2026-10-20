"""Rule signals: the deterministic 8 (design doc section 2.1).

Extracted from the heuristic tagger so both taggers feed the same rule path
(no duplicated regex tables, and the checkpoint tagger inherits the identical
rule semantics). Rule signals are deterministic => confidence is always 1.
"""

from __future__ import annotations

import re
from datetime import datetime

CJK_RE = re.compile(r"[一-鿿]")
_PAYMENT_RE = re.compile(r"支付|扣款|退款|payment|refund", re.IGNORECASE)
_INCIDENT_RE = re.compile(r"故障|宕机|(?<!\d)500(?!\d)")

RULE_SIGNAL_KEYS = ("context", "turns", "language", "structure",
                    "keyword", "tenant", "time_window", "event")


def count_words(text: str) -> int:
    """CJK characters count 1 each; runs of non-CJK count as whitespace words."""
    return len(CJK_RE.findall(text)) + len(CJK_RE.sub(" ", text).split())


def _hm(s: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    h, m = str(s).split(":")[:2]
    return int(h) * 60 + int(m)


def time_window_value(now: datetime, periods: list[dict] | None) -> list[str]:
    """Names of the periods covering ``now`` (same semantics as keyword groups).

    Each period: {name, days: [weekday 0-6, Mon=0], start/end: 'HH:MM'};
    start > end wraps past midnight, start == end means all day."""
    out = []
    if not periods:
        return out
    wd = now.weekday()
    minutes = now.hour * 60 + now.minute
    for p in periods:
        if wd not in (p.get("days") or []):
            continue
        begin, finish = _hm(p.get("start", "00:00")), _hm(p.get("end", "00:00"))
        if begin == finish:
            hit = True
        elif begin < finish:
            hit = begin <= minutes < finish
        else:
            hit = minutes >= begin or minutes < finish
        if hit and p.get("name"):
            out.append(p["name"])
    return out


def rule_signals(text: str, headers: dict, n_messages: int,
                 kw_groups: dict | None = None,
                 now: datetime | None = None,
                 time_periods: list[dict] | None = None) -> dict:
    """Produce the 8 deterministic signals; each {value, confidence=1, is_rule}.

    ``time_periods`` is the configured 时段词表; the time_window value is the
    list of period names covering ``now``."""
    kw_groups = kw_groups or {}
    words = count_words(text)

    def sig(value):
        return {"value": value, "confidence": 1.0, "is_rule": True}

    body = re.sub(r"\s+", "", text)
    now = now or datetime.now().astimezone()

    tenant = next((v for k, v in headers.items()
                   if k.lower() == "x-sr-tenant" and v), "anonymous")
    return {
        "keyword": sig([g for g, ws in kw_groups.items()
                        if any(w and w in text for w in ws)]),
        "language": sig("zh" if body and
                        len(CJK_RE.findall(body)) / len(body) >= 0.3 else "en"),
        "structure": sig("code" if "```" in text else
                         "multiline" if len(text.split("\n")) >= 3 else "plain"),
        "context": sig(3 if words >= 400 else 2 if words >= 100
                       else 1 if words >= 20 else 0),
        "turns": sig(0 if n_messages <= 1 else 1 if n_messages < 10 else 2),
        "tenant": sig(tenant),
        "time_window": sig(time_window_value(now, time_periods)),
        "event": sig("incident" if _INCIDENT_RE.search(text) else
                     "payment_issue" if _PAYMENT_RE.search(text) else "none"),
    }
