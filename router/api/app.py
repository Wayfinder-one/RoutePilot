from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from router.config import Settings
from router.control.errors import (
    AlreadyExists,
    DefaultGraphRequired,
    MissingError,
    ReferencedByError,
    ValidationFailed,
)
from router.api.issue_presenter import issues_payload
from router.jev.client import JevClient
from router.jev.service import JevService
from router.maintenance import MaintenanceWorker
from router.request_logging import RequestLogger
from router.routing.affinity import SessionAffinity
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.reloader import GraphReloadWorker
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import SnapshotPublisher
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.repositories import (
    SessionRepo,
    SettingsRepo,
    UserRepo,
    seed_default,
    transaction,
)
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphConflictError,
    GraphReadOnlyError,
    GraphStorageError,
    RoutingTreeRepo,
)
from router.upstream.breaker import CircuitBreaker
from router.upstream.breaker_store import BreakerStore
from router.upstream.breaker_worker import BreakerSyncWorker

CONSOLE_DIR = Path(__file__).parent.parent / "console"
logger = logging.getLogger(__name__)


def _admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


_ADMIN_PUBLIC_PATHS = {"/admin/login"}
_VIEWER_WRITE_WHITELIST = {("POST", "/admin/logout"), ("POST", "/admin/me/password")}


def _bearer(header: str) -> str:
    prefix = "bearer "
    return header[len(prefix):].strip() if header[:len(prefix)].lower() == prefix else ""


def _viewer_forbidden(method: str, path: str) -> bool:
    if path.startswith("/admin/users"):
        return True
    if method in ("GET", "HEAD"):
        return False
    return (method, path) not in _VIEWER_WRITE_WHITELIST


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "unauthorized: /admin requires login"}, headers={"WWW-Authenticate": "Bearer"})


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "forbidden: admin role required"})


def _install_admin_auth(app: FastAPI, settings: Settings) -> None:
    if not settings.user_auth:
        return
    db_path = settings.db_path

    @app.middleware("http")
    async def admin_auth(request: Request, call_next):
        path = request.url.path
        if not _admin_path(path):
            return await call_next(request)
        normalized = path.rstrip("/") or "/"
        if normalized in _ADMIN_PUBLIC_PATHS:
            return await call_next(request)
        token = _bearer(request.headers.get("authorization", ""))
        if token:
            conn = connect(db_path)
            try:
                user = SessionRepo(conn).user_for_token(token)
            finally:
                conn.close()
            if user is not None:
                request.state.user = {
                    "id": user["id"], "email": user["email"], "role": user["role"],
                    "service": False, "expires_at": user["expires_at"],
                }
                if user["role"] != "admin" and _viewer_forbidden(request.method, normalized):
                    return _forbidden()
                return await call_next(request)
        return _unauthorized()


#: ``settings`` 表里的「JEV 目录已用环境种子初始化过」标记。
#: 与 ``seed_default`` 使用的 ``catalog_seeded``（provider/model 种子）分开：
#: 该标记只在**确实写入**环境 JEV 时设置，用于保证环境种子只发生一次。
JEV_SEEDED_SETTING = "jev_catalog_seeded"


def _seed_jev(conn, settings: Settings) -> None:
    """boot 种子：环境配置只在整个生命周期内创建一次 ``default`` 服务。

    - 目录里已有任何服务就完全跳过（数据库目录是事实源，不覆盖管理结果）。
    - 只有从未用环境种子初始化过（``settings`` 表的 ``jev_catalog_seeded``
      标记缺失）且目录为空时才播种：因此「首启无环境、之后补环境」仍会播种一次，而「全部删除
      后重启」不会把 ``default`` 复活。
    - 没有配置环境 URL/model 时不写标记，留给之后带环境启动的机会。
    - 不读取任何画布、不触发任何图同步：启动路由集只由启用图 JSON 决定。
    - 环境只配置 URL/model 之一时仍是配置错误（保持原启动失败语义）。
    """
    repo = JevServiceRepo(conn)
    if repo.list():
        return
    if SettingsRepo(conn).get(JEV_SEEDED_SETTING) is not None:
        return
    if not settings.jev_url and not settings.jev_model:
        return
    if not settings.jev_url or not settings.jev_model:
        raise RuntimeError("ROUTER_JEV_URL and ROUTER_JEV_MODEL must be configured together")
    service = JevService(
        id="default",
        base_url=settings.jev_url,
        model=settings.jev_model,
        key_env="ROUTER_JEV_API_KEY",
        timeout_seconds=settings.jev_timeout,
    )
    with transaction(conn):
        repo.save(service, api_key=settings.jev_api_key)
        SettingsRepo(conn).set(JEV_SEEDED_SETTING, "1")


