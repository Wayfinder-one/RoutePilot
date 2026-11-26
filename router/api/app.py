"""Peripheral application factory: console, SQLite configuration and execution.

The peripheral owns everything user-facing and execution-related:

* the console static mount, personnel/session login and the ``/admin`` APIs that
  read or write peripheral SQLite (people, providers, model catalog, non-secret
  JEV catalog, time periods, changes);
* the graph-management **proxy** (``routes_core_proxy``): every graph request is
  authenticated here and executed by the kernel — the peripheral has no graph
  repository, publisher or routing service of its own;
* the standalone data plane (``/v1/chat/completions``): ask the kernel for a
  decision, resolve the Provider Key from peripheral SQLite and execute the
  model request; fixed-model admin test chat resolves the catalog directly;
* the volatile 20-event memory log (``/admin/requests/*``).  The kernel's
  decision-event receiver is NOT mounted here: it lives on the separate internal
  listener (``router.api.internal_app``) so external console clients can never
  inject or poison decision logs (I5).

Deliberately absent (kernel-owned): graph JSON, routing/JEV execution, decision
events production and model upstream calls during a decision.  The peripheral
also keeps no request-log database and no graph snapshots any more: the only
decision log is the volatile in-memory event ring.  The only kernel boundary is
``app.state.core`` (CoreClient).

Readiness reflects peripheral state only (SQLite migrated).  An unreachable
kernel is a data-plane/management error, never a readiness failure: the
peripheral stays healthy so operators can still open the console and inspect the
configuration while the kernel is being restarted.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from router.api.issue_presenter import issues_payload
from router.api.routes_core_proxy import router as core_proxy_router
from router.config import (
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    LOOPBACK_HOSTS,
    Settings,
)
from router.control.errors import (
    AlreadyExists,
    DefaultGraphRequired,
    MissingError,
    ReferencedByError,
    ValidationFailed,
)
from router.control.remote_catalog_sync import (
    CatalogSyncDiverged,
    RemoteCatalogSyncService,
)
from router.core_client import CoreClient, CoreGraphError
from router.decision_log_store import DecisionLogStore
from router.jev.service import JevService
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
    GraphStorageError,
)
from router.upstream.breaker import CircuitBreaker

CONSOLE_DIR = Path(__file__).parent.parent / "console"
logger = logging.getLogger(__name__)


def _catalog_sync_factory(client: CoreClient):
    """Bind the cross-service catalog write use-case to a request's SQLite conn."""

    def build(conn) -> RemoteCatalogSyncService:
        return RemoteCatalogSyncService(conn, client)

    return build


def _guard_default_admin(settings: Settings) -> None:
    """C1: refuse publicly-known default admin credentials on a non-loopback bind.

    The seed values exist only so ``python -m router`` on this machine works out
    of the box.  A deployment (compose binds ``0.0.0.0``) must provide explicit
    ``ROUTER_ADMIN_EMAIL`` / ``ROUTER_ADMIN_PASSWORD``; silently seeding
    ``admin@test.com`` / ``1qaz@WSX`` on a reachable listener is a critical
    security defect.
    """
    if not settings.user_auth:
        return
    if settings.host.strip().lower() in LOOPBACK_HOSTS:
        return
    if (settings.admin_email, settings.admin_password) != (
            DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_PASSWORD):
        return
    raise RuntimeError(
        "refusing to start the peripheral console with the publicly-known "
        f"default admin credentials on non-loopback host {settings.host!r}: "
        "set ROUTER_ADMIN_EMAIL and ROUTER_ADMIN_PASSWORD to explicit values "
        "(or bind ROUTER_HOST to 127.0.0.1 for local-only use)"
    )


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

    种子只写非密钥目录字段：JEV 密钥由内核从 ``ROUTER_JEV_API_KEY`` 读取，
    v7 起不再写入 SQLite；外围进程也没有任何 JEV 密钥或密钥通道（I6）。

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
        timeout_seconds=settings.jev_timeout,
    )
    with transaction(conn):
        repo.save(service)
        SettingsRepo(conn).set(JEV_SEEDED_SETTING, "1")


