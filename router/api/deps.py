"""Shared request-scoped dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    from router.store.db import connect
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_tree_repo(request: Request):
    return request.app.state.graph_repo
