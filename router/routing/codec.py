"""Shared strict parsing primitives for the graph and tree codecs.

The graph codec (:mod:`router.routing.graph.model`) and the tree envelope codec
(:mod:`router.routing.tree`) reject unknown fields and mistyped values through
these functions, so the two layers cannot drift apart. Messages are plain
field-oriented English: no UI copy lives here.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping


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
