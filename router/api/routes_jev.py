from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn, get_tree_repo
from router.control.config import ConfigurationService, ValidationFailed
from router.control.validation import active_tree_issues
from router.jev.client import ChoiceAnswer, DecisionQuestion, JevCandidate, JevClient, JevError
from router.jev.config import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
    JevConfigRepo,
    JevSettings,
)
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MAX_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
)
from router.routing.snapshot import read_jev_window
from router.store.repositories import ChangeRepo, SettingsRepo, now_iso
from router.store.routing_trees import RoutingTreeRepo

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
def put_jev(
    body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
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
    service = ConfigurationService(conn, trees, request.app.state.publisher)
    try:
        service.save_jev(settings)
    except ValidationFailed as exc:
        raise HTTPException(422, detail=[dict(issue.__dict__) for issue in exc.issues]) from exc
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
        question = DecisionQuestion("choice", settings.instructions, candidates)
        answers = await client.choose_many(settings, text, {"route": question})
        answer = answers["route"]
        if not isinstance(answer, ChoiceAnswer):
            raise HTTPException(502, detail="JEV route answer is not choice")
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


def _coerce_tokens(value, field: str) -> int:
    if isinstance(value, bool):
        raise HTTPException(422, detail=f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    raise HTTPException(422, detail=f"{field} 必须是整数")


@router.get("/jev/window")
def get_jev_window(conn: sqlite3.Connection = Depends(get_conn)):
    """JEV 输入窗口：服务上下文预算与发送给决策服务的输入上限。"""
    input_tokens, context_tokens = read_jev_window(conn)
    return {
        "context_tokens": context_tokens,
        "input_tokens": input_tokens,
        "defaults": {"context_tokens": DEFAULT_CONTEXT_TOKENS, "input_tokens": DEFAULT_INPUT_TOKENS},
        "min_context_tokens": MIN_CONTEXT_TOKENS,
        "min_input_tokens": MIN_INPUT_TOKENS,
        "max_tokens": MAX_TOKENS,
    }


@router.put("/jev/window")
def put_jev_window(
    body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    """保存输入窗口配置。

    - context_tokens：JEV 服务每请求上下文预算（= llama.cpp -c/--parallel），512..131072；
    - input_tokens：实际发送给 JEV 的输入上限，256..context_tokens；超出按首尾保留截断。
    """
    allowed = {"context_tokens", "input_tokens"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(422, detail=f"unknown JEV window fields: {sorted(unknown)}")
    if not body:
        raise HTTPException(422, detail="至少提供一个字段")

    current_input, current_context = read_jev_window(conn)
    context_tokens = (
        _coerce_tokens(body["context_tokens"], "context_tokens") if "context_tokens" in body else current_context
    )
    input_tokens = (
        _coerce_tokens(body["input_tokens"], "input_tokens") if "input_tokens" in body else current_input
    )
    if not MIN_CONTEXT_TOKENS <= context_tokens <= MAX_TOKENS:
        raise HTTPException(422, detail=f"context_tokens 必须在 {MIN_CONTEXT_TOKENS}..{MAX_TOKENS} 之间")
    if not MIN_INPUT_TOKENS <= input_tokens <= MAX_TOKENS:
        raise HTTPException(422, detail=f"input_tokens 必须在 {MIN_INPUT_TOKENS}..{MAX_TOKENS} 之间")
    if input_tokens > context_tokens:
        raise HTTPException(422, detail="input_tokens 不能大于 context_tokens")

    publisher = request.app.state.publisher
    with publisher.locked():
        repo = SettingsRepo(conn)
        with conn:
            repo.set("jev_context_tokens", str(context_tokens))
            repo.set("jev_input_tokens", str(input_tokens))
            ChangeRepo(conn).record(now_iso(), "jev_window", {
                "action": "window",
                "context_tokens": context_tokens,
                "input_tokens": input_tokens,
            })
        issues = active_tree_issues(conn, trees)
        if issues:
            raise HTTPException(422, detail=[dict(issue.__dict__) for issue in issues])
        publisher.refresh(conn)
    return get_jev_window(conn)
