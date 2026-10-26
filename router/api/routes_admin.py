"""Admin API: pipeline, lexicon, models, providers, signals, try, settings,
changes, eval and the overview aggregates.

Every write that changes what the hot path reads rebuilds and publishes the
immutable ``app.state.snapshot`` in ONE assignment (JEV P1-B) so request
handling never touches the database (design doc section 4.4). Every
configuration write is also appended to the ``changes`` audit trail
(design 5.1).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from router.api.deps import get_conn
from router.api.routes_trees import capability_write_issues
from router.api.upstream import parse_profile_text
from router.evals import summarize
from router.pipeline.errors import NoActiveTreeError, SignalPreparationError
from router.pipeline.evaluator import Hit
from router.pipeline.runtime import RuntimeRequest
from router.pipeline.snapshot import (REVISION_SNAPSHOT, canonical_json,
                                      sha256_text, snapshot_from_content)
from router.decision.catalog import (CapabilityPackError, capability_of_model,
                                     normalize_capability)
from router.presets import PROVIDER_PRESETS
from router.reasoning import (FORMATS, FORMAT_IDS, LEVELS, ReasoningError,
                              normalize_reasoning, validate_model_profile)
from router.signals.registry import GROUPS, SIGNALS
from router.store.repositories import (ChangeRepo, ConfigRevisionRepo,
                                       EvalRepo, KeywordRepo, ModelRepo,
                                       ProviderRepo, SettingsRepo,
                                       TimePeriodRepo,
                                       ensure_questions_revision,
                                       model_ids_using_provider,
                                       model_references)
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()

logger = logging.getLogger(__name__)

#: settings owned by the system (the console may not write them).  The retired
#: capability policy keys disappeared with the capability endpoints; the legacy
#: pipeline keys stay reserved so an old client cannot resurrect dead state.
RESERVED_SETTINGS = {"pipeline_version", "pipeline_revision",
                     "questions_revision"}

#: 模型位 / 提供方名：写进分流树、URL 路径与 OpenAI body，保守字符集。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _publish_snapshot(request: Request, conn: sqlite3.Connection) -> None:
    """Rebuild + publish the routing snapshot after an admin write.

    The publisher holds the cross-write publication lock, rebuilds from ONE
    database state (active tree compiled with the current model catalog,
    questions, backends and runner evidence), appends the append-only
    ``config_revisions(kind='snapshot')`` row and publishes with ONE assignment.
    Callers must already hold ``request.app.state.publisher.locked()`` so their
    mutation and this refresh form a single critical section.
    """
    request.app.state.publisher.refresh(conn)


# --------------------------------------------------------------------------- #
# Keyword lexicon
# --------------------------------------------------------------------------- #
@router.get("/keyword-groups")
def get_keyword_groups(conn: sqlite3.Connection = Depends(get_conn)):
    return {"groups": KeywordRepo(conn).groups()}


@router.put("/keyword-groups")
def put_keyword_groups(body: dict, request: Request,
                       conn: sqlite3.Connection = Depends(get_conn)):
    groups = body.get("groups")
    if not isinstance(groups, dict):
        raise HTTPException(422, detail="groups must be an object")
    KeywordRepo(conn).save(groups)
    request.app.state.tagger.kw_groups = groups   # heuristic reads it live
    ChangeRepo(conn).record(_now(), "keyword-groups", {"groups": len(groups)})
    return {"ok": True}


@router.get("/time-periods")
def get_time_periods(conn: sqlite3.Connection = Depends(get_conn)):
    return {"periods": TimePeriodRepo(conn).load(),
            "now": _now()}   # server clock: the time_window rule judges by it


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.put("/time-periods")
def put_time_periods(body: dict, request: Request,
                     conn: sqlite3.Connection = Depends(get_conn)):
    periods = body.get("periods")
    if not isinstance(periods, list):
        raise HTTPException(422, detail="periods must be a list")
    seen = set()
    for p in periods:
        if (not isinstance(p, dict) or not str(p.get("name", "")).strip()
                or p["name"] in seen):
            raise HTTPException(422, detail="period names must be non-empty and unique")
        seen.add(p["name"])
        days = p.get("days")
        if (not isinstance(days, list) or not days
                or not all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
            raise HTTPException(422,
                                detail=f"{p['name']}: days must be ints 0-6 (Mon=0)")
        for k in ("start", "end"):
            if not isinstance(p.get(k), str) or not _TIME_RE.match(p[k]):
                raise HTTPException(422, detail=f"{p['name']}: {k} must be HH:MM")
    TimePeriodRepo(conn).save(periods)
    tagger = getattr(request.app.state, "tagger", None)
    if tagger is not None:
        tagger.time_periods = periods
    ChangeRepo(conn).record(_now(), "time-periods", {"periods": len(periods)})
    return {"ok": True, "periods": periods}



# --------------------------------------------------------------------------- #
# Models & providers (catalog §3/§5)
# --------------------------------------------------------------------------- #
def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _clean_text(value, field: str, *, max_len: int) -> str:
    """Trimmed, non-empty, bounded, control-char-free string or 422."""
    if not isinstance(value, str):
        raise HTTPException(422, detail=f"{field} 必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(422, detail=f"{field} 不能为空")
    if len(cleaned) > max_len:
        raise HTTPException(422, detail=f"{field} 不能超过 {max_len} 字符")
    if _has_control_chars(cleaned):
        raise HTTPException(422, detail=f"{field} 不能包含控制字符")
    return cleaned


def _clean_base_url(value) -> str:
    cleaned = _clean_text(value, "base_url", max_len=500)
    return cleaned[:-1] if cleaned.endswith("/") else cleaned


def _provider_formats(conn: sqlite3.Connection) -> dict[str, str]:
    """provider name -> reasoning_format（模型级 format 缺省时生效）。"""
    return {p["name"]: (p.get("reasoning_format") or "openai")
            for p in ProviderRepo(conn).list()}


def _require_provider(conn: sqlite3.Connection, name: str) -> dict:
    """存在性校验：provider 不存在 → 422（否则模型会从后端快照里消失）。"""
    row = ProviderRepo(conn).get(name)
    if row is None:
        raise HTTPException(422, detail=f"unknown provider {name}")
    return row


def _undeclared_reasoning(provider_format: str) -> dict:
    """§3.2 shape for "missing declaration" (console renders 未声明 from it)."""
    return {"declared": False, "supported": None,
            "format": provider_format if provider_format in FORMATS else "openai",
            "efforts": [], "default": None}


def _public_model(row: dict, provider_format: str) -> dict:
    normalized = normalize_reasoning(parse_profile_text(row.get("profile")),
                                     provider_format)
    return {**row,
            # Convenience for the console model list / capability panel: the
            # validated description already lives in profile.capability.
            "capability": capability_of_model(row),
            "reasoning": normalized or _undeclared_reasoning(provider_format)}


def _validated_profile(raw: dict | str | None,
                       provider_format: str) -> str | None:
    """POST/PUT profile 入参（dict 或 JSON 文本）→ 校验后的 JSON 文本。"""
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
    # Natural-language capability description (2026-10-26). Stored inside the
    # model profile so it is edited with the model and travels with the same
    # revision; '' / null removes it (the model stops being a route candidate).
    if "capability" in parsed:
        try:
            capability = normalize_capability(parsed.get("capability"))
        except CapabilityPackError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        if capability:
            validated["capability"] = capability
        else:
            validated.pop("capability", None)
    return json.dumps(validated, ensure_ascii=False)


@router.get("/model-catalog")
def get_model_catalog():
    """内置提供方 / 预置模型目录（§5.1）；纯声明数据 + 归一化推理能力。"""
    providers = []
    for preset in PROVIDER_PRESETS:
        fmt = preset.get("reasoning_format") or "openai"
        models = []
        for model in preset.get("models") or ():
            profile = model.get("profile") or {}
            normalized = normalize_reasoning(profile, fmt)
            models.append({
                "id": model["id"],
                "name": model.get("name") or model["id"],
                "context_window": model.get("context_window"),
                "max_tokens": model.get("max_tokens"),
                "tier": model.get("tier"),
                "domains": model.get("domains") or [],
                "profile": profile,
                "reasoning": normalized or _undeclared_reasoning(fmt),
            })
        key_env = preset.get("key_env") or ""
        providers.append({
            "id": preset["id"],
            "name": preset.get("name") or preset["id"],
            "base_url": preset["base_url"],
            "key_env": key_env,
            "reasoning_format": fmt,
            # the server is the only side that can see its own environment
            "key_env_present": bool(key_env and os.environ.get(key_env)),
            "models": models,
        })
    return {"providers": providers,
            # needs_wire / supports_sre come from the server's FormatSpec, so
            # the console never mirrors the backend's protocol table.
            "formats": [{"id": spec.id, "zh": spec.zh, "en": spec.en,
                         "needs_wire": spec.needs_wire,
                         "supports_sre": spec.supports_sre}
                        for spec in FORMATS.values()],
            "levels": list(LEVELS)}


@router.get("/models")
def get_models(conn: sqlite3.Connection = Depends(get_conn)):
    formats = _provider_formats(conn)
    return {"models": [_public_model(row, formats.get(row["provider"], "openai"))
                       for row in ModelRepo(conn).list()]}


class ModelIn(BaseModel):
    upstream: str | None = None
    provider: str | None = None
    profile: str | dict | None = None


class ModelCreateIn(ModelIn):
    id: str


def _model_rows_with_profile(models: list[dict], model_id: str,
                             profile: str | None) -> list[dict]:
    """Hypothetical catalog rows with one model's profile replaced (health pre-check)."""
    if profile is None:
        return models
    return [{**row, "profile": profile} if row.get("id") == model_id else row
            for row in models]


