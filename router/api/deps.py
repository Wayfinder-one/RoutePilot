"""Shared request-scoped dependencies."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator

from fastapi import Request

from router.control.remote_catalog_sync import RemoteCatalogSyncService

logger = logging.getLogger(__name__)


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    from router.store.db import connect
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_tree_repo(request: Request):
    """Peripheral transitional graph repo (``/admin/trees`` local mount, Task 8)."""
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
    """Commit one catalog write, then refresh the transitional local snapshot.

    The SQLite transaction + kernel graph batch + reverse compensation are all
    owned by ``RemoteCatalogSyncService``.  Afterwards the peripheral's local
    publisher (still used by the transitional ``/admin/trees`` mount and local
    routing until Task 8) is refreshed *if the enabled graph fingerprint
    changed*: a kernel commit is authoritative and SQLite is already committed,
    so a refresh failure must not turn a committed write into an error — the
    graph reloader retries it.  The refresh never publishes when only drafts
    changed (``refresh_if_changed`` keeps the same snapshot-set object).
    """
    try:
        return await service.commit(
            db_write=db_write, candidates=candidates, audit=audit,
            recheck=recheck)
    finally:
        refresh_published_snapshot(request)


def refresh_published_snapshot(request: Request) -> None:
    """Best-effort: mirror the kernel's committed graphs into the local snapshot."""
    publisher = getattr(request.app.state, "publisher", None)
    if publisher is None:
        return
    try:
        publisher.refresh_if_changed()
    except Exception:  # noqa: BLE001 - committed write must not be re-failed
        logger.exception("refresh of the peripheral graph snapshot failed")
