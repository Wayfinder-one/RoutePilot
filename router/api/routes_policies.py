"""/admin/policies、layout 与 /admin/try（Task 6，设计 §6.1–§6.3）。

HTTP 边界只做三件事：

- **严格 DTO**：``PolicyCreateIn`` 只收 ``id/policy``，``TryIn`` 只收
  ``policy_id/policy/text/session_id/now``，全部 ``extra="forbid"``；PUT 的
  body 直接是完整 v4 文档（``PolicyDocumentIn`` 根模型），layout PUT 的
  body 直接是 JSON object（``LayoutIn`` 根模型）。解析失败的原始
  ``ValueError`` 统一折叠为
  ``ValidationFailed([Issue("policy_parse_error", "$", ...)])``。
- **资源端点**：写用例全部在 ``router.control.policies.PolicyService``，
  PUT 从 ``If-Match`` 头取 ``base_document_sha256`` 整份替换（不合并）。
- **dry-run**：try 选择存量资源或内联草稿，仅做结构校验
  （``validate_policy``，不查目录），用与线上一致的
  ``build_snapshot`` 构造快照后走真实 ``RoutingService.route(dry_run=True)``
  ——不写文件、不发布、不写粘性、不增加在途计数、不调用上游。

服务层/仓库层异常 → HTTP 的映射安装在 app 级（app.py）：
PolicyConflictError→409（附当前哈希）、AlreadyExists→409、MissingError→404、
ActivePolicyError→409、PolicyReadOnlyError/PolicyStorageError→503、
ValidationFailed→422（Issue 数组）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, RootModel

from router.api.deps import get_conn, get_policy_repo
from router.control.errors import MissingError, ValidationFailed
from router.control.policies import PolicyService
from router.jev.serialization import answer_to_dict
from router.routing.conditions import TimePeriod
from router.routing.policy.model import (
    Policy,
    PolicyResource,
    policy_from_dict,
    policy_to_dict,
)
from router.routing.policy.validate import Issue, validate_policy
from router.routing.snapshot import (
    build_snapshot,
    model_capability,
    read_jev_window,
)
from router.routing.types import (
    NoActivePolicyError,
    NoAvailableBackend,
    RoutingMiss,
    RoutingRequest,
)
from router.store.jev_config import JevConfigRepo
from router.store.policies import PolicyRepo
from router.store.repositories import ModelRepo, TimePeriodRepo
from router.upstream.catalog import load_backends

router = APIRouter()


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------
class PolicyCreateIn(BaseModel):
    """POST /admin/policies：只收 id（可选）/policy，未知字段拒绝。"""

    model_config = ConfigDict(extra="forbid")

    id: Any = None
    policy: Any = None


class PolicyDocumentIn(RootModel):
    """PUT /admin/policies/{id}：body 直接是完整 v4 文档（无信封）。"""

    root: object


class LayoutIn(RootModel):
    """PUT /admin/policies/{id}/layout：body 直接是 JSON object。"""

    root: dict


class TryIn(BaseModel):
    """POST /admin/try：policy_id 与 policy 恰好提供其一。

    ``session_id`` 缺省空串；``now`` 缺省当前时间，显式值必须是带 offset 的
    严格 ISO-8601 字符串。值校验在路由内完成（DTO 只锁字段集合）。
    """

    model_config = ConfigDict(extra="forbid")

    policy_id: Any = None
    policy: Any = None
    text: Any = None
    session_id: Any = None
    now: Any = None


# ---------------------------------------------------------------------------
# 资源响应
# ---------------------------------------------------------------------------
def resource_payload(resource: PolicyResource) -> dict:
    return {
        "id": resource.id,
        "enabled": resource.enabled,
        "policy_revision": resource.policy_revision,
        "updated_at": resource.updated_at,
        "document_sha256": resource.document_sha256,
        "policy": policy_to_dict(resource.policy),
    }


def _service(request: Request, conn: sqlite3.Connection,
             policies: PolicyRepo) -> PolicyService:
    return PolicyService(conn, policies, request.app.state.publisher)


def _published_revision(request: Request) -> str:
    snapshot = request.app.state.snapshot
    return getattr(snapshot, "snapshot_revision", "") if snapshot is not None else ""


def _parse_issue(exc: ValueError) -> ValidationFailed:
    return ValidationFailed([
        Issue("policy_parse_error", "$", params={"detail": str(exc)})])


def _parse_policy(raw) -> Policy:
    if not isinstance(raw, dict):
        raise _parse_issue(ValueError("policy must be a JSON object"))
    try:
        return policy_from_dict(raw)
    except ValueError as exc:
        raise _parse_issue(exc) from exc


def _policies_payload(request: Request, policies: PolicyRepo) -> dict:
    return {
        "policies": [resource_payload(item) for item in policies.list()],
        "snapshot_revision": _published_revision(request),
        "policy_status": request.app.state.publisher.status(),
    }


# ---------------------------------------------------------------------------
# 策略资源端点
# ---------------------------------------------------------------------------
@router.get("/policies")
def list_policies(request: Request, policies: PolicyRepo = Depends(get_policy_repo)):
    return _policies_payload(request, policies)


@router.post("/policies", status_code=201)
def create_policy(
    body: PolicyCreateIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    policies: PolicyRepo = Depends(get_policy_repo),
):
    # id 类型在 API 层先拒绝：非字符串交给仓库会变成 _validate_id 的
    # TypeError（→ 500）；这里折叠为与其它解析失败同型的 422 Issue。
    if body.id is not None and not isinstance(body.id, str):
        raise _parse_issue(ValueError("id must be a string"))
    policy = _parse_policy(body.policy)
    # 字符串 id 的形状仍由仓库 _validate_id 把关 → PolicyStorageError → 503。
    created = _service(request, conn, policies).create(body.id, policy)
    return {"policy_resource": resource_payload(created)}


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str, policies: PolicyRepo = Depends(get_policy_repo)):
    resource = policies.get(policy_id)
    if resource is None:
        raise MissingError(f"unknown policy {policy_id}")
    return {"policy_resource": resource_payload(resource)}


@router.put("/policies/{policy_id}")
def replace_policy(
    policy_id: str, body: PolicyDocumentIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    policies: PolicyRepo = Depends(get_policy_repo),
):
    policy = _parse_policy(body.root)
    updated = _service(request, conn, policies).update(
        policy_id, policy,
        base_document_sha256=request.headers.get("if-match"),
    )
    return {"policy_resource": resource_payload(updated)}


@router.delete("/policies/{policy_id}")
def delete_policy(
    policy_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    policies: PolicyRepo = Depends(get_policy_repo),
):
    _service(request, conn, policies).delete(policy_id)
    return _policies_payload(request, policies)


@router.post("/policies/{policy_id}/enable")
def enable_policy(
    policy_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    policies: PolicyRepo = Depends(get_policy_repo),
):
    _service(request, conn, policies).enable(policy_id)
    return _policies_payload(request, policies)


@router.post("/policies/{policy_id}/disable")
def disable_policy(
    policy_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    policies: PolicyRepo = Depends(get_policy_repo),
):
    _service(request, conn, policies).disable(policy_id)
    return _policies_payload(request, policies)


# ---------------------------------------------------------------------------
# layout：前端私有 JSON，直接走 repo（§6.3）
# ---------------------------------------------------------------------------
@router.get("/policies/{policy_id}/layout")
def get_layout(policy_id: str, policies: PolicyRepo = Depends(get_policy_repo)):
    _require_policy(policies, policy_id)
    return policies.get_layout(policy_id)


@router.put("/policies/{policy_id}/layout")
def put_layout(
    policy_id: str, body: LayoutIn,
    policies: PolicyRepo = Depends(get_policy_repo),
):
    _require_policy(policies, policy_id)
    try:
        return policies.put_layout(policy_id, body.root)
    except ValueError as exc:
        raise ValidationFailed([Issue(
            "policy_parse_error", "$", params={"detail": str(exc)})]) from exc


def _require_policy(policies: PolicyRepo, policy_id: str) -> None:
    if policies.get(policy_id) is None:
        raise MissingError(f"unknown policy {policy_id}")


# ---------------------------------------------------------------------------
# POST /admin/try：dry-run
# ---------------------------------------------------------------------------
def _try_snapshot(conn: sqlite3.Connection, resource: PolicyResource):
    """草稿试算快照：与线上 snapshot 同一后端/JEV/窗口来源，绝不发布。"""
    input_tokens, context_tokens = read_jev_window(conn)
    return build_snapshot(
        resource=resource,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(row) for row in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: model_capability(row) for row in ModelRepo(conn).list()},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
    )


@router.post("/try")
async def try_policy(
    body: TryIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    policies: PolicyRepo = Depends(get_policy_repo),
):
    provided = [name for name in ("policy_id", "policy")
                if getattr(body, name) is not None]
    if len(provided) != 1:
        raise ValidationFailed([Issue(
            "policy_parse_error", "$",
            params={"detail": "try requires exactly one of policy_id or policy"})])

    if body.policy_id is not None:
        policy_id = body.policy_id
        if not isinstance(policy_id, str) or not policy_id:
            raise ValidationFailed([Issue(
                "policy_parse_error", "$",
                params={"detail": "policy_id must be a nonempty string"})])
        stored = policies.get(policy_id)
        if stored is None:
            raise MissingError(f"unknown policy {policy_id}")
        resource = stored
    else:
        policy = _parse_policy(body.policy)
        resource = PolicyResource(id="draft", policy=policy, enabled=False)

    text = body.text
    if not isinstance(text, str):
        raise ValidationFailed([Issue(
            "policy_parse_error", "$", params={"detail": "text must be a string"})])
    session_id = body.session_id if body.session_id is not None else ""
    if not isinstance(session_id, str) or len(session_id) > 256:
        raise ValidationFailed([Issue(
            "policy_parse_error", "$",
            params={"detail": "session_id must be a string of at most 256 characters"})])
    now = None
    if body.now is not None:
        try:
            now = datetime.fromisoformat(body.now)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed([Issue(
                "policy_parse_error", "$",
                params={"detail": "now must be an ISO datetime with offset"})]) from exc
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationFailed([Issue(
                "policy_parse_error", "$",
                params={"detail": "now must carry a UTC offset"})])

    # 结构校验（保存级）：不查模型目录——目录可用性由启用/线上路径把关。
    issues = validate_policy(resource.policy)
    if issues:
        raise ValidationFailed(issues)

    snapshot = _try_snapshot(conn, resource)
    trace: list[dict] = []
    try:
        decision = await request.app.state.routing_service.route(
            snapshot, RoutingRequest(text=text, session_id=session_id),
            now=now, dry_run=True, trace_sink=trace,
        )
    except (RoutingMiss, NoAvailableBackend, NoActivePolicyError, KeyError) as exc:
        # 调度/求值失败不是文档问题：与数据面同型映射 503（含原因文本）。
        # KeyError 覆盖 ZoneInfoNotFoundError：求值期时区意外绝不冒泡成 500。
        raise HTTPException(503, detail=str(exc)) from exc
    try:
        return {
            "target": decision.target_id,
            "models": [item.model for item in decision.target.models],
            "matched_rule": decision.matched_rule,
            "answers": {
                key: answer_to_dict(value) for key, value in decision.answers.items()
            },
            "trace": decision.trace,
            "fallback": decision.fallback_reason is not None,
            "reason": decision.fallback_reason,
        }
    finally:
        decision.release()
