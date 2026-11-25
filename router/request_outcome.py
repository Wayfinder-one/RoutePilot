"""Neutral request-outcome vocabulary shared by the transport and the API layer.

``outcome_state`` is recorded by the upstream transport and rendered by the
console, so the state names are a cross-layer contract rather than a
presentation detail.  They live in this module — not in
``router.api.request_presenter`` — so ``router.upstream`` can record an outcome
without importing FastAPI, Starlette or ``router.api``.

``OutcomeSink`` is the structural contract the transport records through.  The
peripheral ``/v1`` data plane passes :class:`DiscardOutcome` (the kernel emits the
only decision event; execution results are not stored); the admin Test chat and
unit tests pass lighter fakes that only implement ``finish()``.

There is no persistent outcome recorder any more: SQLite request logging was
removed by the core/peripheral split, so the only outcome the peripheral keeps is
the volatile in-memory decision event.
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


class DiscardOutcome:
    """Execution-outcome sink for the peripheral data plane: nothing is stored.

    The peripheral keeps no request logs or execution results (design §3.2: the
    kernel only emits decision events); the upstream transport still reports its
    terminal outcome through the same structural contract, and this
    implementation deliberately throws every field away.
    """

    def finish(self, **fields) -> bool:
        return True


class OutcomeSink(Protocol):
    """Terminal-outcome recorder owned by the caller, not by the transport.

    The first ``finish()`` call wins; later calls are no-ops for implementations
    that finalise a record.  ``DiscardOutcome`` simply always succeeds.
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
