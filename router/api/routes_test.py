"""Admin-only Test chat: Auto routing or a fixed direct model.

Shares the peripheral data-plane executor with ``/v1/chat/completions``;
``use_requested_model=True`` is the only difference (the public data plane keeps
ignoring the request model).  ``auto`` goes through the kernel decision exactly
like the public data plane; any other model resolves through the peripheral
SQLite catalog (``load_backends``) and calls the upstream directly without ever
touching the kernel.

``allow_department_header=True`` is the other admin-only capability: the test
chat trusts the operator-supplied ``x-sr-department`` header so a department
selection can be exercised end to end (the value is forwarded to the kernel),
while the public data plane keeps rejecting that header.
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
