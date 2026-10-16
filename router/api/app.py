"""Application factory. The hot path reads only in-memory snapshots
(app.state.pipeline / app.state.tagger.kw_groups); the database is touched
on admin writes and by the async request logger, never during routing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from router.config import Settings
from router.signals.heuristic import HeuristicTagger
from router.store.db import connect, migrate
from router.store.repositories import KeywordRepo, PipelineRepo, seed_default

CONSOLE_DIR = Path(__file__).parent.parent / "console"

TAGGERS = {"heuristic": HeuristicTagger}   # M2: 'checkpoint' -> CheckpointTagger


def create_app(settings: Settings, *, transport: httpx.BaseTransport | None = None
               ) -> FastAPI:
    conn = connect(settings.db_path)
    migrate(conn)
    seed_default(conn)
    kw_groups = KeywordRepo(conn).groups()
    pipeline = PipelineRepo(conn).load()
    conn.close()

    from router.api.requestlog import RequestLogger
    requestlog = RequestLogger(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await requestlog.start()
        try:
            yield
        finally:
            await requestlog.close()

    app = FastAPI(title="Smart Router", lifespan=lifespan)
    app.state.settings = settings
    app.state.tagger = TAGGERS[settings.tagger](kw_groups=kw_groups)
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    app.state.pipeline = pipeline            # in-memory snapshot (design 4.4)
    from router.api.upstream import CircuitBreaker
    app.state.breaker = CircuitBreaker()
    app.state.requestlog = requestlog

    @app.get("/")
    def root():
        return RedirectResponse("/console/index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    if CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True),
                  name="console")

    from router.api.routes_admin import router as admin_router
    from router.api.routes_v1 import router as v1_router
    app.include_router(admin_router, prefix="/admin")
    app.include_router(v1_router, prefix="/v1")
    return app
