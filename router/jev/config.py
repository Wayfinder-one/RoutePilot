from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from string import Formatter
from urllib.parse import urlsplit

from router.store.repositories import _now_iso, _tx

DEFAULT_INSTRUCTIONS = "你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明，选择最合适处理这条请求的模型。"
DEFAULT_STATE_TEMPLATE = "【待判断的用户请求】\n{request}\n\n【任务】请选择最合适处理这条请求的模型。"


@dataclass(frozen=True)
class JevSettings:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 10.0
    instructions: str = DEFAULT_INSTRUCTIONS
    state_template: str = DEFAULT_STATE_TEMPLATE

    def __post_init__(self):
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be an http/https URL")
        base_url = self.base_url.strip().rstrip("/")
        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise ValueError("base_url must be an http/https URL") from exc
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an http/https URL without query or fragment")
        object.__setattr__(self, "base_url", base_url)
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be nonempty")
        if not isinstance(self.api_key, str):
            raise ValueError("api_key must be a string")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be in [0.1,120]")
        timeout = float(self.timeout_seconds)
        if not 0.1 <= timeout <= 120:
            raise ValueError("timeout_seconds must be in [0.1,120]")
        object.__setattr__(self, "timeout_seconds", timeout)
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("instructions must be nonempty")
        if not isinstance(self.state_template, str):
            raise ValueError("state_template must contain {request}")
        try:
            fields = [field for _, field, _, _ in Formatter().parse(self.state_template) if field is not None]
        except ValueError as exc:
            raise ValueError("state_template has invalid format fields") from exc
        if fields != ["request"]:
            raise ValueError("state_template must contain exactly one {request}")

    def public_dict(self, *, env_key_present: bool) -> dict:
        key_source = "stored" if self.api_key else "environment" if env_key_present else "none"
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "instructions": self.instructions,
            "state_template": self.state_template,
            "key_present": key_source != "none",
            "key_source": key_source,
        }

    def behavior_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "instructions": self.instructions,
            "state_template": self.state_template,
        }


class JevConfigRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> JevSettings | None:
        row = self.conn.execute("SELECT * FROM jev_config WHERE id = 1").fetchone()
        if row is None:
            return None
        return JevSettings(
            base_url=row["base_url"],
            model=row["model"],
            api_key=row["api_key"],
            timeout_seconds=row["timeout_seconds"],
            instructions=row["instructions"],
            state_template=row["state_template"],
        )

    def save(self, settings: JevSettings) -> None:
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO jev_config (id, base_url, model, api_key, timeout_seconds, instructions, state_template, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "base_url = excluded.base_url, model = excluded.model, api_key = excluded.api_key, "
                "timeout_seconds = excluded.timeout_seconds, instructions = excluded.instructions, "
                "state_template = excluded.state_template, updated_at = excluded.updated_at",
                (
                    settings.base_url,
                    settings.model,
                    settings.api_key,
                    settings.timeout_seconds,
                    settings.instructions,
                    settings.state_template,
                    _now_iso(),
                ),
            )
