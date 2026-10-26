"""Application factory. The hot path reads only the immutable in-memory
``app.state.snapshot`` (RoutingSnapshot: stations/backends/revisions/jev_trim)
plus the tagger object; the database is touched on admin writes and by the
async request logger, never during routing (design doc section 4.4).

/admin auth (design doc section 7, user management 2026-10-26): every
``/admin*`` request must carry a console login session
(``router/api/routes_users.py``); ``/admin/login`` is public. The static
``ROUTER_ADMIN_TOKEN`` was removed on 2026-10-26 — scripts log in and reuse the
session token (``scripts/configure_upstream.py``). The console pages stay public
(they hold no data) and gate their data calls; ``/healthz``, ``/v1`` and the
static console are not gated at all.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from router.config import Settings
from router.api.upstream import model_reasoning_from_db
from router.pipeline.health import check_pipeline
from router.pipeline.snapshot import install_snapshot, load_snapshot
from router.signals.heuristic import HeuristicTagger
from router.store.db import connect, migrate
from router.store.repositories import (
    KeywordRepo, ModelRepo, SessionRepo, StationRepo, TimePeriodRepo, UserRepo,
    ensure_pipeline_revision, ensure_questions_revision, seed_default)

CONSOLE_DIR = Path(__file__).parent.parent / "console"

logger = logging.getLogger(__name__)


def _make_tagger(settings: Settings, kw_groups: dict):
    """Tagger registry (design 4.3). Imported lazily so torch is only touched
    in checkpoint mode; an invalid checkpoint raises here and refuses startup."""
    if settings.tagger == "heuristic":
        return HeuristicTagger(kw_groups=kw_groups)
    if settings.tagger == "checkpoint":
        from router.signals.checkpoint import CheckpointTagger
        if not settings.checkpoint_dir:
            raise RuntimeError("ROUTER_TAGGER=checkpoint needs ROUTER_CHECKPOINT")
        return CheckpointTagger(settings.checkpoint_dir, device=settings.device,
                                kw_groups=kw_groups)
    if settings.tagger == "jev":
        from router.signals.jev import JevTagger
        if not settings.jev_model:
            raise RuntimeError("ROUTER_TAGGER=jev needs ROUTER_JEV_MODEL")
        return JevTagger(
            settings.jev_model,
            questions_path=settings.signal_questions or None,
            device=settings.device or None,
            max_length=settings.jev_max_length or None,
            # "" = ROUTER_JEV_WINDOW unset: JevTagger derives the legacy
            # head-only "<max_length>:0" window instead of failing on a literal
            # default when ROUTER_JEV_MAX_LENGTH is customized.
            window=settings.jev_window or None,
            # Long-text PII chunking (0 = off, the default: behavior unchanged).
            chunk_segments=settings.jev_chunk_segments,
            chunk_size=settings.jev_chunk_size,
            chunk_agg=settings.jev_chunk_agg,
            chunk_calibration=settings.jev_chunk_calibration or None,
            kw_groups=kw_groups)
    if settings.tagger == "prompt":
        from router.signals.prompt_judge import PromptJudgeTagger
        if not settings.judge_url or not settings.judge_model:
            raise RuntimeError("ROUTER_TAGGER=prompt needs ROUTER_JUDGE_URL and "
                               "ROUTER_JUDGE_MODEL")
        return PromptJudgeTagger(
            base_url=settings.judge_url, model=settings.judge_model,
            api_key=settings.judge_api_key,
            prompts_path=settings.signal_prompts or None, kw_groups=kw_groups)
    raise RuntimeError(f"unknown tagger {settings.tagger!r}")


def _admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


#: /admin 下不需要任何凭据的路径：登录本身（否则登录接口先被 401 挡住）。
_ADMIN_PUBLIC_PATHS = {"/admin/login"}

#: viewer 也能用的写接口（spec §3 白名单）：登出 + 改自己的密码。
_VIEWER_WRITE_WHITELIST = {("POST", "/admin/logout"),
                           ("POST", "/admin/me/password")}


def _bearer(header: str) -> str:
    prefix = "bearer "
    if header[:len(prefix)].lower() != prefix:
        return ""
    return header[len(prefix):].strip()


def _viewer_forbidden(method: str, path: str) -> bool:
    """viewer 角色门：只读放行、白名单写放行，其余 403（spec §3）。"""
    if path.startswith("/admin/users"):
        return True                     # 用户管理连列表都不给 viewer
    if method in ("GET", "HEAD"):
        return False
    return (method, path) not in _VIEWER_WRITE_WHITELIST


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "unauthorized: /admin requires login"},
        headers={"WWW-Authenticate": "Bearer"})


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403,
                        content={"detail": "forbidden: admin role required"})


def _install_admin_auth(app: FastAPI, settings: Settings) -> None:
    """Gate /admin* with a console login session (2026-10-26 user management).

    ``/admin/login`` 免鉴权；其余 ``/admin*`` 必须带一个未过期的登录会话
    （``Authorization: Bearer <session token>``）。会话用户的 role=viewer 时
    只读放行，写操作 / 用户管理 403。

    两种模式：
    * ``user_auth`` False：2026-10-26 之前的旧行为 —— /admin 完全开放（本地
      dev/旧脚本的兼容路径），不做任何鉴权。
    * ``user_auth`` True：上面的登录体系生效（``Settings.from_env()`` 的默认）。

    静态 ``ROUTER_ADMIN_TOKEN`` 已于 2026-10-26 移除：脚本改用登录拿会话 token
    （见 ``scripts/configure_upstream.py`` / ``scripts/apply_default_pipeline.py``）。

    会话校验在中间件里自己开短连接（中间件在依赖注入作用域之外，拿不到
    ``get_conn``），``finally`` 关闭；这是每次 /admin 请求多一次 SQLite 查询的
    代价，换来中间件保持唯一鉴权入口。
    """
    if not settings.user_auth:
        return                          # 旧行为：没有任何门
    db_path = settings.db_path

    @app.middleware("http")
    async def admin_auth(request: Request, call_next):
        path = request.url.path
        if not _admin_path(path):
            return await call_next(request)
        normalized = path.rstrip("/") or "/"
        if normalized in _ADMIN_PUBLIC_PATHS:
            return await call_next(request)
        bearer = _bearer(request.headers.get("authorization", ""))
        if bearer:
            conn = connect(db_path)
            try:
                user = SessionRepo(conn).user_for_token(bearer)
            finally:
                conn.close()
            if user is not None:
                request.state.user = {
                    "id": user["id"], "email": user["email"],
                    "role": user["role"], "service": False,
                    "expires_at": user["expires_at"]}
                # 非 admin（含被手工写坏的角色值）一律按 viewer 处理：失败关闭。
                if user["role"] != "admin" and _viewer_forbidden(
                        request.method, normalized):
                    return _forbidden()
                return await call_next(request)
        return _unauthorized()


def create_app(settings: Settings, *, transport: httpx.BaseTransport | None = None
               ) -> FastAPI:
    conn = connect(settings.db_path)
    migrate(conn)
    seed_default(conn)
    if settings.user_auth:
        # 只有开启用户体系时才创建默认管理员（空表才行，幂等）；关闭时连
        # 哈希都不算——scrypt 是 0.3s 量级的成本，不该为不用的账号付。
        UserRepo(conn).seed_admin(settings.admin_email, settings.admin_password)
    kw_groups = KeywordRepo(conn).groups()
    time_periods = TimePeriodRepo(conn).load()
    pipeline = StationRepo(conn).load()
    known_models = {m["id"] for m in ModelRepo(conn).list()}
    # Same §3.3 map as PUT /admin/pipeline (single source). A leaf that names
    # a reasoning level the model does not declare is rejected at save time —
    # and pipelines written straight into the database (migrations, older
    # versions, a manual edit) must be judged by the same rule here.
    model_reasoning = model_reasoning_from_db(conn)
    # v15: the tree that is about to serve traffic must be resolvable from
    # config_revisions (first boot after the migration: stations predate the
    # table). Identical content maps to the same sha, so this is idempotent.
    ensure_pipeline_revision(conn, pipeline)
    conn.close()

    # A tree that the engine cannot execute must not reach the hot path: the
    # console would happily save it, but every request would then 502 or pick
    # a wrong branch. Refuse to start instead (the operator fixes the DB and
    # restarts). Warnings are logged, they do not block the boot.
    issues = check_pipeline(pipeline, known_models, model_reasoning)
    hard = [i for i in issues if i.level == "hard"]
    if hard:
        for issue in hard:
            logger.error("pipeline health: %s (%s) station=%s node=%s",
                         issue.en, issue.code, issue.station_id, issue.node_id)
        raise RuntimeError(
            "refusing to start: the stored pipeline has "
            f"{len(hard)} hard issue(s): "
            + ", ".join(sorted({i.code for i in hard})))
    for issue in issues:
        logger.warning("pipeline health: %s (%s) station=%s node=%s",
                       issue.en, issue.code, issue.station_id, issue.node_id)

    from router.api.requestlog import RequestLogger
    requestlog = RequestLogger(settings)

    tagger = _make_tagger(settings, kw_groups)
    tagger.time_periods = time_periods
    # Build the immutable snapshot ONCE at startup (P1-B): the pipeline, the
    # backend map, the revisions and the trimming switch a request may read.
    conn = connect(settings.db_path)
    try:
        # v15: record the questions config the tagger booted with, so the
        # very first ledger rows can be replayed from config_revisions.
        questions_cfg = getattr(tagger, "questions", None)
        if isinstance(questions_cfg, dict):
            ensure_questions_revision(conn, questions_cfg)
        snapshot = load_snapshot(conn, jev_trim=settings.jev_trim)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await requestlog.start()
        try:
            yield
        finally:
            await requestlog.close()
            await _app.state.http.aclose()
            aclose = getattr(_app.state.tagger, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="Smart Router", lifespan=lifespan)
    from router.api.upstream import CircuitBreaker
    app.state.settings = settings
    app.state.tagger = tagger
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    # One assignment publishes the whole configuration to the hot path; the
    # legacy pipeline/backends aliases are refreshed inside install_snapshot.
    install_snapshot(app, snapshot)
    app.state.breaker = CircuitBreaker(db_path=settings.db_path)
    app.state.requestlog = requestlog
    _install_admin_auth(app, settings)

    @app.get("/")
    def root():
        return RedirectResponse("/console/index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # The console is plain static files; force revalidation so a redeploy can
    # never leave a browser running stale JS against a new API (2026-10-19).
    @app.middleware("http")
    async def console_nocache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    if CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True),
                  name="console")

    from router.api.routes_admin import router as admin_router
    from router.api.routes_users import router as users_router
    from router.api.routes_v1 import router as v1_router
    app.include_router(admin_router, prefix="/admin")
    # 登录/登出/me 与用户 CRUD 共享 /admin 前缀（契约按设计 doc §4）
    app.include_router(users_router, prefix="/admin")
    app.include_router(v1_router, prefix="/v1")
    return app
