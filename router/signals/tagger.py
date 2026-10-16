"""Tagger protocol: one request in, a full signal pack out (async).

M2 adds CheckpointTagger (RoutePilot checkpoint, one forward pass -> all
model signals + embedding). Both implementations satisfy this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TagRequest:
    text: str
    headers: dict = field(default_factory=dict)
    n_messages: int = 1


@dataclass
class TagResult:
    signals: dict                     # {key: {"value", "confidence", "is_rule"}}
    vec: list[float] | None = None    # embedding head output (M2; heuristic: None)


class Tagger(Protocol):
    name: str

    async def tag(self, req: TagRequest) -> TagResult: ...
