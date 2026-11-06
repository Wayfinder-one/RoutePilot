"""Shared strict parsing primitives for the graph and tree codecs.

The graph codec (:mod:`router.routing.graph.model`) and the tree envelope codec
(:mod:`router.routing.tree`) reject unknown fields and mistyped values through
these functions, so the two layers cannot drift apart. Messages are plain
field-oriented English: no UI copy lives here.
"""
from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping

_SLOT_PATTERN = re.compile(r"in_([1-9][0-9]*)")
#: 槽位编号上限 = 边限额（spec §2.7）：超过它的 in_N 一定无法被合法图引用，
#: 拒绝后控制台不会按 in_9999 之类编号物化出成千上万行槽位。
MAX_SLOT_INDEX = 400


def slot_sort_key(port: str) -> int:
    """Parse an ``in_N`` input slot into its sortable index."""
    if isinstance(port, str):
        match = _SLOT_PATTERN.fullmatch(port)
        if match is not None:
            number = int(match.group(1))
            if number <= MAX_SLOT_INDEX:
                return number
            raise ValueError(
                f"slot index must be <= {MAX_SLOT_INDEX}, got {port!r}"
            )
    raise ValueError(f"slot must be in_N with a positive integer N (no leading zeros), got {port!r}")


def reject_unknown_fields(kind: str, raw: Mapping[str, object], allowed: Collection[str]) -> None:
    """Reject every key of ``raw`` that is not part of ``allowed``."""
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{kind} has unknown fields: {', '.join(unknown)}")


def text_field(value: object, field: str) -> str:
    """Return ``value`` as a string; empty and whitespace-only strings are rejected."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def optional_text_field(value: object, field: str) -> str:
    """Return ``value`` as a string; empty strings are accepted, non-strings are not."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def finite_number(value: object, field: str) -> float:
    """Return ``value`` as a finite float.

    Bools, non-numbers, NaN/inf and ints too large for ``float()`` are rejected
    with ``ValueError`` (never ``OverflowError``).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def optional_title(raw: Mapping[str, object], kind: str) -> str | None:
    """Return ``raw["title"]`` when present; ``None`` is allowed, non-strings are not."""
    value = raw.get("title")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{kind}.title must be a string")
    return value
