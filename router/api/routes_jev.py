from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.control.config import ConfigurationService, JevWindow
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
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()


class JevConfigIn(BaseModel):
    """PUT /admin/jev 请求 DTO：未知字段一律拒绝（替代手写 set(body)-allowed）。

    值的类型与内容校验仍由 JevSettings.__post_init__ 一体完成（原文案保持）。"""

    model_config = ConfigDict(extra="forbid")

    base_url: Any = None
    model: Any = None
    api_key: Any = None
    timeout_seconds: Any = None
    instructions: Any = None
    state_template: Any = None


class JevWindowIn(BaseModel):
    """PUT /admin/jev/window 请求 DTO：未知字段一律拒绝；数值折算仍走
    _coerce_tokens（bool/字符串按 422 处理），范围校验在 JevWindow。"""

    model_config = ConfigDict(extra="forbid")

    context_tokens: Any = None
    input_tokens: Any = None


def _provided(body: BaseModel, name: str, fallback):
    """DTO 缺失字段才回落到已存值；显式 null 交给值校验报 422（与旧 dict 语义一致）。"""
    return getattr(body, name) if name in body.model_fields_set else fallback


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
    body: JevConfigIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    repo = JevConfigRepo(conn)
    existing = repo.load()
    if "api_key" in body.model_fields_set and not isinstance(body.api_key, str):
        raise HTTPException(422, detail="api_key must be a string")
    try:
        settings = JevSettings(
            base_url=_provided(body, "base_url", existing.base_url if existing else ""),
            model=_provided(body, "model", existing.model if existing else ""),
            api_key=_provided(body, "api_key", existing.api_key if existing else ""),
            timeout_seconds=_provided(body, "timeout_seconds", existing.timeout_seconds if existing else 10.0),
            instructions=_provided(body, "instructions", existing.instructions if existing else DEFAULT_INSTRUCTIONS),
            state_template=_provided(body, "state_template", existing.state_template if existing else DEFAULT_STATE_TEMPLATE),
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    # ValidationFailed（活动图校验失败）由 app 级异常处理器统一映射 422 Issue 数组。
    ConfigurationService(conn, trees, request.app.state.publisher).save_jev(settings)
    return _public(settings)


class JevTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Any = None


@router.post("/jev/test")
async def test_jev(body: JevTestIn, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = JevConfigRepo(conn).load()
    if settings is None:
        raise HTTPException(422, detail="jev_not_configured")
    text = body.text if "text" in body.model_fields_set else "请选择一个候选以验证决策服务。"
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
    body: JevWindowIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    """保存输入窗口配置（服务层 ConfigurationService.save_window）。

    - context_tokens：JEV 服务每请求上下文预算（= llama.cpp -c/--parallel）；
    - input_tokens：实际发送给 JEV 的输入上限；超出按首尾保留截断。
    """
    current_input, current_context = read_jev_window(conn)
    if not body.model_fields_set:
        raise HTTPException(422, detail="至少提供一个字段")
    context_tokens = (_coerce_tokens(body.context_tokens, "context_tokens")
                      if "context_tokens" in body.model_fields_set else current_context)
    input_tokens = (_coerce_tokens(body.input_tokens, "input_tokens")
                    if "input_tokens" in body.model_fields_set else current_input)
    try:
        window = JevWindow(input_tokens=input_tokens, context_tokens=context_tokens)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    # ValidationFailed（活动图校验失败）由 app 级异常处理器统一映射；本用例失败时
    # 窗口 DB 写与审计随事务回滚（brief Step 1 RED 场景）。
    ConfigurationService(conn, trees, request.app.state.publisher).save_window(window)
    return get_jev_window(conn)
