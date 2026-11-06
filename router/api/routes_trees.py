"""决策组管理面：graph CRUD/enable/copy/disable 与完整 envelope 草稿试算。

保存草稿只做结构校验；启用（以及保存已启用组）追加启用校验（模型/时段/决策服务）。
所有校验失败统一返回 `Issue` 数组（spec §5），不再返回字符串 detail。
`POST /admin/try` 按 spec §4.3 的 root 优先 envelope 做 dry-run：不落库、不发布
snapshot、不写粘性、不计 inflight。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import replace
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn, get_tree_repo
from router.api.upstream import load_backends
from router.jev.client import ChoiceAnswer, NoulAnswer, ScoreAnswer
from router.jev.config import JevConfigRepo
from router.routing.conditions import TimePeriod
from router.routing.document import graph_document_bytes
from router.routing.graph.validate import (
    Issue,
    validate_graph_for_enable,
    validate_graph_structure,
)
from router.routing.model import RoutingTree, Sticky, graph_to_dict, tree_from_dict, tree_to_dict
from router.routing.snapshot import (
    _capability,
    build_enable_context,
    build_snapshot,
    read_jev_window,
)
from router.routing.types import NoAvailableBackend, RoutingMiss, RoutingRequest
from router.store.repositories import ChangeRepo, ModelRepo, TimePeriodRepo, new_id, now_iso
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphConflictError,
    RoutingTreeRepo,
)

logger = logging.getLogger(__name__)

router = APIRouter()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _raise_issues(issues) -> None:
    raise HTTPException(422, detail=[dict(issue.__dict__) for issue in issues])


def _recursion_issue() -> Issue:
    return Issue(
        "limit_exceeded",
        "图结构过深，校验无法在安全深度内完成（节点/边可能超过上限）",
        "Graph is too deep to validate safely (node/edge limits may be exceeded)",
    )


def _safe_validate(validate, *args):
    """校验调用兜底：RecursionError 也必须映射为 Issue 数组 422，绝不冒泡成 500。"""
    try:
        return validate(*args)
    except RecursionError:
        _raise_issues([_recursion_issue()])


def _parse_issue(exc: Exception) -> Issue:
    return Issue(
        "graph_parse_error",
        f"图解析失败：root 不符合 sr-routing-graph v3 结构（{exc}）",
        f"Graph parse failed: root does not conform to the sr-routing-graph v3 structure ({exc})",
    )


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


def _parse_tree(raw: dict) -> RoutingTree:
    try:
        return tree_from_dict(raw)
    except (ValueError, RecursionError) as exc:
        _raise_issues([_parse_issue(exc)])


def _tree_or_404(trees: RoutingTreeRepo, tree_id: str) -> RoutingTree:
    tree = trees.get(tree_id)
    if tree is None:
        raise HTTPException(404, detail=f"unknown tree {tree_id}")
    return tree


def _record_graph_change(conn: sqlite3.Connection, action: str, detail: dict) -> None:
    try:
        with conn:
            ChangeRepo(conn).record(now_iso(), "tree", {"action": action, **detail})
    except sqlite3.Error:
        logger.exception("graph change committed but audit write failed: %s", action)


def _published_revision(request: Request) -> str:
    return getattr(request.app.state.snapshot, "snapshot_revision", "")


def _trees_payload(request: Request, trees: RoutingTreeRepo) -> dict:
    return {
        "trees": [tree_to_dict(tree) for tree in trees.list()],
        "snapshot_revision": _published_revision(request),
        "graph_status": request.app.state.publisher.status(),
    }


@router.get("/trees")
def get_trees(request: Request, trees: RoutingTreeRepo = Depends(get_tree_repo)):
    return _trees_payload(request, trees)


@router.post("/trees", status_code=201)
def create_tree(
    body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    if "enabled" in body:
        raise HTTPException(422, detail="use enable/disable operations")
    tree = _parse_tree({
        "id": _clean_id(body.get("id")), "name": _clean_name(body.get("name")),
        "enabled": False,
        "root": body.get("root"), "sticky": body.get("sticky"),
        "timezone": body.get("timezone", "Asia/Shanghai"),
    })
    issues = _safe_validate(validate_graph_structure, tree)
    if issues:
        _raise_issues(issues)
    publisher = request.app.state.publisher
    with publisher.locked():
        try:
            created = trees.create(tree)
        except GraphAlreadyExistsError:
            raise HTTPException(409, detail=f"tree {tree.id} already exists") from None
        _record_graph_change(conn, "create", {"id": tree.id})
    return {"tree": tree_to_dict(created), "snapshot_revision": _published_revision(request)}


@router.get("/trees/{tree_id}")
def get_tree(tree_id: str, trees: RoutingTreeRepo = Depends(get_tree_repo)):
    return {"tree": tree_to_dict(_tree_or_404(trees, tree_id))}


@router.put("/trees/{tree_id}")
def update_tree(
    tree_id: str, body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    if "enabled" in body:
        raise HTTPException(422, detail="use enable/disable operations")
    if "id" in body and body["id"] != tree_id:
        raise HTTPException(422, detail="tree id mismatch")
    publisher = request.app.state.publisher
    with publisher.locked():
        current = _tree_or_404(trees, tree_id)
        # enabled 是外部状态：持久化文档必须与 current 一致（enable 只能走专用端点）。
        tree = _parse_tree({
            "id": tree_id,
            "name": _clean_name(body.get("name", current.name)),
            "enabled": current.enabled,
            "root": body.get("root", graph_to_dict(current.root)),
            "sticky": body.get("sticky", {"enabled": current.sticky.enabled, "ttl_seconds": current.sticky.ttl_seconds}),
            "timezone": body.get("timezone", current.timezone),
        })
        base_document_sha256 = body.get("base_document_sha256")
        if not isinstance(base_document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", base_document_sha256):
            raise HTTPException(409, detail={
                "code": "graph_conflict",
                "actual_document_sha256": current.document_sha256,
            })
        if current.enabled:
            issues = _safe_validate(validate_graph_for_enable, tree, build_enable_context(conn))
        else:
            issues = _safe_validate(validate_graph_structure, tree)
        if issues:
            _raise_issues(issues)

        candidate = None
        if current.enabled:
            fingerprint = (tree.id, hashlib.sha256(graph_document_bytes(tree)).hexdigest())
            candidate = publisher.prepare(conn, tree, fingerprint)
        try:
            updated = trees.update(tree, base_document_sha256=base_document_sha256)
        except GraphConflictError as exc:
            raise HTTPException(409, detail={
                "code": "graph_conflict",
                "actual_document_sha256": exc.actual_document_sha256,
            }) from None
        if candidate is not None:
            publisher.publish(candidate)
        _record_graph_change(conn, "update", {"id": tree_id, "tree_revision": updated.tree_revision})
        return {
            "tree": tree_to_dict(updated),
            "snapshot_revision": _published_revision(request),
            "graph_status": publisher.status(),
            "warnings": [],
        }


@router.post("/trees/{tree_id}/copy", status_code=201)
def copy_tree(
    tree_id: str, body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    new_id = _clean_id(body.get("id"))
    new_name = _clean_name(body.get("name"))
    publisher = request.app.state.publisher
    with publisher.locked():
        _tree_or_404(trees, tree_id)
        try:
            copied = trees.copy(tree_id, new_id, new_name)
        except KeyError:
            raise HTTPException(404, detail=f"unknown tree {tree_id}") from None
        except GraphAlreadyExistsError:
            raise HTTPException(409, detail=f"tree {new_id} already exists") from None
        _record_graph_change(conn, "copy", {"source": tree_id, "id": new_id})
    return {"tree": tree_to_dict(copied), "snapshot_revision": _published_revision(request)}


@router.post("/trees/{tree_id}/enable")
def enable_tree(
    tree_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    publisher = request.app.state.publisher
    with publisher.locked():
        tree = _tree_or_404(trees, tree_id)
        issues = _safe_validate(validate_graph_for_enable, tree, build_enable_context(conn))
        if issues:
            _raise_issues(issues)
        enabled_tree = replace(tree, enabled=True)
        fingerprint = (tree.id, tree.document_sha256)
        candidate = publisher.prepare(conn, enabled_tree, fingerprint)
        try:
            trees.enable(tree_id)
        except KeyError:
            raise HTTPException(404, detail=f"unknown tree {tree_id}") from None
        publisher.publish(candidate)
        _record_graph_change(conn, "enable", {"id": tree_id})
        return _trees_payload(request, trees)


@router.post("/trees/{tree_id}/disable")
def disable_tree(
    tree_id: str, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    publisher = request.app.state.publisher
    with publisher.locked():
        _tree_or_404(trees, tree_id)
        candidate = publisher.prepare(conn, None, (None, "")) if trees.active_id() == tree_id else None
        if not trees.disable(tree_id):
            raise HTTPException(404, detail=f"unknown tree {tree_id}")
        if candidate is not None:
            publisher.publish(candidate)
        _record_graph_change(conn, "disable", {"id": tree_id})
        return _trees_payload(request, trees)


def draft_tree(body: dict, stored: RoutingTree | None) -> RoutingTree:
    """按 spec §4.3 合并 envelope：root 优先；显式字段覆盖，缺失字段继承已存值。"""
    root = body.get("root")
    if root is None and stored is None:
        raise HTTPException(422, detail="try needs root or tree_id")
    sticky_default = stored.sticky if stored else Sticky()
    timezone_default = stored.timezone if stored else "Asia/Shanghai"
    try:
        return tree_from_dict({
            "id": stored.id if stored else "draft",
            "name": stored.name if stored else "",
            "enabled": False,
            "root": root if root is not None else graph_to_dict(stored.root),
            "timezone": body.get("timezone", timezone_default),
            "sticky": body.get("sticky", {
                "enabled": sticky_default.enabled, "ttl_seconds": sticky_default.ttl_seconds,
            }),
        })
    except (ValueError, RecursionError) as exc:
        _raise_issues([_parse_issue(exc)])


def _try_snapshot(conn: sqlite3.Connection, tree: RoutingTree):
    """草稿快照：与线上 snapshot 使用同一 JEV 窗口配置，但绝不替换 app.state.snapshot。"""
    models = ModelRepo(conn).list()
    input_tokens, context_tokens = read_jev_window(conn)
    return build_snapshot(
        tree=tree,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(period) for period in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: _capability(row) for row in models},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
    )


def _answer_payload(answer) -> dict:
    if isinstance(answer, ChoiceAnswer):
        return {
            "probabilities": dict(answer.probabilities),
            "ranking": list(answer.ranking),
            "p_top": answer.p_top,
            "margin": answer.margin,
            "usage": answer.usage,
            "duration_ms": answer.duration_ms,
        }
    if isinstance(answer, NoulAnswer):
        return {
            "yes": answer.yes,
            "no": answer.no,
            "usage": answer.usage,
            "duration_ms": answer.duration_ms,
        }
    if isinstance(answer, ScoreAnswer):
        return {
            "value": answer.value,
            "usage": answer.usage,
            "duration_ms": answer.duration_ms,
        }
    raise TypeError(f"unsupported answer type {type(answer).__name__}")


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
    body: dict, request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    trees: RoutingTreeRepo = Depends(get_tree_repo),
):
    tree_id = body.get("tree_id")
    stored = None
    if tree_id is not None:
        if not isinstance(tree_id, str) or not tree_id:
            raise HTTPException(422, detail="try tree_id must be a nonempty string")
        stored = _tree_or_404(trees, tree_id)
    tree = draft_tree(body, stored)
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(422, detail="try text must be a string")
    session_id = body.get("session_id", "")
    if not isinstance(session_id, str) or len(session_id) > 256:
        raise HTTPException(422, detail="invalid session_id")
    now = None
    if body.get("now") is not None:
        try:
            now = datetime.fromisoformat(body["now"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, detail="now must be an ISO datetime") from exc
    issues = _safe_validate(validate_graph_structure, tree)
    if issues:
        _raise_issues(issues)
    # 启用级问题只作为 warnings 返回，不阻塞试算（spec §4.3）。
    warnings = [dict(issue.__dict__) for issue in _safe_validate(validate_graph_for_enable, tree, build_enable_context(conn))]
    snapshot = _try_snapshot(conn, tree)
    try:
        decision = await request.app.state.engine.route(
            snapshot, RoutingRequest(text=text, session_id=session_id), now=now, dry_run=True
        )
    except (RoutingMiss, NoAvailableBackend) as exc:
        raise HTTPException(503, detail=str(exc)) from exc
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
            "answers": {node_id: _answer_payload(answer) for node_id, answer in decision.answers.items()},
            "trace": decision.trace,
            "tree_id": tree.id,
            "tree_revision": snapshot.tree_revision,
            "snapshot_revision": snapshot.snapshot_revision,
            "warnings": warnings,
            "draft": body.get("root") is not None,
        }
    finally:
        decision.release()
