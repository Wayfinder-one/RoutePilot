"""控制面启用校验：active graph 的启用级问题列表（迁移自 router/api/routes_trees.py）。

返回 `Issue` 列表（空列表 = 无问题）；由调用方决定如何呈现（API 422 / 服务层回滚）。
"""
from __future__ import annotations

import sqlite3

from router.routing.graph.validate import Issue, validate_graph_for_enable
from router.routing.model import RoutingTree
from router.routing.snapshot import build_enable_context
from router.store.routing_trees import RoutingTreeRepo


def active_tree(trees: RoutingTreeRepo) -> RoutingTree | None:
    return trees.active()


def active_tree_issues(conn: sqlite3.Connection, trees: RoutingTreeRepo) -> list[Issue]:
    tree = active_tree(trees)
    if tree is None:
        return []
    try:
        return validate_graph_for_enable(tree, build_enable_context(conn))
    except RecursionError:
        return [Issue(
            "limit_exceeded",
            "图结构过深，校验无法在安全深度内完成（节点/边可能超过上限）",
            "Graph is too deep to validate safely (node/edge limits may be exceeded)",
        )]
