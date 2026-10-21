"""Admin API: pipeline, lexicon, models, providers, signals, try, settings,
changes, eval and the overview aggregates.

Every write that changes what the hot path reads swaps the in-memory snapshot
(pipeline / backends / keyword groups) so request handling never touches the
database (design doc section 4.4). Every configuration write is also appended to
the ``changes`` audit trail (design 5.1).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from router.api.deps import get_conn
from router.api.sessions import read_last_vec
from router.api.upstream import load_backends
from router.evals import summarize
from router.pipeline.engine import evaluate
from router.pipeline.health import check_pipeline
from router.pipeline.model import station_from_dict, station_to_dict
from router.presets import PROVIDER_PRESETS
from router.reasoning import (FORMATS, FORMAT_IDS, LEVELS, ReasoningError,
                              normalize_reasoning, validate_model_profile)
from router.signals.registry import GROUPS, SIGNALS
from router.signals.tagger import TagRequest
from router.store.repositories import (ChangeRepo, EvalRepo, KeywordRepo,
                                        TimePeriodRepo,
                                       ModelRepo, ProviderRepo,
                                       SettingsRepo, StationRepo,
                                       model_ids_using_provider,
                                       model_references)

router = APIRouter()

RESERVED_SETTINGS = {"pipeline_version"}

#: 模型位 / 提供方名：写进分流树、URL 路径与 OpenAI body，保守字符集。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _refresh_backends(request: Request, conn: sqlite3.Connection) -> None:
    request.app.state.backends = load_backends(conn)   # wholesale swap


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class PipelineIn(BaseModel):
    # `stations` is the current shape; `nodes` is accepted for one release so
    # an older console build cannot 422 on a save. Exactly one wins.
    stations: list[dict] | None = None
    nodes: list[dict] | None = None


@router.get("/pipeline")
def get_pipeline(conn: sqlite3.Connection = Depends(get_conn)):
    return {"stations": [station_to_dict(s) for s in StationRepo(conn).load()],
            "version": SettingsRepo(conn).get("pipeline_version", "0")}


@router.put("/pipeline")
def put_pipeline(body: PipelineIn, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    payload = body.stations if body.stations is not None else (body.nodes or [])
    try:
        stations = [station_from_dict(d) for d in payload]
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    models = ModelRepo(conn).list()
    known = {m["id"] for m in models}
    formats = _provider_formats(conn)
    # §3.3: a leaf that selects a reasoning level must target a model that
    # declares it; the map is only passed here so old callers keep working.
    model_reasoning = {
        m["id"]: normalize_reasoning(_parse_profile_text(m.get("profile")),
                                     formats.get(m["provider"], "openai"))
        for m in models}
    issues = check_pipeline(stations, known, model_reasoning)
    # hard issues reject with 422 (pointing at station/node); warn-level issues
    # ride along in the response so the console can show them without blocking.
    hard = [i for i in issues if i.level == "hard"]
    warns = [i.__dict__ for i in issues if i.level != "hard"]
    if hard:
        raise HTTPException(422, detail=[i.__dict__ for i in hard])
    # No-op saves must not bump the version or append an audit row: the console
    # smoke check re-saves the unchanged pipeline, and an editor save without
    # edits should not look like a config change (2026-10-17 review N3).
    current = [station_to_dict(s) for s in StationRepo(conn).load()]
    changed = [station_to_dict(s) for s in stations] != current
    if not changed:
        return {"ok": True, "changed": False, "warnings": warns,
                "version": SettingsRepo(conn).get("pipeline_version", "0")}
    StationRepo(conn).save(stations)
    request.app.state.pipeline = stations       # refresh the hot-path snapshot
    version = SettingsRepo(conn).get("pipeline_version")
    ChangeRepo(conn).record(_now(), "pipeline",
                            {"stations": len(stations), "version": version})
    return {"ok": True, "changed": True, "warnings": warns, "version": version}


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


def _parse_profile_text(text: str | None) -> dict:
    """models.profile JSON text -> dict; empty/broken JSON behaves as undeclared."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _undeclared_reasoning(provider_format: str) -> dict:
    """§3.2 shape for "missing declaration" (console renders 未声明 from it)."""
    return {"declared": False, "supported": None,
            "format": provider_format if provider_format in FORMATS else "openai",
            "efforts": [], "default": None}


