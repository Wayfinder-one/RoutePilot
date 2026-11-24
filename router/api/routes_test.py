"""Admin-only Test chat: Auto routing or a fixed direct model.

Shares the ``/v1/chat/completions`` executor; ``use_requested_model=True`` is
the only difference (the public data plane keeps ignoring the request model).
The catalog connection is injected here so a fixed model resolves through the
SQLite catalog, independent of the published route set.

``allow_department_header=True`` is the other admin-only capability: the test
chat trusts the operator-supplied ``x-sr-department`` header so a department
selection can be exercised end to end, while the public data plane keeps
rejecting that header.
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
    return await run_chat_completion(
        request, use_requested_model=True, conn=conn,
        allow_department_header=True,
    )
