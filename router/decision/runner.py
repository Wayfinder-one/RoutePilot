"""Decision-runner contract for natural-language tree nodes (pure DTOs).

The routing-tree evaluator never touches a model directly: an ``nl_choice``
node hands the runner the node's own question plus its **ordered** candidate
list and receives one probability per candidate.  The concrete adapter (JEV in
production, a fake in tests) owns batching, timeouts, serial gates and evidence
capture; the tree engine only validates and gates what comes back.

The candidate order in :class:`ChoiceRequest` is behaviour: it must reach the
runner exactly in configuration order (never sorted) and the returned
probabilities are interpreted against that same order.

``DecisionRunner.info()`` is the runner's version evidence (model fingerprints
and revision) and is part of the effective routing snapshot; every runner —
including test fakes — must implement it.

Pure module: no torch, no sqlite, no FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ChoiceOption:
    """One ordered candidate: its stable key plus the text the model reads."""

    key: str
    text: str


@dataclass(frozen=True)
class ChoiceRequest:
    """One ``choice`` question over the node's ordered candidates."""

    state: str
    instructions: str
    options: tuple[ChoiceOption, ...]


@dataclass(frozen=True)
class ChoiceResult:
    """One runner answer plus the evidence the request trace records.

    ``probabilities`` maps every candidate key to its (already normalised)
    probability; ``input``/``model_info``/``duration_ms`` are record-only
    evidence: the evaluator copies them into the ``nl_call`` trace event and
    never routes on them.
    """

    probabilities: dict[str, float]
    input: dict = field(default_factory=dict)
    model_info: dict = field(default_factory=dict)
    duration_ms: float = 0.0


class DecisionRunner(Protocol):
    """What the tree evaluator needs from a decision model adapter."""

    def info(self) -> dict:
        """Version evidence (model fingerprints / revision) for the snapshot."""
        ...

    async def choose(self, request: ChoiceRequest) -> ChoiceResult:
        """Answer one choice question; raise on any model/transport failure."""
        ...
