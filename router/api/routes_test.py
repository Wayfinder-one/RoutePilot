"""Admin-only Test chat: Auto routing or a fixed direct model.

Shares the ``/v1/chat/completions`` executor; ``use_requested_model=True`` is
the only difference (the public data plane keeps ignoring the request model).
The catalog connection is injected here so a fixed model resolves through the
SQLite catalog, independent of the active canvas snapshot.
"""
import sqlite3

from fastapi import APIRouter, Depends, Request

from router.api.deps import get_conn
from router.api.routes_v1 import run_chat_completion

router = APIRouter()


@router.post("/test/chat/completions")
async def test_chat_completions(
    request: Request, conn: sqlite3.Connection = Depends(get_conn),
):
    return await run_chat_completion(request, use_requested_model=True, conn=conn)
