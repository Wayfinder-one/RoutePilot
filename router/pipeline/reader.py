"""Strict signal preparation/access for the unified routing runtime+evaluator.

Tree evaluation reads ordinary signals only on the paths it actually reaches, but
the runtime has already prepared (batched) the whole required signal pack.  A
missing or structurally broken required signal is therefore a **preparation
failure**, not a normal condition miss: it must abort routing with a 503
``signal_preparation_failed`` instead of silently falling into a fallback model.

Two boundaries, one strictness definition:

* :func:`validate_signal_pack` runs in the runtime BEFORE tree evaluation: every
  key in ``RoutingSnapshot.required_signals`` must be present and healthy, so a
  broken pack is caught even when the branch that reads it is never reached.
* :class:`PreparedSignalReader` is the lazy per-read view the evaluator uses; it
  applies the same checks so a broken pack can never be read as a miss.

A *legal* ``value=None`` (the tagger had no evidence) is returned as-is and
becomes a normal non-match downstream (:func:`router.pipeline.matching.match_row`
returns ``(0, 0.0)``) — that is a healthy "unknown", not a broken pack.

Pure module: no torch, no FastAPI; the runtime builds the pack, validates it once
and hands the reader to :class:`~router.pipeline.evaluator.TreeEvaluator`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from router.pipeline.errors import SignalPreparationError


def _is_finite_number(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _is_scalar_value(value) -> bool:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return True
    return _is_finite_number(value)


def _is_valid_value(value) -> bool:
    """A signal value: null, a scalar, or a multi-value list of non-null scalars."""
    if isinstance(value, (list, tuple)):
        return all(item is not None and _is_scalar_value(item) for item in value)
    return _is_scalar_value(value)


def _validate_signal(key: str, signal) -> dict:
    """Validate one signal entry; returns it when healthy (shared boundary)."""
    if not isinstance(signal, dict):
        raise SignalPreparationError(
            f"signal {key!r} must be an object, got {type(signal).__name__}")
    if "value" not in signal:
        raise SignalPreparationError(f"signal {key!r} has no value")
    confidence = signal.get("confidence")
    if confidence is not None and not _is_finite_number(confidence):
        raise SignalPreparationError(
            f"signal {key!r} confidence must be a finite number or null, "
            f"got {confidence!r}")
    if not _is_valid_value(signal["value"]):
        raise SignalPreparationError(
            f"signal {key!r} value must be null, a scalar or a multi-value "
            f"list, got {signal['value']!r}")
    conf_by_value = signal.get("conf_by_value")
    if conf_by_value is not None:
        if not isinstance(conf_by_value, Mapping):
            raise SignalPreparationError(
                f"signal {key!r} conf_by_value must be a mapping, got "
                f"{type(conf_by_value).__name__}")
        for value, number in conf_by_value.items():
            if not _is_finite_number(number):
                raise SignalPreparationError(
                    f"signal {key!r} conf_by_value[{value!r}] must be a finite "
                    f"number, got {number!r}")
    return signal


def validate_signal_pack(required: Iterable[str], signals) -> None:
    """Validate the whole prepared pack for the tree's required signals.

    Raises :class:`SignalPreparationError` on the first missing/malformed entry
    and returns ``None`` for a healthy pack.  ``required`` comes from the
    immutable snapshot (``RoutingSnapshot.required_signals``), so this is the
    single place that knows one request's complete preparation contract.
    """
    if not isinstance(signals, Mapping):
        raise SignalPreparationError(
            f"signal pack must be a mapping, got {type(signals).__name__}")
    for key in required:
        if key not in signals:
            raise SignalPreparationError(
                f"required signal {key!r} is missing from the prepared pack")
        _validate_signal(key, signals[key])


class PreparedSignalReader:
    """Lazily expose the prepared signal pack behind a strict ``read``."""

    def __init__(self, signals: dict):
        if not isinstance(signals, Mapping):
            raise SignalPreparationError(
                f"signal pack must be a mapping, got {type(signals).__name__}")
        self._signals = signals

    def read(self, key: str) -> dict:
        """Return the prepared signal dict for ``key`` (never a silent miss)."""
        if key not in self._signals:
            raise SignalPreparationError(
                f"required signal {key!r} is missing from the prepared pack")
        return _validate_signal(key, self._signals[key])
