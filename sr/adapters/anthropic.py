"""Anthropic Messages 协议适配（spec §7.4 + §7.2-2/3/9/10 + §7.6 跨协议）：七件套。

- :func:`parse` —— 请求体 → IR（§7.2-2）：``system`` 独立字段（str 或块数组，
  归一为 str）；``stop_sequences`` 归一为 ``stop``；metadata/thinking 等收进
  extensions。
- :func:`main_text` —— 最后一条 role=user 消息：content 块数组取 text 块拼接
  （§7.2-3；content 为 str 时原样）。
- :func:`to_upstream` —— IR → 上游请求体（§7.2-9）：换出站名 + stop 还原为
  ``stop_sequences`` + think 映射（``thinking: {"type":"enabled",
  "budget_tokens": N}``，THINK_ANTHROPIC）+ 其余透传。
- :func:`from_upstream` —— 上游响应 → 客户端响应：``model`` 回写入站名
  （§7.2-10），其余原样。
- :func:`chunk_to_ir` —— Anthropic 流事件 → IRChunk：``content_block_delta`` 的
  text_delta / ``message_delta`` 的 stop_reason + usage（§7.6-2 反向同理）；
  stop_reason 双写——``finish`` 归一到 OpenAI 口径，``raw_finish`` 存协议原值
  （同协议回环的忠实放行）。
- :class:`StreamState` + :func:`ir_to_events` —— IRChunk → Anthropic SSE 事件块
  列表（§7.4 事件序列，§7.6-3 重发）：message_start → (content_block_start →
  content_block_delta\\* → content_block_stop)\\* → message_delta(usage/stop_reason)
  → message_stop；跨 chunk 状态（content_block_index/started/closed/stop_reason）
  由 StreamState 维护（closed 为终止闩，终止段只发一次）。stop_reason 优先
  ``raw_finish``（同协议），无原值时查共享表（跨协议）。
- :func:`parse_error` —— §7.4 错误信封 ``{"type":"error","error":{"type",
  "message"}}`` → 统一 :class:`~sr.adapters.UpstreamError`。

依赖方向：仅 import sr.adapters，无工程内其他依赖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sr.adapters import (FINISH_TO_STOP, PARAM_KEYS, STOP_TO_FINISH,
                         THINK_ANTHROPIC, IR, IRChunk, UpstreamError,
                         error_payload, text_of)

# 跨协议 stop_reason 归一直接用 sr.adapters 的 FINISH_TO_STOP（I4 唯一表；
# 审核 §4 清单：原 _STOP_REASON 兼容别名已删，未收录值映射为 None——不猜语义，
# 客户端拿到 null 自行判断）。


def _sse(event: str, data: dict) -> str:
    """一个完整 SSE 块：``event: <名>\\n`` 行 + ``data: <json>\\n`` 行 + 空行收尾。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# 2) parse / main_text —— §7.2-2/3
# ─────────────────────────────────────────────────────────────────────────────


def parse(body: dict) -> IR:
    """Anthropic 请求体 → IR（§7.2-2）：system 独立字段，stop_sequences→stop。"""
    params = dict.fromkeys(PARAM_KEYS)                     # 缺失为 None
    params.update(max_tokens=body.get("max_tokens"),
                  temperature=body.get("temperature"),
                  top_p=body.get("top_p"),
                  stop=body.get("stop_sequences"),         # 同义字段归一（§7.4）
                  top_k=body.get("top_k"))
    known = set(PARAM_KEYS) | {"model", "messages", "system", "stream", "tools",
                               "stop_sequences"}
    extensions = {k: v for k, v in body.items() if k not in known}
    system = text_of(body.get("system")) or None
    return IR(proto="anthropic",
              messages=body.get("messages") or [],
              system=system,
              tools=body.get("tools"),
              params=params,
              stream=bool(body.get("stream")),
              model_asked=body.get("model"),
              extensions=extensions)


def main_text(ir: IR) -> str:
    """最后一条 role=user 消息：content 块数组取 text 块拼接（§7.2-3）。"""
    for m in reversed(ir.messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return text_of(m.get("content"))
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 3) to_upstream / from_upstream —— §7.2-9 序列化 + §7.2-10 入站名回写
# ─────────────────────────────────────────────────────────────────────────────


def to_upstream(ir: IR, model_upstream: str, think: str | None) -> dict:
    """IR → Anthropic 上游请求体：换出站名 + think→thinking.budget_tokens + 透传。

    - ``max_tokens`` 缺失不出站（Anthropic 必填，缺省策略归 server 层 Task 8，
      协议层不替上游做主）；
    - stop 还原为 ``stop_sequences``；extensions（metadata/thinking…）原样回放；
    - think 值域外抛 ValueError 不静默兜底（对齐 §6.9 口径）。
    """
    body: dict = {"model": model_upstream, "stream": ir.stream,
                  "messages": list(ir.messages)}
    if ir.system is not None:
        body["system"] = ir.system
    if ir.tools is not None:
        body["tools"] = ir.tools
    body.update({k: v for k, v in ir.params.items() if v is not None
                 and k != "stop"})
    if ir.params.get("stop") is not None:
        body["stop_sequences"] = ir.params["stop"]
    body.update(ir.extensions)
    if think is not None and think != "off":
        if think not in THINK_ANTHROPIC:
            raise ValueError(f"unknown think: {think!r}")
        body["thinking"] = {"type": "enabled", "budget_tokens": THINK_ANTHROPIC[think]}
    return body


