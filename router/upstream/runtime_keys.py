"""熔断与 inflight 内部键的唯一构造入口。

图内逻辑模型 ID 在响应 ``x-sr-model``、请求账本 ``model``、图文档、调度
trace 与 ``RoutingDecision.model`` 里保持不变；只有 ``CircuitBreaker`` /
``BreakerStore`` / ``LoadBalancer`` 的 inflight 计数使用这里的运行键。

作用域由 ``RoutingSnapshot.tree_id`` 决定（跨副本稳定，不用进程 ID/对象
ID/随机数/tree_revision）：Auto 图路由得到 ``tree:<tree_id>``，固定模型直连
保持空 scope，因此裸键与任何 Auto 图互不共享熔断或 inflight 状态。
"""

from __future__ import annotations


def tree_scope(tree_id: str) -> str:
    """把图 ID 变成稳定作用域前缀；空/非字符串 ID 是编程错误，直接拒绝。"""
    if not isinstance(tree_id, str) or not tree_id:
        raise ValueError('tree_id must be nonempty')
    return f"tree:{tree_id}"


def runtime_model_key(scope: str, model: str) -> str:
    """作用域内的运行键；空 scope（固定模型直连）保持裸逻辑模型 ID。"""
    return f"{scope}:{model}" if scope else model
