"""Row matching: the single definition of m (match) and c (confidence).

m: 1 when the signal value equals the row target (multi signals: membership).
c: JEV confidence for model signals; rule signals are deterministic => 1.0.
Unknown signals / missing values are (0, 0.0), never an exception: the
request path must not explode on a stale or partial signal pack.
"""

from __future__ import annotations

from router.signals.registry import SIGNALS


def match_row(signal_key: str, target: str, signals: dict) -> tuple[int, float]:
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
    return (1 if m else 0), float(conf) if isinstance(conf, (int, float)) else 0.0
