"""One-way routing orchestration: evaluate the graph, then schedule a model."""
from __future__ import annotations

from router.routing.graph.evaluator import GraphEvaluator
from router.routing.scheduling import ModelScheduler
from router.routing.snapshot import RoutingSnapshot
from router.routing.types import RoutingDecision, RoutingRequest


class RoutingService:
    def __init__(self, evaluator: GraphEvaluator, scheduler: ModelScheduler):
        self.evaluator = evaluator
        self.scheduler = scheduler

    async def route(
        self,
        snapshot: RoutingSnapshot,
        request: RoutingRequest,
        *,
        now=None,
        dry_run: bool = False,
        trace_sink: list[dict] | None = None,
    ) -> RoutingDecision:
        # One caller-owned list: evaluator events and scheduling events share the
        # decision trace (and the caller's sink) without either stage owning it.
        events: list[dict] = [] if trace_sink is None else trace_sink
        outcome = await self.evaluator.evaluate(
            snapshot, request, now=now, trace_sink=events,
        )
        return self.scheduler.schedule(
            snapshot=snapshot, request=request, outcome=outcome,
            dry_run=dry_run, trace_sink=events,
        )
