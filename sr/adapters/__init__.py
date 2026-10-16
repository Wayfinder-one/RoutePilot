"""协议适配层公共件：统一中间表示 IR + 流式 chunk 归一 + 上游错误统一 + think 映射。

模块职责（spec §7 协议适配）：
- :class:`IR` —— §7.2-2 「解析请求体 → IR」的产物：一次入站请求的协议无关表示
  （proto/messages/system/tools/params/stream/model_asked/extensions，字段序按
  实施计划 Task 7 接口逐字）。``system`` 从 OpenAI messages[0].role='system' /
  Anthropic 独立 system 字段归一到本字段（§7.3/§7.4 请求体差异的吸收点）。
- :class:`IRChunk` —— §7.6-2 「归一化为内部 chunk」的三要素：content delta 文本 /
  finish / usage。跨协议流式（§7.6）以它为中转：上游 chunk → IRChunk → 客户端
  事件（openai.chunk_to_ir → anthropic.ir_to_events 或反向同理）。
- :class:`UpstreamError` —— 上游错误统一形状 ``(status, message, type)``；
  openai.parse_error / anthropic.parse_error 各自从 §7.3/§7.4 错误信封构造之，
  server 层（Task 8）只认这一个形状。
- ``PARAM_KEYS`` —— 两协议顶层同义采样参数的归一键集（缺失为 None）：
  Anthropic ``stop_sequences`` 归一为 ``stop``，其余同名直取。
- ``THINK_OPENAI`` / ``THINK_ANTHROPIC`` —— think 值域（RouteDecision.think：
  'off' | 'light' | 'deep'，engine §5.1）到协议字段的映射表：
  deep → ``reasoning_effort='high'`` / ``thinking.budget_tokens=8192``；
  light → ``'low'`` / ``2048``；off → 不加字段（表中 None 占位，to_upstream 判空跳过）。
- ``STOP_TO_FINISH`` / ``FINISH_TO_STOP`` —— OpenAI finish_reason ↔ Anthropic
  stop_reason 的**唯一**互查表（I4）：server 的跨协议非流式 shim 与
  anthropic.chunk_to_ir（流式归一路径）共用，不再各抄一份、不再漏用。

依赖方向：openai/anthropic 单向 import 本模块，本模块不 import 工程内任何模块
（协议层无状态；编排、重试、会话归 server 层 Task 8）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# 1) IR / IRChunk —— §7.2-2 与 §7.6-2 的数据形状（字段序按实施计划 Task 7）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class IR:
    """一次入站请求的协议无关表示（§7.2-2）。"""

    proto: str                                     # 'openai' | 'anthropic'（§7.1 按路径判定）
    messages: list                                 # 除 system 外的消息数组（块结构原样保留）
    system: str | None = None                      # 系统提示（两协议归一；None = 未带）
    tools: list | None = None                      # tools 原样透传
    params: dict = field(default_factory=dict)     # 采样参数归一（PARAM_KEYS，缺失为 None）
    stream: bool = False                           # 是否流式
    model_asked: str | None = None                 # 入站 model 名（§7.2-10 响应回写用）
    extensions: dict = field(default_factory=dict) # 协议特有其余字段（stream_options /
                                                   # metadata / thinking…，to_upstream 原样回放）


@dataclass
class IRChunk:
    """一个流式 chunk 的归一三要素（§7.6-2：content delta + usage + finish）。

    ``raw_finish``（收尾微修）是**协议原值旁路**：当上游是 Anthropic 时，
    anthropic.chunk_to_ir 把原始 ``stop_reason`` 存这里，anthropic.ir_to_events
    优先用它——同协议回环（anthropic→anthropic）因此能保住 ``stop_sequence``
    与 ``end_turn`` 的区别（只查 ``finish`` 会把两者都折成 "stop" 再映射回
    end_turn）。OpenAI 来源的 chunk 该字段恒 None，跨协议路径仍由
    ``FINISH_TO_STOP`` 决定，行为不变。
    """

    text: str | None = None      # content delta 文本（None = 本 chunk 无文本）
    finish: str | None = None    # finish_reason（OpenAI）/ stop_reason（Anthropic）
    usage: dict | None = None    # usage 原样（§7.3 include_usage 时末 chunk 带）
    raw_finish: str | None = None  # Anthropic stop_reason 原值旁路（同协议回环用）
    input_tokens: int | None = None  # Anthropic message_start 的 input_tokens
                                     # （backlog：同协议回环曾恒 0——上游 message_start
                                     # 被丢、我方合成值硬编码 0；此字段把它带过去）


@dataclass
class IRResponse:
    """一次**非流式**响应的协议无关表示（A3：跨协议经 IR 中转，不再两两互转）。

    由各适配器的 ``response_to_ir`` 从上游响应产出、``ir_to_response`` 渲染为客户端
    响应。M1 跨协议面是纯文本（tool_calls 等复杂面留 M2），故只需 text/finish/usage。

    ``finish`` 是 OpenAI finish_reason 口径（跨协议统一）；``raw_finish`` 是协议原生
    终止值（Anthropic stop_reason），同协议回环/原生渲染优先用它。``usage`` 原样
    （各适配器渲染时按需归一键名）。
    """

    text: str = ""
    finish: str | None = None
    raw_finish: str | None = None
    usage: dict = field(default_factory=dict)
    model_asked: str | None = None
    id: str | None = None


class UpstreamError(Exception):
    """上游错误统一形状（Task 7 接口裁定）：server 层只认 (status, message, type)。"""

    def __init__(self, status: int, message: str, type: str):
        self.status = status
        self.message = message
        self.type = type
        super().__init__(f"[{status}] {type}: {message}")


# ─────────────────────────────────────────────────────────────────────────────
# 2) 采样参数归一键 + think 映射常量（简报接口：映射表常量写在本模块）
# ─────────────────────────────────────────────────────────────────────────────

# 两协议顶层同义采样参数的归一键集；缺失为 None。Anthropic 的 stop_sequences
# 归一为 stop（§7.4 请求体 ↔ §7.3 请求体同义字段），其余同名直取。
PARAM_KEYS = ("max_tokens", "temperature", "top_p", "stop", "top_k")

# think 值域 → OpenAI reasoning_effort（deep='high' / light='low' / off=不加字段）
THINK_OPENAI = {"deep": "high", "light": "low", "off": None}
# think 值域 → Anthropic thinking.budget_tokens（deep=8192 / light=2048 / off=不加字段）
THINK_ANTHROPIC = {"deep": 8192, "light": 2048, "off": None}


# ─────────────────────────────────────────────────────────────────────────────
# 2b) 终止原因互查表（I4：**唯一导出表**，server 与 adapters 共用）
# ─────────────────────────────────────────────────────────────────────────────
# OpenAI finish_reason ↔ Anthropic stop_reason 是两条协议里同义而拼写不同的枚举。
# 旧实现把这张表抄了两份（server._STOP_TO_FINISH / anthropic._STOP_REASON），且
# 流式路径**漏用**——Anthropic 上游 → OpenAI 客户端的流式终止帧直接把 end_turn /
# max_tokens / tool_use / stop_sequence 原样吐给 OpenAI 客户端（终审实测）；非流式
# shim 反而是对的。消重后两边都从这里取，杜绝再次漂移。
#   语义损失声明：OpenAI 侧无「自然结束 vs stop 序列」的区分，Anthropic 的
#   end_turn 与 stop_sequence 都映射到 "stop"（信息丢失但不误报终止类型）；
#   枚举外取值一律 None（不猜语义）。
STOP_TO_FINISH: dict[str, str] = {
    "end_turn": "stop", "max_tokens": "length",
    "tool_use": "tool_calls", "stop_sequence": "stop",
}
FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn", "length": "max_tokens",
    "tool_calls": "tool_use", "function_call": "tool_use",
}


# ─────────────────────────────────────────────────────────────────────────────
# 3) parse_error 共用内核：payload 容错为 dict（str/bytes 先试 JSON，失败按纯文本）
# ─────────────────────────────────────────────────────────────────────────────


def error_payload(payload) -> dict:
    """上游错误响应体 → dict（parse_error 两协议共用）。

    上游 5xx 的 body 不保证是合法 JSON（反代截断、纯文本栈都常见），解析失败时
    降级为 ``{"error": {"message": <原文>}}``，让 message 至少保住原文供诊断。
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {"error": {"message": payload}}
    return payload if isinstance(payload, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# 3b) content 归一（两协议同构公共件，审核 §4 清单：原 openai/anthropic 逐字重复）
# ─────────────────────────────────────────────────────────────────────────────


def text_of(content) -> str:
    """content → 文本：str 原样；块数组取 ``type='text'`` 块的 text 拼接。

    非文本块（image_url / input_audio / tool_use / tool_result 等）不产文本，跳过
    ——M1 主文本只服务打标与分流（§7.2-3），多模态内容本就不进标签器。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(blk.get("text", "") for blk in content
                       if isinstance(blk, dict) and blk.get("type", "text") == "text")
    return str(content)
