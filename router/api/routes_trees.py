"""决策组管理面：graph CRUD/enable/copy/disable 与完整 envelope 草稿试算。

写用例全部在 `router.control.graphs.GraphService`：API 只做 DTO 解析
与异常映射（ValidationFailed/GraphConflictError/MissingError 由 app 级处理器
映射）。所有校验失败统一返回 `Issue` 数组（spec §5）。
`POST /admin/try` 按 spec §4.3 的 root 优先 envelope 做 dry-run：不落库、不发布
snapshot、不写粘性、不计 inflight。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.deps import get_conn, get_tree_repo
from router.api.trace_presenter import present_trace
from router.control.graphs import GraphPatch, GraphService, graph_parse_failed
from router.api.issue_presenter import issues_payload
from router.control.runtime import effective_runtime, runtime_for_tree, snapshot_parts
from router.control.validation import tree_issues
from router.jev.serialization import answer_to_dict
from router.routing.graph.compiled import compile_graph
from router.routing.graph.model import canvas_to_dict, graph_to_dict
from router.routing.graph.validate import validate_graph_structure
from router.routing.runtime import RUNTIME_FIELDS, runtime_to_dict
from router.routing.tree import RoutingTree, Sticky, tree_from_dict, tree_to_dict
from router.routing.snapshot import build_snapshot
from router.routing.types import NoAvailableBackend, RoutingMiss, RoutingRequest
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
    return getattr(request.app.state.snapshot, "snapshot_revision", "")


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

    ``runtime`` 不是文件字段，而是写用例的控制字段：只允许显式 null，表示
    「丢弃当前图内嵌配置，再由服务端按活动图/数据库物化」。非 null 值或与四个
    扁平 runtime 字段同时出现都拒绝，避免出现两套来源互相覆盖。"""

    model_config = ConfigDict(extra="forbid")

    id: Any = None
    name: Any = None
    root: Any = None
    canvas: Any = None
    sticky: Any = None
    timezone: Any = None
    backends: Any = None
    jev: Any = None
    jev_window: Any = None
    time_periods: Any = None
    runtime: Any = None
    base_document_sha256: Any = None


class TreeCopyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Any = None
    name: Any = None


class TryIn(BaseModel):
    """POST /admin/try 的 DTO：未知字段一律拒绝。

    timezone/sticky 的显式 null 按 base dict 语义处理（timezone null 交给图解析
    → 422，sticky null 重置为默认粘性配置）；字段缺失才继承已存值。"""

    model_config = ConfigDict(extra="forbid")

    tree_id: Any = None
    root: Any = None
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


def _validate_runtime_control(body: TreeUpsertIn) -> None:
    """`runtime` 只能是显式 null，且不能与四个扁平 runtime 字段同时出现。"""
    if "runtime" not in body.model_fields_set:
        return
    if body.runtime is not None:
        raise HTTPException(422, detail="runtime must be null; use the four runtime fields")
    if body.model_fields_set & RUNTIME_FIELDS:
        raise HTTPException(422, detail="runtime cannot accompany runtime fields")


@router.post("/trees", status_code=201)
def create_tree(
    body: TreeUpsertIn, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    payload = {
        "id": _clean_id(body.id), "name": _clean_name(body.name),
        "enabled": False,
        "root": body.root, "canvas": body.canvas, "sticky": body.sticky,
    }
    if "timezone" in body.model_fields_set:
        payload["timezone"] = body.timezone
    runtime_fields = body.model_fields_set & RUNTIME_FIELDS
    _validate_runtime_control(body)
    if runtime_fields:
        payload.update({
            field: getattr(body, field)
            for field in runtime_fields
        })
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
    for field in ("root", "canvas", "sticky", "timezone"):
        if field in fields:
            changes[field] = getattr(body, field)
    runtime_fields = fields & RUNTIME_FIELDS
    _validate_runtime_control(body)
    if "runtime" in fields:
        changes["runtime"] = None
    elif runtime_fields:
        changes["runtime"] = {
            field: getattr(body, field)
            for field in runtime_fields
        }
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


def draft_tree(body: TryIn, stored: RoutingTree | None) -> RoutingTree:
    """按 spec §4.3 合并 envelope：root 优先；显式字段覆盖，缺失字段继承已存值。

    显式 null 与缺失不同（base dict 语义）：timezone null 原样交给图解析 → 422，
    sticky null 重置为默认粘性配置。
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
        "enabled": False,
        "root": root if root is not None else graph_to_dict(stored.root),
        "canvas": canvas,
        "timezone": body.timezone if "timezone" in fields else timezone_default,
        "sticky": body.sticky if "sticky" in fields else {
            "enabled": sticky_default.enabled, "ttl_seconds": sticky_default.ttl_seconds,
        },
    }
    runtime_fields = fields & RUNTIME_FIELDS
    if runtime_fields:
        payload.update({field: getattr(body, field) for field in runtime_fields})
    elif stored is not None and stored.runtime is not None:
        payload.update(runtime_to_dict(stored.runtime))
    try:
        return tree_from_dict(payload)
    except ValueError as exc:
        raise HTTPException(422, detail=issues_payload(graph_parse_failed(exc))) from exc


def _try_snapshot(conn: sqlite3.Connection, tree: RoutingTree):
    """草稿快照：与线上 snapshot 使用同一运行配置，但不替换 app.state.snapshot。"""
    runtime = runtime_for_tree(conn, tree)
    return build_snapshot(
        tree=tree,
        **snapshot_parts(conn, runtime),
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
    tree_id = body.tree_id
    stored = None
    if tree_id is not None:
        if not isinstance(tree_id, str) or not tree_id:
            raise HTTPException(422, detail="try tree_id must be a nonempty string")
        stored = _tree_or_404(trees, tree_id)
    tree = draft_tree(body, stored)
    if tree.runtime is None:
        tree = replace(tree, runtime=effective_runtime(conn, trees))
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
    # 结构校验是编译静态拓扑的前提；启用级问题只作为 warnings 返回，不阻塞试算（spec §4.3）。
    issues = validate_graph_structure(tree)
    if issues:
        raise HTTPException(422, detail=issues_payload(issues))
    # 启用级问题只作为 warnings 返回，不阻塞试算（spec §4.3）。
    warnings = issues_payload(tree_issues(conn, tree))
    snapshot = _try_snapshot(conn, tree)
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
            "tree_id": tree.id,
            "tree_revision": snapshot.tree_revision,
            "snapshot_revision": snapshot.snapshot_revision,
            "warnings": warnings,
            "draft": body.root is not None,
        }
    finally:
        decision.release()