def create_app(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> FastAPI:
    requestlog = RequestLogger(settings.db_path)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        seed_default(conn)
        if settings.user_auth:
            UserRepo(conn).seed_admin(settings.admin_email, settings.admin_password)
        requestlog.hydrate_recent(conn)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 共享熔断 quarantine 必须在接收流量之前进入内存：start() 先 await
        # 一次跨副本同步，然后才启动后台轮询。
        await app.state.breaker_worker.start()
        await app.state.maintenance.start()
        await requestlog.start()
        await app.state.graph_reloader.start()
        try:
            yield
        finally:
            # 先停全部后台 worker（它们可能仍在用数据库/外部资源），最后才
            # 关闭 HTTP client。
            await app.state.breaker_worker.close()
            await app.state.maintenance.close()
            await app.state.graph_reloader.close()
            await requestlog.close()
            await app.state.http.aclose()

    app = FastAPI(title="RoutePilot", lifespan=lifespan)
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    app.state.breaker = CircuitBreaker()
    app.state.breaker_store = BreakerStore(settings.db_path)
    app.state.breaker_worker = BreakerSyncWorker(
        app.state.breaker, app.state.breaker_store)
    app.state.maintenance = MaintenanceWorker(settings.db_path)
    app.state.balancer = LoadBalancer(app.state.breaker)
    app.state.affinity = SessionAffinity(max_entries=settings.session_max_entries)
    app.state.jev_client = JevClient(app.state.http)
    app.state.routing_service = RoutingService(
        GraphEvaluator(app.state.jev_client),
        ModelScheduler(app.state.affinity, app.state.balancer),
    )
    app.state.requestlog = requestlog

    app.state.graph_repo = RoutingTreeRepo(settings.graph_dir, writer=settings.graph_writer)
    if settings.graph_writer:
        app.state.graph_repo.initialize()
    app.state.snapshot_set = None
    app.state.publisher = SnapshotPublisher(app, app.state.graph_repo)
    conn = connect(settings.db_path)
    try:
        # boot 种子只写 JEV 目录；启动路由集只由启用图文档构造（零启用图为空集）。
        _seed_jev(conn, settings)
        app.state.publisher.refresh(conn)
    except GraphStorageError:
        logger.exception("initial graph snapshot is unavailable")
    finally:
        conn.close()
    app.state.graph_reloader = GraphReloadWorker(app)

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

    # 服务层异常 → HTTP 的统一映射：ValidationFailed/引用冲突/缺失
    # 由服务抛出，这里构成唯一映射点；路由层只做 DTO 解析与最小 try 包装。
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

    _install_admin_auth(app, settings)

    @app.get("/")
    def root():
        return RedirectResponse("/console/index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        status = app.state.publisher.status()
        return JSONResponse(status_code=200 if status["ready"] else 503, content=status)

    @app.middleware("http")
    async def console_nocache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    if CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")

    from router.api.routes_jev import router as jev_router
    from router.api.routes_logs import router as logs_router
    from router.api.routes_models import router as models_router
    from router.api.routes_test import router as test_router
    from router.api.routes_time import router as time_router
    from router.api.routes_trees import router as trees_router
    from router.api.routes_users import router as users_router
    from router.api.routes_v1 import router as v1_router

    app.include_router(models_router, prefix="/admin")
    app.include_router(time_router, prefix="/admin")
    app.include_router(trees_router, prefix="/admin")
    app.include_router(jev_router, prefix="/admin")
    app.include_router(logs_router, prefix="/admin")
    app.include_router(users_router, prefix="/admin")
    app.include_router(test_router, prefix="/admin")
    app.include_router(v1_router, prefix="/v1")
    return app
