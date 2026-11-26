"""管理面多 JEV 服务目录 API（目录式 CRUD + 影响确认同步 + 逐条连接测试）。

目录只存 SQLite（``jev_services``）：列表/详情/测试都以数据库行为准，不读取
任何图的画布快照。画布自身的 JEV 由检查路由（``runtime_mismatch``）验证，见 spec
「多 JEV 目录」。

更新/删除采用与模型、provider 相同的两阶段协议（spec「引用影响与确认同步」）：

1. 先纯读扫描全部画布（``router.control.impacts.jev_impacts``）；存在受影响画布
   且请求未带确认时返回 409 ``impact_confirmation_required``，携带受影响画布
   清单（含当前 ``document_sha256``）与删除可选的替代服务目录。此阶段数据库、
   审计、画布文件、快照都不变。
2. 带确认重试时，由 ``RemoteCatalogSyncService`` 在 SQLite 写事务内重算影响
   （``recheck``，从内核重新读取当前画布）、写目录行，再用一次内核条件图批次
   原子替换全部候选画布，最后写审计并提交 SQLite；内核提交成功而 SQLite 失败
   时按原图与新基线哈希反向补偿，补偿失败显式为 503 ``catalog_sync_diverged``。
   确认哈希过期 → 409 ``graph_conflict``（报告当前哈希）。
3. JEV 同步永远把服务快照与窗口一起替换；删除 JEV 时凡有画布引用都必须提供
   替代服务（确认后缺失 → 422，未知 → 404，自身 → 422），保证不留下无决策
   服务的问题节点。新建/更新/删除都在同一事务里写 ``changes`` 审计行
   （``kind="jev"``，含 ``synced_trees``）。

密钥通道（v7 起彻底移出外围）：目录不再保存、查询或回显任何 JEV 密钥材料，
``api_key`` 是未知字段（422），``key_source``/``key_present`` 派生状态已删除；
真实密钥只由内核从 ``ROUTER_JEV_API_KEY`` 环境变量读取。目录投影里的
``key_env`` 固定为该环境变量名，仅供图文档/画布运行配置 schema 兼容。
连接测试（``POST /admin/jev/{id}/test``）同样不碰密钥：外围只把非密钥服务
描述代理到内核 ``POST /internal/v1/jev-test``，由内核带自己的 Key 执行（I6）。

旧单配置形态（单配置 PUT、单一 test 端点、独立 window 读写入口）已删除，
不保留兼容层。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import (
    catalog_service,
    commit_catalog_write,
    get_conn,
)
from router.control.errors import AlreadyExists, MissingError
from router.control.impacts import (
    delete_jev as delete_tree_jev,
    jev_impacts,
    require_confirmation,
    sync_jev,
)
from router.core_client import CoreGraphError
from router.jev.config import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
)
from router.jev.service import JEV_KEY_ENV, JevService
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ChangeRepo, now_iso

router = APIRouter()

#: DTO 可写的 ``JevService`` 非密钥字段；``key_env`` 固定为内核环境变量名，
#: 不再由请求体决定（密钥本身 v7 起根本不进外围）。
_SERVICE_FIELDS = (
    "id",
    "base_url",
    "model",
    "timeout_seconds",
    "instructions",
    "state_template",
    "input_tokens",
    "context_tokens",
)


class JevServiceIn(BaseModel):
    """``POST /admin/jev`` / ``PUT /admin/jev/{id}`` 请求 DTO：未知字段一律拒绝。

    值的类型与内容校验由 ``JevService.__post_init__`` 一体完成（原文案保持），
    因此这里只做显式字段过滤。``api_key``/``key_env`` 已随 v7 删除：密钥只在
    内核环境变量里，未知字段一律 422；``impact_confirmation`` 在 PUT（更新被
    引用服务）时是两阶段确认载荷，POST（新建）没有受影响画布而忽略。
    """

    model_config = ConfigDict(extra="forbid")

    id: Any = None
    base_url: Any = None
    model: Any = None
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


def _service_from_body(body: JevServiceIn, defaults: dict | None = None) -> JevService:
    base = defaults or {}
    values = {
        field: _provided(body, field, base.get(field, getattr(body, field)))
        for field in _SERVICE_FIELDS
    }
    # key_env 是图文档兼容字段：固定内核环境变量名，请求体无法改写。
    values["key_env"] = JEV_KEY_ENV
    try:
        return JevService(**values)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _public_service(service: JevService) -> dict:
    """公共投影：非密钥快照字段 + 窗口；没有任何密钥材料或密钥派生状态。"""
    return {
        **service.snapshot_dict(),
        **service.window_dict(),
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


@router.get("/jev")
def get_jev(conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    return {"services": [_public_service(service) for service in repo.list()]}


@router.post("/jev", status_code=201)
async def post_jev(
    body: JevServiceIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    service = _service_from_body(body)
    repo = JevServiceRepo(conn)
    if repo.get(service.id) is not None:
        raise HTTPException(409, detail=f"jev service {service.id} 已存在")

    def db_write(c):
        # 锁内再查一次：路由层检查与事务之间可能有另一个请求抢先创建同 id。
        if JevServiceRepo(c).get(service.id) is not None:
            raise AlreadyExists(f"jev service {service.id} 已存在")
        JevServiceRepo(c).save(service)

    def audit(c):
        ChangeRepo(c).record(now_iso(), "jev", {
            "action": "create", "id": service.id, "model": service.model,
            "synced_trees": [],
        })

    # 新建目录项不触发任何画布同步：已存画布可能早已声明了一个尚不存在的
    # jev.id（严格解析只校验结构），新建服务不会替它补齐快照，响应也没有
    # synced_trees。目录行与审计仍走共享原子写路径（空候选 = 只写 DB，不调用
    # 内核图批次）。
    await commit_catalog_write(
        request, catalog_service(request, conn),
        db_write=db_write, candidates=[], audit=audit)
    return {"service": _public_service(service)}


@router.get("/jev/{service_id}")
def get_jev_service(service_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    repo = JevServiceRepo(conn)
    return {"service": _public_service(_require(repo, service_id))}


@router.put("/jev/{service_id}")
async def put_jev_service(
    service_id: str, body: JevServiceIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    repo = JevServiceRepo(conn)
    # 资源存在性先于请求体校验：未知服务始终是 404，而不是被 body 细节抢先报 422。
    existing = _require(repo, service_id)
    if "id" in body.model_fields_set and body.id != service_id:
        raise HTTPException(422, detail="id in body must match path")
    service = _service_from_body(body, _defaults(existing))

    tree_list = await request.app.state.core.list_graphs()
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
        JevServiceRepo(c).save(service)

    def audit(c):
        ChangeRepo(c).record(now_iso(), "jev", {
            "action": "update", "id": service_id, "model": service.model,
            # 只记录非密钥事实：变化的目录字段（不存在密钥或密钥意图）。
            **_changed_audit_fields(existing, service),
            "synced_trees": synced,
        })

    async def recheck(_conn):
        # SQLite 写事务内、写目录行之前从内核重算影响清单：路由层扫描之后新出现
        # 的画布引用会让确认哈希不匹配 → 409，绝不带着过期名单提交。
        require_confirmation(
            jev_impacts(await request.app.state.core.list_graphs(), service_id),
            body.impact_confirmation,
            resource={"kind": "jev", "id": service_id}, replacements=[])

    await commit_catalog_write(
        request, catalog_service(request, conn),
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
    return {"service": _public_service(service), "synced_trees": synced}


@router.delete("/jev/{service_id}")
async def delete_jev_service(
    service_id: str, request: Request,
    body: JevDeleteIn | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    repo = JevServiceRepo(conn)
    _require(repo, service_id)
    replacement_id = body.replacement_jev_id if body is not None else None
    confirmation = body.impact_confirmation if body is not None else None

    tree_list = await request.app.state.core.list_graphs()
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

    async def recheck(_conn):
        # 路由层预览之后、SQLite 写事务之前可能有新画布开始引用该服务；确认哈希
        # 必须与事务内最新影响清单一致，否则 409 且数据库/审计/画布零写入。
        require_confirmation(
            jev_impacts(await request.app.state.core.list_graphs(), service_id),
            confirmation,
            resource={"kind": "jev", "id": service_id},
            replacements=_jev_replacements(_conn, service_id))
        # 确认之后才校验替代服务（与路由层同一优先级）：替代服务可能在路由层
        # 校验之后、事务之前被删除；事务内重查，404 语义与路由层一致，
        # 绝不让画布引用一个数据库里已不存在的替代服务。
        if replacement_id is not None \
                and JevServiceRepo(_conn).get(replacement_id) is None:
            raise MissingError(f"unknown jev service {replacement_id}")

    await commit_catalog_write(
        request, catalog_service(request, conn),
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
    """用数据库目录里的服务配置请内核试连：不读取任何图的画布快照。

    I6：JEV 密钥只属于内核。外围只把**非密钥**服务描述转发到内核的
    ``POST /internal/v1/jev-test``，由内核用它自己环境变量里的 Key 调用 JEV
    端点；外围进程既不构造 ``JevSettings``，也不注入任何密钥。
    没有内核客户端（手工装配的 app）时直接 503，绝不退回外围自行试连。
    """
    repo = JevServiceRepo(conn)
    service = _require(repo, service_id)
    text = body.text if "text" in body.model_fields_set else "请选择一个候选以验证决策服务。"
    if not isinstance(text, str) or len(text) > 10000:
        raise HTTPException(422, detail="text must be a string of at most 10000 characters")
    core = getattr(request.app.state, "core", None)
    if core is None:
        raise HTTPException(503, detail={"code": "core_unavailable"})
    payload = {
        "base_url": service.base_url,
        "model": service.model,
        "instructions": service.instructions,
        "state_template": service.state_template,
        "timeout_seconds": service.timeout_seconds,
        "text": text,
    }
    try:
        return await core.jev_test(payload)
    except CoreGraphError as exc:
        detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
        if exc.status_code in (422, 502):
            raise HTTPException(exc.status_code, detail=detail) from exc
        raise HTTPException(503, detail={"code": "core_unavailable"}) from exc
