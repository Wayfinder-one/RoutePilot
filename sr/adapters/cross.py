"""跨协议**非流式**响应转换：一律经 IRResponse 中转（A3，audit §3）。

旧实现（sr/server.py ``_from_upstream``）对「上游协议 × 客户端协议」两两手写互转，
加第三个协议要写 O(n²) 条分支。改为：任何跨协议对都 `source.response_to_ir` →
`client.ir_to_response`，只有「同协议」走各自 ``from_upstream`` 原样回写（§7.2-10）。
加协议时只需各适配器实现 ``response_to_ir`` / ``ir_to_response``，本模块零改动。

M1 跨协议面是**纯文本**（tool_calls 等复杂面留 M2）；行为与旧两两 shim 逐字一致。
"""

from __future__ import annotations

from sr.adapters.registry import ADAPTERS


def from_upstream(client_proto: str, up_proto: str, resp: dict,
                  model_asked: str | None) -> dict:
    """上游响应 → 客户端响应：同协议原样回写 model；跨协议经 IRResponse 中转。

    :param client_proto: 客户端协议（openai / anthropic）。
    :param up_proto: 上游协议（openai / anthropic）。
    :param resp: 上游响应体（已 JSON 解析）。
    :param model_asked: 入站 model 名（§7.2-10 回写用）。
    """
    if client_proto == up_proto:
        return ADAPTERS[client_proto].from_upstream(resp, model_asked)
    ir_resp = ADAPTERS[up_proto].response_to_ir(resp)
    return ADAPTERS[client_proto].ir_to_response(ir_resp, model_asked)
