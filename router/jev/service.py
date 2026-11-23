"""多 JEV 服务的公开配置模型。

这里只保存可写入画布快照与运行时的非密钥字段：服务 ``id``、连接地址、模型、
``key_env``、超时与提示词模板，以及属于同一服务的输入/上下文窗口。真实密钥
永远留在 ``jev_services`` 表或 ``key_env`` 指向的环境变量里，不出现在
``snapshot_dict()`` 或任何图 JSON 中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from router.jev.config import (
    DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings,
)
from router.jev.window import JevWindow

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class JevService:
    id: str
    base_url: str
    model: str
    key_env: str = ""
    timeout_seconds: float = 10.0
    instructions: str = DEFAULT_INSTRUCTIONS
    state_template: str = DEFAULT_STATE_TEMPLATE
    input_tokens: int = 1024
    context_tokens: int = 4096

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID_RE.fullmatch(self.id):
            raise ValueError("invalid JEV service id")
        if not isinstance(self.key_env, str) or (
                self.key_env and not _ENV_RE.fullmatch(self.key_env)):
            raise ValueError("invalid JEV key_env")
        settings = self.settings()
        object.__setattr__(self, "base_url", settings.base_url)
        object.__setattr__(self, "timeout_seconds", settings.timeout_seconds)
        JevWindow(self.input_tokens, self.context_tokens)

    def settings(self, *, api_key: str = "") -> JevSettings:
        return JevSettings(
            base_url=self.base_url, model=self.model, api_key=api_key,
            timeout_seconds=self.timeout_seconds,
            instructions=self.instructions, state_template=self.state_template,
        )

    def snapshot_dict(self) -> dict:
        return {
            "id": self.id, "base_url": self.base_url, "model": self.model,
            "key_env": self.key_env, "timeout_seconds": self.timeout_seconds,
            "instructions": self.instructions, "state_template": self.state_template,
        }

    def window_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "context_tokens": self.context_tokens}