def create_app(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    core_transport: httpx.AsyncBaseTransport | None = None,
    decision_logs: DecisionLogStore | None = None,
) -> FastAPI:
    """Build the peripheral service: no graph ownership, no local routing.

    ``decision_logs`` lets :func:`router.api.internal_app.create_service_apps`
    share one memory store between the public console app and the internal
    receiver app; standalone callers (and tests) may omit it and get a fresh
    store.
    """
    _guard_default_admin(settings)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        seed_default(conn)
        if settings.user_auth:
            UserRepo(conn).seed_admin(settings.admin_email, settings.admin_password)
        _seed_jev(conn, settings)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            # The kernel boundary first, then the model-upstream client: no
            # background worker or SQLite connection is owned by this app.
            await app.state.core.aclose()
            await app.state.http.aclose()

    app = FastAPI(title="RoutePilot", lifespan=lifespan)
    app.state.settings = settings
    #: SQLite migrated at construction: readiness does not depend on the kernel.
    app.state.db_ready = True
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    # 内核客户端自备 AsyncClient：浏览器/上游 transport 不复用到内核管理面。
    app.state.core = CoreClient(
        settings.core_url,
        http=httpx.AsyncClient(
            timeout=settings.core_timeout_seconds, transport=core_transport),
        timeout_seconds=settings.core_timeout_seconds,
    )
    # 目录写（模型 / provider / JEV / 时段）的跨服务事务：SQLite 单写事务 +
    # 内核条件图批次 + 反向补偿。路由只做 DTO 与影响预览。
    app.state.catalog_sync_factory = _catalog_sync_factory(app.state.core)
    # 外围执行层的上游熔断（只记录本进程的上游执行结果，不参与内核调度）。
    # JEV 调用（含连接测试）只属于内核：外围没有 JEV 客户端，也没有密钥。
    app.state.breaker = CircuitBreaker()
    # 决策日志只保存在内存：完整事件来自内核事件接收口，重启即清空（Global Constraint 5）。
    # 生产进程通过 create_service_apps 传入与内部监听共享的 store；独立使用时自建。
    app.state.decision_logs = (
        decision_logs if decision_logs is not None else DecisionLogStore())

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

    @app.exception_handler(CoreGraphError)
    async def core_graph_error_handler(request: Request, exc: CoreGraphError):
        """内核图管理面不可用：保留内核的结构化 detail（503 graph_* 码）。"""
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        detail = payload.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            return JSONResponse(status_code=503, content={"detail": detail})
        return JSONResponse(
            status_code=503, content={"detail": {"code": "core_unavailable"}})

    @app.exception_handler(CatalogSyncDiverged)
    async def catalog_sync_diverged_handler(
            request: Request, exc: CatalogSyncDiverged):
        """SQLite 已回滚但内核补偿失败：显式 503，绝不返回成功。"""
        return JSONResponse(status_code=503, content={
            "detail": {"code": "catalog_sync_diverged"}})

    _install_admin_auth(app, settings)

    @app.get("/")
    def root():
        return RedirectResponse("/console/index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        """Peripheral readiness: SQLite migrated. The kernel is not probed."""
        ready = bool(app.state.db_ready)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "database": "ok" if ready else "unavailable"},
        )

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
    from router.api.routes_users import router as users_router
    from router.api.routes_v1 import router as v1_router

    app.include_router(models_router, prefix="/admin")
    app.include_router(time_router, prefix="/admin")
    # 图管理经内核执行：外围只保留 /admin/trees、/admin/try 的鉴权与代理契约。
    app.include_router(core_proxy_router, prefix="/admin")
    app.include_router(jev_router, prefix="/admin")
    app.include_router(logs_router, prefix="/admin")
    app.include_router(users_router, prefix="/admin")
    app.include_router(test_router, prefix="/admin")
    # I5：内核事件接收口刻意不挂在这里。它只存在于独立内部监听
    # （router.api.internal_app），公开控制台监听上该路径是 404。
    # 决策端点（/internal/v1/route-decision）只属于内核：外围不挂载它；
    # 外围与内核的 HTTP 边界是 app.state.core（CoreClient）。
    app.include_router(v1_router, prefix="/v1")
    return app
