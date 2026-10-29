from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn
from router.jev.client import JevCandidate, JevClient, JevError
from router.jev.config import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
    JevConfigRepo,
    JevSettings,
)
from router.store.repositories import ChangeRepo, _now_iso

router = APIRouter()


def _public(settings: JevSettings | None) -> dict:
    return {
        "config": settings.public_dict(env_key_present=bool(os.environ.get("ROUTER_JEV_API_KEY"))) if settings else None,
        "defaults": {"instructions": DEFAULT_INSTRUCTIONS, "state_template": DEFAULT_STATE_TEMPLATE},
    }


@router.get("/jev")
def get_jev(conn: sqlite3.Connection = Depends(get_conn)):
    return _public(JevConfigRepo(conn).load())


@router.put("/jev")
def put_jev(body: dict, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    allowed = {"base_url", "model", "api_key", "timeout_seconds", "instructions", "state_template"}
    if set(body) - allowed:
        raise HTTPException(422, detail="unknown JEV config fields")
    repo = JevConfigRepo(conn)
    existing = repo.load()
    if "api_key" in body and not isinstance(body["api_key"], str):
        raise HTTPException(422, detail="api_key must be a string")
    try:
        settings = JevSettings(
            base_url=body.get("base_url", existing.base_url if existing else ""),
            model=body.get("model", existing.model if existing else ""),
            api_key=body.get("api_key", existing.api_key if existing else ""),
            timeout_seconds=body.get("timeout_seconds", existing.timeout_seconds if existing else 10.0),
            instructions=body.get("instructions", existing.instructions if existing else DEFAULT_INSTRUCTIONS),
            state_template=body.get("state_template", existing.state_template if existing else DEFAULT_STATE_TEMPLATE),
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    publisher = request.app.state.publisher
    with publisher.locked():
        repo.save(settings)
        ChangeRepo(conn).record(_now_iso(), "jev", {"action": "save", "model": settings.model})
        publisher.refresh(conn)
    return _public(settings)


@router.post("/jev/test")
async def test_jev(body: dict, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = JevConfigRepo(conn).load()
    if settings is None:
        raise HTTPException(422, detail="jev_not_configured")
    text = body.get("text", "请选择一个候选以验证决策服务。")
    if not isinstance(text, str) or len(text) > 10000:
        raise HTTPException(422, detail="text must be a string of at most 10000 characters")
    candidates = (
        JevCandidate("a", "候选 A", "用于验证决策服务连通性的第一个候选。"),
        JevCandidate("b", "候选 B", "用于验证决策服务连通性的第二个候选。"),
    )
    client = getattr(request.app.state, "jev_client", None) or JevClient(request.app.state.http)
    try:
        answer = await client.choose(settings, text, candidates)
    except JevError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    return {
        "selected_key": answer.selected_key,
        "probabilities": answer.probabilities,
        "p_top": answer.p_top,
        "margin": answer.margin,
        "usage": answer.usage,
        "duration_ms": answer.duration_ms,
    }
