"""Pipeline health checks (design doc section 8). PUT /admin/pipeline runs
these and rejects with 422 on any issue. Evaluators assume a healthy tree;
this module is the gatekeeper that keeps the assumption true.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.pipeline.model import NODE_TYPES, Node
from router.pipeline.tree import (derive, is_rule_signal, leaf_paths,
                                  level_keys, orphan_branches, remaining_rows)
from router.signals.registry import SIGNALS


@dataclass
class Issue:
    code: str
    zh: str
    en: str
    node_id: str = ""
    branch_id: str = ""
    level: str = "hard"             # 'hard' rejects the save (422); 'warn' does not


def _split_issues(node: Node) -> list[Issue]:
    """SPEC-TREE-4 section 5: the five split-node checks.

    The tree itself is derived (tree.derive); dead/orphan branches are read off
    the derivation, so what health rejects is exactly what the engine would
    never reach."""
    issues: list[Issue] = []
    if not node.branches:
        issues.append(Issue("split_empty",
                            "分流节点 0 条分支（惰性节点，永不决策）",
                            "Split node has no branches; it never decides",
                            node_id=node.id, level="warn"))
        return issues

    level = derive(node, node.branches, set())
    keys = level_keys(level)

    # split_dead_branch: same parent path + identical remaining rows (+ the
    # same min_score, or the two-tier pattern would be legal) -> the later
    # branch can never win, ties keep the earlier one. The path is part of the
    # signature: identical leaves under DIFFERENT paths are the whole point of
    # grouping, not duplicates.
    seen: dict[tuple, str] = {}
    for branch, path in leaf_paths(level):
        used = {signal for signal, _value in path}
        rest = remaining_rows(branch, used)
        sig = (tuple(path),
               frozenset((r.signal, r.value, r.weight) for r in rest),
               branch.min_score)
        if sig in seen:
            issues.append(Issue("split_dead_branch",
                                "同一父路径下与上方分支条件完全相同，恒被压制",
                                "Identical to an earlier branch under the same "
                                "path; never wins", node.id, branch.id))
        else:
            seen[sig] = branch.id

    # split_orphan_value: a branch whose fork-key row carries an empty value
    # lands in the 「待选」 bucket, which no request can ever enter.
    for branch, path in orphan_branches(level):
        key = next((k for k, v in path if v is None), "?")
        issues.append(Issue("split_orphan_value",
                            f"分组条件 {key} 的取值为空，落入「待选」组，永不可达，请补值",
                            f"Fork key {key} has no value; the branch lands in "
                            f"the unreachable pending group",
                            node.id, branch.id))

    # split_dead_min_score: zero remaining conditions -> min_score never read.
    for branch, path in leaf_paths(level):
        used = {signal for signal, _value in path}
        if not remaining_rows(branch, used) and branch.min_score is not None:
            issues.append(Issue("split_dead_min_score",
                                "路径已完全确定（命中即路由），min_score 不再生效",
                                "Path is fully determined (hit routes at once); "
                                "min_score is ignored", node.id, branch.id,
                                level="warn"))

    # split_rule_floor: a floor that no layer can ever consult. A model-signal
    # layer anywhere still reads the floor, so only an all-rule / keyless tree
    # warns.
    if node.floor is not None and not any(not is_rule_signal(k) for k in keys):
        issues.append(Issue("split_rule_floor",
                            "分组键全是规则信号（确定性），进组门槛不会生效",
                            "Every fork key is a rule signal (deterministic); "
                            "the entry floor never applies", node.id,
                            level="warn"))
    return issues


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


def _rows_signature(branch) -> tuple:
    """Duplicate-branch signature: identical rows AND identical threshold.

    The design definition (section 8) is "weight rows identical and threshold
    identical" — same rows with a different min_score is the legitimate
    high-confidence / low-confidence two-tier pattern, so min_score is part of
    the signature. conf_min is included so two gate branches that differ only
    in their confidence threshold are not collapsed either.
    """
    rows = frozenset((r.signal, r.value, r.weight, r.conf_min)
                     for r in branch.rows)
    return (rows, branch.min_score)


def _gate_exclusive_row_issue(node, branch) -> Issue | None:
    """A gate branch ANDs its rows; two target values of the same single-valued
    signal can never both match, so the branch is dead."""
    targets: dict[str, set[str]] = {}
    for r in branch.rows:
        spec = SIGNALS.get(r.signal)
        if spec is not None and not spec.multi:
            targets.setdefault(r.signal, set()).add(r.value)
    for signal, values in targets.items():
        if len(values) > 1:
            return Issue("gate_exclusive_rows",
                         f"闸门分支内信号 {signal} 写了多个目标值，AND 语义下分支永假",
                         f"Gate branch pins signal {signal} to multiple values; "
                         f"with AND semantics the branch can never fire",
                         node.id, branch.id)
    return None


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
        seen_signatures: set[tuple] = set()
        for b in node.branches:
            needs_model = node.type in ("score", "split") or b.action == "route"
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
            if node.type == "gate":
                gate_issue = _gate_exclusive_row_issue(node, b)
                if gate_issue is not None:
                    issues.append(gate_issue)
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
        if node.type == "gate":
            # An empty-row gate branch always fires; anywhere but last it makes
            # every following branch unreachable (design doc section 8).
            for i, b in enumerate(node.branches):
                if not b.rows and i < len(node.branches) - 1:
                    issues.append(Issue("empty_branch_shadow",
                                        "空条件分支不在末位，其后分支永不可达",
                                        "An empty-row branch is not last; every "
                                        "branch behind it is unreachable",
                                        node.id, b.id))
        if node.type == "split":
            if node.floor is not None and not 0 <= node.floor <= 1:
                issues.append(Issue("floor_out_of_range",
                                    "进组门槛必须在 [0,1] 内",
                                    "Entry floor must be within [0,1]",
                                    node_id=node.id))
            issues.extend(_split_issues(node))
    last = nodes[-1]
    if last.type != "gate" or not any(b.action == "route" and not b.rows
                                      and b.model for b in last.branches):
        issues.append(Issue("missing_fallback",
                            "末节点必须是空条件的直达闸门（兜底）",
                            "Last node must be a gate with an empty-row route branch "
                            "(fallback)", node_id=last.id))
    return issues
