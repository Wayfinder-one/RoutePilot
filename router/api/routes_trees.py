"""决策组管理面：graph CRUD、启停/默认图、部门设置与完整 envelope 草稿试算。

写用例全部在 `router.control.graphs.GraphService`：API 只做 DTO 解析
与异常映射（ValidationFailed/GraphConflictError/MissingError/
DefaultGraphRequired 由 app 级处理器映射）。所有校验失败统一返回 `Issue` 数组
（spec §5）；默认图停用限制返回结构化 409。
`POST /admin/try` 有两种路径：带 `tree_id`/`root` 时按 spec §4.3 的 root 优先
envelope 做 dry-run 草稿试算；两者都不带时按已发布路由集选图（部门精确命中 →
部门图，未知/空部门 → 默认图）。两条路径都 dry-run：不落库、不发布
snapshot、不写粘性、不计 inflight。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.api.trace_presenter import present_trace
from router.control.graphs import GraphPatch, GraphService, graph_parse_failed
from router.api.issue_presenter import issues_payload
from router.control.consistency import runtime_mismatch_issues
from router.control.runtime import snapshot_parts
from router.control.validation import tree_issues
from router.jev.serialization import answer_to_dict
from router.routing.graph.compiled import compile_graph
from router.routing.graph.model import canvas_to_dict, graph_to_dict
from router.routing.graph.validate import validate_graph_structure
from router.routing.runtime import RUNTIME_FIELDS, runtime_to_dict
from router.routing.tree import RoutingTree, Sticky, tree_from_dict, tree_to_dict
from router.routing.snapshot import build_snapshot
from router.routing.types import (
    NoActiveTreeError,
    NoAvailableBackend,
    NoDefaultTreeError,
    RoutingMiss,
    RoutingRequest,
)
from router.store.repositories import new_id
from router.store.routing_trees import RoutingTreeRepo

router = APIRouter()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _service(request: Request, conn: sqlite3.Connection, trees: RoutingTreeRepo) -> GraphService:
    return GraphService(conn, trees, request.app.state.publisher)


def _clean_id(raw) -> str:
    value = new_id() if raw is None else raw
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise HTTPException(422, detail="invalid tree id")
    return value


def _clean_name(raw) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(422, detail="tree name must be a string")
    return raw.strip()


def _tree_or_404(trees: RoutingTreeRepo, tree_id: str) -> RoutingTree:
    tree = trees.get(tree_id)
    if tree is None:
        raise HTTPException(404, detail=f"unknown tree {tree_id}")
    return tree


def _published_revision(request: Request) -> str:
    return getattr(request.app.state.snapshot_set, "snapshot_revision", "")


def _trees_payload(request: Request, trees: RoutingTreeRepo) -> dict:
    return {
        "trees": [tree_to_dict(tree) for tree in trees.list()],
        "snapshot_revision": _published_revision(request),
        "graph_status": request.app.state.publisher.status(),
    }


class TreeUpsertIn(BaseModel):
    """POST/PUT /admin/trees 的 DTO：未知字段一律拒绝；id/name/root/sticky/
    timezone 的类型校验保留原文案（_clean_id/_clean_name/图解析）。

    PUT 时字段缺失 = 继承已存值；PUT 的显式 null 按重构前的 dict 语义处理（name
    清空、root/timezone 解析失败 422、sticky 重置为默认、id → 422 tree id
    mismatch）；POST 的 null 语义同样维持 base（如 id=null 生成新 id）。路由用
    ``model_fields_set`` 区分字段缺失与显式 null。

    四个扁平运行字段``backends`` / ``jev`` / ``jev_window`` / ``time_periods``
    是画布快照的一部分，必须成组出现；不存在运行配置控制字段，也不存在服务端
    按数据库补齐运行配置的兼容路径。"""

    model_config = ConfigDict(extra="forbid")

    id: Any = None
    name: Any = None
    root: Any = None
    canvas: Any = None
    sticky: Any = None
    timezone: Any = None
    departments: Any = None
    backends: Any = None
    jev: Any = None
    jev_window: Any = None
    time_periods: Any = None
    base_document_sha256: Any = None


class TreeCopyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Any = None
    name: Any = None


class DepartmentUpdateIn(BaseModel):
    """PUT /admin/trees/{tree_id}/departments 的 DTO：未知字段一律拒绝。

    departments 的类型/长度/重复校验与普通保存共用 ``tree_from_dict``
    （非法值 → 422 ``graph_parse_error``）；``base_document_sha256`` 与普通保存
    同语义（过期/非法 → 409 ``graph_conflict``）。显式省略 departments 清空该图
    的部门列表。
    """

    model_config = ConfigDict(extra="forbid")

    departments: Any = None
    base_document_sha256: Any = None


class TryIn(BaseModel):
    """POST /admin/try 的 DTO：未知字段一律拒绝。

    ``department`` 只在无 ``tree_id``/``root`` 的实际路由集路径生效（可信部门，
    由管理端页面填写）；带草稿 envelope 时忽略。timezone/sticky 的显式 null 按
    base dict 语义处理（timezone null 交给图解析 → 422，sticky null 重置为默认
    粘性配置）；字段缺失才继承已存值。"""

    model_config = ConfigDict(extra="forbid")

    tree_id: Any = None
    root: Any = None
    department: Any = None
    canvas: Any = None
    timezone: Any = None
    sticky: Any = None
    backends: Any = None
    jev: Any = None
    jev_window: Any = None
    time_periods: Any = None
    text: Any = None
    session_id: Any = None
    now: Any = None


@router.get("/trees")
def get_trees(request: Request, trees: RoutingTreeRepo = Depends(get_tree_repo)):
    return _trees_payload(request, trees)


def _runtime_payload(body: "TreeUpsertIn | TryIn", fields: set[str]) -> dict:
    """从 DTO 里挑出显式给出的四个扁平运行字段（缺失字段不参与合并）。"""
    return {
        field: getattr(body, field)
        for field in RUNTIME_FIELDS
        if field in fields
    }


@router.post("/trees", status_code=201)
def create_tree(
    body: TreeUpsertIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    payload = {
        "id": _clean_id(body.id), "name": _clean_name(body.name),
        # 生命周期是服务端状态：新建图一律关闭、非默认，客户端不能写入。
        "enabled": False, "default": False,
        "root": body.root, "canvas": body.canvas, "sticky": body.sticky,
    }
    if "timezone" in body.model_fields_set:
        payload["timezone"] = body.timezone
    if "departments" in body.model_fields_set:
        payload["departments"] = body.departments
    payload.update(_runtime_payload(body, body.model_fields_set))
    tree = _parse_tree(payload)
    created = _service(request, conn, trees).create(tree)
    return {"tree": tree_to_dict(created), "snapshot_revision": _published_revision(request)}


@router.get("/trees/{tree_id}")
def get_tree(tree_id: str, trees: RoutingTreeRepo = Depends(get_tree_repo)):
    return {"tree": tree_to_dict(_tree_or_404(trees, tree_id))}


def _parse_tree(raw: dict) -> RoutingTree:
    try:
        return tree_from_dict(raw)
    except ValueError as exc:
        raise HTTPException(422, detail=issues_payload(graph_parse_failed(exc))) from exc


@router.put("/trees/{tree_id}")
def update_tree(
    tree_id: str, body: TreeUpsertIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    fields = body.model_fields_set
    # base 用 `"id" in body` 判断：显式 null 同样触发 mismatch（→ 422）。
    if "id" in fields and body.id != tree_id:
        raise HTTPException(422, detail="tree id mismatch")
    changes: dict[str, Any] = {}
    if "name" in fields:
        changes["name"] = _clean_name(body.name)  # 显式 null → ""（清空名称）
    for field in ("root", "canvas", "sticky", "timezone", "departments"):
        if field in fields:
            changes[field] = getattr(body, field)
    changes.update(_runtime_payload(body, fields))
    updated = _service(request, conn, trees).update(
        tree_id, GraphPatch(**changes), base_document_sha256=body.base_document_sha256)
    return {
        "tree": tree_to_dict(updated),
        "snapshot_revision": _published_revision(request),
        "graph_status": request.app.state.publisher.status(),
        "warnings": [],
    }


@router.post("/trees/{tree_id}/copy", status_code=201)
def copy_tree(
    tree_id: str, body: TreeCopyIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    copied = _service(request, conn, trees).copy(
        tree_id, _clean_id(body.id), _clean_name(body.name))
    return {"tree": tree_to_dict(copied), "snapshot_revision": _published_revision(request)}


@router.post("/trees/{tree_id}/enable")
def enable_tree(
    tree_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    _service(request, conn, trees).enable(tree_id)
    return _trees_payload(request, trees)


@router.post("/trees/{tree_id}/disable")
def disable_tree(
    tree_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    _service(request, conn, trees).disable(tree_id)
    return _trees_payload(request, trees)


@router.put("/trees/{tree_id}/departments")
def update_departments(
    tree_id: str, body: DepartmentUpdateIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    """部门设置：与普通保存共用校验/写入/发布/审计路径。

    返回更新后的 tree、路由集 snapshot_revision、发布器 graph_status 与
    warnings（部门设置不产生警告，保留字段与普通保存一致）。
    """
    updated = _service(request, conn, trees).set_departments(
        tree_id, body.departments, base_document_sha256=body.base_document_sha256)
    return {
        "tree": tree_to_dict(updated),
        "snapshot_revision": _published_revision(request),
        "graph_status": request.app.state.publisher.status(),
        "warnings": [],
    }


@router.post("/trees/{tree_id}/default")
def set_default(
    tree_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    """默认图事务：目标设默认并启用，旧默认图保持启用但改为非默认。"""
    _service(request, conn, trees).set_default(tree_id)
    return _trees_payload(request, trees)


def draft_tree(body: TryIn, stored: RoutingTree | None) -> RoutingTree:
    """按 spec §4.3 合并 envelope：root 优先；显式字段覆盖，缺失字段继承已存值。

    显式 null 与缺失不同（base dict 语义）：timezone null 原样交给图解析 → 422，
    sticky null 重置为默认粘性配置。

    运行配置只有两个来源：草稿显式携带的完整四字段，或已存画布的快照。缺任何一个
    运行字段都拒绝（部分字段绝不从数据库补齐）；既没有 tree_id 也没有完整运行
    字段的 root-only 草稿同样拒绝。
    """
    root = body.root
    if root is None and stored is None:
        raise HTTPException(422, detail="try needs root or tree_id")
    fields = body.model_fields_set
    sticky_default = stored.sticky if stored else Sticky()
    timezone_default = stored.timezone if stored else "Asia/Shanghai"
    if "canvas" in fields:
        canvas = body.canvas
    elif root is None:
        canvas = canvas_to_dict(stored.root)
    else:
        canvas = None
    payload = {
        "id": stored.id if stored else "draft",
        "name": stored.name if stored else "",
        "enabled": False, "default": False,
        # TryIn 没有 departments 字段且 extra="forbid"：草稿部门只继承已存图。
        "departments": list(stored.departments) if stored else [],
        "root": root if root is not None else graph_to_dict(stored.root),
        "canvas": canvas,
        "timezone": body.timezone if "timezone" in fields else timezone_default,
        "sticky": body.sticky if "sticky" in fields else {
            "enabled": sticky_default.enabled, "ttl_seconds": sticky_default.ttl_seconds,
        },
    }
    runtime_fields = fields & RUNTIME_FIELDS
    if runtime_fields:
        payload.update({
            field: getattr(body, field) for field in runtime_fields
        })
    elif stored is not None:
        payload.update(runtime_to_dict(stored.runtime))
    try:
        return tree_from_dict(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=issues_payload(graph_parse_failed(exc))) from exc


def _try_snapshot(conn: sqlite3.Connection, tree: RoutingTree):
    """草稿快照：运行配置完全来自草稿 envelope / 已存画布，不读数据库目录。"""
    return build_snapshot(
        tree=tree,
        **snapshot_parts(conn, tree.runtime),
        compiled=compile_graph(tree.root),
    )


def _candidate_payload(candidate) -> dict:
    return {
        "target_id": candidate.target_id,
        "priority": candidate.priority,
        "via_node": candidate.via_node,
        "via_port": candidate.via_port,
        "wave": candidate.wave,
    }


def _wave_payload(wave) -> dict:
    return {
        "wave": wave.wave,
        "questions": list(wave.questions),
        "duration_ms": wave.duration_ms,
        "failed": wave.failed,
        "error": wave.error,
    }


@router.post("/try")
async def try_tree(
    body: TryIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    """试算：无 ``tree_id``/``root`` 时按部门从已发布路由集选图，否则合并草稿 envelope。

    路由集路径只读已启用图：部门精确命中 → 部门图，未知/空部门 → 默认图；
    零启用图 503 ``no_active_tree``，启用集合缺默认图 503 ``no_default_tree``，
    路由集未就绪 503 ``no_valid_snapshot``。响应用 ``graph_selection`` 报告
    ``department``/``default``；草稿路径该字段为 null。
    """
    tree_id = body.tree_id
    stored = None
    selection = None
    # 两种互斥路径：只有既没有 tree_id 也没有草稿 root 时才使用实际路由集。
    if tree_id is None and body.root is None:
        department = body.department if body.department is not None else ""
        if not isinstance(department, str):
            raise HTTPException(422, detail="try department must be a string")
        department = department.strip()
        if len(department) > 64 or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in department):
            raise HTTPException(422, detail="invalid department")
        route_set = request.app.state.snapshot_set
        if route_set is None:
            raise HTTPException(503, detail="no_valid_snapshot")
        try:
            selection = route_set.select(department)
        except NoActiveTreeError as exc:
            raise HTTPException(503, detail="no_active_tree") from exc
        except NoDefaultTreeError as exc:
            raise HTTPException(503, detail="no_default_tree") from exc
        # 选中快照已过启用校验并编译，直接复用，不再做草稿结构校验/快照装配。
        snapshot = selection.snapshot
        warnings: list[dict] = []
    else:
        if tree_id is not None:
            if not isinstance(tree_id, str) or not tree_id:
                raise HTTPException(422, detail="try tree_id must be a nonempty string")
            stored = _tree_or_404(trees, tree_id)
        tree = draft_tree(body, stored)
        # 结构校验是编译静态拓扑的前提；启用级问题只作为 warnings 返回，不阻塞试算（spec §4.3）。
        issues = validate_graph_structure(tree)
        if issues:
            raise HTTPException(422, detail=issues_payload(issues))
        # 启用级问题只作为 warnings 返回，不阻塞试算（spec §4.3）。
        # 画布快照与数据库目录的差异也是提示：只读比较、不改变执行配置（spec 保存/启用/检查）。
        warnings = issues_payload(
            tree_issues(conn, tree) + runtime_mismatch_issues(conn, tree.runtime))
        snapshot = _try_snapshot(conn, tree)
    text = body.text
    if not isinstance(text, str):
        raise HTTPException(422, detail="try text must be a string")
    session_id = body.session_id if "session_id" in body.model_fields_set else ""
    if not isinstance(session_id, str) or len(session_id) > 256:
        raise HTTPException(422, detail="invalid session_id")
    now = None
    if body.now is not None:
        try:
            now = datetime.fromisoformat(body.now)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, detail="now must be an ISO datetime") from exc
    trace: list[dict] = []
    try:
        decision = await request.app.state.routing_service.route(
            snapshot, RoutingRequest(text=text, session_id=session_id),
            now=now, dry_run=True, trace_sink=trace,
        )
    except (RoutingMiss, NoAvailableBackend) as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    trace[:] = present_trace(trace)
    try:
        return {
            "outcome": {
                "type": "hit",
                "model": decision.model,
                "target_id": decision.target_id,
                "reasoning": decision.reasoning,
                "selection_reason": decision.selection_reason,
                "fallback_reason": decision.fallback_reason,
                "sticky_hit": decision.sticky_hit,
                "policy_path": decision.policy_path,
                "candidates": [_candidate_payload(candidate) for candidate in decision.candidates],
                "waves": [_wave_payload(wave) for wave in decision.waves],
            },
            "answers": {node_id: answer_to_dict(answer) for node_id, answer in decision.answers.items()},
            "trace": decision.trace,
            "tree_id": snapshot.tree_id,
            "tree_revision": snapshot.tree_revision,
            "snapshot_revision": snapshot.snapshot_revision,
            "warnings": warnings,
            "draft": body.root is not None,
            "graph_selection": selection.reason if selection is not None else None,
        }
    finally:
        decision.release()
