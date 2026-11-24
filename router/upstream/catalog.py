"""上游目录：models ⋈ providers 的模型目录与推理能力归一化。

本模块是 frozen ``Backend`` 的定义处，并提供「从 SQLite 目录装配
``model_id -> Backend``」的唯一入口 :func:`load_backends`。它只服务**固定模型
直连**（管理端测试对话、``/admin/test``）与目录检查：Auto 路由始终读取启用图
发布的运行快照（``router.routing.snapshot``，由 ``router.control.runtime`` 按
画布内嵌运行配置装配），既不访问数据库，也不经过本模块。``/admin/models`` /
``/admin/providers`` 的目录写不会重写任何画布，因此这里也不存在「每次目录写
重建快照」的路径。

``Backend`` 是 frozen dataclass：快照构造时复制 ``headers``/``reasoning``，
发布后的 MappingProxy 里不再包着可被写者顺手改掉的可变对象。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from router.reasoning import normalize_reasoning


@dataclass(frozen=True)
class Backend:
    url: str
    headers: dict
    upstream_name: str
    #: 归一化后的推理能力（§3.2）；None = 未声明/明确不支持 → 热路径透传。
    reasoning: dict | None = None
    provider: str = ""


def _headers_for(key_env: str, api_key: str = "") -> dict:
    # Stored key wins over the environment (contract §5.3); empty both = no auth.
    key = api_key or (os.environ.get(key_env, "") if key_env else "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _reasoning_capability(profile_text: str | None,
                          provider_format: str) -> dict | None:
    """Parse profile JSON + normalize; unusable/illegal declarations -> None.

    Auto 路由热路径零解析：本函数只在固定模型直连按 SQLite 目录装配
    ``load_backends`` 时运行。损坏/非法的旧声明绝不能阻止服务启动，因此回退为
    「未声明」。
    """
    if not profile_text:
        return None
    try:
        profile = json.loads(profile_text)
    except (TypeError, ValueError):
        return None
    normalized = normalize_reasoning(profile, provider_format)
    if not normalized or not normalized.get("declared") \
            or not normalized.get("supported"):
        return None
    return normalized


def load_backends(conn: sqlite3.Connection) -> dict[str, Backend]:
    """Build the model_id -> Backend catalog for fixed-model direct requests.

    A model whose provider row is missing is simply absent from the catalog; the
    fixed-model direct handler then answers 404 ``unknown_model`` for it. Auto
    routing never calls this: it reads the published canvas snapshot only.
    """
    rows = conn.execute(
        "SELECT m.id AS id, m.upstream AS upstream, m.provider AS provider, "
        "       m.profile AS profile, p.base_url AS base_url, "
        "       p.key_env AS key_env, p.api_key AS api_key, "
        "       p.reasoning_format AS reasoning_format "
        "FROM models m JOIN providers p ON m.provider = p.name")
    return {r["id"]: Backend(
        url=f"{r['base_url']}/chat/completions",
        headers=_headers_for(r["key_env"], r["api_key"] or ""),
        upstream_name=r["upstream"],
        reasoning=_reasoning_capability(r["profile"], r["reasoning_format"]),
        provider=r["provider"])
        for r in rows}


def parse_profile_text(text: str | None) -> dict:
    """models.profile JSON text -> dict; empty/broken JSON behaves as undeclared."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