def from_upstream(resp: dict, model_asked: str | None) -> dict:
    """上游响应 → 客户端响应：``model`` 回写入站名（§7.2-10），其余原样。"""
    out = dict(resp)
    out["model"] = model_asked
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 4) chunk_to_ir —— Anthropic 流事件 → IRChunk（§7.6-2 反向）
# ─────────────────────────────────────────────────────────────────────────────


def chunk_to_ir(chunk: dict) -> IRChunk:
    """Anthropic 流事件 → IRChunk：text_delta 文本 / message_delta 的终止与用量。

    message_start / content_block_start / content_block_stop / ping 不产三要素，
    回空 IRChunk（文本收集只需盯 ``content_block_delta`` 与 ``message_delta``）。

    **I4**：IRChunk.finish 的口径是 OpenAI finish_reason（它要经 openai.ir_to_chunk
    直接重发给 OpenAI 客户端），故这里就把 Anthropic 的 stop_reason 归一
    （``STOP_TO_FINISH``）——旧实现原样透传，OpenAI 客户端会收到 end_turn /
    max_tokens / tool_use / stop_sequence。

    **同协议原值旁路**（收尾微修）：stop_reason 的原值同时存进
    ``IRChunk.raw_finish``，供 :func:`ir_to_events` 在 anthropic→anthropic 回环里
    原样放行——归一是**有损**的（stop_sequence 与 end_turn 都折到 "stop"），
    回程若只查 finish 就会把 stop_sequence 误报成 end_turn、refusal/pause_turn
    丢成 null。非 message_delta 事件不带原值（默认 None）。
    """
    kind = chunk.get("type")
    if kind == "content_block_delta":
        delta = chunk.get("delta") or {}
        text = delta.get("text") if delta.get("type", "text_delta") == "text_delta" else None
        return IRChunk(text=text)
    if kind == "message_delta":
        delta = chunk.get("delta") or {}
        raw = delta.get("stop_reason")
        return IRChunk(finish=STOP_TO_FINISH.get(raw),
                       usage=chunk.get("usage"),
                       raw_finish=raw)
    return IRChunk()


# ─────────────────────────────────────────────────────────────────────────────
# 5) StreamState + ir_to_events —— §7.4 事件序列 + §7.6-3 重发
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StreamState:
    """跨 chunk 流式状态（简报接口三字段；§7.6-3 维护 content_block_index/stop_reason）。

    ``content_block_index = -1`` 表示文本块尚未开（content_block_start 未发）；
    ``started`` 表示 message_start 已发；``closed`` 为终止闩——终止段
    （message_delta + message_stop）只随首次终止触发产出一次，其后任何 chunk
    幂等跳过（spec §7.4 以单个 message_stop 结束；OpenAI include_usage 线序里
    finish chunk 与纯 usage chunk（choices:[]）前后脚两包，无闩会双终止）。
    ``stop_reason`` 由 finish chunk 先行锁存、随 message_delta 发出（取值优先
    IRChunk.raw_finish，见 :func:`_stop_reason`）。M1 只发单
    文本块，index 恒 0——字段仍按接口保留，多块（tool_use 等）留 M2 扩展。
    """

    content_block_index: int = -1   # 当前内容块序号；-1 = content_block_start 未发
    started: bool = False           # message_start 是否已发
    closed: bool = False            # 终止闩：message_delta+message_stop 是否已发
    stop_reason: str | None = None  # 终止原因（finish 锁存，message_delta 时发出）


