"""Decision-model routing helpers (unified routing tree).

The decision layer exposes exactly three pure boundaries, consumed by the tree
compiler/evaluator and the admin API:

* :mod:`router.decision.catalog` — the model catalog side of routing: validate
  and read ``profile.capability`` (the natural-language text of a direct model
  candidate).  Survives the unified-tree migration because it is a model-catalog
  field, not a routing policy.
* :mod:`router.decision.choice` — the pure JEV ``choice`` record builder and
  probability decoder used by the decision adapter.
* :mod:`router.decision.runner` — the :class:`DecisionRunner` protocol plus the
  request/result DTOs every adapter implements.

The retired flat capability policy (global instructions/thresholds/fallback and
the decision-only candidate signal) and its pack module were removed with the
unified routing tree (2026-10-26); per-node ``nl_choice`` configuration replaced
them.  The concrete JEV adapter lives in ``router.decision.jev_runner`` and is
wired by ``router.api.app``.

Pure package: no torch, no sqlite, no FastAPI.
"""

from router.decision.catalog import (MAX_CAPABILITY_CHARS, CapabilityPackError,
                                     capability_of_model, normalize_capability)
from router.decision.choice import (ChoiceError, build_choice_record,
                                    decode_choice)
from router.decision.runner import (ChoiceOption, ChoiceRequest, ChoiceResult,
                                    DecisionRunner)

__all__ = [
    "MAX_CAPABILITY_CHARS",
    "CapabilityPackError",
    "ChoiceError",
    "ChoiceOption",
    "ChoiceRequest",
    "ChoiceResult",
    "DecisionRunner",
    "build_choice_record",
    "capability_of_model",
    "decode_choice",
    "normalize_capability",
]
