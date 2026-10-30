from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from router.jev.client import JevCandidate
from router.jev.window import apply_window
from router.routing.conditions import matching_periods, when_matches
from router.routing.model import Branch, Jev, Node, Target
from router.routing.snapshot import RoutingSnapshot
from router.routing.types import (
    NoActiveTreeError,
    NoAvailableBackend,
    RoutingDecision,
    RoutingMiss,
    RoutingRequest,
    session_digest,
)


class RoutingEngine:
    def __init__(self, jev_client, affinity, balancer):
        self.jev_client = jev_client
        self.affinity = affinity
        self.balancer = balancer

    async def route(
        self,
        snapshot: RoutingSnapshot,
        request: RoutingRequest,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> RoutingDecision:
        if snapshot.tree is None:
            raise NoActiveTreeError("no routing tree is enabled")
        tree = snapshot.tree
        timezone = ZoneInfo(tree.timezone)
        if now is None:
            local_now = datetime.now(timezone)
        elif now.tzinfo is None:
            local_now = now.replace(tzinfo=timezone)
        else:
            local_now = now.astimezone(timezone)
        active_periods = matching_periods(local_now, list(snapshot.time_periods))
        trace: list[dict] = [{"event": "time", "active_periods": active_periods, "timezone": tree.timezone}]
        policy, policy_path = self._policy(tree.root, active_periods, trace)

        def available(targets):
            models = self._models(targets, snapshot)
            health_filter = getattr(self.balancer, "available", None)
            return health_filter(models) if health_filter is not None else models

        targets = self._targets(policy)
        allowed_models = available(targets)
        path_text = "/".join(policy_path)
        preferred = None
        if tree.sticky.enabled and request.session_id:
            candidate = self.affinity.get(
                request.session_id,
                tree_revision=snapshot.tree_revision,
                policy_path=path_text,
                allowed_models=allowed_models,
                touch=not dry_run,
            )
            if candidate in allowed_models:
                preferred = candidate
        target = None
        answer = None
        sticky_hit = False
        fallback_reason = None
        if preferred is not None:
            target = next(target for target in targets if preferred in self._models((target,), snapshot))
            selection_reason = "sticky"
            sticky_hit = True
            trace.append({"event": "sticky_hit", "model": preferred, "policy_path": path_text})
        elif isinstance(policy, Target):
            target = policy
            selection_reason = "direct"
        elif isinstance(policy, Jev):
            target, selection_reason, answer, fallback_reason = await self._choose(
                snapshot, policy, request.text, trace
            )
        else:
            raise RoutingMiss("no target matched")
        models = available((target,))
        if not models:
            raise NoAvailableBackend("target has no configured backend")
        lease = self.balancer.acquire(models, preferred=preferred, dry_run=dry_run)
        model = lease.model
        reasoning = next(item.reasoning for item in target.models if item.model == model)
        if tree.sticky.enabled and request.session_id and not dry_run:
            self.affinity.put(
                request.session_id,
                model,
                tree_revision=snapshot.tree_revision,
                policy_path=path_text,
                ttl_seconds=tree.sticky.ttl_seconds,
            )
        trace.append({
            "event": "scheduled",
            "target_id": target.id,
            "model": model,
            "reason": selection_reason,
            "dry_run": dry_run,
        })
        return RoutingDecision(
            model=model,
            target=target,
            reasoning=reasoning,
            policy_path=path_text,
            trace=trace,
            sticky_hit=sticky_hit,
            selection_reason=selection_reason,
            session_hash=session_digest(request.session_id),
            lease=lease,
            jev_answer=answer,
            fallback_reason=fallback_reason,
        )

    def _policy(self, node: Node, active_periods: list[str], trace: list[dict]) -> tuple[Node, list[str]]:
        if not isinstance(node, Branch):
            return node, [node.id]
        for child in node.children:
            if not when_matches(child.when, active_periods):
                trace.append({"event": "branch_skipped", "node_id": child.id})
                continue
            trace.append({"event": "branch_selected", "node_id": node.id, "child_id": child.id})
            selected, path = self._policy(child, active_periods, trace)
            return selected, [node.id, *path]
        if node.otherwise is None:
            raise RoutingMiss(f"branch {node.id} has no fallback")
        trace.append({"event": "branch_otherwise", "node_id": node.id, "child_id": node.otherwise.id})
        selected, path = self._policy(node.otherwise, active_periods, trace)
        return selected, [node.id, *path]

    @staticmethod
    def _targets(policy: Node) -> tuple[Target, ...]:
        if isinstance(policy, Target):
            return (policy,)
        if isinstance(policy, Jev):
            targets = [option.target for option in policy.options]
            if policy.fallback is not None:
                targets.append(policy.fallback)
            return tuple(targets)
        return ()

    @staticmethod
    def _models(targets: tuple[Target, ...], snapshot: RoutingSnapshot) -> list[str]:
        models = []
        for target in targets:
            for item in target.models:
                if item.model in snapshot.backends and item.model not in models:
                    models.append(item.model)
        return models

    async def _choose(self, snapshot: RoutingSnapshot, node: Jev, text: str, trace: list[dict]):
        answer = None
        target = None
        reason = None
        try:
            if snapshot.jev is None:
                raise RuntimeError("JEV is not configured")
            candidates = tuple(
                JevCandidate(option.key, option.label, self._capability(snapshot, option.target))
                for option in node.options
            )
            window = apply_window(text, snapshot.jev_input_tokens)
            if window.truncated:
                trace.append({
                    "event": "jev_input_window",
                    "node_id": node.id,
                    "input_tokens_limit": snapshot.jev_input_tokens,
                    "tokens_before": window.tokens_before,
                    "tokens_after": window.tokens_after,
                    "truncated": True,
                })
            answer = await self.jev_client.choose(
                snapshot.jev,
                window.text,
                candidates,
                instructions=node.instructions,
            )
            event = {
                "event": "jev_choice",
                "node_id": node.id,
                "candidates": [option.key for option in node.options],
                "probabilities": answer.probabilities,
                "ranking": answer.ranking,
                "selected_key": answer.selected_key,
                "p_top": answer.p_top,
                "margin": answer.margin,
                "duration_ms": answer.duration_ms,
                "usage": answer.usage,
            }
            if answer.p_top < node.min_prob:
                reason = "jev_low_probability"
            elif answer.margin < node.min_margin:
                reason = "jev_ambiguous"
            else:
                target = next(option.target for option in node.options if option.key == answer.selected_key)
            event["reason"] = reason
            trace.append(event)
        except Exception as exc:
            reason = "jev_error"
            trace.append({"event": "jev_choice", "node_id": node.id, "reason": reason, "error": type(exc).__name__})
        if target is None:
            if node.fallback is None:
                raise RoutingMiss(f"JEV node {node.id} has no fallback")
            target = node.fallback
            trace.append({"event": "fallback", "node_id": node.id, "target_id": target.id, "reason": reason})
            return target, "fallback", answer, reason
        return target, "jev", answer, None

    @staticmethod
    def _capability(snapshot: RoutingSnapshot, target: Target) -> str:
        if target.capability.strip():
            return target.capability.strip()
        if len(target.models) == 1:
            return snapshot.capabilities.get(target.models[0].model, "")
        return ""
