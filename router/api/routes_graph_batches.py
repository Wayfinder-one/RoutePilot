"""Kernel-side atomic multi-graph batch: ``POST /internal/v1/graph-batches``.

The peripheral owns SQLite (catalogs) and the kernel owns the graph JSON; a
catalog write can touch several graph documents at once.  This endpoint gives
the peripheral one conditional batch so the two stores cannot end up half
updated by the batch itself:

1. under the publisher lock re-read every current document and verify the full
   base ``document_sha256`` set — any mismatch rejects the **whole** batch with
   409 ``graph_conflict`` before a single file is staged;
2. strictly validate every candidate (``candidate_document_issues``: enable
   level issues + the exact bytes the repository is about to write) — any
   invalid candidate is 422 with the usual Issue array and nothing is written;
3. validate the resulting enabled route set (enable validation + route-set
   invariants such as exactly one default graph) before committing;
4. commit one ``GraphBatch`` and publish the rebuilt enabled snapshot set.

Any exception after staging restores the original batch and re-raises the
original status, so a failure never publishes (or leaves) half a batch.  The
success answer is ``{"trees": [tree_to_dict(committed document), ...]}`` in
request order; each document carries its new ``document_sha256`` so the
peripheral can compensate with the new base hashes when its SQLite commit
fails afterwards.

``writes: []`` is an explicit no-op success: the peripheral never sends an
empty batch (pure SQLite catalog writes skip it), so treating it as a no-op
keeps the wire contract total instead of inventing an error case.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.api.issue_presenter import issues_payload
from router.control.errors import ValidationFailed
from router.control.graphs import candidate_document_issues, graph_parse_failed
from router.routing.graph.validate import Issue
from router.routing.tree import RoutingTree, tree_from_dict, tree_to_dict
from router.store.routing_trees import (
    GraphBatch,
    GraphConflictError,
    GraphStorageError,
    RoutingTreeRepo,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: 单条批次写只允许这两个字段（未知字段=客户端 bug，立即 422）。
_WRITE_FIELDS = {"tree", "base_document_sha256"}


class GraphBatchIn(BaseModel):
    """``{"writes": [{"tree": <tree_to_dict>, "base_document_sha256": <sha>}]}``。"""

    model_config = ConfigDict(extra="forbid")

    writes: Any = None


def _reject(message: str) -> None:
    """请求结构非法 → 422 Issue 数组（与图校验共用同一载荷形状）。"""
    raise HTTPException(
        422,
        detail=issues_payload(
            [Issue("graph_parse_error", params={"detail": message})]),
    )


def _parse_writes(raw) -> list[tuple[RoutingTree, str]]:
    """解析并做请求级校验；返回 ``(候选画布, 基线哈希)`` 列表（保持请求顺序）。"""
    if not isinstance(raw, list):
        _reject("graph batch writes must be a list")
    writes: list[tuple[RoutingTree, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            _reject(f"writes[{index}] must be an object")
        unknown = sorted(set(item) - _WRITE_FIELDS)
        if unknown:
            _reject(f"writes[{index}] has unknown fields: {', '.join(unknown)}")
        base = item.get("base_document_sha256")
        if not isinstance(base, str) or not base:
            _reject(f"writes[{index}].base_document_sha256 must be a nonempty string")
        document = item.get("tree")
        if not isinstance(document, dict):
            _reject(f"writes[{index}].tree must be an object")
        try:
            candidate = tree_from_dict(document)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(graph_parse_failed(exc)) from exc
        if candidate.id in seen:
            _reject(f"duplicate tree in graph batch: {candidate.id}")
        seen.add(candidate.id)
        writes.append((candidate, base))
    return writes


def _verified_updates(repo: RoutingTreeRepo,
                      writes: list[tuple[RoutingTree, str]]) -> list:
    """基线门：任一哈希过期 → 整批 409，且尚未暂存/改写任何文件。"""
    updates = []
    for candidate, base in writes:
        current = repo.get(candidate.id)
        actual = current.document_sha256 if current is not None else None
        if actual != base:
            raise GraphConflictError(actual)
        updates.append((candidate, base))
    return updates


def _resulting_enabled(repo: RoutingTreeRepo,
                       candidates: list[RoutingTree]) -> list[RoutingTree]:
    """候选写入后的完整启用集合（排序与 ``enabled()`` 一致：按 id）。"""
    by_id = {candidate.id: candidate for candidate in candidates}
    resulting = [by_id.get(tree.id, tree) for tree in repo.list()]
    return sorted((tree for tree in resulting if tree.enabled),
                  key=lambda tree: tree.id)


def _restore_batch(batch: GraphBatch | None, publisher) -> None:
    """提交失败后恢复本批次已替换的文件；回滚失败时刷新快照并保持 503 语义。"""
    if batch is None:
        return
    try:
        batch.rollback()
    except GraphStorageError:
        logger.exception("kernel graph batch rollback failed")
        try:
            publisher.refresh()
        except Exception:  # noqa: BLE001 - 回滚失败后的尽力刷新
            logger.exception("kernel snapshot refresh after rollback failure failed")
        raise


@router.post("/graph-batches")
def replace_graph_batch(body: GraphBatchIn, request: Request):
    writes = _parse_writes(body.writes)
    if not writes:
        return {"trees": []}

    repo: RoutingTreeRepo = request.app.state.graph_repo
    publisher = request.app.state.publisher
    batch: GraphBatch | None = None
    # 一个发布锁 + 一个 GraphBatch：基线复核、候选校验、路由集校验、提交与发布
    # 处于同一临界区，内核是唯一图写者（全局约束 2）。
    with publisher.locked():
        try:
            updates = _verified_updates(repo, writes)
            # 严格候选校验：任一份非法 → 422，且文件一个都没动。
            for candidate, _base in updates:
                issues = candidate_document_issues(candidate)
                if issues:
                    raise ValidationFailed(issues)
            # 结果启用集合必须能装配成完整路由集（启用校验 + 默认图/部门不变量）；
            # 提交前校验保证失败时不会先落盘再发现无法发布。
            publisher.prepare(
                _resulting_enabled(repo, [candidate for candidate, _ in updates]),
                repo.route_fingerprint(),
            )
            batch = repo.stage_updates(updates)
            batch.commit()
            # 只发布「全部候选已落盘」之后的完整启用集合；本块内失败会回滚批次。
            publisher.publish(
                publisher.prepare(repo.enabled(), repo.route_fingerprint()))
        except BaseException:
            _restore_batch(batch, publisher)
            raise
        batch.discard()
        committed = [repo.get(candidate.id) for candidate, _base in updates]

    return {"trees": [tree_to_dict(tree) for tree in committed]}