def _public_model(row: dict, provider_format: str) -> dict:
    normalized = normalize_reasoning(_parse_profile_text(row.get("profile")),
                                     provider_format)
    return {**row,
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
            "formats": [{"id": spec.id, "zh": spec.zh, "en": spec.en}
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
    provider_row = ProviderRepo(conn).get(provider)
    if provider_row is None:
        raise HTTPException(422, detail=f"unknown provider {provider}")
    profile = _validated_profile(body.profile,
                                 provider_row.get("reasoning_format") or "openai")
    if not ModelRepo(conn).create(model_id, upstream, provider, profile or "{}"):
        raise HTTPException(409, detail=f"model {model_id} 已存在")
    _refresh_backends(request, conn)
    ChangeRepo(conn).record(_now(), "model", {
        "action": "create", "id": model_id, "upstream": upstream,
        "provider": provider, "profile": body.profile})
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
    profile = None
    if body.profile is not None:
        formats = _provider_formats(conn)
        effective = provider if provider is not None else existing["provider"]
        profile = _validated_profile(body.profile,
                                     formats.get(effective, "openai"))
    ModelRepo(conn).update(model_id, upstream=upstream, provider=provider,
                           profile=profile)
    _refresh_backends(request, conn)
    ChangeRepo(conn).record(_now(), "model", {
        "action": "update", "id": model_id, "upstream": body.upstream,
        "provider": body.provider, "profile": body.profile})
    row = ModelRepo(conn).get(model_id)
    return {"ok": True, "model": _public_model(
        row, _provider_formats(conn).get(row["provider"], "openai"))}


@router.delete("/models/{model_id}")
def delete_model(model_id: str, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    if ModelRepo(conn).get(model_id) is None:
        raise HTTPException(404, detail=f"unknown model {model_id}")
    refs = model_references(StationRepo(conn).load(), model_id)
    if refs:
        raise HTTPException(409, detail={
            "message": f"model {model_id} 被分流树引用，先解除引用再删除",
            "references": refs})
    ModelRepo(conn).delete(model_id)
    _refresh_backends(request, conn)
    ChangeRepo(conn).record(_now(), "model",
                            {"action": "delete", "id": model_id})
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
    ProviderRepo(conn).upsert(name, base_url, key_env=key_env,
                              reasoning_format=reasoning_format,
                              api_key=api_key)
    _refresh_backends(request, conn)
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
    ProviderRepo(conn).delete(name)
    _refresh_backends(request, conn)
    ChangeRepo(conn).record(_now(), "provider",
                            {"action": "delete", "name": name})
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
    return {"signals": [{**s.__dict__, "free": not s.values}
                        for s in SIGNALS.values()],
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
    try:
        cfg = load_question_config(path)
    except Exception:                     # noqa: BLE001 - first boot / bad path
        try:
            cfg = load_question_config(DEFAULT_QUESTIONS_PATH)
        except Exception as exc:          # noqa: BLE001
            raise HTTPException(500, detail=f"cannot load question config: {exc}")
    return {"config": cfg, "meta": _tagger_meta(request)}


class SignalQuestionsIn(BaseModel):
    config: dict


@router.put("/signal-questions")
def put_signal_questions(body: SignalQuestionsIn, request: Request,
                         conn: sqlite3.Connection = Depends(get_conn)):
    """Validate + persist the editable JEV questions and hot-apply them."""
    from pathlib import Path

    from router.signals.jev import JevTaggerError, validate_question_config
    try:
        cfg = validate_question_config(body.config)
    except JevTaggerError as exc:
        raise HTTPException(422, detail=str(exc))
    path = Path(_questions_path(request))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, detail=f"cannot write question config: {exc}")
    tagger = request.app.state.tagger
    setter = getattr(tagger, "set_questions", None)
    if setter is not None:
        setter(cfg)                            # hot apply (JEV mode)
    ChangeRepo(conn).record(_now(), "signal_questions", f"saved {path}")
    return {"ok": True, "applied": setter is not None, "meta": _tagger_meta(request)}


@router.get("/requests/recent")
async def recent_requests(request: Request, limit: int = 20):
    """Newest requests from the in-memory ring (console 'calculation log').
    Detail is never persisted: the ring keeps the last 20 requests only."""
    drop = ("vec", "session_model")     # unused by the console; vec is 768 floats
    out = [{k: v for k, v in r.items() if k not in drop}
           for r in request.app.state.requestlog.recent_requests(limit)]
    return {"requests": out}


# --------------------------------------------------------------------------- #
# Try (calibration; never persisted)
# --------------------------------------------------------------------------- #
class TryIn(BaseModel):
    text: str
    session: str | None = None


@router.post("/try")
async def try_pipeline(body: TryIn, request: Request):
    from router.pipeline.engine import decision_to_dict
    result = await request.app.state.tagger.tag(TagRequest(
        text=body.text, last_vec=read_last_vec(request, body.session)))
    decision, trace = evaluate(request.app.state.pipeline, result.signals)
    return {"signals": result.signals, "vec_dim": len(result.vec or []),
            "decision": decision_to_dict(decision), "trace": trace}


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
class EvalIn(BaseModel):
    mode: str                       # 'replay' | 'labels'
    days: int = 7
    labels: list[dict] = []


async def _eval_pairs(body: EvalIn, request: Request, conn: sqlite3.Connection):
    nodes = request.app.state.pipeline
    pairs: list[tuple] = []
    mismatches: list[dict] = []
    if body.mode == "replay":
        since = (datetime.now().astimezone()
                 - timedelta(days=body.days)).date().isoformat()
        for r in request.app.state.requestlog.replayable(since):
            decision, _ = evaluate(nodes, r["signals"])
            got = decision.model if decision.kind == "route" else None
            pairs.append((r["model"], got))
            if got != r["model"] and len(mismatches) < 100:
                mismatches.append({"id": r["id"], "expect": r["model"], "got": got})
        return pairs, mismatches, {"days": body.days, "source": "memory"}
    if body.mode == "labels":
        for case in body.labels:
            text = str(case.get("text", ""))
            expect = case.get("expect_model")
            result = await request.app.state.tagger.tag(TagRequest(text=text))
            decision, _ = evaluate(nodes, result.signals)
            got = decision.model if decision.kind == "route" else None
            pairs.append((expect, got))
            if got != expect and len(mismatches) < 100:
                mismatches.append({"text": text[:200], "expect": expect, "got": got})
        return pairs, mismatches, {"labels": len(body.labels)}
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
def get_evals(limit: int = 3, conn: sqlite3.Connection = Depends(get_conn)):
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
    logger = request.app.state.requestlog
    return {"daily": daily, "routes": routes, "blocked": blocked, "total": total,
            "confidence": logger.confidence_histogram(),
            "confidence_since": logger.started_at,
            "evals": EvalRepo(conn).recent(3)}
