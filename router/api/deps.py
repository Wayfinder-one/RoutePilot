"""Shared request-scoped dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from router.control.remote_catalog_sync import RemoteCatalogSyncService


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    from router.store.db import connect
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_tree_repo(request: Request):
    """Kernel graph repository (the kernel mounts the tree routes; the peripheral
    only proxies them, so this dependency is resolved on the kernel app only)."""
    return request.app.state.graph_repo


def catalog_service(request: Request,
                    conn: sqlite3.Connection) -> RemoteCatalogSyncService:
    """One catalog write use-case bound to this request's SQLite connection.

    ``app.state.catalog_sync_factory`` is wired by ``create_app()`` so tests can
    reuse the production wiring with an in-process kernel transport.
    """
    return request.app.state.catalog_sync_factory(conn)


async def commit_catalog_write(request: Request, service: RemoteCatalogSyncService,
                               *, db_write, candidates, audit,
                               recheck=None) -> list[str]:
    """Commit one catalog write through the cross-service transaction.

    The SQLite transaction + kernel graph batch + reverse compensation are all
    owned by ``RemoteCatalogSyncService``; the peripheral has no local graph
    snapshot to refresh (the kernel is the only graph writer and the only
    reader that publishes a route set).
    """
    return await service.commit(
        db_write=db_write, candidates=candidates, audit=audit, recheck=recheck)
