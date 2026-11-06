"""Neutral request-outcome vocabulary shared by the transport and the API layer.

``outcome_state`` is persisted by ``router.request_logging``, recorded by the
upstream transport and rendered by the console, so the state names are a
cross-layer contract rather than a presentation detail.  They live in this
module — not in ``router.api.request_presenter`` — so ``router.upstream`` can
record an outcome without importing FastAPI, Starlette or ``router.api``.

``OutcomeSink`` is the structural contract the transport records through.  The
route passes ``router.api.request_presenter.OutcomeRecorder`` (exactly-once
finalisation); tests pass lighter fakes that only implement ``finish()``.
"""
from __future__ import annotations

from typing import Protocol

OUTCOME_COMPLETED = "completed"
OUTCOME_UPSTREAM_ERROR = "upstream_error"
OUTCOME_CLIENT_DISCONNECTED = "client_disconnected"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_ROUTING_ERROR = "routing_error"

OUTCOME_STATES = (
    OUTCOME_COMPLETED,
    OUTCOME_UPSTREAM_ERROR,
    OUTCOME_CLIENT_DISCONNECTED,
    OUTCOME_CANCELLED,
    OUTCOME_INCOMPLETE,
    OUTCOME_ROUTING_ERROR,
)


class OutcomeSink(Protocol):
    """Terminal-outcome recorder owned by the caller, not by the transport.

    Implementations finalise a request record; the first ``finish()`` call wins
    and later calls are no-ops (``OutcomeRecorder`` enforces this and returns
    ``False`` for the losers).
    """

    def finish(
        self,
        *,
        outcome_state: str,
        error_class: str | None = None,
        upstream_model: str | None = None,
        upstream_status: int | None = None,
        upstream_ms: float | None = None,
        ttft_ms: float | None = None,
        usage: dict | None = None,
    ) -> bool:
        ...