def ir_to_events(ic: IRChunk, state: StreamState,
                 model: str | None = None) -> list[str]:
    """IRChunk → Anthropic SSE 事件块列表（§7.4 顺序，§7.6-3 按序重发）。

    每个元素是完整 SSE 块字符串（``event: <名>\\n`` + ``data: <json>\\n\\n``）。
    产出规则：

    - 首个 chunk 先补 ``message_start``（state.started 惰性触发一次）；
    - 有文本 delta：块未开先补 ``content_block_start``，再发 ``content_block_delta``；
    - **终止段（message_delta + message_stop）只发一次**（state.closed 闩，closed 后
      任何 chunk 不再产终止事件，spec §7.4 以单个 message_stop 结束）。触发方：
      **usage 到达**即收尾（content_block_stop → message_delta → message_stop）——
      usage 单独触发是硬要求，OpenAI include_usage 的末 chunk 无 finish_reason
      （简报测试第三包即此形状），不认 usage 会永远等不到终止事件；message_delta
      的 usage 数字必须落在这一包（§7.4 message_delta 携带最终用量）。
      **finish 先到**（include_usage 真实线序：finish chunk → 纯 usage chunk
      前后脚两包）：只锁存 stop_reason + 关文本块（content_block_stop），终止段
      留给随后的 usage chunk，从而 message_delta 同时带上 stop_reason 与 usage
      数字——若在 finish 包就收尾，usage 尚未到达，数字会被后到的 usage 包顶丢
      或产出双终止。finish 与 usage 同包到达（部分上游合并形状）时一次收尾兼顾
      两者。
    - **stop_reason 取值**（收尾微修）：优先 :attr:`IRChunk.raw_finish`（Anthropic
      上游原值，同协议回环原样放行——stop_sequence 不再被归一成 "stop" 后回程
      误报 end_turn）；``raw_finish`` 为 None（来源是 OpenAI chunk）时仍由共享表
      ``FINISH_TO_STOP`` 映射，跨协议行为不变。锁存/收尾的触发条件同样认
      ``raw_finish``（Anthropic 的 pause_turn/refusal 映射不到 finish，但原值要发）。

    ``model`` 为可选入站名（Task 8 传 §7.2-10 回写用的 model_asked），仅写进
    message_start 的 message.model；不传回空串。§7.4 的 15s ``ping`` 心跳是
    server 层按墙钟注入的（Task 8），不属于单 chunk 转换，此处不产。
    """
    evts: list[str] = []
    if not state.started:
        state.started = True
        evts.append(_sse("message_start", {
            "type": "message_start",
            "message": {"id": "msg_smart-router", "type": "message",
                        "role": "assistant", "model": model or "",
                        "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}}}))
    if ic.text:                                        # 空 delta 不产事件
        if state.content_block_index < 0:
            state.content_block_index = 0
            evts.append(_sse("content_block_start", {
                "type": "content_block_start", "index": state.content_block_index,
                "content_block": {"type": "text", "text": ""}}))
        evts.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": state.content_block_index,
            "delta": {"type": "text_delta", "text": ic.text}}))
    if (ic.finish is not None or ic.raw_finish is not None) and not state.closed:
        # finish 先到（include_usage 线序首包）：锁 stop_reason + 关文本块；
        # 终止段不在此发——usage 数字在后到的 usage 包里，message_delta 必须带上。
        if state.content_block_index >= 0:
            evts.append(_sse("content_block_stop", {
                "type": "content_block_stop", "index": state.content_block_index}))
            state.content_block_index = -1
        state.stop_reason = _stop_reason(ic)
    if ic.usage is not None and not state.closed:
        state.closed = True                                # 终止闩：终止段只发一次
        if state.content_block_index >= 0:
            evts.append(_sse("content_block_stop", {
                "type": "content_block_stop", "index": state.content_block_index}))
            state.content_block_index = -1
        if ic.finish or ic.raw_finish:                     # 同包带终止：就地锁存
            state.stop_reason = _stop_reason(ic)
        evts.append(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": state.stop_reason, "stop_sequence": None},
            "usage": _usage(ic.usage)}))
        evts.append(_sse("message_stop", {"type": "message_stop"}))
    return evts


def _stop_reason(ic: IRChunk) -> str | None:
    """IRChunk → Anthropic ``stop_reason``：有协议原值优先原样，否则查共享表。

    同协议回环（anthropic→anthropic）走原值旁路，保住 ``stop_sequence`` /
    ``end_turn`` 的区别与枚举外原值；``raw_finish`` 为 None（来源是 OpenAI
    chunk）时按 §7.4 经 :data:`FINISH_TO_STOP` 映射，枚举外取值回 None（不猜）。
    """
    if ic.raw_finish is not None:
        return ic.raw_finish
    return FINISH_TO_STOP.get(ic.finish)


def _usage(usage: dict | None) -> dict:
    """usage 字段名归一：OpenAI prompt/completion_tokens → Anthropic input/output。

    Anthropic 原生形状原样通过；其余键（如 total_tokens）保留。
    """
    u = dict(usage or {})
    if "prompt_tokens" in u:
        u["input_tokens"] = u.pop("prompt_tokens")
    if "completion_tokens" in u:
        u["output_tokens"] = u.pop("completion_tokens")
    return u


# ─────────────────────────────────────────────────────────────────────────────
# 6) parse_error —— §7.4 错误信封 → UpstreamError
# ─────────────────────────────────────────────────────────────────────────────


def parse_error(status: int, payload) -> UpstreamError:
    """§7.4 信封 ``{"type":"error","error":{"type","message"}}`` → UpstreamError。

    type 缺失兜 ``api_error``（§7.4 示例值，5xx 语义）；body 非法 JSON 时由
    error_payload 降级保住原文。
    """
    data = error_payload(payload)
    err = data.get("error") or {}
    if not isinstance(err, dict):
        err = {}
    return UpstreamError(status=status,
                         message=str(err.get("message", "")),
                         type=err.get("type") or "api_error")
