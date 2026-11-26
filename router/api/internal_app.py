"""Peripheral internal listener: the kernel's decision-event feed only.

C1/I5: the kernel posts complete decision events without any admin login, so the
receiver must never share the publicly published console listener.  This module
builds a minimal app that mounts **only** the existing
``router.api.routes_logs.internal_router`` under ``/internal/v1`` plus
``/healthz``: no console, no admin API, no ``/v1`` data plane and no CORS.

``create_service_apps`` wires the production peripheral process: one shared
:class:`~router.decision_log_store.DecisionLogStore` feeds both the internal
receiver app and the public console app (``/admin/requests/*`` keeps serving it).
``create_app`` stays usable standalone and then creates its own store.
"""
from __future__ import annotations

from fastapi import FastAPI

from router.api.routes_logs import internal_router
from router.config import Settings
from router.decision_log_store import DecisionLogStore


def create_internal_app(decision_logs: DecisionLogStore) -> FastAPI:
    """Minimal internal app: receiver + health, sharing the given memory store."""
    app = FastAPI(title="RoutePilot internal")
    #: 与公开控制台应用共享的同一个内存 store（路由用这一份读取/写入）。
    app.state.decision_logs = decision_logs

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # 内核事件接收口：无管理面登录、无控制台、无 /v1。接收的事件写入与公开
    # 控制台应用共享的同一个内存 store。
    app.include_router(internal_router, prefix="/internal/v1")
    return app


def create_service_apps(
    settings: Settings,
    *,
    transport=None,
    core_transport=None,
) -> tuple[FastAPI, FastAPI]:
    """Build ``(public_app, internal_app)`` sharing one decision-log store."""
    # 延迟导入，保持 app.py → internal_app.py 之间没有模块级循环依赖。
    from router.api.app import create_app

    decision_logs = DecisionLogStore()
    public_app = create_app(
        settings,
        transport=transport,
        core_transport=core_transport,
        decision_logs=decision_logs,
    )
    internal_app = create_internal_app(decision_logs)
    return public_app, internal_app
