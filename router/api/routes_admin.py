"""Admin API: pipeline, lexicon, models, providers, signals, try, settings,
changes, eval and the overview aggregates.

Every write that changes what the hot path reads swaps the in-memory snapshot
(pipeline / backends / keyword groups) so request handling never touches the
database (design doc section 4.4). Every configuration write is also appended to
the ``changes`` audit trail (design 5.1).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from router.api.deps import get_conn
from router.api.sessions import read_last_vec
from router.api.upstream import load_backends
from router.evals import summarize
from router.pipeline.engine import evaluate
from router.pipeline.health import check_pipeline
from router.pipeline.model import station_from_dict, station_to_dict
from router.signals.registry import GROUPS, SIGNALS
from router.signals.tagger import TagRequest
from router.store.repositories import (ChangeRepo, EvalRepo, KeywordRepo,
                                       ModelRepo, ProviderRepo, RequestRepo,
                                       SettingsRepo, StationRepo)

router = APIRouter()

RESERVED_SETTINGS = {"pipeline_version"}


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
    known = {m["id"] for m in ModelRepo(conn).list()}
    issues = check_pipeline(stations, known)
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


# --------------------------------------------------------------------------- #
# Models & providers
# --------------------------------------------------------------------------- #
@router.get("/models")
def get_models(conn: sqlite3.Connection = Depends(get_conn)):
    return {"models": ModelRepo(conn).list()}


class ModelIn(BaseModel):
    upstream: str | None = None
    provider: str | None = None
    profile: str | dict | None = None


@router.put("/models/{model_id}")
def put_model(model_id: str, body: ModelIn, request: Request,
              conn: sqlite3.Connection = Depends(get_conn)):
    profile = (json.dumps(body.profile, ensure_ascii=False)
               if isinstance(body.profile, dict) else body.profile)
    if not ModelRepo(conn).update(model_id, upstream=body.upstream,
                                  provider=body.provider, profile=profile):
        raise HTTPException(404, detail=f"unknown model {model_id}")
    _refresh_backends(request, conn)
    ChangeRepo(conn).record(_now(), "model",
                            {"id": model_id, "upstream": body.upstream,
                             "provider": body.provider, "profile": body.profile})
    return {"ok": True, "model": ModelRepo(conn).get(model_id)}


@router.get("/providers")
def get_providers(conn: sqlite3.Connection = Depends(get_conn)):
    return {"providers": ProviderRepo(conn).list()}


class ProviderIn(BaseModel):
    base_url: str
    key_env: str = ""


@router.put("/providers/{name}")
def put_provider(name: str, body: ProviderIn, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    ProviderRepo(conn).upsert(name, body.base_url, body.key_env)
    _refresh_backends(request, conn)
    ChangeRepo(conn).record(_now(), "provider",
                            {"name": name, "base_url": body.base_url,
                             "key_env": body.key_env})
    return {"ok": True}


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
            "keyword_groups": list(KeywordRepo(conn).groups())}


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
def recent_requests(limit: int = 20,
                    conn: sqlite3.Connection = Depends(get_conn)):
    """Newest persisted requests with their signal pack (calculation log)."""
    limit = max(1, min(int(limit), 100))
    return {"requests": RequestRepo(conn).recent(limit)}


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
        for r in RequestRepo(conn).replayable(since):
            decision, _ = evaluate(nodes, r["signals"])
            got = decision.model if decision.kind == "route" else None
            pairs.append((r["model"], got))
            if got != r["model"] and len(mismatches) < 100:
                mismatches.append({"id": r["id"], "expect": r["model"], "got": got})
        return pairs, mismatches, {"days": body.days}
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
CONF_BUCKETS = 5


@router.get("/overview")
def get_overview(days: int = 14, conn: sqlite3.Connection = Depends(get_conn)):
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
    buckets = [0] * CONF_BUCKETS
    # model signals only: rule signals are deterministic (c == 1) and would
    # swamp the distribution the dashboard exists to show (design 6).
    for (conf,) in conn.execute("SELECT confidence FROM request_signals "
                                "WHERE confidence IS NOT NULL AND is_rule = 0"):
        buckets[min(CONF_BUCKETS - 1, int(float(conf) * CONF_BUCKETS))] += 1
    confidence = [{"lo": i / CONF_BUCKETS, "hi": (i + 1) / CONF_BUCKETS,
                   "count": c} for i, c in enumerate(buckets)]
    return {"daily": daily, "routes": routes, "blocked": blocked, "total": total,
            "confidence": confidence, "evals": EvalRepo(conn).recent(3)}
