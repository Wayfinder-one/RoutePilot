"""Tagger protocol: one request in, a full signal pack out (async).

M2 adds CheckpointTagger (RoutePilot checkpoint, one forward pass -> all
model signals + embedding). Both implementations satisfy this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:                                # avoid a signals -> pipeline import
    from router.pipeline.scope import SignalScope


@dataclass
class TagRequest:
    text: str
    headers: dict = field(default_factory=dict)
    n_messages: int = 1
    #: Optional pipeline-derived trimming hint: the model signals and PII
    #: subtypes this request can actually be matched on
    #: (``router.pipeline.scope.compute_signal_scope``). ``None`` = compute the
    #: complete pack — the default for /admin/try, the eval endpoints and every
    #: tagger that does not implement trimming.
    scope: "SignalScope | None" = None


@dataclass
class TagResult:
    signals: dict                     # {key: {"value", "confidence", "is_rule"}}
    vec: list[float] | None = None    # embedding head output (M2; heuristic: None)
    #: Model-input window actually scored by the tagger, for the request log:
    #: ``{"input_tokens", "input_visible_tokens", "input_truncated"}``. Empty
    #: when the tagger reads the whole text (heuristic/checkpoint) or does not
    #: measure input size. The prompt judge fills it with character counts (it
    #: never tokenizes); JEV fills it with token counts.
    input: dict = field(default_factory=dict)


class Tagger(Protocol):
    name: str

    async def tag(self, req: TagRequest) -> TagResult: ...
