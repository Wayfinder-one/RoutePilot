"""控制面启用校验：EnableContext 构造与策略 v4 的启用级 Issue 列表（设计 §7.2）。

所有管理路由与控制面服务统一使用本公开入口。校验器是纯函数，因此这里只
负责从数据库装配 ``EnableContext`` 并透传 Issue 列表（空列表 = 无问题）；
由调用方决定如何呈现（API 422 / 服务层回滚）。
"""
from __future__ import annotations

import sqlite3

from router.routing.policy.model import Policy
from router.routing.policy.validate import (
    EnableContext,
    Issue,
    validate_policy_for_enable,
)
from router.store.jev_config import JevConfigRepo
from router.store.repositories import ModelRepo, TimePeriodRepo
from router.upstream.catalog import model_reasoning_from_db


def build_enable_context(conn: sqlite3.Connection) -> EnableContext:
    models = ModelRepo(conn).list()
    return EnableContext(
        known_models={row["id"] for row in models},
        model_reasoning=model_reasoning_from_db(conn),
        known_periods={row["name"] for row in TimePeriodRepo(conn).load()},
        jev_available=JevConfigRepo(conn).load() is not None,
    )


def policy_issues(conn: sqlite3.Connection, policy: Policy) -> list[Issue]:
    """启用级校验任意策略（active/草稿/已存关闭策略共此入口）。"""
    return validate_policy_for_enable(policy, build_enable_context(conn))


def active_policy_issues(conn: sqlite3.Connection, policies) -> list[Issue]:
    """启用级校验当前启用策略；``policies.active()`` 返回 ``PolicyResource | None``。"""
    resource = policies.active()
    return [] if resource is None else policy_issues(conn, resource.policy)
