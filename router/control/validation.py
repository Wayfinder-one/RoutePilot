"""控制面启用校验：EnableContext 构造与图的启用级 Issue 列表（spec §5）。

所有管理路由与控制面服务统一使用本公开入口。校验器不做递归遍历，因此这里
只透传 Issue 列表（空列表 = 无问题）；由调用方决定如何呈现（API 422 / 服务层回滚）。

启用校验的事实源只有一个：画布自身的运行配置（``tree.runtime``）。数据库目录
不参与启用判断；数据库与画布的差异只由检查提示（runtime_mismatch，后续单元）
报告，不改变校验/执行结果。
"""
from __future__ import annotations

import sqlite3

from router.control.runtime import enable_context_for_runtime
from router.routing.graph.validate import Issue, validate_graph_for_enable
from router.routing.tree import RoutingTree
from router.store.routing_trees import RoutingTreeRepo


def tree_issues(conn: sqlite3.Connection, tree: RoutingTree) -> list[Issue]:
    """启用级校验任意树（active/草稿/已存关闭树共此入口）。

    ``conn`` 仅为调用方签名兼容保留：启用上下文完全来自画布内嵌运行配置。
    """
    return validate_graph_for_enable(tree, enable_context_for_runtime(tree.runtime))


def enabled_tree_issues(conn: sqlite3.Connection, trees: RoutingTreeRepo) -> list[Issue]:
    """校验所有启用图，按树 id 排序、逐图 Issue 顺序合并。

    快照发布会装配全部启用图，因此任何一份启用图校验失败都必须整体拒绝；
    集合不变量（默认图/部门唯一）由装配边界 ``load_snapshot_set()`` 负责，
    本函数只做逐图启用校验，不重复推导路由集索引。
    """
    issues: list[Issue] = []
    for tree in sorted(trees.enabled(), key=lambda item: item.id):
        issues.extend(tree_issues(conn, tree))
    return issues
