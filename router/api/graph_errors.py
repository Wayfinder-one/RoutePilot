"""Shared graph-management exception → HTTP mapping for both app factories.

The graph repository and ``GraphService`` raise typed errors; the console
contract for them is a structured JSON body with a stable status code.  Both
applications render that contract here — the peripheral ``create_app()`` (which
proxies graph management) and the kernel ``create_core_app()`` (which executes
it) — so a graph conflict/missing/corrupt answer never depends on which process
serves the request.

This module only maps exceptions to responses: it imports no SQLite, no app
factory, no request state and no route module.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from router.api.issue_presenter import issues_payload
from router.control.errors import (
    AlreadyExists,
    DefaultGraphRequired,
    MissingError,
    ReferencedByError,
    ValidationFailed,
)
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphConflictError,
    GraphReadOnlyError,
    GraphStorageError,
)


def install_graph_error_handlers(app: FastAPI) -> None:
    """Register the shared graph/control error vocabulary on ``app``."""

    @app.exception_handler(GraphConflictError)
    async def graph_conflict_handler(request: Request, exc: GraphConflictError):
        return JSONResponse(status_code=409, content={"detail": {
            "code": "graph_conflict",
            "actual_document_sha256": exc.actual_document_sha256,
        }})

    @app.exception_handler(GraphAlreadyExistsError)
    async def graph_exists_handler(request: Request, exc: GraphAlreadyExistsError):
        return JSONResponse(status_code=409, content={"detail": {
            "code": "graph_already_exists",
            "tree_id": str(exc),
        }})

    @app.exception_handler(GraphReadOnlyError)
    async def graph_read_only_handler(request: Request, exc: GraphReadOnlyError):
        return JSONResponse(status_code=503, content={"detail": {"code": "graph_read_only"}})

    @app.exception_handler(GraphStorageError)
    async def graph_storage_handler(request: Request, exc: GraphStorageError):
        return JSONResponse(status_code=503, content={"detail": {
            "code": "graph_storage_error",
            "message": str(exc),
        }})

    # 服务层异常 → HTTP 的统一映射：ValidationFailed/引用冲突/缺失由服务抛出，
    # 这里构成唯一映射点；路由层只做 DTO 解析与最小 try 包装。
    @app.exception_handler(ValidationFailed)
    async def validation_failed_handler(request: Request, exc: ValidationFailed):
        return JSONResponse(status_code=422, content={"detail": issues_payload(exc.issues)})

    @app.exception_handler(MissingError)
    async def missing_error_handler(request: Request, exc: MissingError):
        return JSONResponse(status_code=404, content={"detail": exc.message})

    @app.exception_handler(AlreadyExists)
    async def already_exists_handler(request: Request, exc: AlreadyExists):
        return JSONResponse(status_code=409, content={"detail": exc.message})

    @app.exception_handler(ReferencedByError)
    async def referenced_by_error_handler(request: Request, exc: ReferencedByError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    @app.exception_handler(DefaultGraphRequired)
    async def default_graph_required_handler(
            request: Request, exc: DefaultGraphRequired):
        """默认图之外仍有启用图时禁止直接停用默认图（结构化 409）。"""
        return JSONResponse(status_code=409, content={"detail": {
            "code": "default_graph_required",
            "tree_id": exc.tree_id,
        }})
