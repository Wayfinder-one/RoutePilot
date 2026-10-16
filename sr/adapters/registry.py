"""协议注册表：proto → 同构适配器（spec §7.1 按路径判定 proto）。

A3（audit §3）：注册表从 sr/server.py 的 ``ADAPTERS`` 常量迁入本模块，成为 server /
upstream / cross 共用的**同一对象**——加第三个协议时只需在此登记，路由/上游/跨协议
三处的 proto→适配器分派不再各写一份。

每个适配器（sr/adapters/openai.py、anthropic.py）导出同构七件套 + A3 扩展件：
parse / main_text / to_upstream / from_upstream / chunk_to_ir / ir_to_chunk(或
ir_to_events) / parse_error，外加 ``response_to_ir`` / ``ir_to_response``
（非流式跨协议经 IR 中转，见 sr/adapters/cross.py）。
"""

from __future__ import annotations

from sr.adapters import anthropic, openai

#: proto 键 → 适配器模块（server / upstream / cross 共用）
ADAPTERS = {"openai": openai, "anthropic": anthropic}
