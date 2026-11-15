"""Admin-only Test chat: Auto routing or a fixed direct model.

Shares the ``/v1/chat/completions`` executor; ``use_requested_model=True`` is
the only difference (the public data plane keeps ignoring the request model).
"""
from fastapi import APIRouter, Request

from router.api.routes_v1 import run_chat_completion

router = APIRouter()


@router.post("/test/chat/completions")
async def test_chat_completions(request: Request):
    return await run_chat_completion(request, use_requested_model=True)