@router.post("/models", status_code=201)
def post_model(body: ModelCreateIn, request: Request,
               conn: sqlite3.Connection = Depends(get_conn)):
    model_id = body.id.strip()
    if not _ID_RE.match(model_id):
        raise HTTPException(
            422, detail="id 必须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    upstream = _clean_text(body.upstream, "upstream", max_len=200)
    if body.provider is None:
        raise HTTPException(422, detail="provider 不能为空")
    provider = _clean_text(body.provider, "provider", max_len=64)
    provider_row = _require_provider(conn, provider)
    profile = _validated_profile(body.profile,
                                 provider_row.get("reasoning_format") or "openai")
    publisher = request.app.state.publisher
    with publisher.locked():
        models_after = _model_rows_with_profile(ModelRepo(conn).list(),
                                                model_id, profile)
        blocked = capability_write_issues(conn, request, models_after)
        if blocked:
            raise HTTPException(
                422, detail=[dict(issue.__dict__) for issue in blocked])
        if not ModelRepo(conn).create(model_id, upstream, provider,
                                      profile or "{}"):
            raise HTTPException(409, detail=f"model {model_id} 已存在")
        ChangeRepo(conn).record(_now(), "model", {
            "action": "create", "id": model_id, "upstream": upstream,
            "provider": provider, "profile": body.profile})
        _publish_snapshot(request, conn)
    row = ModelRepo(conn).get(model_id)
    return {"ok": True, "model": _public_model(
        row, provider_row.get("reasoning_format") or "openai")}


@router.put("/models/{model_id}")
def put_model(model_id: str, body: ModelIn, request: Request,
              conn: sqlite3.Connection = Depends(get_conn)):
    existing = ModelRepo(conn).get(model_id)
    if existing is None:
        raise HTTPException(404, detail=f"unknown model {model_id}")
    upstream = (_clean_text(body.upstream, "upstream", max_len=200)
                if body.upstream is not None else None)
    provider = (_clean_text(body.provider, "provider", max_len=64)
                if body.provider is not None else None)
    if provider is not None:
        # A model whose provider does not exist disappears from the backend
        # snapshot (the JOIN drops it) and every routed request 502s.
        _require_provider(conn, provider)
    profile = None
    formats = _provider_formats(conn)
    effective = provider if provider is not None else existing["provider"]
    if body.profile is not None:
        profile = _validated_profile(body.profile,
                                     formats.get(effective, "openai"))
    publisher = request.app.state.publisher
    with publisher.locked():
        if profile is not None:
            blocked = capability_write_issues(
                conn, request,
                _model_rows_with_profile(ModelRepo(conn).list(),
                                         model_id, profile))
            if blocked:
                raise HTTPException(
                    422, detail=[dict(issue.__dict__) for issue in blocked])
        ModelRepo(conn).update(model_id, upstream=upstream, provider=provider,
                               profile=profile)
        ChangeRepo(conn).record(_now(), "model", {
            "action": "update", "id": model_id, "upstream": body.upstream,
            "provider": body.provider, "profile": body.profile})
        _publish_snapshot(request, conn)
    row = ModelRepo(conn).get(model_id)
    return {"ok": True, "model": _public_model(
        row, _provider_formats(conn).get(row["provider"], "openai"))}


@router.delete("/models/{model_id}")
def delete_model(model_id: str, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    if ModelRepo(conn).get(model_id) is None:
        raise HTTPException(404, detail=f"unknown model {model_id}")
    refs = model_references(RoutingTreeRepo(conn).list(), model_id)
    if refs:
        raise HTTPException(409, detail={
            "message": f"model {model_id} 被路由树引用，先解除引用再删除",
            "references": refs})
    publisher = request.app.state.publisher
    with publisher.locked():
        ModelRepo(conn).delete(model_id)
        ChangeRepo(conn).record(_now(), "model",
                                {"action": "delete", "id": model_id})
        _publish_snapshot(request, conn)
    return {"ok": True, "deleted": model_id}


def _public_provider(row: dict) -> dict:
    """§5.3 shape: key state only — the stored api_key never leaves the server."""
    key_env = row.get("key_env") or ""
    api_key = row.get("api_key") or ""
    env_present = bool(key_env) and bool(os.environ.get(key_env))
    if api_key:
        source = "stored"
    elif env_present:
        source = "env"
    else:
        source = "none"
    return {"name": row["name"], "base_url": row["base_url"],
            "key_env": key_env,
            "reasoning_format": row.get("reasoning_format") or "openai",
            "key_present": source != "none", "key_source": source}


@router.get("/providers")
def get_providers(conn: sqlite3.Connection = Depends(get_conn)):
    return {"providers": [_public_provider(row)
                          for row in ProviderRepo(conn).list()]}


class ProviderIn(BaseModel):
    base_url: str
    #: None = 保持；"" = 不用环境变量；非空 = 新环境变量名
    key_env: str | None = None
    #: None = 保持；合法值见 FORMAT_IDS
    reasoning_format: str | None = None
    #: 三态：None = 保持；"" = 清除；非空 = 写入（明文存库，永不回显）
    api_key: str | None = None


@router.put("/providers/{name}")
def put_provider(name: str, body: ProviderIn, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    if not _ID_RE.match(name):
        raise HTTPException(
            422, detail="name 必须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    base_url = _clean_base_url(body.base_url)
    key_env = None
    if body.key_env is not None:
        key_env = body.key_env.strip()
        if key_env and not _ENV_RE.match(key_env):
            raise HTTPException(
                422, detail="key_env 必须是合法的环境变量名（或空字符串）")
    reasoning_format = None
    if body.reasoning_format is not None:
        if body.reasoning_format not in FORMAT_IDS:
            raise HTTPException(
                422, detail=f"reasoning_format 必须是 {', '.join(FORMAT_IDS)} 之一")
        reasoning_format = body.reasoning_format
    api_key = body.api_key
    if api_key is not None:
        api_key = api_key.strip()
    existing = ProviderRepo(conn).get(name)
    publisher = request.app.state.publisher
    with publisher.locked():
        ProviderRepo(conn).upsert(name, base_url, key_env=key_env,
                                  reasoning_format=reasoning_format,
                                  api_key=api_key)
        # Never put the secret itself in the audit trail (/admin/changes is a GET).
        audit = {
            "action": "upsert", "name": name, "base_url": base_url,
            "key_env": key_env if key_env is not None
            else (existing or {}).get("key_env", ""),
            "reasoning_format": reasoning_format if reasoning_format is not None
            else (existing or {}).get("reasoning_format", "openai"),
            "api_key": "unchanged" if api_key is None
            else ("cleared" if api_key == "" else "set"),
        }
        ChangeRepo(conn).record(_now(), "provider", audit)
        # A provider key/url change is invisible to backends_revision (secrets
        # are never hashed), so the runtime backend map MUST be refreshed from
        # the DB — never skip the publish by comparing revisions.
        _publish_snapshot(request, conn)
    row = ProviderRepo(conn).get(name)
    return {"ok": True, "provider": _public_provider(row)}


@router.delete("/providers/{name}")
def delete_provider(name: str, request: Request,
                    conn: sqlite3.Connection = Depends(get_conn)):
    if ProviderRepo(conn).get(name) is None:
        raise HTTPException(404, detail=f"unknown provider {name}")
    used_by = model_ids_using_provider(conn, name)
    if used_by:
        raise HTTPException(409, detail={
            "message": f"provider {name} 被模型引用，先把这些模型改到别的提供方",
            "models": used_by})
    publisher = request.app.state.publisher
    with publisher.locked():
        ProviderRepo(conn).delete(name)
        ChangeRepo(conn).record(_now(), "provider",
                                {"action": "delete", "name": name})
        _publish_snapshot(request, conn)
    return {"ok": True, "deleted": name}


class FetchModelsIn(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    key_env: str | None = None
    api_key: str | None = None


@router.post("/fetch-models")
async def fetch_models(body: FetchModelsIn, request: Request,
                       conn: sqlite3.Connection = Depends(get_conn)):
    """Read a provider's GET {base_url}/models without persisting anything (§5.4)."""
    stored_key = ""
    key_env = ""
    if body.provider:
        provider_row = ProviderRepo(conn).get(body.provider)
        if provider_row is None:
            raise HTTPException(404, detail=f"unknown provider {body.provider}")
        base_url = (provider_row["base_url"] or "").rstrip("/")
        stored_key = provider_row.get("api_key") or ""
        key_env = (body.key_env if body.key_env is not None
                   else (provider_row.get("key_env") or ""))
    elif body.base_url:
        base_url = _clean_base_url(body.base_url)
        key_env = body.key_env or ""
    else:
        raise HTTPException(422, detail="provider 或 base_url 至少提供一个")
    api_key = body.api_key
    if api_key is not None:
        api_key = api_key.strip()
    # credential order (§5.4): body.api_key -> stored key -> env(key_env)
    key = api_key or stored_key or (os.environ.get(key_env, "") if key_env else "")
    endpoint = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.get(endpoint, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"无法连接上游 {endpoint}: {exc}") from exc
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:300]
        raise HTTPException(
            502, detail=f"上游 {endpoint} 返回 HTTP {resp.status_code}: {snippet}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise HTTPException(502, detail=f"上游 /models 不是 JSON: {exc}") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(502, detail="上游 /models 响应缺少 data 数组")
    models = [{"id": item["id"],
               "name": item.get("name") or item["id"]}
              for item in items
              if isinstance(item, dict) and isinstance(item.get("id"), str)]
    return {"endpoint": endpoint, "models": models}


# --------------------------------------------------------------------------- #
# Signal registry
# --------------------------------------------------------------------------- #
@router.get("/signals")
def get_signals(conn: sqlite3.Connection = Depends(get_conn)):
    # kind ('model'|'rule') + free (values not enumerable) drive the console's
    # fork-key candidates and the deterministic-vs-floor rendering (SPEC-TREE-4 §6).
    # groups follow the registry's canonical order (a raw set() is arbitrary).
    # The retired capability-routing signal and its dynamic model-id values are
    # gone with the capability endpoints; every remaining signal is a plain
    # registry entry (rule signals carry keyword/time-period dynamic values).
    payload = [{**spec.__dict__, "free": not spec.values}
               for spec in SIGNALS.values()]
    return {"signals": payload,
            "groups": [g for g in GROUPS if any(s.group == g
                                                for s in SIGNALS.values())],
            "keyword_groups": list(KeywordRepo(conn).groups()),
            "time_periods": [p["name"] for p in TimePeriodRepo(conn).load()],
            "now": _now()}


# --------------------------------------------------------------------------- #
# JEV signal engine: tagger metadata, editable question config, calc log
# --------------------------------------------------------------------------- #
def _questions_path(request: Request) -> str:
    from router.signals.jev import DEFAULT_QUESTIONS_PATH
    settings = request.app.state.settings
    return settings.signal_questions or str(DEFAULT_QUESTIONS_PATH)


def _tagger_meta(request: Request) -> dict:
    tagger = request.app.state.tagger
    meta = {"name": getattr(tagger, "name", "?"),
            "questions_path": _questions_path(request)}
    for attr in ("model_dir", "temperature", "max_length"):
        value = getattr(tagger, attr, None)
        if value is not None:
            meta[attr] = str(value) if attr == "model_dir" else value
    return meta


@router.get("/tagger")
def get_tagger(request: Request):
    """Which signal engine runs now (JEV model metadata for the console)."""
    return {"tagger": _tagger_meta(request)}


@router.get("/signal-questions")
def get_signal_questions(request: Request):
    from router.signals.jev import DEFAULT_QUESTIONS_PATH, load_question_config
    path = _questions_path(request)
    warning = None
    try:
        cfg = load_question_config(path)
    except Exception:                     # noqa: BLE001 - first boot / bad path
        try:
            cfg = load_question_config(DEFAULT_QUESTIONS_PATH)
        except Exception as exc:          # noqa: BLE001
            raise HTTPException(500, detail=f"cannot load question config: {exc}")
        # Failing silently left the console editing the built-in defaults while
        # the running tagger (or the disk) had another config: surface the
        # fallback so the operator can fix the path/file.
        warning = (f"configured question file {path} is unreadable; the "
                   "built-in defaults are served instead — the running "
                   "tagger and the editor may disagree until it is fixed")
    body: dict = {"config": cfg, "meta": _tagger_meta(request)}
    if warning:
        body["warning"] = warning
    return body


class SignalQuestionsIn(BaseModel):
    config: dict


def validate_questions(raw: dict) -> dict:
    """Step 1: validate the editable questions config (422 on bad input)."""
    from router.signals.jev import JevTaggerError, validate_question_config
    try:
        return validate_question_config(raw)
    except JevTaggerError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def apply_questions(tagger, cfg: dict):
    """Step 2: hot-apply the config in memory; returns the previous for rollback."""
    setter = getattr(tagger, "set_questions", None)
    previous = getattr(tagger, "questions", None)
    if setter is not None:
        setter(cfg)                            # atomic replace (JEV mode)
    return previous


def rollback_questions(tagger, previous) -> None:
    """Undo :func:`apply_questions` (persistence failed: memory must match disk)."""
    setter = getattr(tagger, "set_questions", None)
    if setter is None or previous is None:
        return
    try:
        setter(previous)
    except Exception:                          # noqa: BLE001 - best effort rollback
        logger.exception("cannot roll back the in-memory question config")


class _QuestionPersistError(RuntimeError):
    """The questions config could not be written to disk."""


def persist_questions(request: Request, conn: sqlite3.Connection,
                      cfg: dict) -> tuple[str, str]:
    """Step 3: atomic file write + canonical revision + audit row.

    A half-written file would be unparseable JSON at the next boot: write a
    ``*.tmp`` sibling and ``os.replace`` over the target (atomic on one
    filesystem).  The revision row is appended through ``ensure_questions_revision``
    so a racing request still resolves to the config it actually scored.
    """
    from pathlib import Path

    path = Path(_questions_path(request))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():                   # only on a failed write/replace
                tmp.unlink(missing_ok=True)
    except OSError as exc:
        raise _QuestionPersistError(str(exc)) from exc
    revision = ensure_questions_revision(conn, cfg)
    ChangeRepo(conn).record(_now(), "signal_questions",
                            {"path": str(path), "revision": revision})
    return str(path), revision


def publish_questions(request: Request, conn: sqlite3.Connection,
                      cfg: dict) -> None:
    """Step 4: publish a snapshot whose questions are the just-saved config."""
    request.app.state.publisher.refresh(conn, questions=cfg)


@router.put("/signal-questions")
def put_signal_questions(body: SignalQuestionsIn, request: Request,
                         conn: sqlite3.Connection = Depends(get_conn)):
    """Validate + persist the editable JEV questions and hot-apply them.

    The whole write runs inside the publisher lock: the running tagger, the
    on-disk file, the recorded revision and the published snapshot change as
    ONE critical section.  A persistence failure rolls the in-memory config
    back and publishes nothing (memory and disk must agree).
    """
    cfg = validate_questions(body.config)
    tagger = request.app.state.tagger
    publisher = request.app.state.publisher
    applied = getattr(tagger, "set_questions", None) is not None
    with publisher.locked():
        previous = apply_questions(tagger, cfg)
        try:
            _path, revision = persist_questions(request, conn, cfg)
        except _QuestionPersistError as exc:
            rollback_questions(tagger, previous)
            raise HTTPException(
                500, detail=f"cannot write question config: {exc}") from exc
        publish_questions(request, conn, cfg)
    return {"ok": True, "applied": applied,
            "revision": revision, "meta": _tagger_meta(request)}


@router.get("/requests/recent")
async def recent_requests(request: Request, limit: int = 20):
    """Newest requests from the in-memory ring (console 'calculation log').
    Detail is never persisted: the ring keeps the last 20 requests only."""
    return {"requests": request.app.state.requestlog.recent_requests(limit)}


@router.get("/requests/stats")
def request_stats(request: Request):
    """Ledger/ring health (JEV P1-C §3): drop counters are never silent."""
    return request.app.state.requestlog.stats()


# --------------------------------------------------------------------------- #
# Settings (M3) + change audit trail
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_conn)):
    return {"settings": SettingsRepo(conn).all()}


@router.put("/settings")
def put_settings(body: dict, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    for key, value in body.items():
        if (not isinstance(key, str) or not key or key.startswith("_")
                or key in RESERVED_SETTINGS):
            raise HTTPException(422, detail=f"reserved or invalid setting key {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise HTTPException(422, detail=f"setting {key!r} must be a scalar")
    # One transaction for the whole write: the settings must not depend on a
    # later (unrelated) write to be persisted.
    with conn:
        for key, value in body.items():
            SettingsRepo(conn).set(key, str(value))
        ChangeRepo(conn).record(_now(), "settings", {"keys": sorted(body)})
    return {"ok": True, "settings": SettingsRepo(conn).all()}


@router.get("/changes")
def get_changes(limit: int = 50, conn: sqlite3.Connection = Depends(get_conn)):
    return {"changes": ChangeRepo(conn).list(limit)}


# --------------------------------------------------------------------------- #
# Evaluation (M3)
# --------------------------------------------------------------------------- #
#: How many mismatches an evaluation report lists (the console shows them).
MAX_MISMATCHES = 100
#: How many evaluation reports the overview and /admin/evals show.
EVAL_REPORTS = 3


class EvalIn(BaseModel):
    mode: str                       # 'replay' | 'labels'
    days: int = 7
    labels: list[dict] = []


def _hit_model(outcome) -> str | None:
    """Evaluation expectation: a route Hit's model; Miss/block has no model."""
    if isinstance(outcome, Hit) and outcome.kind == "route":
        return outcome.model
    return None


def _current_tagger_info(request: Request) -> dict:
    """Current tagger fingerprint (only taggers implementing info() have one)."""
    tagger = request.app.state.tagger
    info = getattr(tagger, "info", None)
    return dict(info()) if callable(info) else {}


def _current_decision_revision(request: Request) -> str:
    """The DecisionRunner evidence hash — same rule as build_snapshot."""
    runner = getattr(request.app.state, "runner", None)
    info = dict(runner.info()) if runner is not None else {}
    return "" if not info else sha256_text(canonical_json(info))


def _replay_snapshot(conn: sqlite3.Connection, revision):
    """Restore the historical effective snapshot (never today's tree).

    Returns ``(snapshot, None)`` or ``(None, reason)``.  The content is read
    from the append-only ``config_revisions(kind='snapshot')`` store, so a
    replay uses the compiled tree/candidates/questions that actually produced
    the recorded decision.
    """
    if not revision:
        return None, "missing_snapshot_revision"
    row = ConfigRevisionRepo(conn).get(REVISION_SNAPSHOT, revision)
    if row is None:
        return None, "missing_snapshot_content"
    try:
        return snapshot_from_content(row["content"]), None
    except (ValueError, TypeError):
        return None, "invalid_snapshot_content"


def _replay_blocker(record: dict, conn: sqlite3.Connection, *,
                    tagger_info: dict, decision_revision: str) -> str | None:
    """Concrete reason a ring record cannot be replayed (None = replayable).

    Checked in order: the original request inputs -> the historical snapshot
    pointer/content -> the model fingerprints.  Nothing is defaulted: a record
    missing the text, the headers/n_messages actually used, or the revision
    content is listed in ``non_replayable`` instead of being replayed with
    fabricated inputs or today's configuration.
    """
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return "missing_text"
    if not isinstance(record.get("headers"), dict):
        return "missing_request_headers"
    n_messages = record.get("n_messages")
    if isinstance(n_messages, bool) or not isinstance(n_messages, int):
        return "missing_request_n_messages"
    snapshot, reason = _replay_snapshot(conn, record.get("snapshot_revision"))
    if snapshot is None:
        return reason
    recorded_tagger = record.get("tagger_info")
    if not isinstance(recorded_tagger, dict):
        return "missing_tagger_evidence"      # pre-Task-10 ring record
    if recorded_tagger != tagger_info:
        return "tagger_model_mismatch"
    if snapshot.decision_revision != decision_revision:
        return "decision_runner_mismatch"
    return None


def _replay_case(record: dict) -> dict:
    """Record-only fields of one replay case (no text: replay never persists it)."""
    return {"id": record.get("id"), "ts": record.get("ts"),
            "expect": record.get("model"), "got": None,
            "tree_id": record.get("tree_id"),
            "tree_revision": record.get("tree_revision"),
            "snapshot_revision": record.get("snapshot_revision")}


async def _eval_pairs(body: EvalIn, request: Request, conn: sqlite3.Connection):
    """Run one evaluation: labels re-routes the caller's texts, replay restores
    the historical snapshot and re-routes the recorded original inputs.

    Both use the SAME ``app.state.runtime`` as ``/v1``: no separate routing
    logic, no stored-signal shortcut.  A case that cannot be replayed is listed
    in ``non_replayable`` with a concrete reason and is NOT part of the
    accuracy denominator.
    """
    runtime = request.app.state.runtime

    if body.mode == "labels":
        snapshot = request.app.state.snapshot
        pairs: list[tuple] = []
        mismatches: list[dict] = []
        cases: list[dict] = []
        for index, raw in enumerate(body.labels):
            case = raw if isinstance(raw, dict) else {}
            text = str(case.get("text") or "")
            expect = case.get("expect_model")
            case_id = str(case.get("id") or f"label-{index}")
            got = None
            reason = None
            try:
                result = await runtime.route(snapshot,
                                             RuntimeRequest(text=text))
                got = _hit_model(result.outcome)
            except NoActiveTreeError:
                reason = "no_active_tree"
            except SignalPreparationError:
                reason = "signal_preparation_failed"
            entry = {"id": case_id, "text": text, "expect": expect,
                     "got": got, "tree_id": snapshot.tree_id,
                     "tree_revision": snapshot.tree_revision,
                     "snapshot_revision": snapshot.snapshot_revision}
            if reason is not None:
                entry["reason"] = reason
            cases.append(entry)
            pairs.append((expect, got))
            if got != expect and len(mismatches) < MAX_MISMATCHES:
                mismatches.append({"id": case_id, "text": text[:200],
                                   "expect": expect, "got": got})
        return pairs, mismatches, {"labels": len(body.labels), "cases": cases}

    if body.mode == "replay":
        since = (datetime.now().astimezone()
                 - timedelta(days=body.days)).date().isoformat()
        tagger_info = _current_tagger_info(request)
        decision_revision = _current_decision_revision(request)
        pairs = []
        mismatches = []
        cases = []
        non_replayable: list[dict] = []
        for record in request.app.state.requestlog.replayable(since):
            entry = _replay_case(record)
            reason = _replay_blocker(record, conn, tagger_info=tagger_info,
                                     decision_revision=decision_revision)
            snapshot = None
            if reason is None:
                snapshot, reason = _replay_snapshot(
                    conn, record["snapshot_revision"])
            if reason is not None:
                entry.update({"got": None, "replayable": False,
                              "reason": reason})
                cases.append(entry)
                non_replayable.append(entry)
                continue
            try:
                result = await runtime.route(snapshot, RuntimeRequest(
                    text=record["text"], headers=dict(record["headers"]),
                    n_messages=record["n_messages"]))
                got = _hit_model(result.outcome)
            except (NoActiveTreeError, SignalPreparationError) as exc:
                entry.update({"got": None, "replayable": False,
                              "reason": f"replay_error:{type(exc).__name__}"})
                cases.append(entry)
                non_replayable.append(entry)
                continue
            entry.update({"got": got, "replayable": True,
                          "tree_id": snapshot.tree_id,
                          "tree_revision": snapshot.tree_revision,
                          "snapshot_revision": snapshot.snapshot_revision})
            cases.append(entry)
            pairs.append((entry["expect"], got))
            if got != entry["expect"] and len(mismatches) < MAX_MISMATCHES:
                mismatches.append({"id": entry["id"],
                                   "expect": entry["expect"], "got": got})
        return pairs, mismatches, {"days": body.days, "source": "memory",
                                   "cases": cases,
                                   "non_replayable": non_replayable}

    raise HTTPException(422, detail="mode must be 'replay' or 'labels'")


@router.post("/eval")
async def post_eval(body: EvalIn, request: Request,
                    conn: sqlite3.Connection = Depends(get_conn)):
    pairs, mismatches, extra = await _eval_pairs(body, request, conn)
    report = summarize(body.mode, pairs, mismatches, **extra)
    EvalRepo(conn).save(_now(), body.mode, report["total"], report["matched"],
                        report["rate"], report)
    return report


@router.get("/evals")
def get_evals(limit: int = EVAL_REPORTS, conn: sqlite3.Connection = Depends(get_conn)):
    return {"evals": EvalRepo(conn).recent(limit)}


# --------------------------------------------------------------------------- #
# Overview aggregates (M3: traffic + confidence distribution + recent evals)
# --------------------------------------------------------------------------- #
@router.get("/overview")
def get_overview(request: Request, days: int = 14,
                 conn: sqlite3.Connection = Depends(get_conn)):
    # requests.ts is stored in local time with an offset; compare against a
    # local-date cutoff (date('now') would be UTC and skew the window).
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).date().isoformat()
    daily = [dict(r) for r in conn.execute(
        "SELECT substr(ts, 1, 10) AS d, COUNT(*) AS count FROM requests "
        "WHERE substr(ts, 1, 10) >= ? GROUP BY d ORDER BY d", (cutoff,))]
    routes = [dict(r) for r in conn.execute(
        "SELECT COALESCE(model, '(blocked)') AS model, COUNT(*) AS count "
        "FROM requests GROUP BY model ORDER BY count DESC")]
    blocked = conn.execute("SELECT COUNT(*) FROM requests WHERE decision = 'block'"
                           ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    # confidence distribution is memory-only (detail is not persisted) and
    # resets on restart — the console labels it "since restart".
    requestlog = request.app.state.requestlog
    return {"daily": daily, "routes": routes, "blocked": blocked, "total": total,
            "confidence": requestlog.confidence_histogram(),
            "confidence_since": requestlog.started_at,
            "evals": EvalRepo(conn).recent(EVAL_REPORTS)}
