"""管理面多 JEV 服务目录 API（目录式 CRUD + 影响确认同步 + 逐条连接测试）。

目录只存 SQLite（``jev_services``）：列表/详情/测试都以数据库行为准，不读取
任何图的画布快照。画布自身的 JEV 由检查路由（``runtime_mismatch``）验证，见 spec
「多 JEV 目录」。

更新/删除采用与模型、provider 相同的两阶段协议（spec「引用影响与确认同步」）：

1. 先纯读扫描全部画布（``router.control.impacts.jev_impacts``）；存在受影响画布
   且请求未带确认时返回 409 ``impact_confirmation_required``，携带受影响画布
   清单（含当前 ``document_sha256``）与删除可选的替代服务目录。此阶段数据库、
   审计、画布文件、快照都不变。
2. 带确认重试时，由 ``CatalogSyncService`` 在单写锁 + 单数据库事务内重算影响
   （``recheck``）、写目录行与审计行、严格校验候选画布并原子替换；同步集合里含
   任一启用图时重发布完整启用路由集。确认哈希过期 → 409 ``graph_conflict``（报
   告当前哈希）。
3. JEV 同步永远把服务快照与窗口一起替换；删除 JEV 时凡有画布引用都必须提供
   替代服务（确认后缺失 → 422，未知 → 404，自身 → 422），保证不留下无决策
   服务的问题节点。新建/更新/删除都在同一事务里写 ``changes`` 审计行
   （``kind="jev"``，含 ``synced_trees``）。

密钥通道：真实 ``api_key`` 只存在数据库列里，任何响应、影响清单、替代目录与
审计都不回显；``key_source`` 是派生状态（stored/env/none），不是密钥本身。

旧单配置形态（单配置 PUT、单一 test 端点、独立 window 读写入口）已删除，
不保留兼容层。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.control.catalog_sync import CatalogSyncService
from router.control.errors import AlreadyExists, MissingError
from router.control.impacts import (
    delete_jev as delete_tree_jev,
    jev_impacts,
    require_confirmation,
    sync_jev,
)
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
from router.store.repositories import ChangeRepo, now_iso
from router.store.routing_trees import RoutingTreeRepo

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
    因此这里只做显式字段过滤。``api_key`` 单独写入数据库列，不进 ``JevService``；
    ``impact_confirmation`` 在 PUT（更新被引用服务）时是两阶段确认载荷，POST
    （新建）没有受影响画布而忽略。
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


class JevDeleteIn(BaseModel):
    """``DELETE /admin/jev/{id}`` 请求 DTO。

    有画布引用该服务时，确认请求必须带 ``replacement_jev_id``（替代服务 id）；
    未确认的第一次调用返回 409 影响预览，不要求替代服务。
    """

    model_config = ConfigDict(extra="forbid")

    replacement_jev_id: Any = None
    impact_confirmation: Any = None


class JevTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Any = None


def _provided(body: BaseModel, name: str, fallback):
    """DTO 缺失字段才回落到已存值；显式 null 交给值校验报 422（与旧语义一致）。"""
    return getattr(body, name) if name in body.model_fields_set else fallback


def _validate_api_key(body: BaseModel, *, allow_null: bool) -> None:
    """校验显式传入的 ``api_key``。

    ``allow_null=True``（POST）把显式 null 当作「不提供密钥」；``allow_null=False``
    （PUT）下 null 与省略不同：省略表示保留已存密钥，显式 null 是非法值。两种
    情况下非字符串都是 422。
    """
    if "api_key" not in body.model_fields_set:
        return
    if body.api_key is None and allow_null:
        return
    if not isinstance(body.api_key, str):
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


#: 更新审计里逐字段记录的**非密钥**变化（model 单独固定记录）。
_AUDIT_CHANGE_FIELDS = (
    "base_url",
    "instructions",
    "state_template",
    "timeout_seconds",
    "input_tokens",
    "context_tokens",
)


def _key_state_intent(body: JevServiceIn, existing: JevService,
                      service: JevService) -> str:
    """更新审计的密钥意图枚举（set / cleared / preserved），绝不记录密钥值。

    与 ``JevServiceRepo.save(api_key=None)`` 的保留规则一致：只有 base_url 与
    model 都不变时才保留已存密钥；端点/模型变化但未显式提供 api_key 时旧密钥
    被清除。
    """
    if "api_key" in body.model_fields_set:
        return "set" if body.api_key else "cleared"
    if existing.base_url == service.base_url and existing.model == service.model:
        return "preserved"
    return "cleared"


def _changed_audit_fields(existing: JevService, service: JevService) -> dict:
    return {
        field: getattr(service, field)
        for field in _AUDIT_CHANGE_FIELDS
        if getattr(service, field) != getattr(existing, field)
    }


def _jev_replacements(conn: sqlite3.Connection, service_id: str) -> list[dict]:
    """删除预览可选的替代目录：仅非密钥字段，供控制台展示 id / 模型 / 地址。"""
    return [
        {
            "id": service.id,
            "base_url": service.base_url,
            "model": service.model,
            "input_tokens": service.input_tokens,
            "context_tokens": service.context_tokens,
        }
        for service in JevServiceRepo(conn).list()
        if service.id != service_id
    ]


def _catalog_service(request: Request, conn: sqlite3.Connection,
                     trees: RoutingTreeRepo) -> CatalogSyncService:
    return CatalogSyncService(conn, trees, request.app.state.publisher)


@router.get("/jev")
def get_jev(conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    return {"services": [_public_service(conn, service) for service in repo.list()]}


@router.post("/jev", status_code=201)
def post_jev(
    body: JevServiceIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    _validate_api_key(body, allow_null=True)
    service = _service_from_body(body)
    repo = JevServiceRepo(conn)
    if repo.get(service.id) is not None:
        raise HTTPException(409, detail=f"jev service {service.id} 已存在")

    def db_write(c):
        # 锁内再查一次：路由层检查与事务之间可能有另一个请求抢先创建同 id。
        if JevServiceRepo(c).get(service.id) is not None:
            raise AlreadyExists(f"jev service {service.id} 已存在")
        JevServiceRepo(c).save(service, api_key=body.api_key)

    def audit(c):
        ChangeRepo(c).record(now_iso(), "jev", {
            "action": "create", "id": service.id, "model": service.model,
            "synced_trees": [],
        })

    # 新建目录项不触发任何画布同步：已存画布可能早已声明了一个尚不存在的
    # jev.id（严格解析只校验结构），新建服务不会替它补齐快照，响应也没有
    # synced_trees。目录行与审计仍走共享原子写路径（空候选 = 只写 DB）。
    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=[], audit=audit)
    return {"service": _public_service(conn, service)}


@router.get("/jev/{service_id}")
def get_jev_service(service_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    return {"service": _public_service(conn, _require(repo, service_id))}


@router.put("/jev/{service_id}")
def put_jev_service(
    service_id: str, body: JevServiceIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    repo = JevServiceRepo(conn)
    # 资源存在性先于请求体校验：未知服务始终是 404，而不是被 body 细节抢先报 422。
    existing = _require(repo, service_id)
    if "id" in body.model_fields_set and body.id != service_id:
        raise HTTPException(422, detail="id in body must match path")
    _validate_api_key(body, allow_null=False)
    service = _service_from_body(body, _defaults(existing))

    tree_list = trees.list()
    impacts = jev_impacts(tree_list, service_id)
    require_confirmation(
        impacts, body.impact_confirmation,
        resource={"kind": "jev", "id": service_id}, replacements=[])

    impacted_ids = {item["id"] for item in impacts}
    candidates = [
        sync_jev(tree, service) for tree in tree_list if tree.id in impacted_ids
    ]
    synced = [tree.id for tree in candidates]

    def db_write(c):
        if JevServiceRepo(c).get(service_id) is None:
            raise MissingError(f"unknown jev service {service_id}")
        # 未提供 api_key 时交给仓储的端点匹配规则：仅 base_url+model 不变才复用旧密钥。
        JevServiceRepo(c).save(
            service,
            api_key=body.api_key if "api_key" in body.model_fields_set else None)

    def audit(c):
        ChangeRepo(c).record(now_iso(), "jev", {
            "action": "update", "id": service_id, "model": service.model,
            # 只记录非密钥事实：变化的字段值 + 密钥意图枚举（不含密钥本身）。
            **_changed_audit_fields(existing, service),
            "key_state": _key_state_intent(body, existing, service),
            "synced_trees": synced,
        })

    def recheck(_conn):
        # 写锁内、事务内、写目录行之前重算影响清单：路由层扫描之后新出现的
        # 画布引用会让确认哈希不匹配 → 409，绝不带着过期名单提交。
        require_confirmation(
            jev_impacts(trees.list(), service_id), body.impact_confirmation,
            resource={"kind": "jev", "id": service_id}, replacements=[])

    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
    return {"service": _public_service(conn, service), "synced_trees": synced}


@router.delete("/jev/{service_id}")
def delete_jev_service(
    service_id: str, request: Request,
    body: JevDeleteIn | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    repo = JevServiceRepo(conn)
    _require(repo, service_id)
    replacement_id = body.replacement_jev_id if body is not None else None
    confirmation = body.impact_confirmation if body is not None else None

    tree_list = trees.list()
    impacts = jev_impacts(tree_list, service_id)
    require_confirmation(
        impacts, confirmation,
        resource={"kind": "jev", "id": service_id},
        replacements=_jev_replacements(conn, service_id))

    # 确认之后才校验替代服务：未确认请求先拿到影响预览，而不是被替代参数抢先报错。
    replacement = None
    if replacement_id is not None:
        if not isinstance(replacement_id, str):
            raise HTTPException(422, detail="replacement_jev_id 必须是字符串")
        if replacement_id == service_id:
            raise HTTPException(422, detail="replacement_jev_id 不能是服务自身")
        replacement = repo.get(replacement_id)
        if replacement is None:
            raise HTTPException(404, detail=f"unknown jev service {replacement_id}")
    if impacts and replacement is None:
        raise HTTPException(
            422, detail="JEV 服务仍被画布引用，必须提供 replacement_jev_id")

    impacted_ids = {item["id"] for item in impacts}
    candidates = [
        delete_tree_jev(tree, service_id, replacement=replacement)
        for tree in tree_list if tree.id in impacted_ids
    ]
    synced = [tree.id for tree in candidates]

    def db_write(c):
        if not JevServiceRepo(c).delete(service_id):
            raise MissingError(f"unknown jev service {service_id}")

    def audit(c):
        ChangeRepo(c).record(now_iso(), "jev", {
            "action": "delete", "id": service_id,
            "replacement": replacement_id, "synced_trees": synced,
        })

    def recheck(_conn):
        # 路由层预览之后、加锁之前可能有新画布开始引用该服务；确认哈希必须与
        # 锁内最新影响清单一致，否则 409 且数据库/审计/画布零写入。
        require_confirmation(
            jev_impacts(trees.list(), service_id), confirmation,
            resource={"kind": "jev", "id": service_id},
            replacements=_jev_replacements(_conn, service_id))
        # 确认之后才校验替代服务（与路由层同一优先级）：替代服务可能在路由层
        # 校验之后、加锁之前被删除；锁内重查，404 语义与路由层一致，
        # 绝不让画布引用一个数据库里已不存在的替代服务。
        if replacement_id is not None \
                and JevServiceRepo(_conn).get(replacement_id) is None:
            raise MissingError(f"unknown jev service {replacement_id}")

    _catalog_service(request, conn, trees).commit(
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
    return {
        "deleted": service_id,
        "replacement_jev_id": replacement_id,
        "synced_trees": synced,
    }


@router.post("/jev/{service_id}/test")
async def test_jev_service(
    service_id: str, body: JevTestIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """用数据库目录里的服务配置试连：不读取任何图的画布快照。"""
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
