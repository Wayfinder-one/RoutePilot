"""Split-tree derivation (SPEC-TREE-4 section 3).

The tree of a ``split`` node is *derived*, never stored: ``derive`` is a pure
function of (node, branches). Same-signal branches group automatically, level
by level; conditions no level could consume stay on the leaf and are compared
by weight. The console re-implements this exact algorithm in JS — both sides
must stay in sync (the acceptance list pins determinism).
"""

from __future__ import annotations

from router.pipeline.model import DEFAULT_FLOOR, Branch, Node
from router.signals.registry import SIGNALS


def is_rule_signal(key: str) -> bool:
    """Rule signals come straight from request attributes (confidence == 1).

    「确定性」≠「置信度 1」: a confidence of 1.0 can still be filtered by a
    floor; deterministic signals never take part in threshold checks at all.
    """
    spec = SIGNALS.get(key)
    return spec is not None and spec.kind == "rule"


def signal_order(node: Node) -> list[str]:
    """Candidate fork keys in first-appearance order (skip empty values).

    Ordering by first appearance guarantees the same data always derives the
    same tree (acceptance: derive is a pure function).
    """
    seen: list[str] = []
    for b in node.branches:
        for r in b.rows:
            if r.value and r.signal not in seen:
                seen.append(r.signal)
    return seen


def next_fork_key(node: Node, branches: list[Branch], used: set[str]) -> str | None:
    """The signal every branch of this bucket carries and no level used yet.

    Two branches minimum (a single branch has no layer to split; it scores per
    section 4.2). Values do NOT have to differ — equal values still surface the
    trunk first, and the group simply has one child.
    """
    if len(branches) < 2:
        return None
    for sig in signal_order(node):
        if sig in used:
            continue
        if all(any(r.signal == sig for r in b.rows) for b in branches):
            return sig
    return None


def _root_key(node: Node, branches: list[Branch], used: set[str]) -> str | None:
    """Root fork key: the manual override applies ONLY at the root level
    (SPEC-TREE-4 §3.3) and only when every branch carries the signal; deeper
    levels always derive."""
    if not used and node.type == "split" and node.split_signal:
        if branches and all(
                any(r.signal == node.split_signal for r in b.rows)
                for b in branches):
            return node.split_signal
    return next_fork_key(node, branches, used)


def derive(node: Node, branches: list[Branch], used: set[str]) -> dict:
    """Recursively group branches by fork key.

    Returns ``{"key", "groups": [{"value", "child"}]}`` or, when nothing can
    fork, ``{"leaves": [Branch]}``. A branch whose key row is empty-valued
    (「待选值」) lands in the ``value=None`` bucket — rendered as 「待选」 and
    rejected by health as unreachable (section 5).
    """
    if not branches:
        return {"leaves": []}
    key = _root_key(node, branches, used)
    if key is None:
        return {"leaves": list(branches)}
    buckets: dict[str | None, list[Branch]] = {}
    for b in branches:
        row = next((r for r in b.rows if r.signal == key), None)
        # An empty value (「待选值」 chip) or a missing row both land in the
        # None bucket: rendered as 「待选」, unreachable, rejected by health.
        value = (row.value if row is not None else "") or None
        buckets.setdefault(value, []).append(b)
    child_used = set(used) | {key}
    return {"key": key,
            "groups": [{"value": value, "child": derive(node, bs, child_used)}
                       for value, bs in buckets.items()]}


def count_leaves(level: dict) -> int:
    if "leaves" in level:
        return len(level["leaves"])
    return sum(count_leaves(g["child"]) for g in level["groups"])


def level_keys(level: dict, keys: list[str] | None = None) -> list[str]:
    """Every fork key of the derivation, in layer order (deduplicated)."""
    keys = [] if keys is None else keys
    if "key" in level and level["key"] not in keys:
        keys.append(level["key"])
    for g in level.get("groups", []):
        level_keys(g["child"], keys)
    return keys


def orphan_branches(level: dict, path: list[tuple[str, str | None]] | None = None
                    ) -> list[tuple[Branch, list[tuple[str, str | None]]]]:
    """Branches that fell into a 待选 (value=None) bucket: unreachable.

    Each hit carries its own (signal, value) ancestor path, so health can name
    the key the branch is missing a value for."""
    path = [] if path is None else path
    found: list[tuple[Branch, list[tuple[str, str | None]]]] = []
    if "leaves" in level:
        if path and any(v is None for _k, v in path):
            found.extend((b, path) for b in level["leaves"])
        return found
    for g in level["groups"]:
        found.extend(orphan_branches(
            g["child"], path + [(level["key"], g["value"])]))
    return found


def leaf_paths(level: dict, path: list[str] | None = None
               ) -> list[tuple[Branch, list[tuple[str, str]]]]:
    """Every leaf with its full (signal, value) path — for health checks."""
    path = [] if path is None else path
    if "leaves" in level:
        return [(b, path) for b in level["leaves"]]
    out: list[tuple[Branch, list[tuple[str, str]]]] = []
    for g in level["groups"]:
        if g["value"] is not None:
            out.extend(leaf_paths(g["child"],
                                  path + [(level["key"], g["value"])]))
    return out


def remaining_rows(branch: Branch, used: set[str]) -> list:
    """Rows no ancestor level consumed, i.e. the leaf's own conditions."""
    return [r for r in branch.rows if r.signal not in used]


def node_floor(node: Node) -> float:
    """Effective entry floor (None → 0.70, SPEC-TREE-4 section 2.1)."""
    return DEFAULT_FLOOR if node.floor is None else node.floor
