"""Pipeline health checks (design doc section 8). PUT /admin/pipeline runs
these and rejects with 422 on any issue. Evaluators assume a healthy tree;
this module is the gatekeeper that keeps the assumption true.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.model import NODE_TYPES, Node
from router.signals.registry import SIGNALS


@dataclass
class Issue:
    code: str
    zh: str
    en: str
    node_id: str = ""
    branch_id: str = ""


def _attainable_cap(rows) -> tuple[float, float]:
    """Max attainable score for a score branch. A single-valued signal can
    only match one target, so for signals written with multiple targets only
    the heaviest row can count. Returns (cap, sum_w)."""
    sum_w = sum(r.weight or 0.0 for r in rows)
    by_signal: dict[str, dict[str, float]] = {}
    for r in rows:
        spec = SIGNALS.get(r.signal)
        if spec is not None and not spec.multi:
            by_signal.setdefault(r.signal, {})[r.value] = \
                max(by_signal.get(r.signal, {}).get(r.value, 0.0), r.weight or 0.0)
    lost = 0.0
    for targets in by_signal.values():
        if len(targets) > 1:
            lost += sum(targets.values()) - max(targets.values())
    return (sum_w - lost), sum_w


def _rows_signature(branch) -> frozenset:
    return frozenset((r.signal, r.value, r.weight) for r in branch.rows)


def check_pipeline(nodes: list[Node], known_models: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    if not nodes:
        issues.append(Issue("empty_pipeline", "管道为空", "Pipeline is empty"))
        return issues
    for node in nodes:
        if node.type not in NODE_TYPES:
            issues.append(Issue("unknown_node_type",
                                f"未知节点类型 {node.type}", f"Unknown node type {node.type}",
                                node_id=node.id))
            continue
        seen_signatures: set[frozenset] = set()
        for b in node.branches:
            needs_model = node.type == "score" or b.action == "route"
            if needs_model and not b.model:
                issues.append(Issue("missing_model", "分支缺少去向模型",
                                    "Branch has no target model",
                                    node.id, b.id))
            elif needs_model and b.model not in known_models:
                issues.append(Issue("unknown_model", f"未知模型 {b.model}",
                                    f"Unknown model {b.model}", node.id, b.id))
            for r in b.rows:
                if r.signal not in SIGNALS:
                    issues.append(Issue("unknown_signal", f"未知信号 {r.signal}",
                                        f"Unknown signal {r.signal}", node.id, b.id))
            if node.type == "score":
                cap, sum_w = _attainable_cap(b.rows)
                if sum_w == 0:
                    issues.append(Issue("zero_weight", "分支权重全为 0",
                                        "All weights are zero", node.id, b.id))
                elif b.min_score is not None and cap / sum_w < b.min_score:
                    issues.append(Issue("dead_branch",
                                        "存在互斥行，理论最高分低于门槛分，分支恒不可达",
                                        "Mutually exclusive rows cap the attainable score "
                                        "below min_score; branch can never win",
                                        node.id, b.id))
                sig = _rows_signature(b)
                if sig in seen_signatures:
                    issues.append(Issue("duplicate_branch",
                                        "与上方分支完全相同，恒被压制",
                                        "Identical to an earlier branch; never wins",
                                        node.id, b.id))
                seen_signatures.add(sig)
    last = nodes[-1]
    if last.type != "gate" or not any(b.action == "route" and not b.rows
                                      and b.model for b in last.branches):
        issues.append(Issue("missing_fallback",
                            "末节点必须是空条件的直达闸门（兜底）",
                            "Last node must be a gate with an empty-row route branch "
                            "(fallback)", node_id=last.id))
    return issues
