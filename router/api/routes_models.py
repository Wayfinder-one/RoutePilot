from __future__ import annotations

import json
import os
import re
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.control.config import ConfigurationService
from router.presets import PROVIDER_PRESETS
from router.reasoning import FORMATS, FORMAT_IDS, LEVELS, ReasoningError, normalize_reasoning, validate_model_profile
from router.store.repositories import ModelRepo, ProviderRepo
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
    return {row["name"]: row.get("reasoning_format") or "openai" for row in ProviderRepo(conn).list()}


def _require_provider(conn: sqlite3.Connection, name: str) -> dict:
    row = ProviderRepo(conn).get(name)
    if row is None:
        raise HTTPException(422, detail=f"unknown provider {name}")
    return row


def _undeclared_reasoning(provider_format: str) -> dict:
    return {"declared": False, "supported": None, "format": provider_format if provider_format in FORMATS else "openai", "efforts": [], "default": None}


def _public_model(row: dict, provider_format: str) -> dict:
    """API 出参契约：``profile``/``reasoning`` 归一为对象，``capability`` 为纯文本。

    DB 中 ``profile`` 仍是 JSON TEXT（``parse_profile_text`` 容忍非法旧值并归一为
    ``{}``）；归一只发生在 API 边界。"""
    profile = parse_profile_text(row.get("profile"))
    normalized = normalize_reasoning(profile, provider_format)
    capability = profile.get("capability")
    return {
        **row,
        "profile": profile,
        "capability": capability.strip() if isinstance(capability, str) else "",
        "reasoning": normalized or _undeclared_reasoning(provider_format),
    }


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
    return json.dumps(validated, ensure_ascii=False)


def _service(request: Request, conn: sqlite3.Connection, trees: RoutingTreeRepo):
    """ConfigurationService 只由 API 在解析 DTO 后构造；写用例本体见 router.control.config。"""
    return ConfigurationService(conn, trees, request.app.state.publisher)


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
    formats = _provider_formats(conn)
    return {"models": [_public_model(row, formats.get(row["provider"], "openai")) for row in ModelRepo(conn).list()]}


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream: str | None = None
    provider: str | None = None
    profile: str | dict | None = None


class ModelCreateIn(ModelIn):
    id: str


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
    _service(request, conn, trees).create_model(
        model_id=model_id, upstream=upstream, provider=provider,
        profile_text=profile, audit_profile=body.profile,
    )
    return {"ok": True, "model": _public_model(ModelRepo(conn).get(model_id), provider_row.get("reasoning_format") or "openai")}


@router.put("/models/{model_id}")
def put_model(
    model_id: str, body: ModelIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    if ModelRepo(conn).get(model_id) is None:
        raise HTTPException(404, detail=f"unknown model {model_id}")
    upstream = _clean_text(body.upstream, "upstream", max_len=200) if body.upstream is not None else None
    provider = _clean_text(body.provider, "provider", max_len=64) if body.provider is not None else None
    if provider is not None:
        _require_provider(conn, provider)
    existing = ModelRepo(conn).get(model_id)
    effective_provider = provider or existing["provider"]
    profile = _validated_profile(body.profile, _provider_formats(conn).get(effective_provider, "openai")) if body.profile is not None else None
    _service(request, conn, trees).update_model(
        model_id=model_id, upstream=upstream, provider=provider, profile_text=profile,
        audit_upstream=body.upstream, audit_provider=body.provider, audit_profile=body.profile,
    )
    row = ModelRepo(conn).get(model_id)
    return {"ok": True, "model": _public_model(row, _provider_formats(conn).get(row["provider"], "openai"))}


@router.delete("/models/{model_id}")
def delete_model(
    model_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    _service(request, conn, trees).delete_model(model_id)
    return {"ok": True, "deleted": model_id}


def _public_provider(row: dict) -> dict:
    key_env = row.get("key_env") or ""
    source = "stored" if row.get("api_key") else "env" if key_env and os.environ.get(key_env) else "none"
    return {"name": row["name"], "base_url": row["base_url"], "key_env": key_env, "reasoning_format": row.get("reasoning_format") or "openai", "key_present": source != "none", "key_source": source}


@router.get("/providers")
def get_providers(conn: sqlite3.Connection = Depends(get_conn)):
    return {"providers": [_public_provider(row) for row in ProviderRepo(conn).list()]}


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    key_env: str | None = None
    reasoning_format: str | None = None
    api_key: str | None = None


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
    api_key = body.api_key.strip() if body.api_key is not None else None
    _service(request, conn, trees).upsert_provider(
        name=name, base_url=base_url, key_env=key_env,
        reasoning_format=body.reasoning_format, api_key=api_key,
    )
    return {"ok": True, "provider": _public_provider(ProviderRepo(conn).get(name))}


@router.delete("/providers/{name}")
def delete_provider(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    trees: RoutingTreeRepo = Depends(get_tree_repo)):
    _service(request, conn, trees).delete_provider(name)
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
        provider = ProviderRepo(conn).get(body.provider)
        if provider is None:
            raise HTTPException(404, detail=f"unknown provider {body.provider}")
        base_url = provider["base_url"].rstrip("/")
        stored_key = provider.get("api_key") or ""
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
