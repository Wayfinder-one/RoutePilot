from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from router.jev.client import JevCandidate
from router.jev.window import apply_window
from router.routing.conditions import matching_periods, when_matches
from router.routing.model import Branch, Jev, Node, Target, walk_nodes
from router.routing.snapshot import RoutingSnapshot
from router.routing.trace_display import bilingual, described
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
        trace_sink: list[dict] | None = None,
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
        trace: list[dict] = trace_sink if trace_sink is not None else []
        if any(node.when is not None for node in walk_nodes(tree.root)):
            periods = ", ".join(active_periods)
            trace.append(described(
                {"event": "time", "active_periods": active_periods, "timezone": tree.timezone},
                phase="time", label=bilingual("时间条件", "Time condition"),
                cell=bilingual(periods or "未命中", periods or "No match"),
                summary=bilingual(f"匹配时段：{periods}" if periods else "没有匹配的时段",
                                  f"Matched periods: {periods}" if periods else "No matching period"),
                facts=((bilingual("时区", "Timezone"), tree.timezone),
                       (bilingual("匹配时段", "Matched periods"), periods or "—")),
                tone="hit" if active_periods else "",
            ))
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
            trace.append(described(
                {"event": "sticky_hit", "model": preferred, "policy_path": path_text},
                phase="sticky", label=bilingual("粘性命中", "Sticky hit"),
                cell=preferred, summary=bilingual(f"沿用会话模型 {preferred}", f"Reused session model {preferred}"),
                facts=((bilingual("模型", "Model"), preferred),
                       (bilingual("策略路径", "Policy path"), path_text)), tone="hit",
            ))
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
        trace.append(described({
            "event": "scheduled",
            "target_id": target.id,
            "model": model,
            "reason": selection_reason,
            "dry_run": dry_run,
        }, phase="schedule", label=bilingual("模型调度", "Model schedule"),
            cell=model, summary=bilingual(f"选择模型 {model}", f"Selected model {model}"),
            facts=((bilingual("目标", "Target"), target.id),
                   (bilingual("选择原因", "Selection reason"), selection_reason)), tone="hit"))
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
                trace.append(described(
                    {"event": "branch_skipped", "node_id": child.id},
                    phase="condition", label=bilingual("条件判断", "Condition"),
                    cell=bilingual("未命中", "No match"),
                    summary=bilingual(f"分支 {child.id} 未命中", f"Branch {child.id} did not match"),
                    facts=((bilingual("分支", "Branch"), child.id),),
                ))
                continue
            trace.append(described(
                {"event": "branch_selected", "node_id": node.id, "child_id": child.id},
                phase="condition" if child.when is not None else "branch",
                label=bilingual("条件判断", "Condition") if child.when is not None
                      else bilingual("分支选择", "Branch selection"),
                cell=child.id,
                summary=bilingual(f"命中分支 {child.id}", f"Matched branch {child.id}")
                        if child.when is not None else bilingual(f"进入分支 {child.id}", f"Entered branch {child.id}"),
                facts=((bilingual("决策节点", "Decision node"), node.id),
                       (bilingual("命中分支", "Matched branch"), child.id)), tone="hit",
            ))
            selected, path = self._policy(child, active_periods, trace)
            return selected, [node.id, *path]
        if node.otherwise is None:
            raise RoutingMiss(f"branch {node.id} has no fallback")
        trace.append(described(
            {"event": "branch_otherwise", "node_id": node.id, "child_id": node.otherwise.id},
            phase="condition", label=bilingual("条件判断", "Condition"),
            cell=node.otherwise.id,
            summary=bilingual(f"使用默认分支 {node.otherwise.id}", f"Used default branch {node.otherwise.id}"),
            facts=((bilingual("决策节点", "Decision node"), node.id),
                   (bilingual("默认分支", "Default branch"), node.otherwise.id)), tone="",
        ))
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
                trace.append(described({
                    "event": "jev_input_window",
                    "node_id": node.id,
                    "input_tokens_limit": snapshot.jev_input_tokens,
                    "tokens_before": window.tokens_before,
                    "tokens_after": window.tokens_after,
                    "truncated": True,
                }, phase="window", label=bilingual("JEV 输入窗口", "JEV input window"),
                    cell=f"{window.tokens_after} tok",
                    summary=bilingual(f"输入从 {window.tokens_before} 截断至 {window.tokens_after} token",
                                      f"Truncated input from {window.tokens_before} to {window.tokens_after} tokens"),
                    facts=((bilingual("上限", "Limit"), snapshot.jev_input_tokens),
                           (bilingual("截断前", "Before"), window.tokens_before),
                           (bilingual("截断后", "After"), window.tokens_after)), tone="warn"))
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
            selected = answer.selected_key
            summary_zh = f"选择 {selected}" if reason is None else f"JEV 未通过阈值：{reason}"
            summary_en = f"Selected {selected}" if reason is None else f"JEV threshold not met: {reason}"
            trace.append(described(
                event, phase="jev", label=bilingual("JEV 选择", "JEV choice"),
                cell=selected if reason is None else reason,
                summary=bilingual(summary_zh, summary_en),
                facts=((bilingual("最高概率", "Top probability"), answer.p_top),
                       (bilingual("概率差", "Margin"), answer.margin),
                       (bilingual("耗时 ms", "Duration ms"), answer.duration_ms)),
                tone="hit" if reason is None else "warn",
            ))
        except Exception as exc:
            reason = "jev_error"
            trace.append(described(
                {"event": "jev_choice", "node_id": node.id, "reason": reason, "error": type(exc).__name__},
                phase="jev", label=bilingual("JEV 选择", "JEV choice"),
                cell=bilingual("不可用", "Unavailable"),
                summary=bilingual("JEV 调用失败，尝试兜底", "JEV call failed; trying fallback"),
                facts=((bilingual("错误类型", "Error type"), type(exc).__name__),), tone="warn",
            ))
        if target is None:
            if node.fallback is None:
                raise RoutingMiss(f"JEV node {node.id} has no fallback")
            target = node.fallback
            trace.append(described(
                {"event": "fallback", "node_id": node.id, "target_id": target.id, "reason": reason},
                phase="fallback", label=bilingual("兜底选择", "Fallback"),
                cell=target.id,
                summary=bilingual(f"兜底到 {target.id}", f"Fell back to {target.id}"),
                facts=((bilingual("原因", "Reason"), reason),
                       (bilingual("目标", "Target"), target.id)), tone="warn",
            ))
            return target, "fallback", answer, reason
        return target, "jev", answer, None

    @staticmethod
    def _capability(snapshot: RoutingSnapshot, target: Target) -> str:
        if target.capability.strip():
            return target.capability.strip()
        if len(target.models) == 1:
            return snapshot.capabilities.get(target.models[0].model, "")
        return ""
