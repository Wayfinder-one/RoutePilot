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
    #: ``{"input_tokens", "input_visible_tokens", "input_truncated",
    #: "input_window"}``. ``input_window`` is the ``"<head>:<tail>"`` token
    #: window spec (JEV only, e.g. ``"256:768"``; other taggers omit the key).
    #: Empty when the tagger reads the whole text (heuristic/checkpoint) or does
    #: not measure input size. The prompt judge fills it with character counts
    #: (it never tokenizes); JEV fills it with token counts.
    #: When the opt-in long-text PII chunking actually runs (JEV only), the same
    #: dict additionally carries ``chunk_segments`` (planned segments),
    #: ``chunk_scored_segments``, ``chunk_prompt_tokens`` (all segment prompts)
    #: and ``chunk_strategy``; with chunking off (the default) or a short
    #: request those keys are absent, so pre-chunk consumers see no change.
    input: dict = field(default_factory=dict)
    #: sha256 (canonical JSON) of the question config the model signals were
    #: rendered from. JEV only; captured at ``tag()`` entry so a hot config
    #: edit mid-request cannot desync the fingerprint from the scored pack.
    #: ``None`` for taggers without an editable question config.
    questions_sha256: str | None = None
    #: Frozen version snapshot of the tagger taken at ``tag()`` entry (JEV:
    #: ``JevTagger.info()`` -- engine/model/window + head/tokenizer/
    #: chat-template/weights content hashes). The request path must use this
    #: instead of re-reading the tagger after the response. Empty for taggers
    #: that do not implement ``info()``.
    tagger_info: dict = field(default_factory=dict)


class Tagger(Protocol):
    name: str

    async def tag(self, req: TagRequest) -> TagResult: ...
