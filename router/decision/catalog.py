"""Model capability catalog: ``profile.capability`` validation and lookup.

The model catalog (``GET/PUT /admin/models``) stores each model's
natural-language capability description under the ``capability`` key of the
``models.profile`` JSON column. This module owns the single validation rule for
that text and the tolerant read the routing hot path uses:

* :func:`normalize_capability` — strict validation used by API writes: a
  malformed value raises :class:`CapabilityPackError`;
* :func:`capability_of_model` — tolerant lookup used when building the
  decision pack / candidate list: an invalid stored value is skipped (``''``)
  instead of breaking every request, because the catalog is operator data.

Pure module: no torch, no sqlite, no FastAPI.

(2026-10-26 unified routing tree: extracted from the retired decision pack and
kept as the model-catalog boundary after Task 11; ``profile.capability`` is a
model field, not a routing policy, so this module survives the cleanup.)

This module deliberately imports **nothing** from the retired capability-policy
modules: the catalog must be importable on its own (it outlives them), so the
description length limit moved here with the validator.
"""

from __future__ import annotations

import json

#: Max chars of one model's ``profile.capability`` description (product limit;
#: owned by the catalog because that is the surviving boundary).
MAX_CAPABILITY_CHARS = 2000

#: Model profile key holding the natural-language capability description.
PROFILE_KEY = "capability"


class CapabilityPackError(ValueError):
    """The capability description / policy is malformed."""


def normalize_capability(value) -> str:
    """Validate one model's ``profile.capability`` text ('' = unset)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CapabilityPackError("profile.capability 必须是字符串")
    text = value.strip()
    if len(text) > MAX_CAPABILITY_CHARS:
        raise CapabilityPackError(
            f"profile.capability 超过 {MAX_CAPABILITY_CHARS} 字符")
    return text


def _profile_of(row: dict) -> dict:
    """Tolerant ``models.profile`` read (JSON text column or a dict)."""
    raw = row.get("profile")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def capability_of_model(row: dict) -> str:
    """The model's validated capability text, or '' when unset/invalid.

    Invalid stored values are skipped (not raised): the catalog is user data and
    a bad row must not break every request; the model simply does not become a
    capability candidate until fixed.  API writes validate strictly.
    """
    try:
        return normalize_capability(_profile_of(row).get(PROFILE_KEY))
    except CapabilityPackError:
        return ""
