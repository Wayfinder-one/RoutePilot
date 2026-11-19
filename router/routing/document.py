"""Routing Policy v4 文档 bytes 编解码：存储层唯一面向的文档写入/读入边界。

KIND/VERSION 在 :mod:`router.routing.policy.model` 定义并在此再导出，旧 graph
函数按设计整体移除，不做兼容层。
"""
from __future__ import annotations

import json

from router.routing.policy.model import (
    KIND,
    VERSION,
    PolicyResource,
    policy_from_dict,
    policy_revision_of,
    policy_to_dict,
)

__all__ = [
    "KIND",
    "VERSION",
    "parse_policy_document",
    "policy_document_bytes",
]


def policy_document_bytes(resource: PolicyResource) -> bytes:
    text = json.dumps(
        policy_to_dict(resource.policy), ensure_ascii=False,
        indent=2, allow_nan=False,
    ) + "\n"
    return text.encode("utf-8")


def parse_policy_document(content: bytes, *, policy_id: str, enabled: bool,
                          updated_at: str, document_sha256: str) -> PolicyResource:
    try:
        raw = json.loads(content.decode("utf-8"))
        policy = policy_from_dict(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # JSON 解码是唯一能由任意嵌套输入触发递归的边界；在这里一次性归一为
        # ValueError，存储/控制/API 层只处理 ValueError，不再各自兜 RecursionError。
        raise ValueError("policy document is not valid Routing Policy v4") from exc
    return PolicyResource(
        id=policy_id, policy=policy, enabled=enabled,
        policy_revision=policy_revision_of(policy), updated_at=updated_at,
        document_sha256=document_sha256,
    )
