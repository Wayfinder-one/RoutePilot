"""路由集合的不变量：默认启用图与部门 → 图映射（纯函数，不触碰存储）。

v4 文档自带 ``enabled`` / ``default``，仓储只按文件内容扫描。路由集合的
不变量在这里一次性表达：

- 有启用图时恰好一张默认图；零启用图合法（返回 ``None``）。
- 部门在所有启用图之间唯一，默认图负责兜底。

本模块只做索引，不构造快照、不做部门选图执行（后续单元实现
``RoutingSnapshotSet.select()``）。``RouteSetInvariantError`` 由装配边界
（仓储读取、快照发布）转换成存储错误，避免损坏的文件把请求打成 500。
"""
from __future__ import annotations

from collections.abc import Iterable

from router.routing.tree import RoutingTree


class RouteSetInvariantError(ValueError):
    """路由集合不变量被破坏：``no_default_tree`` / ``department_conflict``。"""

    def __init__(self, code, *, department='', tree_ids=()):
        super().__init__(code)
        self.code = code
        self.department = department
        self.tree_ids = tuple(tree_ids)


def route_set_indices(trees: Iterable[RoutingTree]) -> tuple[str | None, dict[str, str]]:
    """返回 ``(默认图 id, 部门 → 图 id 映射)``；零启用图返回 ``(None, {})``。"""
    enabled = list(trees)
    defaults = [tree.id for tree in enabled if tree.default]
    if enabled and len(defaults) != 1:
        raise RouteSetInvariantError('no_default_tree', tree_ids=defaults)
    default = defaults[0] if defaults else None
    mapping = {}
    for tree in sorted(enabled, key=lambda item: item.id):
        for department in tree.departments:
            if department in mapping:
                raise RouteSetInvariantError(
                    'department_conflict', department=department,
                    tree_ids=(mapping[department], tree.id))
            mapping[department] = tree.id
    return default, mapping


def default_enabled_tree(trees: Iterable[RoutingTree]) -> RoutingTree | None:
    """过渡期辅助：路由集中唯一的默认启用图；零启用图返回 ``None``。

    集合不变量被破坏时抛 ``RouteSetInvariantError``。Task 3 引入
    ``RoutingSnapshotSet`` 后，本函数的调用点全部改为 ``default_snapshot``。
    """
    rows = list(trees)
    default_id, _ = route_set_indices(rows)
    if default_id is None:
        return None
    return next(tree for tree in rows if tree.id == default_id)
