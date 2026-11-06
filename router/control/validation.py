"""控制面启用校验：EnableContext 构造与图的启用级 Issue 列表（spec §5）。

所有管理路由与控制面服务统一使用本公开入口。校验器不做递归遍历，因此这里
只透传 Issue 列表（空列表 = 无问题）；由调用方决定如何呈现（API 422 / 服务层回滚）。
"""
from __future__ import annotations

import sqlite3

from router.api.issue_presenter import present_issue
from router.jev.config import JevConfigRepo
from router.routing.graph.validate import EnableContext, Issue, validate_graph_for_enable
from router.routing.tree import RoutingTree
from router.store.repositories import ModelRepo, TimePeriodRepo
from router.store.routing_trees import RoutingTreeRepo
from router.upstream.catalog import model_reasoning_from_db


def build_enable_context(conn: sqlite3.Connection) -> EnableContext:
    models = ModelRepo(conn).list()
    return EnableContext(
        known_models={row["id"] for row in models},
        model_reasoning=model_reasoning_from_db(conn),
        known_periods={row["name"] for row in TimePeriodRepo(conn).load()},
        jev_available=JevConfigRepo(conn).load() is not None,
    )


def issues_payload(issues) -> list[dict]:
    """Issue 列表 → spec §5 的 422 载荷（dict of 双语字段 + 机器定位）。

    Issue 本身只存机器字段；中文/英文在这里由 issue_presenter 统一生成，
    控制台收到的键集合与重构前一致。
    """
    payload = []
    for issue in issues:
        message = present_issue(issue)
        payload.append({
            "code": issue.code,
            "zh": message["zh"],
            "en": message["en"],
            "node_id": issue.node_id,
            "edge_id": issue.edge_id,
            "port": issue.port,
            "level": issue.level,
        })
    return payload


def tree_issues(conn: sqlite3.Connection, tree: RoutingTree) -> list[Issue]:
    """启用级校验任意树（active/草稿/已存关闭树共此入口）。"""
    return validate_graph_for_enable(tree, build_enable_context(conn))


def active_tree_issues(conn: sqlite3.Connection, trees: RoutingTreeRepo) -> list[Issue]:
    tree = trees.active()
    return [] if tree is None else tree_issues(conn, tree)
