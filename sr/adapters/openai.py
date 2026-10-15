"""OpenAI Chat 协议适配（spec §7.3 + §7.2-2/3/9/10 + §7.6）：七函数无状态。

- :func:`parse` —— 请求体 → IR（§7.2-2）：system 从 messages 提取（§7.3 请求体
  ``system`` 作为 ``messages[0].role='system'``），采样参数按 PARAM_KEYS 归一
  （缺失为 None），其余顶层字段（stream_options/user/…）收进 extensions。
- :func:`main_text` —— 最后一条 role=user 消息的文本（§7.2-3）；content 为 str
  原样，块数组取 text 块拼接（兼容多模态 content parts 形状）。
- :func:`to_upstream` —— IR → 上游请求体（§7.2-9）：换出站模型名 + think 映射
  （reasoning_effort，THINK_OPENAI）+ params/extensions 原样透传。
- :func:`from_upstream` —— 上游响应 → 客户端响应：``model`` 回写入站名
  （§7.2-10 入站名回写），其余字段原样。
- :func:`chunk_to_ir` / :func:`ir_to_chunk` —— 流式 chunk ↔ IRChunk（§7.3 流式：
  ``data: {chunk}\\n\\n`` … ``data: [DONE]\\n\\n``；include_usage 时末 chunk 带
  usage，形状为 ``choices: []`` 的纯 usage chunk——OpenAI 官方口径）。
- :func:`parse_error` —— §7.3 错误信封 ``{"error":{"message","type","code"}}``
  → 统一 :class:`~sr.adapters.UpstreamError`。

依赖方向：仅 import sr.adapters（IR/IRChunk/UpstreamError/PARAM_KEYS/
THINK_OPENAI/error_payload），无工程内其他依赖。
"""

from __future__ import annotations

import time

from sr.adapters import (PARAM_KEYS, THINK_OPENAI, IR, IRChunk, UpstreamError,
                         error_payload)

# ir_to_chunk 产出的 chunk 公共头（id/created 非协议语义字段，固定值即可）
_CHUNK_ID = "chatcmpl-smart-router"


# ─────────────────────────────────────────────────────────────────────────────
# 1) content 归一：str 原样；块数组取 text 块拼接（两协议同构，本模块私有）
# ─────────────────────────────────────────────────────────────────────────────


def _text_of(content) -> str:
    """content → 文本：str 原样；块数组取 ``type='text'`` 块的 text 拼接。

    非文本块（image_url / input_audio 等）不产文本，跳过——M1 主文本只服务
    打标与分流（§7.2-3），多模态内容本就不进标签器。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(blk.get("text", "") for blk in content
                       if isinstance(blk, dict) and blk.get("type", "text") == "text")
    return str(content)


# ─────────────────────────────────────────────────────────────────────────────
# 2) parse / main_text —— §7.2-2/3
# ─────────────────────────────────────────────────────────────────────────────


def parse(body: dict) -> IR:
    """OpenAI 请求体 → IR（§7.2-2）：system 出 messages，采样参数归一。"""
    system_parts: list[str] = []
    messages: list = []
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "system":
            text = _text_of(m.get("content"))
            if text:
                system_parts.append(text)      # 多条 system 换行拼接，不静默丢弃
        else:
            messages.append(m)
    params = {k: body.get(k) for k in PARAM_KEYS}          # 缺失为 None
    known = set(PARAM_KEYS) | {"model", "messages", "stream", "tools"}
    extensions = {k: v for k, v in body.items() if k not in known}
    return IR(proto="openai",
              messages=messages,
              system="\n".join(system_parts) or None,
              tools=body.get("tools"),
              params=params,
              stream=bool(body.get("stream")),
              model_asked=body.get("model"),
              extensions=extensions)


def main_text(ir: IR) -> str:
    """最后一条 role=user 消息的文本（§7.2-3）；无 user 消息回空串。"""
    for m in reversed(ir.messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return _text_of(m.get("content"))
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 3) to_upstream / from_upstream —— §7.2-9 序列化 + §7.2-10 入站名回写
# ─────────────────────────────────────────────────────────────────────────────


def to_upstream(ir: IR, model_upstream: str, think: str | None) -> dict:
    """IR → OpenAI 上游请求体：换出站名 + think→reasoning_effort + 其余透传。

    - system 还原为 ``messages[0].role='system'``（§7.3 请求体口径）；
    - params 缺失（None）不出现在出站体——缺失与显式 null 语义不同，不替上游做主；
    - think 值域外抛 ValueError 不静默兜底（对齐 §6.9「值空间外拒绝」口径）。
    """
    messages = list(ir.messages)
    if ir.system is not None:
        messages.insert(0, {"role": "system", "content": ir.system})
    body: dict = {"model": model_upstream, "stream": ir.stream, "messages": messages}
    body.update({k: v for k, v in ir.params.items() if v is not None})
    body.update(ir.extensions)
    if think is not None and think != "off":
        if think not in THINK_OPENAI:
            raise ValueError(f"unknown think: {think!r}")
        body["reasoning_effort"] = THINK_OPENAI[think]
    return body


def from_upstream(resp: dict, model_asked: str | None) -> dict:
    """上游响应 → 客户端响应：``model`` 回写入站名（§7.2-10），其余原样。"""
    out = dict(resp)
    out["model"] = model_asked
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 4) chunk_to_ir / ir_to_chunk —— §7.3 流式 + §7.6-2 归一
# ─────────────────────────────────────────────────────────────────────────────


def chunk_to_ir(chunk: dict) -> IRChunk:
    """OpenAI SSE chunk → IRChunk：delta 文本 / finish_reason / usage 三要素。

    ``choices`` 为空数组（include_usage 的纯 usage 末 chunk）或缺 delta 时各要素
    取 None——真实上游（含 OpenAI 官方）的末 usage chunk 即此形状。
    """
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    return IRChunk(text=delta.get("content"),
                   finish=choice.get("finish_reason"),
                   usage=chunk.get("usage"))


def ir_to_chunk(ic: IRChunk, model_asked: str | None) -> dict:
    """IRChunk → OpenAI SSE chunk：含 include_usage 时末 chunk 带 usage（§7.3）。

    纯 usage chunk（无文本无 finish）产出 ``choices: []``——与 OpenAI 官方
    include_usage 末 chunk 形状一致；其余 chunk 带 ``delta``/``finish_reason``。
    """
    if ic.text is None and ic.finish is None:
        choices: list = []                                 # 纯 usage chunk（官方形状）
    else:
        delta = {"content": ic.text} if ic.text is not None else {}
        choices = [{"index": 0, "delta": delta, "finish_reason": ic.finish}]
    chunk = {"id": _CHUNK_ID, "object": "chat.completion.chunk",
             "created": int(time.time()), "model": model_asked, "choices": choices}
    if ic.usage is not None:
        chunk["usage"] = ic.usage
    return chunk


# ─────────────────────────────────────────────────────────────────────────────
# 5) parse_error —— §7.3 错误信封 → UpstreamError
# ─────────────────────────────────────────────────────────────────────────────


def parse_error(status: int, payload) -> UpstreamError:
    """§7.3 信封 ``{"error":{"message","type","code"}}`` → UpstreamError。

    type 取信封 ``type``，缺失退 ``code``（部分供应商只给 code，如实测火山 Ark
    只回 ``code: AuthenticationError``），再缺失兜 ``api_error``（5xx 语义）。
    """
    err = error_payload(payload).get("error") or {}
    if not isinstance(err, dict):
        err = {}
    return UpstreamError(status=status,
                         message=str(err.get("message", "")),
                         type=err.get("type") or err.get("code") or "api_error")
