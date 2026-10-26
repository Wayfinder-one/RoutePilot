"""Capability routing pack: the model catalog → JEV decision candidates.

The capability-routing policy lives in two places an operator already edits:
each model's free-text ``profile.capability`` description (model catalog) and
the global policy settings (``capability_instructions`` / ``capability_min_prob``
/ ``capability_min_margin`` / ``capability_fallback_model``).  This module turns
them into the immutable pack a request carries:

    {"candidates": {model_id: capability_description, ...},
     "instructions": "...", "min_prob": 0.45, "min_margin": 0.10,
     "fallback_model": "deepseek-v3" | None,
     "fingerprint": "<sha256 of the decision document>"}

The pack is built on config writes (``routes_admin._refresh_snapshot``) and at
boot, stored in the immutable :class:`RoutingSnapshot`, and passed to the tagger
via ``TagRequest.capability``.  Nothing on the hot path reads the database.

Pure module: no torch, no sqlite, no FastAPI.
"""

from __future__ import annotations

import hashlib
import json

from router.decision.capability import (DEFAULT_INSTRUCTIONS, DEFAULT_MIN_MARGIN,
                                        DEFAULT_MIN_PROB, MAX_INSTRUCTIONS_CHARS)
from router.decision.catalog import (CapabilityPackError, capability_of_model,
                                     normalize_capability)

#: Signal key produced by the tagger (``router/signals/registry.py``).
CAPABILITY_SIGNAL = "capability_route"

#: Settings keys (models.settings key/value table).
SETTING_INSTRUCTIONS = "capability_instructions"
SETTING_MIN_PROB = "capability_min_prob"
SETTING_MIN_MARGIN = "capability_min_margin"
SETTING_FALLBACK = "capability_fallback_model"
POLICY_SETTINGS = (SETTING_INSTRUCTIONS, SETTING_MIN_PROB,
                   SETTING_MIN_MARGIN, SETTING_FALLBACK)

# ``normalize_capability`` / ``capability_of_model`` (and the
# ``CapabilityPackError`` they raise) now live in ``router.decision.catalog``;
# they are imported above and stay re-exported here for existing
# ``router.decision.pack`` callers until the cleanup task deletes this
# forwarding module (2026-10-26 unified routing tree, Task 1).


def normalize_instructions(value) -> str:
    if value is None:
        return DEFAULT_INSTRUCTIONS
    if not isinstance(value, str):
        raise CapabilityPackError("capability_instructions 必须是字符串")
    text = value.strip()
    if not text:
        return DEFAULT_INSTRUCTIONS
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise CapabilityPackError(
            f"capability_instructions 超过 {MAX_INSTRUCTIONS_CHARS} 字符")
    return text


def _unit(value, name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityPackError(f"{name} 必须是 [0,1] 的数字") from exc
    if not 0.0 <= number <= 1.0:
        raise CapabilityPackError(f"{name} 必须在 [0,1] 之间")
    return number


def build_pack(models: list[dict], settings: dict | None = None) -> dict:
    """Build the immutable capability pack from catalog rows + policy settings."""
    settings = settings or {}
    candidates: dict[str, str] = {}
    for row in models:
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        capability = capability_of_model(row)
        if capability:
            candidates[model_id] = capability
    instructions = normalize_instructions(settings.get(SETTING_INSTRUCTIONS))
    min_prob = _unit(settings.get(SETTING_MIN_PROB), "capability_min_prob",
                     DEFAULT_MIN_PROB)
    min_margin = _unit(settings.get(SETTING_MIN_MARGIN),
                       "capability_min_margin", DEFAULT_MIN_MARGIN)
    fallback = settings.get(SETTING_FALLBACK)
    if isinstance(fallback, str):
        fallback = fallback.strip() or None
    else:
        fallback = None
    pack = {"candidates": candidates, "instructions": instructions,
            "min_prob": min_prob, "min_margin": min_margin,
            "fallback_model": fallback}
    pack["fingerprint"] = pack_revision(pack)
    return pack


def empty_pack() -> dict:
    """An unconfigured pack (no candidates → the signal is never produced)."""
    pack = {"candidates": {}, "instructions": DEFAULT_INSTRUCTIONS,
            "min_prob": DEFAULT_MIN_PROB, "min_margin": DEFAULT_MIN_MARGIN,
            "fallback_model": None}
    pack["fingerprint"] = ""
    return pack


def is_configured(pack: dict | None) -> bool:
    return bool(pack and pack.get("candidates"))


def pack_document(pack: dict) -> dict:
    """The canonical decision document (what a revision hash covers).

    Deliberately excludes ``fingerprint`` itself and any cached metadata, so
    the hash is stable and recomputable from the stored content.
    """
    return {
        "candidates": dict(pack.get("candidates") or {}),
        "instructions": pack.get("instructions") or DEFAULT_INSTRUCTIONS,
        "min_prob": pack.get("min_prob", DEFAULT_MIN_PROB),
        "min_margin": pack.get("min_margin", DEFAULT_MIN_MARGIN),
        "fallback_model": pack.get("fallback_model"),
    }


def canonical_pack_json(pack: dict) -> str:
    return json.dumps(pack_document(pack), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def pack_revision(pack: dict) -> str:
    """Content hash of the policy; '' when no candidate is configured."""
    if not is_configured(pack):
        return ""
    return hashlib.sha256(canonical_pack_json(pack).encode("utf-8")).hexdigest()


def freeze_pack(pack: dict | None) -> dict:
    """Read-only view stored in the immutable routing snapshot."""
    if not is_configured(pack):
        return empty_pack()
    frozen = dict(pack)
    frozen["candidates"] = dict(pack.get("candidates") or {})
    frozen["fingerprint"] = pack_revision(frozen)
    return frozen
