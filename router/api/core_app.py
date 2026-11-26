"""Standalone decision-kernel application factory.

The kernel owns the graph JSON, graph management (create/update/enable/try),
graph execution/JEV selection and the decision event output.  It deliberately
contains **none** of the peripheral capabilities: no SQLite (no
migration/seed/catalog/breaker store), no request-log writer or maintenance
worker, no console static mount, no users/providers/logs/admin routes and no
model upstream execution (``UpstreamProxy`` is never constructed here).  The
only HTTP client it holds is used by the configured JEV service and by the
decision event sink.

``graph management`` reuses the existing tree routes unchanged: the kernel
overrides the ``get_conn`` dependency with a provider that yields ``None``, so
the same handlers run without a SQLite connection (audit sink and database
catalog hints are peripheral-only and simply absent).

Wiring order follows ``create_app()``'s graph/JEV/routing part exactly; the
peripheral-only pieces are simply absent.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from router.api.deps import get_conn
from router.api.graph_errors import install_graph_error_handlers
from router.api.routes_graph_batches import router as graph_batches_router
from router.api.routes_internal import router as internal_router
from router.api.routes_jev_test import router as jev_test_router
from router.api.routes_trees import router as trees_router
from router.core_config import CoreSettings
from router.decision_log_sink import create_decision_sink
from router.jev.client import JevClient
from router.routing.affinity import SessionAffinity
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.reloader import GraphReloadWorker
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import SnapshotPublisher
from router.store.routing_trees import GraphStorageError, RoutingTreeRepo
from router.upstream.breaker import CircuitBreaker

logger = logging.getLogger(__name__)


def _kernel_connections():
    """Kernel ``get_conn`` override: graph management runs with no SQLite at all.

    The tree routes keep their ``Depends(get_conn)`` contract; the kernel simply
    resolves it to ``None`` so no database is ever opened, no audit row is
    written and no catalog-drift hint is computed.
    """
    yield None


def create_core_app(
    settings: CoreSettings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Build the decision kernel: graphs + JEV + routing + decision events."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 事件 sink 的发送 worker 必须先于决策流量启动，关闭时先排空再关 client。
        await app.state.decision_sink.start()
        await app.state.graph_reloader.start()
        try:
            yield
        finally:
            await app.state.graph_reloader.close()
            await app.state.decision_sink.close()
            await app.state.http.aclose()

    app = FastAPI(title="RoutePilot Core", lifespan=lifespan)
    app.state.settings = settings
    # JEV Key 只从内核环境变量读取：没有 SQLite，也没有配置里的密钥值。
    app.state.jev_api_key = os.environ.get(settings.jev_api_key_env, "")
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    app.state.breaker = CircuitBreaker()
    app.state.balancer = LoadBalancer(app.state.breaker)
    app.state.affinity = SessionAffinity()
    app.state.jev_client = JevClient(app.state.http)
    app.state.routing_service = RoutingService(
        GraphEvaluator(app.state.jev_client),
        ModelScheduler(app.state.affinity, app.state.balancer),
    )
    app.state.decision_sink = create_decision_sink(settings.logging, app.state.http)

    # 图 JSON 只由内核写入（全局约束 2）：唯一事实源是图目录，没有数据库回退。
    app.state.graph_repo = RoutingTreeRepo(settings.graph_dir, writer=True)
    app.state.graph_repo.initialize()
    app.state.snapshot_set = None
    app.state.publisher = SnapshotPublisher(app, app.state.graph_repo)
    try:
        app.state.publisher.refresh()
    except GraphStorageError:
        logger.exception("initial graph snapshot is unavailable")
    app.state.graph_reloader = GraphReloadWorker(app)

    install_graph_error_handlers(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        status = app.state.publisher.status()
        return JSONResponse(status_code=200 if status["ready"] else 503, content=status)

    # 内核图管理：复用现有 trees/try 路由，conn 依赖固定解析为 None（无 SQLite）。
    app.dependency_overrides[get_conn] = _kernel_connections
    app.include_router(internal_router, prefix="/internal/v1")
    # I6: the credentialed JEV connection test lives here (kernel env key only);
    # the peripheral console proxies to this internal route.
    app.include_router(jev_test_router, prefix="/internal/v1")
    app.include_router(trees_router, prefix="/internal/v1")
    # 跨服务目录写的条件图批次：外围一次原子替换全部候选画布（设计 §6.2）。
    app.include_router(graph_batches_router, prefix="/internal/v1")
    return app
