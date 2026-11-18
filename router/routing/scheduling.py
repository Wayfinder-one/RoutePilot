"""Model scheduling: sticky selection, lease acquisition and affinity writes.

``ModelScheduler`` is the only place that reads session affinity or touches the
``LoadBalancer``.  It consumes the policy decision produced by
``PolicyEvaluator`` and turns it into a ``RoutingDecision``.  Ordering is fixed:
final policy decision (already emitted by the evaluator) → sticky lookup →
acquire → affinity put → scheduled event.  The evaluator always runs first, so a
sticky hit never skips JEV; dry-run touches neither affinity nor in-flight
counters.
"""
from __future__ import annotations

from router.routing.policy.evaluator import PolicyOutcome
from router.routing.snapshot import RoutingSnapshot
from router.routing.types import (
    NoAvailableBackend,
    RoutingDecision,
    RoutingRequest,
    session_digest,
)


class ModelScheduler:
    def __init__(self, affinity, balancer):
        self.affinity = affinity
        self.balancer = balancer

    def schedule(
        self,
        *,
        snapshot: RoutingSnapshot,
        request: RoutingRequest,
        outcome: PolicyOutcome,
        dry_run: bool,
        trace_sink: list[dict] | None,
    ) -> RoutingDecision:
        events: list[dict] = [] if trace_sink is None else trace_sink
        # 求值器先于调度运行：NoActivePolicyError 在此之前已抛出，sticky 只读
        # 已启用策略的会话粘性配置。
        policy = snapshot.policy
        target = outcome.target
        configured_models = [item.model for item in target.models]
        models = [item.model for item in target.models if item.model in snapshot.backends]
        unregistered = {
            model: "unregistered" for model in configured_models if model not in snapshot.backends
        }
        policy_path = target.id
        preferred: str | None = None
        sticky_hit = False
        if not policy.sticky.enabled:
            sticky_status = "disabled"
        elif not request.session_id:
            sticky_status = "no_session"
        else:
            sticky_status = "not_checked"

        def append_schedule(selection):
            events.append({
                "event": "schedule",
                "target_id": target.id,
                "strategy": target.strategy,
                "configured_models": list(configured_models),
                "registered_models": list(models),
                "available_models": list(selection.get("available_models", [])),
                "unavailable": {**unregistered, **selection.get("unavailable", {})},
                "inflight": dict(selection.get("inflight", {})),
                "sticky": {"status": sticky_status, "preferred_model": preferred},
            })

        initial_selection = self.balancer.selection(models)
        available = list(initial_selection["available_models"])
        if not available:
            append_schedule(initial_selection)
            raise NoAvailableBackend(f"target {target.id!r} has no available backend")

        selection_reason = "target" if outcome.fallback_reason is None else "fallback"
        if policy.sticky.enabled and request.session_id:
            sticky_status = "miss"
            hit = self.affinity.get(
                request.session_id,
                policy_revision=snapshot.policy_revision,
                policy_path=policy_path,
                allowed_models=available,
                touch=not dry_run,
            )
            if hit is not None and hit[1] == target.id and hit[0] in available:
                preferred = hit[0]
                sticky_hit = True
                sticky_status = "hit"
                selection_reason = "sticky"

        try:
            lease = self.balancer.acquire(models, preferred=preferred, dry_run=dry_run)
        except NoAvailableBackend as exc:
            if preferred is None:
                append_schedule(exc.selection)
                raise
            # The sticky/preferred backend went away between the availability
            # snapshot and the lock (breaker opened or it was removed).  Re-read
            # the selection under the lock and schedule normally by least
            # in-flight instead of turning a dead sticky into a 503.
            preferred = None
            sticky_hit = False
            sticky_status = "unavailable"
            selection_reason = "target" if outcome.fallback_reason is None else "fallback"
            try:
                lease = self.balancer.acquire(models, dry_run=dry_run)
            except NoAvailableBackend as retry_exc:
                append_schedule(retry_exc.selection)
                raise
        append_schedule(lease.selection)
        model = lease.model
        reasoning = next(
            (item.reasoning for item in target.models if item.model == model),
            None,
        )
        if sticky_hit:
            events.append({"event": "sticky_hit", "model": model})
        if policy.sticky.enabled and request.session_id and not dry_run:
            self.affinity.put(
                request.session_id,
                model,
                target_id=target.id,
                policy_revision=snapshot.policy_revision,
                policy_path=policy_path,
                ttl_seconds=policy.sticky.ttl_seconds,
            )
        events.append({
            "event": "scheduled",
            "model": model,
            "reason": selection_reason,
            "dry_run": dry_run,
        })
        return RoutingDecision(
            model=model, target=target, reasoning=reasoning,
            policy_path=policy_path, trace=events, sticky_hit=sticky_hit,
            selection_reason=selection_reason, session_hash=session_digest(request.session_id),
            lease=lease, answers=dict(outcome.answers),
            fallback_reason=outcome.fallback_reason,
            matched_rule=outcome.matched_rule.name if outcome.matched_rule is not None else None,
            jev_duration_ms=outcome.jev_duration_ms,
            jev_questions=outcome.jev_questions,
        )
