"""Application factory. The hot path reads only in-memory snapshots
(app.state.pipeline / app.state.backends / app.state.tagger.kw_groups); the
database is touched on admin writes and by the async request logger, never
during routing (design doc section 4.4).

Optional /admin auth (design doc section 7): with ``ROUTER_ADMIN_TOKEN`` set,
every ``/admin*`` request must carry ``Authorization: Bearer <token>``. The
console pages stay public (they hold no data) and ask for the token in the
browser; ``/healthz``, ``/v1`` and the static console are not gated.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from router.config import Settings
from router.pipeline.health import check_pipeline
from router.signals.heuristic import HeuristicTagger
from router.store.db import connect, migrate
from router.store.repositories import (
    KeywordRepo, ModelRepo, StationRepo, TimePeriodRepo, seed_default)

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


def _install_admin_auth(app: FastAPI, token: str) -> None:
    """Require a static bearer token on /admin* (no-op when token is empty)."""
    if not token:
        return
    expected = f"Bearer {token}"

    @app.middleware("http")
    async def admin_auth(request: Request, call_next):
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            got = request.headers.get("authorization", "")
            if not hmac.compare_digest(got.encode(), expected.encode()):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "unauthorized: /admin requires "
                                       "Authorization: Bearer <ROUTER_ADMIN_TOKEN>"},
                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


def create_app(settings: Settings, *, transport: httpx.BaseTransport | None = None
               ) -> FastAPI:
    conn = connect(settings.db_path)
    migrate(conn)
    seed_default(conn)
    kw_groups = KeywordRepo(conn).groups()
    time_periods = TimePeriodRepo(conn).load()
    pipeline = StationRepo(conn).load()
    known_models = {m["id"] for m in ModelRepo(conn).list()}
    conn.close()

    # A tree that the engine cannot execute must not reach the hot path: the
    # console would happily save it, but every request would then 502 or pick
    # a wrong branch. Refuse to start instead (the operator fixes the DB and
    # restarts). Warnings are logged, they do not block the boot.
    issues = check_pipeline(pipeline, known_models)
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
    app.state.settings = settings
    app.state.tagger = _make_tagger(settings, kw_groups)
    app.state.tagger.time_periods = time_periods
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    app.state.pipeline = pipeline            # in-memory snapshot (design 4.4)
    from router.api.upstream import CircuitBreaker, load_backends
    conn = connect(settings.db_path)
    try:
        app.state.backends = load_backends(conn)   # model_id -> Backend snapshot
    finally:
        conn.close()
    app.state.breaker = CircuitBreaker(db_path=settings.db_path)
    app.state.requestlog = requestlog
    _install_admin_auth(app, settings.admin_token)

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
    from router.api.routes_v1 import router as v1_router
    app.include_router(admin_router, prefix="/admin")
    app.include_router(v1_router, prefix="/v1")
    return app
