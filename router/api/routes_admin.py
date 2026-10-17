"""Admin API: pipeline CRUD, lexicon, model registry, signal registry.

Every write that changes what the hot path reads also swaps the in-memory
snapshot (pipeline / backends / keyword groups) so request handling never
touches the database (design doc section 4.4).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from router.api.deps import get_conn
from router.api.upstream import load_backends
from router.pipeline.health import check_pipeline
from router.pipeline.model import node_from_dict, node_to_dict
from router.signals.registry import SIGNALS
from router.store.repositories import (KeywordRepo, ModelRepo, PipelineRepo,
                                       ProviderRepo, SettingsRepo)

router = APIRouter()


def _refresh_backends(request: Request, conn: sqlite3.Connection) -> None:
    request.app.state.backends = load_backends(conn)   # wholesale swap


class PipelineIn(BaseModel):
    nodes: list[dict]


@router.get("/pipeline")
def get_pipeline(conn: sqlite3.Connection = Depends(get_conn)):
    return {"nodes": [node_to_dict(n) for n in PipelineRepo(conn).load()],
            "version": SettingsRepo(conn).get("pipeline_version", "0")}


@router.put("/pipeline")
def put_pipeline(body: PipelineIn, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)):
    try:
        nodes = [node_from_dict(n) for n in body.nodes]
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    known = {m["id"] for m in ModelRepo(conn).list()}
    issues = check_pipeline(nodes, known)
    if issues:
        raise HTTPException(422, detail=[i.__dict__ for i in issues])
    PipelineRepo(conn).save(nodes)
    request.app.state.pipeline = nodes       # refresh the hot-path snapshot
    return {"ok": True}


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
    return {"ok": True}


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
    return {"ok": True}


@router.get("/signals")
def get_signals(conn: sqlite3.Connection = Depends(get_conn)):
    return {"signals": [s.__dict__ for s in SIGNALS.values()],
            "groups": list({s.group for s in SIGNALS.values()}),
            "keyword_groups": list(KeywordRepo(conn).groups())}


class TryIn(BaseModel):
    text: str


@router.post("/try")
async def try_pipeline(body: TryIn, request: Request):
    from router.pipeline.engine import decision_to_dict, evaluate
    from router.signals.tagger import TagRequest
    result = await request.app.state.tagger.tag(TagRequest(text=body.text))
    decision, trace = evaluate(request.app.state.pipeline, result.signals)
    return {"signals": result.signals,
            "decision": decision_to_dict(decision), "trace": trace}


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
    return {"daily": daily, "routes": routes, "blocked": blocked, "total": total}
