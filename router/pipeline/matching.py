"""Row matching: the single definition of m (match) and c (confidence).

m: 1 when the signal value equals the row target (multi signals: membership).
c: JEV confidence for model signals; rule signals are deterministic => 1.0.
Unknown signals / missing values are (0, 0.0), never an exception: the
request path must not explode on a stale or partial signal pack.
"""

from __future__ import annotations

from router.signals.registry import SIGNALS


def is_rule_signal(signal_key: str) -> bool:
    """Rule signals come straight from request attributes (c == 1.0)."""
    spec = SIGNALS.get(signal_key)
    return spec is not None and spec.kind == "rule"


def match_row(signal_key: str, target: str, signals: dict) -> tuple[int, float]:
    """m/c for one row. Multi signals resolve confidence per matched value.

    Design doc section 2.2: a multi signal (pii / keyword) may carry
    ``conf_by_value``; when matching a target we prefer that value's confidence
    and fall back to the scalar ``confidence``. A non-match keeps the scalar
    confidence (so the caller can still see how confident the tagger was).
    """
    spec = SIGNALS.get(signal_key)
    sig = signals.get(signal_key)
    if spec is None or not isinstance(sig, dict) or sig.get("value") is None:
        return 0, 0.0
    value = sig["value"]
    if spec.multi:
        m = isinstance(value, (list, tuple)) and target in value
    else:
        m = str(value) == str(target)
    if spec.kind == "rule":
        return (1 if m else 0), 1.0
    conf = sig.get("confidence")
    if m and spec.multi:
        per_value = sig.get("conf_by_value")
        if isinstance(per_value, dict):
            conf = per_value.get(target, conf)
    return (1 if m else 0), float(conf) if isinstance(conf, (int, float)) else 0.0
