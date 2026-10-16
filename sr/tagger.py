"""打标器协议层（A2，审核报告 §3-A2）：把打标从「被 import 的模块级函数」
升级为「注入的 async 协议对象」——M2 checkpoint 模式（torch 一次前向产出
9 个模型标签、CUDA/GPU、§4.3 微批本质是 async、嵌入头回传 768-d 向量写
sessions.last_vec）的前置接口，现在落地成本最低。

- :class:`TagRequest`：一次打标的输入包。text/headers/n_messages/now/
  last_msg 与 tag_all 形参一一对应（now 由调用方注入，可测）；
  last_vec 是 M2 checkpoint 的嵌入比对输入（heuristic 忽略）；
  context_thresholds 是 M8 接线成果（None = 打标器内部用模块默认切点）。
- :class:`TagResult`：signals 形状与 sr.tags.tag_all 返回**完全一致**
  （engine/server 消费端零改动）；vec 是 M2 嵌入向量回传通道，
  heuristic 恒 None（server 侧非 None 时写 sessions.last_vec）。
- :class:`Tagger`：structural Protocol。name 进 x-sr-tagger 诊断头
  （'heuristic' | 'checkpoint'）；tag **必须 async**——M2 的 torch 前向
  必须 await 化，签名现在就是 async，哪怕 heuristic 内部纯计算。
- :class:`HeuristicTagger`：M1 启发式打标器，name="heuristic"；内部一行
  不改地转调 tags.tag_all（tags.py 纯函数是各打标器的内部实现，本类只是
  async 外壳 + 注入签名）。

M2 接线点：sr serve --checkpoint 的 CheckpointTagger 实现本协议即可被
create_app 注入；返回 vec 时 server 写 sessions.last_vec，落库即生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sr import tags


@dataclass
class TagRequest:
    """一次打标的输入（字段与 tags.tag_all 形参一一对应，纯搬运）。"""

    text: str            # 主文本（最后一条 user 消息）
    headers: dict        # 请求头（tenant 等规则标签用）
    n_messages: int      # turns 规则标签用
    now: datetime        # time_window 用；调用方注入，可测
    last_msg: str | None  # similarity/reask 用
    last_vec: list[float] | None = None    # checkpoint 嵌入对比用（M2）；heuristic 忽略
    context_thresholds: list[int] | None = None  # M8 接线成果，None 用模块默认


@dataclass
class TagResult:
    """一次打标的产物：signals 与 tags.tag_all 返回同形状；vec 恒 None（heuristic）。"""

    signals: dict                            # {tag_key: {"value","confidence","is_rule"}}
    vec: list[float] | None = None           # M2 嵌入向量回传通道；heuristic 恒 None


class Tagger(Protocol):
    """打标器协议：M1 HeuristicTagger 与 M2 CheckpointTagger 同一签名。

    name 进 x-sr-tagger 响应头；tag 必须 async——M2 的 torch 前向
    （§4.3 微批本质是 async）必须 await 化，签名先到位。
    """

    name: str

    async def tag(self, req: TagRequest) -> TagResult: ...


class HeuristicTagger:
    """M1 启发式打标器：async 外壳转调 tags.tag_all（纯函数一行不动）。"""

    name = "heuristic"

    async def tag(self, req: TagRequest) -> TagResult:
        return TagResult(signals=tags.tag_all(
            req.text, req.headers, req.n_messages, req.now,
            last_msg=req.last_msg,
            context_thresholds=req.context_thresholds))
