"""管理面模型 / provider 目录：读取、影响确认与确认后的原子画布同步。

严格自包含画布语义下，数据库目录是**管理面**事实源：更新/删除模型或 provider
不会无确认地重写画布。写用例分两阶段：

1. 扫描全部画布得到影响清单，未携带确认的第一次写入返回 409
   ``impact_confirmation_required``（数据库、画布文件、审计、快照全不变）；
2. 携带每个画布当前 ``document_sha256`` 的确认重试，由
   ``CatalogSyncService`` 在单写锁 + 单数据库事务内写目录行、校验并原子替换
   受影响画布、记录审计；同步集合里含任一启用图时重发布完整启用路由集。

哈希不匹配（过期确认）返回 409 ``graph_conflict`` 并报告当前哈希。确认后的
提交还会在写锁内、事务内重算一次最新引用/影响（``recheck``）：路由层扫描之后
新出现的画布/模型引用会变成 409，而不是漏同步。

provider 删除不提供隐式替换、也不可被确认绕过：只要数据库模型或**画布模型**
仍引用它就拒绝；画布运行时目录里只有它的 provider 条目、没有任何模型引用时
不阻塞删除（影响清单仍如实列出）。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.control.catalog_sync import CatalogSyncService
from router.control.errors import AlreadyExists, MissingError, ReferencedByError
from router.control.impacts import (
    delete_model as delete_tree_model,
    model_impacts,
    provider_impacts,
    require_confirmation,
    sync_model,
    sync_provider,
)
from router.presets import PROVIDER_PRESETS
from router.reasoning import (
    FORMATS,
    FORMAT_IDS,
    LEVELS,
    ReasoningError,
    normalize_reasoning,
    validate_model_profile,
)
from router.routing.runtime import (
    reject_runtime_secret_fields,
    without_runtime_secret_fields,
)
from router.store.repositories import (
    ChangeRepo,
    ModelRepo,
    ProviderRepo,
    model_ids_using_provider,
    now_iso,
)
from router.store.routing_trees import RoutingTreeRepo
from router.upstream.catalog import parse_profile_text

router = APIRouter()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_CAPABILITY_CHARS = 2000


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _clean_text(value, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise HTTPException(422, detail=f"{field} 必须是字符串")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_len or _has_control_chars(cleaned):
        raise HTTPException(422, detail=f"{field} 为空、超长或含控制字符")
    return cleaned


def _clean_base_url(value) -> str:
    return _clean_text(value, "base_url", max_len=500).rstrip("/")


def _provider_formats(conn: sqlite3.Connection) -> dict[str, str]:
    rows = ProviderRepo(conn).list()
    return {row["name"]: row.get("reasoning_format") or "openai" for row in rows}


def _require_provider(conn: sqlite3.Connection, name: str,
                      *, missing_status: int = 422) -> dict:
    row = ProviderRepo(conn).get(name)
    if row is None:
        raise HTTPException(missing_status, detail=f"unknown provider {name}")
    return row


def _provider_with_key_state(conn: sqlite3.Connection, row: dict) -> dict:
    stored = ProviderRepo(conn).get(row["name"])
    api_key = ""
    if stored is not None and stored["base_url"].rstrip("/") == row["base_url"].rstrip("/"):
        api_key = stored.get("api_key") or ""
    return {**row, "api_key": api_key}


def _undeclared_reasoning(provider_format: str) -> dict:
    return {"declared": False, "supported": None, "format": provider_format if provider_format in FORMATS else "openai", "efforts": [], "default": None}


def _public_model(row: dict, provider_format: str) -> dict:
    """归一模型出参；旧库可宽松读取，但密钥只剔除、不回写。

    两种来源都先过同一份脱敏投影：数据库 JSON TEXT（``parse_profile_text`` 容忍
    非法旧值并归一为 ``{}``）与图内 runtime 的 dict。合法元数据原样保留，旧库
    非法 reasoning 的 ``reasoning.error`` 展示不变。"""
    raw = row.get("profile")
    if isinstance(raw, dict):
        profile = raw
    elif isinstance(raw, str):
        profile = parse_profile_text(raw)
    else:
        profile = {}
    profile = without_runtime_secret_fields(profile)
    normalized = normalize_reasoning(profile, provider_format)
    capability = profile.get("capability")
    return {
        **row,
        "profile": profile,
        "capability": capability.strip() if isinstance(capability, str) else "",
        "reasoning": normalized or _undeclared_reasoning(provider_format),
    }


def _reject_non_finite_json(value, path: str = "profile") -> None:
    """拒绝 JSON 无法表达的浮点值（``NaN`` / ``Infinity`` / ``-Infinity``）。

    ``json.loads`` 默认接受这三个字面量，而 Starlette 的 ``JSONResponse`` 用
    ``allow_nan=False`` 序列化：把它们写进数据库会让之后的 echo/GET 直接 500。
    管理写路径必须在落库前拒绝，数据库里已有的旧行由读取/试算路径继续容忍。
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise HTTPException(
            422,
            detail=f"profile 包含非 JSON 兼容数值（NaN/Infinity），请改用有限数：{path}")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite_json(item, f"{path}.{key}" if isinstance(key, str) else path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_finite_json(item, f"{path}[{index}]")


def _validated_profile(raw: dict | str | None, provider_format: str) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(422, detail=f"profile 不是合法 JSON: {exc}") from exc
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        raise HTTPException(422, detail="profile 必须是 JSON 对象")
    _reject_non_finite_json(parsed)
    try:
        reject_runtime_secret_fields(parsed, "profile")
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        validated = validate_model_profile(parsed, provider_format)
    except ReasoningError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if "capability" in parsed:
        value = parsed.get("capability")
        if value is not None and not isinstance(value, str):
            raise HTTPException(422, detail="capability 必须是字符串或 null")
        capability = (value or "").strip()
        if len(capability) > MAX_CAPABILITY_CHARS:
            raise HTTPException(422, detail=f"capability 不能超过 {MAX_CAPABILITY_CHARS} 字符")
        if capability:
            validated["capability"] = capability
        else:
            validated.pop("capability", None)
    # allow_nan=False 是第二道闸：任何未预料到的非有限值都不得进入数据库 JSON TEXT。
    return json.dumps(validated, ensure_ascii=False, allow_nan=False)


# ── 画布同步辅助：数据库目录行 → 非密钥运行时目录项 ────────────────────
def _runtime_provider_entry(row: dict) -> dict:
    return {
        "name": row["name"],
        "base_url": (row.get("base_url") or "").rstrip("/"),
        "key_env": row.get("key_env") or "",
        "reasoning_format": row.get("reasoning_format") or "openai",
    }


def _runtime_model_entry(row: dict, provider_format: str) -> dict:
    """把数据库模型行投影成画布运行时模型项（只含非密钥字段）。"""
    profile = parse_profile_text(row.get("profile"))
    validated = _validated_profile(profile, provider_format)
    return {
        "id": row["id"],
        "upstream": row["upstream"],
        "provider": row["provider"],
        "profile": json.loads(validated or "{}"),
    }


def _catalog_service(request: Request, conn: sqlite3.Connection,
                     trees: RoutingTreeRepo) -> CatalogSyncService:
    return CatalogSyncService(conn, trees, request.app.state.publisher)


def _provider_entries(conn: sqlite3.Connection, names) -> list[dict]:
    entries = []
    for name in names:
        row = ProviderRepo(conn).get(name)
        if row is not None:
            entries.append(_runtime_provider_entry(row))
    return entries


def _impacted_trees(tree_list, impacts):
    impacted = {item["id"] for item in impacts}
    return [tree for tree in tree_list if tree.id in impacted]


def _canvas_models_using_provider(impacts) -> list[dict]:
    """影响清单里「有画布模型引用该 provider」的条目（裸目录条目不阻塞删除）。"""
    return [item for item in impacts if item["usage"]["models"]["use"]]


def _provider_reference_error(used_by: list[str], canvas_models: list[dict],
                              impacts: list[dict]) -> ReferencedByError:
    """provider 删除被引用时的 409 载荷：DB 模型与画布模型都要列出。"""
    if used_by and canvas_models:
        message = "provider 同时被数据库模型与自包含画布引用，请先解除引用"
    elif used_by:
        message = "provider 被数据库模型引用，请先更新/删除这些模型"
    else:
        message = "provider 被自包含画布引用；删除不提供隐式替换，请先解除画布引用"
    return ReferencedByError({
        "message": message,
        "code": "provider_referenced",
        "models": used_by,
        "affected_trees": impacts,
    })


def _provider_delete_blockers(conn, trees, name) -> ReferencedByError | None:
    """provider 删除引用检查（路由层预览与锁内 recheck 共用同一规则）。

    只有数据库模型或**画布模型**引用 provider 时才阻塞：画布运行时目录里仅有
    它的 provider 条目（没有模型使用它）不影响删除，但影响清单仍完整列出。
    """
    used_by = model_ids_using_provider(conn, name)
    impacts = provider_impacts(trees.list(), name)
    canvas_models = _canvas_models_using_provider(impacts)
    if used_by or canvas_models:
        return _provider_reference_error(used_by, canvas_models, impacts)
    return None


@router.get("/model-catalog")
def get_model_catalog():
    providers = []
    for preset in PROVIDER_PRESETS:
        fmt = preset.get("reasoning_format") or "openai"
        models = []
        for model in preset.get("models") or ():
            profile = model.get("profile") or {}
            models.append({
                "id": model["id"], "name": model.get("name") or model["id"],
                "context_window": model.get("context_window"), "max_tokens": model.get("max_tokens"),
                "tier": model.get("tier"), "domains": model.get("domains") or [],
                "profile": profile, "reasoning": normalize_reasoning(profile, fmt) or _undeclared_reasoning(fmt),
            })
        key_env = preset.get("key_env") or ""
        providers.append({
            "id": preset["id"], "name": preset.get("name") or preset["id"],
            "base_url": preset["base_url"], "key_env": key_env, "reasoning_format": fmt,
            "key_env_present": bool(key_env and os.environ.get(key_env)), "models": models,
        })
    return {
        "providers": providers,
        "formats": [{"id": spec.id, "zh": spec.zh, "en": spec.en, "needs_wire": spec.needs_wire, "supports_sre": spec.supports_sre} for spec in FORMATS.values()],
        "levels": list(LEVELS),
    }


@router.get("/models")
def get_models(conn: sqlite3.Connection = Depends(get_conn)):
    # 目录读取只查 SQLite：不因任何图的内嵌运行配置而切换数据源。
    formats = _provider_formats(conn)
    rows = ModelRepo(conn).list()
    return {"models": [
        _public_model(row, formats.get(row["provider"], "openai"))
        for row in rows
    ]}


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream: str | None = None
    provider: str | None = None
    profile: str | dict | None = None
    # 影响确认：受影响画画的当前 document_sha256 清单（Task 4 两阶段协议）。
    impact_confirmation: Any = None


class ModelCreateIn(ModelIn):
    id: str


class ModelDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 被 target 引用时必填：替代模型 id（来自影响预览的 replacements）。
    replacement_model_id: Any = None
    impact_confirmation: Any = None


@router.post("/models", status_code=201)
def post_model(
    body: ModelCreateIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    model_id = body.id.strip()
    if not _ID_RE.match(model_id):
        raise HTTPException(422, detail="invalid model id")
    upstream = _clean_text(body.upstream, "upstream", max_len=200)
    if body.provider is None:
        raise HTTPException(422, detail="provider 不能为空")
    provider = _clean_text(body.provider, "provider", max_len=64)
    provider_row = _require_provider(conn, provider)
    profile = _validated_profile(body.profile, provider_row.get("reasoning_format") or "openai")

    def db_write(c):
        if not ModelRepo(c).create(model_id, upstream, provider, profile or "{}"):
            raise AlreadyExists(f"model {model_id} 已存在")

    def audit(c):
        ChangeRepo(c).record(now_iso(), "model", {
            "action": "create", "id": model_id,
            "upstream": upstream, "provider": provider,
            "profile": body.profile, "synced_trees": [],
        })

    def recheck(c):
        # 路由层校验之后、加锁之前 provider 行可能被删；锁内重查，避免落下一个
        # 数据库里已无对应 provider 的模型（422 与路由层 ``_require_provider`` 一致）。
        _require_provider(c, provider)

    # 新建模型不会影响任何已存画布（画布只引用已存在模型）→ 无候选。
    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=[], audit=audit, recheck=recheck)
    return {"ok": True, "model": _public_model(ModelRepo(conn).get(model_id), provider_row.get("reasoning_format") or "openai")}


@router.put("/models/{model_id}")
def put_model(
    model_id: str, body: ModelIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    existing = ModelRepo(conn).get(model_id)
    if existing is None:
        raise HTTPException(404, detail=f"unknown model {model_id}")
    upstream = _clean_text(body.upstream, "upstream", max_len=200) if body.upstream is not None else None
    provider = _clean_text(body.provider, "provider", max_len=64) if body.provider is not None else None
    if provider is not None:
        _require_provider(conn, provider)
    effective_provider = provider or existing["provider"]
    formats = _provider_formats(conn)
    profile = _validated_profile(body.profile, formats.get(effective_provider, "openai")) if body.profile is not None else None

    tree_list = trees.list()
    impacts = model_impacts(tree_list, model_id)
    require_confirmation(
        impacts, body.impact_confirmation,
        resource={"kind": "model", "id": model_id}, replacements=[])

    impacted_trees = _impacted_trees(tree_list, impacts)
    candidates = []
    if impacted_trees:
        # 只有存在受影响画布时才构造运行时目录项：无画布受影响时目录写不应因为
        # 旧库里的历史非法 profile 而失败。
        effective = dict(existing)
        if upstream is not None:
            effective["upstream"] = upstream
        if provider is not None:
            effective["provider"] = provider
        if profile is not None:
            effective["profile"] = profile
        runtime_model = _runtime_model_entry(
            effective, formats.get(effective["provider"], "openai"))
        providers = _provider_entries(conn, [effective["provider"]])
        candidates = [
            sync_model(tree, runtime_model, providers)
            for tree in impacted_trees
        ]
    synced = [tree.id for tree in candidates]

    def db_write(c):
        if not ModelRepo(c).update(
                model_id, upstream=upstream, provider=provider, profile=profile):
            raise MissingError(f"unknown model {model_id}")

    def audit(c):
        ChangeRepo(c).record(now_iso(), "model", {
            "action": "update", "id": model_id,
            "upstream": body.upstream, "provider": body.provider,
            "profile": body.profile, "synced_trees": synced,
        })

    def recheck(_conn):
        # 写锁内、事务内、写目录行之前重算影响清单：路由层扫描之后新出现的
        # 画布引用会让确认哈希不匹配 → 409，绝不带着过期名单提交。
        if provider is not None:
            # 请求要换到的 provider 可能在路由层校验之后被删除：锁内重查，
            # 绝不让数据库模型引用一个已不存在的 provider。
            _require_provider(_conn, provider)
        require_confirmation(
            model_impacts(trees.list(), model_id), body.impact_confirmation,
            resource={"kind": "model", "id": model_id}, replacements=[])

    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
    row = ModelRepo(conn).get(model_id)
    return {
        "ok": True,
        "model": _public_model(
            row, _provider_formats(conn).get(row["provider"], "openai")),
        "synced_trees": synced,
    }


@router.delete("/models/{model_id}")
def delete_model(
    model_id: str, request: Request,
    body: ModelDeleteIn | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    existing = ModelRepo(conn).get(model_id)
    if existing is None:
        raise HTTPException(404, detail=f"unknown model {model_id}")
    replacement_id = body.replacement_model_id if body is not None else None
    replacement_row = None
    if replacement_id is not None:
        if not isinstance(replacement_id, str):
            raise HTTPException(422, detail="replacement_model_id 必须是字符串")
        replacement_row = ModelRepo(conn).get(replacement_id)
        if replacement_row is None:
            raise HTTPException(404, detail=f"unknown model {replacement_id}")
        if replacement_id == model_id:
            raise HTTPException(422, detail="replacement_model_id 不能是模型自身")

    formats = _provider_formats(conn)
    tree_list = trees.list()
    impacts = model_impacts(tree_list, model_id)
    replacements = [
        {"id": row["id"], "upstream": row["upstream"], "provider": row["provider"]}
        for row in ModelRepo(conn).list() if row["id"] != model_id
    ]
    require_confirmation(
        impacts, body.impact_confirmation if body is not None else None,
        resource={"kind": "model", "id": model_id}, replacements=replacements)

    target_referenced = any(item["usage"]["targets"] for item in impacts)
    if target_referenced and replacement_id is None:
        raise HTTPException(
            422, detail="模型仍被 target 引用，必须提供 replacement_model_id")

    replacement_entry = None
    provider_entries: list[dict] = []
    if replacement_row is not None and target_referenced:
        replacement_entry = _runtime_model_entry(
            replacement_row, formats.get(replacement_row["provider"], "openai"))
        provider_entries = _provider_entries(conn, [replacement_row["provider"]])

    usage_by_id = {item["id"]: item["usage"] for item in impacts}
    candidates = []
    for tree in _impacted_trees(tree_list, impacts):
        needs_replacement = bool(usage_by_id[tree.id]["targets"])
        candidates.append(delete_tree_model(
            tree, model_id,
            replacement=replacement_entry if needs_replacement else None,
            providers=provider_entries if needs_replacement else (),
        ))
    synced = [tree.id for tree in candidates]

    def db_write(c):
        if not ModelRepo(c).delete(model_id):
            raise MissingError(f"unknown model {model_id}")

    def audit(c):
        ChangeRepo(c).record(now_iso(), "model", {
            "action": "delete", "id": model_id,
            "replacement_model_id": replacement_id,
            "synced_trees": synced,
        })

    def recheck(_conn):
        # 替代模型可能在路由层校验之后、加锁之前被删除：锁内重查，404 语义与
        # 路由层一致（``unknown model {id}``），绝不把画布 target 换成一个数据库
        # 里已不存在的模型。
        if replacement_id is not None \
                and ModelRepo(_conn).get(replacement_id) is None:
            raise MissingError(f"unknown model {replacement_id}")
        require_confirmation(
            model_impacts(trees.list(), model_id),
            body.impact_confirmation if body is not None else None,
            resource={"kind": "model", "id": model_id},
            replacements=[
                {"id": row["id"], "upstream": row["upstream"],
                 "provider": row["provider"]}
                for row in ModelRepo(_conn).list() if row["id"] != model_id
            ])

    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
    return {
        "ok": True,
        "deleted": model_id,
        "replacement_model_id": replacement_id,
        "synced_trees": synced,
    }


def _public_provider(row: dict) -> dict:
    key_env = row.get("key_env") or ""
    source = "stored" if row.get("api_key") else "env" if key_env and os.environ.get(key_env) else "none"
    return {"name": row["name"], "base_url": row["base_url"], "key_env": key_env, "reasoning_format": row.get("reasoning_format") or "openai", "key_present": source != "none", "key_source": source}


@router.get("/providers")
def get_providers(conn: sqlite3.Connection = Depends(get_conn)):
    rows = ProviderRepo(conn).list()
    return {"providers": [
        _public_provider(_provider_with_key_state(conn, row))
        for row in rows
    ]}


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    key_env: str | None = None
    reasoning_format: str | None = None
    api_key: str | None = None
    # 影响确认：受影响画布的当前 document_sha256 清单。
    impact_confirmation: Any = None


class ProviderDeleteIn(BaseModel):
    """provider 删除 DTO：接受 ``impact_confirmation`` 仅为协议兼容。

    删除不可绕过：只要数据库模型或画布模型仍引用 provider 就始终 409。
    """

    model_config = ConfigDict(extra="forbid")

    impact_confirmation: Any = None


@router.put("/providers/{name}")
def put_provider(
    name: str, body: ProviderIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    if not _ID_RE.match(name):
        raise HTTPException(422, detail="invalid provider name")
    base_url = _clean_base_url(body.base_url)
    key_env = body.key_env.strip() if body.key_env is not None else None
    if key_env and not _ENV_RE.match(key_env):
        raise HTTPException(422, detail="invalid key_env")
    if body.reasoning_format is not None and body.reasoning_format not in FORMAT_IDS:
        raise HTTPException(422, detail="invalid reasoning_format")
    current = ProviderRepo(conn).get(name)
    api_key = body.api_key.strip() if body.api_key is not None else None
    if api_key is None and current is not None \
            and current["base_url"].rstrip("/") != base_url:
        # 端点变了而请求未显式提供密钥：旧密钥不得静默跟随到新地址（spec §5）。
        api_key = ""

    tree_list = trees.list()
    impacts = provider_impacts(tree_list, name)
    require_confirmation(
        impacts, body.impact_confirmation,
        resource={"kind": "provider", "name": name})

    runtime_provider = {
        "name": name,
        "base_url": base_url,
        "key_env": key_env if key_env is not None else (current or {}).get("key_env", ""),
        "reasoning_format": (
            body.reasoning_format or (current or {}).get("reasoning_format") or "openai"),
    }
    candidates = [
        sync_provider(tree, runtime_provider)
        for tree in _impacted_trees(tree_list, impacts)
    ]
    synced = [tree.id for tree in candidates]
    audit_key_env = (
        key_env if key_env is not None else (current or {}).get("key_env", ""))
    audit_format = (
        body.reasoning_format or (current or {}).get("reasoning_format", "openai"))

    def db_write(c):
        ProviderRepo(c).upsert(
            name, base_url, key_env=key_env,
            reasoning_format=body.reasoning_format, api_key=api_key)

    def audit(c):
        ChangeRepo(c).record(now_iso(), "provider", {
            "action": "upsert", "name": name, "base_url": base_url,
            "key_env": audit_key_env,
            "reasoning_format": audit_format,
            "api_key": "unchanged" if api_key is None else "cleared" if api_key == "" else "set",
            "synced_trees": synced,
        })

    def recheck(_conn):
        require_confirmation(
            provider_impacts(trees.list(), name), body.impact_confirmation,
            resource={"kind": "provider", "name": name})

    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
    return {
        "ok": True,
        "provider": _public_provider(ProviderRepo(conn).get(name)),
        "synced_trees": synced,
    }


@router.delete("/providers/{name}")
def delete_provider(
    name: str, request: Request,
    body: ProviderDeleteIn | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    # DTO 里的 impact_confirmation 只用于协议兼容，provider 删除不允许绕过检查。
    if ProviderRepo(conn).get(name) is None:
        raise MissingError(f"unknown provider {name}")
    blocked = _provider_delete_blockers(conn, trees, name)
    if blocked is not None:
        raise blocked

    def db_write(c):
        if not ProviderRepo(c).delete(name):
            raise MissingError(f"unknown provider {name}")

    def audit(c):
        ChangeRepo(c).record(now_iso(), "provider", {
            "action": "delete", "name": name, "synced_trees": [],
        })

    def recheck(_conn):
        # 路由层预览之后、加锁之前可能有画布/模型开始引用 provider；在写锁内
        # 用同一规则重算一次，命中即 409 且数据库与审计都不变。
        blocked = _provider_delete_blockers(_conn, trees, name)
        if blocked is not None:
            raise blocked

    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=[], audit=audit, recheck=recheck)
    return {"ok": True, "deleted": name}


class FetchModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    base_url: str | None = None
    key_env: str | None = None
    api_key: str | None = None


@router.post("/fetch-models")
async def fetch_models(body: FetchModelsIn, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    stored_key = ""
    key_env = ""
    if body.provider:
        # fetch-models 沿用原端点的 404 契约：未知 provider 是「路径/资源不存在」，
        # 与模型写路径的 422 引用错误区分。
        provider = _require_provider(conn, body.provider, missing_status=404)
        base_url = provider["base_url"].rstrip("/")
        stored_key = _provider_with_key_state(conn, provider)["api_key"]
        key_env = body.key_env if body.key_env is not None else provider.get("key_env") or ""
    elif body.base_url:
        base_url = _clean_base_url(body.base_url)
        key_env = body.key_env or ""
    else:
        raise HTTPException(422, detail="provider 或 base_url 至少提供一个")
    api_key = body.api_key.strip() if body.api_key is not None else ""
    key = api_key or stored_key or (os.environ.get(key_env, "") if key_env else "")
    endpoint = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = await request.app.state.http.get(endpoint, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"无法连接上游 {endpoint}: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(502, detail=f"上游返回 HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, detail=f"上游 /models 不是 JSON: {exc}") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(502, detail="上游 /models 响应缺少 data 数组")
    return {"endpoint": endpoint, "models": [{"id": item["id"], "name": item.get("name") or item["id"]} for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]}
