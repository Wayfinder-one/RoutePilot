"""Strict lazy signal access for the unified tree evaluator.

Tree evaluation reads ordinary signals only on the paths it actually reaches.
The runtime has already prepared (batched) the whole required signal pack, so a
missing or structurally broken signal here is a **preparation failure**, not a
normal condition miss: it must abort routing with a 503
``signal_preparation_failed`` instead of silently falling into a default model.

This reader is the single strict boundary for that distinction:

* missing pack entry, non-object signal, no ``value`` key or a non-finite
  ``confidence`` → :class:`SignalPreparationError`;
* a *legal* ``value=None`` (the tagger had no evidence) is returned as-is and
  becomes a normal non-match downstream (:func:`router.pipeline.matching.match_row`
  returns ``(0, 0.0)``) — that is a healthy "unknown", not a broken pack.

Pure module: no torch, no FastAPI; the runtime builds it from the validated
signal pack in :class:`~router.pipeline.runtime.RoutingRuntime` (Task 8).
"""

from __future__ import annotations

import math

from router.pipeline.errors import SignalPreparationError


class PreparedSignalReader:
    """Lazily expose the prepared signal pack behind a strict ``read``."""

    def __init__(self, signals: dict):
        if not isinstance(signals, dict):
            raise SignalPreparationError(
                f"signal pack must be a mapping, got {type(signals).__name__}")
        self._signals = signals

    def read(self, key: str) -> dict:
        """Return the prepared signal dict for ``key`` (never a silent miss)."""
        if key not in self._signals:
            raise SignalPreparationError(
                f"required signal {key!r} is missing from the prepared pack")
        signal = self._signals[key]
        if not isinstance(signal, dict):
            raise SignalPreparationError(
                f"signal {key!r} must be an object, got "
                f"{type(signal).__name__}")
        if "value" not in signal:
            raise SignalPreparationError(f"signal {key!r} has no value")
        confidence = signal.get("confidence")
        if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)):
            raise SignalPreparationError(
                f"signal {key!r} confidence must be a finite number or null, "
                f"got {confidence!r}")
        return signal
