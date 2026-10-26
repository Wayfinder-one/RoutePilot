"""Shared routing errors (no dependency on the evaluator or the runtime).

Kept in a leaf module so the strict signal reader, the tree evaluator and the
routing runtime can all raise/catch them without importing each other:

* :class:`NoActiveTreeError` — no enabled tree: ``/v1`` answers 503
  ``no_active_tree`` (raised before any tagging or decision call).
* :class:`SignalPreparationError` — a required ordinary signal is missing or
  malformed: ``/v1`` answers 503 ``signal_preparation_failed``.  This is an
  infrastructure failure, never a normal tree ``Miss``.
"""

from __future__ import annotations


class NoActiveTreeError(RuntimeError):
    """No routing tree is enabled; the request cannot be routed."""


class SignalPreparationError(RuntimeError):
    """A required signal was not prepared correctly (missing/malformed)."""
