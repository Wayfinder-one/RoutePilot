"""``jev_config`` 单行配置表的 SQLite 存储。

领域 DTO（:class:`router.jev.config.JevSettings`）与默认文案保持纯数据；本模块
是唯一持有 SQL 与连接的地方，因此 ``router.jev`` 包不再依赖 ``sqlite3``。
"""
from __future__ import annotations

import sqlite3

from router.jev.config import JevSettings
from router.store.repositories import now_iso, transaction


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
        with transaction(self.conn):
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
                    now_iso(),
                ),
            )
