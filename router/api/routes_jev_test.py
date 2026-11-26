"""Kernel-side JEV connection test: the only place that holds the JEV key.

I6: the peripheral JEV catalog stores **non-secret** service fields only, and the
peripheral process has no JEV key at all.  When the console asks for a connection
test, the peripheral proxies the non-secret service description here
(``POST /internal/v1/jev-test``); the kernel builds a
:class:`~router.jev.config.JevSettings` whose ``api_key`` comes from the kernel's
own environment variable (``config.json`` ``jev.api_key_env``, default
``ROUTER_JEV_API_KEY``) and calls the JEV endpoint through its own
``app.state.jev_client``.

The request body is deliberately non-secret: a key can never be supplied in the
body or in a header (``api_key`` is an unknown field → 422).  Responses mirror
the former peripheral endpoint exactly: the choice answer contract on success,
502 for JEV failures/non-choice answers, 422 for invalid request values.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from router.jev.client import (
    ChoiceAnswer,
    DecisionQuestion,
    JevCandidate,
    JevError,
)
from router.jev.config import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STATE_TEMPLATE,
    JevSettings,
)

router = APIRouter()

#: Same limits/candidates as the endpoint this replaces on the peripheral.
TEST_TEXT_LIMIT = 10000
DEFAULT_TEST_TEXT = "请选择一个候选以验证决策服务。"
TEST_CANDIDATES = (
    JevCandidate("a", "候选 A", "用于验证决策服务连通性的第一个候选。"),
    JevCandidate("b", "候选 B", "用于验证决策服务连通性的第二个候选。"),
)


class JevTestIn(BaseModel):
    """Non-secret catalog service description + probe text.

    ``extra="forbid"`` keeps the credential channel dead: ``api_key``,
    ``key_env`` or ``authorization`` in the body are 422, never forwarded.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    text: str = DEFAULT_TEST_TEXT
    timeout_seconds: float = 10.0
    instructions: str = DEFAULT_INSTRUCTIONS
    state_template: str = DEFAULT_STATE_TEMPLATE


@router.post("/jev-test")
async def jev_test(body: JevTestIn, request: Request) -> dict:
    """Call the JEV endpoint with the kernel's own key; never from the body."""
    if len(body.text) > TEST_TEXT_LIMIT:
        raise HTTPException(
            422,
            detail=(
                "text must be a string of at most "
                f"{TEST_TEXT_LIMIT} characters"),
        )
    try:
        settings = JevSettings(
            base_url=body.base_url,
            model=body.model,
            # 唯一密钥来源：内核环境变量（create_core_app 在启动时解析到 state）。
            api_key=getattr(request.app.state, "jev_api_key", ""),
            timeout_seconds=body.timeout_seconds,
            instructions=body.instructions,
            state_template=body.state_template,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    client = request.app.state.jev_client
    try:
        question = DecisionQuestion(
            "choice", settings.instructions, TEST_CANDIDATES)
        answers = await client.choose_many(settings, body.text, {"route": question})
        answer = answers["route"]
        if not isinstance(answer, ChoiceAnswer):
            raise HTTPException(502, detail="JEV route answer is not choice")
    except JevError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    return {
        "selected_key": answer.selected_key,
        "probabilities": answer.probabilities,
        "p_top": answer.p_top,
        "margin": answer.margin,
        "usage": answer.usage,
        "duration_ms": answer.duration_ms,
    }
