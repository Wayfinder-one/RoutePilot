"""fake-upstream：故障注入假上游（spec §1.3/§14.4；Task 8/11 的测试底座）。

:func:`create_app` 返回 FastAPI 应用，双协议端点（§7.1 路径同构）：

- ``POST /v1/chat/completions`` —— OpenAI 协议（§7.3）
- ``POST /v1/messages``        —— Anthropic 协议（§7.4）

应答为回显式固定内容：

- 非流式回固定 JSON，``model`` 回显请求里的上游模型名（§7.2-8 出站名——
  router 侧 §7.2-10 回写后，客户端看到的应与之无关，借此可核对回写链路）；
- 流式回 3 段 delta + 正常终止：OpenAI ``data: {...}\\n\\n`` 序列 + finish chunk
  （``stream_options.include_usage`` 时再补 ``choices: []`` 纯 usage chunk）+
  ``data: [DONE]\\n\\n``；Anthropic 按 §7.4 事件序列 message_start →
  content_block_start → content_block_delta×3 → content_block_stop →
  message_delta → message_stop。

查询参数（两端点同，故障注入面）：

- ``?latency_ms=N``   响应前延迟 N 毫秒（超时/慢上游用）；
- ``?fail=500``       回 HTTP 500 + 协议错误信封（§7.3/§7.4 各自形状）；
- ``?fail=midstream`` 流式：发出首包后切断——客户端收到 ≥1 包但收不到终止符
  （``data: [DONE]`` / ``message_stop``），即 §7.5「首包一旦发出，不重试、不换
  模型、不回退」红线的验收器具；非流式下无"中"可断，等同 fail=500。

:func:`run` 为 CLI ``sr fake-upstream``（Task 8）的实现体，默认端口 8123
（实施计划 §14.4 e2e fixture 口径）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 流式 3 段 delta；非流式 content 即其拼接（两口径内容一致，便于核对）。
DELTAS = ["你好", "，这是 ", "fake-upstream。"]

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ─────────────────────────────────────────────────────────────────────────────
# 1) SSE 帧构造 —— OpenAI 无 event 行；Anthropic event + data 两行（§7.3/§7.4）
# ─────────────────────────────────────────────────────────────────────────────


def _sse_oai(chunk: dict) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _sse_anthropic(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# 2) OpenAI 应答体（§7.3）
# ─────────────────────────────────────────────────────────────────────────────


def _openai_json(body: dict) -> dict:
    """非流式固定 JSON；model 回显请求的上游名（§7.2-8 出站名）。"""
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
            "created": int(time.time()), "model": body.get("model"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "".join(DELTAS)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}}


def _openai_stream(body: dict, fail: str | None):
    """流式生成器：role 首包 → 3 段 delta → finish chunk →（可选 usage）→ [DONE]。

    fail=midstream 在首包后直接 return——客户端只见连接收束、终止符永不到达。
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = body.get("model")

    def head(delta: dict, finish: str | None = None) -> dict:
        return {"id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def gen():
        yield _sse_oai(head({"role": "assistant", "content": ""}))       # 首包
        if fail == "midstream":
            return                                                       # 切断（§7.5 器具）
        for text in DELTAS:
            yield _sse_oai(head({"content": text}))
        yield _sse_oai(head({}, finish="stop"))
        if (body.get("stream_options") or {}).get("include_usage"):
            yield _sse_oai({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model, "choices": [],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 9,
                                      "total_tokens": 14}})              # §7.3 末 usage chunk
        yield "data: [DONE]\n\n"

    return gen()


# ─────────────────────────────────────────────────────────────────────────────
# 3) Anthropic 应答体（§7.4 事件序列逐项）
# ─────────────────────────────────────────────────────────────────────────────


def _anthropic_stream(body: dict, fail: str | None):
    """流式生成器：message_start 首包 → 块序列 → message_delta → message_stop。

    fail=midstream 在 message_start 后直接 return（§7.5 器具，同上）。
    """
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    model = body.get("model")

    def gen():
        yield _sse_anthropic("message_start", {
            "type": "message_start",
            "message": {"id": message_id, "type": "message", "role": "assistant",
                        "model": model, "content": [], "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0}}})
        if fail == "midstream":
            return                                                       # 切断（§7.5 器具）
        yield _sse_anthropic("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})
        for text in DELTAS:
            yield _sse_anthropic("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text}})
        yield _sse_anthropic("content_block_stop",
                              {"type": "content_block_stop", "index": 0})
        yield _sse_anthropic("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 9}})
        yield _sse_anthropic("message_stop", {"type": "message_stop"})

    return gen()


# ─────────────────────────────────────────────────────────────────────────────
# 4) create_app —— 双端点 + 公共查询参（latency_ms / fail）
# ─────────────────────────────────────────────────────────────────────────────


def _error_envelope(path: str, fail: str | None) -> dict:
    """按端点协议回错误信封（§7.3 / §7.4 各自形状）。"""
    if path.endswith("/messages"):
        return {"type": "error",
                "error": {"type": "api_error",
                          "message": f"fake-upstream injected failure (fail={fail or '500'})"}}
    return {"error": {"message": f"fake-upstream injected failure (fail={fail or '500'})",
                      "type": "api_error", "code": "fake_upstream_failure"}}


async def _prep(request: Request) -> tuple[dict, str | None, JSONResponse | None]:
    """读 body + 应用公共查询参；返回 (body, fail, 错误响应或 None)。"""
    body = await request.json()
    latency = request.query_params.get("latency_ms")
    if latency:
        await asyncio.sleep(int(latency) / 1000)
    fail = request.query_params.get("fail")
    should_fail = fail == "500" or (fail == "midstream" and not body.get("stream"))
    if should_fail:
        return body, fail, JSONResponse(status_code=500, content=_error_envelope(
            request.url.path, fail))
    return body, fail, None


def create_app() -> FastAPI:
    """构造假上游 FastAPI 应用（docs 关闭：测试底座不需要 OpenAPI 面）。"""
    app = FastAPI(title="smart-router fake-upstream", docs_url=None, redoc_url=None)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body, fail, err = await _prep(request)
        if err is not None:
            return err
        if body.get("stream"):
            return StreamingResponse(_openai_stream(body, fail),
                                     media_type="text/event-stream",
                                     headers=_SSE_HEADERS)
        return _openai_json(body)

    @app.post("/v1/messages")
    async def messages(request: Request):
        body, fail, err = await _prep(request)
        if err is not None:
            return err
        if body.get("stream"):
            return StreamingResponse(_anthropic_stream(body, fail),
                                     media_type="text/event-stream",
                                     headers=_SSE_HEADERS)
        return {"id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message",
                "role": "assistant", "model": body.get("model"),
                "content": [{"type": "text", "text": "".join(DELTAS)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 9}}

    return app


def run(host: str = "127.0.0.1", port: int = 8123) -> None:
    """``sr fake-upstream`` 的实现体（Task 8 CLI 转调；8123 = 实施计划 §14.4）。"""
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port)
