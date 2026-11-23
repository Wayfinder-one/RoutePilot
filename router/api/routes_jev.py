"""管理面多 JEV 服务目录 API（目录式 CRUD + 逐条连接测试）。

目录只存 SQLite（``jev_services``）：列表/详情/测试都以数据库行为准，不再读取
活动画布快照。画布自身的 JEV 由检查路由（``runtime_mismatch``）验证，见 spec
「多 JEV 目录」。

密钥通道：真实 ``api_key`` 只存在数据库列里，任何响应都不回显；``key_source``
是派生状态（stored/env/none），不是密钥本身。

旧单配置形态（单配置 PUT、单一 test 端点、独立 window 读写入口）已删除，
不保留兼容层。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn
from router.jev.client import (
    ChoiceAnswer,
    DecisionQuestion,
    JevCandidate,
    JevClient,
    JevError,
)
from router.jev.config import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
)
from router.jev.service import JevService
from router.store.jev_services import JevServiceRepo

router = APIRouter()

#: ``JevService`` 的字段集合（api_key 只进数据库列，不属于领域 DTO）。
_SERVICE_FIELDS = (
    "id",
    "base_url",
    "model",
    "key_env",
    "timeout_seconds",
    "instructions",
    "state_template",
    "input_tokens",
    "context_tokens",
)


class JevServiceIn(BaseModel):
    """``POST /admin/jev`` / ``PUT /admin/jev/{id}`` 请求 DTO：未知字段一律拒绝。

    值的类型与内容校验由 ``JevService.__post_init__`` 一体完成（原文案保持），
    因此这里只做显式字段过滤。``api_key`` 与 ``impact_confirmation`` 不进
    ``JevService``：前者单独写入数据库列，后者本单元接受但忽略（Task 5 使用）。
    """

    model_config = ConfigDict(extra="forbid")

    id: Any = None
    base_url: Any = None
    model: Any = None
    key_env: Any = ""
    api_key: Any = None
    timeout_seconds: Any = 10.0
    instructions: Any = DEFAULT_INSTRUCTIONS
    state_template: Any = DEFAULT_STATE_TEMPLATE
    input_tokens: Any = 1024
    context_tokens: Any = 4096
    impact_confirmation: Any = None


class JevTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Any = None


def _provided(body: BaseModel, name: str, fallback):
    """DTO 缺失字段才回落到已存值；显式 null 交给值校验报 422（与旧语义一致）。"""
    return getattr(body, name) if name in body.model_fields_set else fallback


def _validate_api_key(body: BaseModel, *, allow_null: bool) -> None:
    if "api_key" not in body.model_fields_set:
        return
    if body.api_key is None and allow_null:
        return
    if not isinstance(body.api_key, str):
        # PUT 的显式 null 不是「省略」（省略 = 保留已存密钥），null 是非法值。
        raise HTTPException(422, detail="api_key must be a string")


def _service_from_body(body: JevServiceIn, defaults: dict | None = None) -> JevService:
    base = defaults or {}
    values = {
        field: _provided(body, field, base.get(field, getattr(body, field)))
        for field in _SERVICE_FIELDS
    }
    try:
        return JevService(**values)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _public_service(conn: sqlite3.Connection, service: JevService) -> dict:
    """公共投影：非密钥快照字段 + 窗口 + 派生密钥状态；绝不包含 api_key。"""
    repo = JevServiceRepo(conn)
    stored = repo.stored_api_key(service)
    env_present = bool(service.key_env and os.environ.get(service.key_env))
    return {
        **service.snapshot_dict(),
        **service.window_dict(),
        "key_present": bool(stored or env_present),
        "key_source": "stored" if stored else "env" if env_present else "none",
    }


def _require(repo: JevServiceRepo, service_id: str) -> JevService:
    service = repo.get(service_id)
    if service is None:
        raise HTTPException(404, detail=f"unknown jev service {service_id}")
    return service


def _defaults(service: JevService | None) -> dict:
    if service is None:
        return {}
    return {
        **service.snapshot_dict(),
        **service.window_dict(),
    }


@router.get("/jev")
def get_jev(conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    return {"services": [_public_service(conn, service) for service in repo.list()]}


@router.post("/jev", status_code=201)
def post_jev(body: JevServiceIn, conn: sqlite3.Connection = Depends(get_conn)):
    _validate_api_key(body, allow_null=True)
    service = _service_from_body(body)
    repo = JevServiceRepo(conn)
    if repo.get(service.id) is not None:
        raise HTTPException(409, detail=f"jev service {service.id} 已存在")
    repo.save(service, api_key=body.api_key)
    return {"service": _public_service(conn, service)}


@router.get("/jev/{service_id}")
def get_jev_service(service_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    return {"service": _public_service(conn, _require(repo, service_id))}


@router.put("/jev/{service_id}")
def put_jev_service(
    service_id: str, body: JevServiceIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    _validate_api_key(body, allow_null=False)
    if "id" in body.model_fields_set and body.id != service_id:
        raise HTTPException(422, detail="id in body must match path")
    repo = JevServiceRepo(conn)
    existing = _require(repo, service_id)
    service = _service_from_body(body, _defaults(existing))
    # 未提供 api_key 时交给仓储的端点匹配规则：仅 base_url+model 不变才复用旧密钥。
    repo.save(service, api_key=body.api_key if "api_key" in body.model_fields_set else None)
    return {"service": _public_service(conn, service)}


@router.delete("/jev/{service_id}")
def delete_jev_service(service_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    if not repo.delete(service_id):
        raise HTTPException(404, detail=f"unknown jev service {service_id}")
    return {"deleted": service_id}


@router.post("/jev/{service_id}/test")
async def test_jev_service(
    service_id: str, body: JevTestIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """用数据库目录里的服务配置试连：不读取活动画布快照。"""
    repo = JevServiceRepo(conn)
    service = _require(repo, service_id)
    settings = service.settings(
        api_key=repo.stored_api_key(service)
        or (os.environ.get(service.key_env, "") if service.key_env else ""))
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
