from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from router.api.requestlog import RequestLogger
from router.api.upstream import CircuitBreaker
from router.config import Settings
from router.jev.client import JevClient
from router.jev.config import JevConfigRepo, JevSettings
from router.routing.affinity import SessionAffinity
from router.routing.engine import RoutingEngine
from router.routing.scheduler import LoadBalancer
from router.routing.snapshot import SnapshotPublisher
from router.store.db import connect, migrate
from router.store.repositories import SessionRepo, UserRepo, seed_default

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


def _seed_jev(conn, settings: Settings) -> None:
    if JevConfigRepo(conn).load() is not None:
        return
    if not settings.jev_url and not settings.jev_model:
        return
    if not settings.jev_url or not settings.jev_model:
        raise RuntimeError("ROUTER_JEV_URL and ROUTER_JEV_MODEL must be configured together")
    JevConfigRepo(conn).save(JevSettings(
        base_url=settings.jev_url,
        model=settings.jev_model,
        api_key=settings.jev_api_key,
        timeout_seconds=settings.jev_timeout,
    ))


def create_app(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> FastAPI:
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        seed_default(conn)
        if settings.user_auth:
            UserRepo(conn).seed_admin(settings.admin_email, settings.admin_password)
        _seed_jev(conn, settings)
        from router.api.routes_trees import active_tree_issues
        issues = active_tree_issues(conn)
        if issues:
            raise RuntimeError(
                "refusing to start: invalid active routing tree: "
                + ", ".join(sorted({issue.code for issue in issues}))
            )
    finally:
        conn.close()

    requestlog = RequestLogger(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await requestlog.start()
        try:
            yield
        finally:
            await requestlog.close()
            await app.state.http.aclose()

    app = FastAPI(title="RoutePilot", lifespan=lifespan)
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    app.state.breaker = CircuitBreaker(db_path=settings.db_path)
    app.state.balancer = LoadBalancer(app.state.breaker)
    app.state.affinity = SessionAffinity(max_entries=settings.session_max_entries)
    app.state.jev_client = JevClient(app.state.http)
    app.state.engine = RoutingEngine(app.state.jev_client, app.state.affinity, app.state.balancer)
    app.state.requestlog = requestlog
    app.state.publisher = SnapshotPublisher(app)

    conn = connect(settings.db_path)
    try:
        app.state.publisher.refresh(conn)
    finally:
        conn.close()

    _install_admin_auth(app, settings)

    @app.get("/")
    def root():
        return RedirectResponse("/console/index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

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
    app.include_router(v1_router, prefix="/v1")
    return app
