"""上游目录：models ⋈ providers 的模型目录与推理能力归一化。

热路径纪律（设计文档 §4.4）：routing 不访问数据库。应用启动（以及每次
admin 写 model/provider）时整体重建 ``model_id -> Backend`` 快照，/v1
只读内存快照。

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

    Hot path does zero parsing: this runs at startup and on every admin write
    that swaps the snapshot. A corrupt/illegal declaration must never prevent
    the service from starting (old databases), so fall back to "undeclared".
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
    """Build the model_id -> Backend snapshot in one join.

    A model whose provider row is missing is simply absent from the snapshot;
    the /v1 handler then answers 503 ``no_available_backend`` for that model.
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


def reasoning_map_for_rows(conn: sqlite3.Connection,
                           rows) -> dict[str, dict | None]:
    """model_id -> normalized reasoning for a model-catalog row set.

    ``rows`` are catalog dicts (``id``/``profile``/``provider``) and may be a
    **prospective** snapshot that is not stored yet (a pending POST/PUT
    /admin/models).  The provider ``reasoning_format`` comes from the providers
    table (missing provider -> ``"openai"``), exactly like
    :func:`load_backends`/:func:`model_reasoning_from_db`: one rule for boot,
    hot-path backends and admin-write validation, so a write can never be
    accepted by the API and rejected by the next startup.
    """
    formats = {row["name"]: (row["reasoning_format"] or "openai")
               for row in conn.execute(
                   "SELECT name, reasoning_format FROM providers")}
    return {
        row["id"]: normalize_reasoning(
            parse_profile_text(row.get("profile")),
            formats.get(row.get("provider"), "openai"))
        for row in rows}


def model_reasoning_from_db(conn: sqlite3.Connection) -> dict[str, dict | None]:
    """model_id -> normalized reasoning capability of the CURRENT catalog.

    Same source for every consumer that must never disagree about a model's
    declared levels (startup check, ``/admin/trees`` enable validation):
    rows written straight into the DB (bypassing the API) are judged by the
    same rule as rows coming through the API.
    """
    rows = [{"id": row["id"], "profile": row["profile"],
             "provider": row["provider"]}
            for row in conn.execute(
                "SELECT id, profile, provider FROM models")]
    return reasoning_map_for_rows(conn, rows)
