"""``jev_services`` 多服务目录表的 SQLite 存储（只存非密钥字段）。

领域 DTO（:class:`router.jev.service.JevService`）与窗口校验保持纯数据；本模块
是唯一持有这张表的 SQL 与连接的地方。JEV 真实密钥不再进入 SQLite：内核只从
``ROUTER_JEV_API_KEY`` 环境变量读取，本表只保存 id/地址/模型/超时/提示词与窗口。
"""
from __future__ import annotations

import sqlite3

from router.jev.service import JEV_KEY_ENV, JevService
from router.store.repositories import now_iso, transaction


def _from_row(row) -> JevService:
    # 目录读取把 key_env 固定为内核环境变量名：旧库里可能保存过任意环境变量名，
    # 但它只是图文档兼容字段，真实密钥只由内核从环境变量读取。
    return JevService(
        id=row["id"], base_url=row["base_url"], model=row["model"],
        key_env=JEV_KEY_ENV, timeout_seconds=row["timeout_seconds"],
        instructions=row["instructions"], state_template=row["state_template"],
        input_tokens=row["input_tokens"], context_tokens=row["context_tokens"],
    )


class JevServiceRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> list[JevService]:
        return [_from_row(row) for row in self.conn.execute(
            "SELECT * FROM jev_services ORDER BY id")]

    def get(self, service_id: str) -> JevService | None:
        row = self.conn.execute(
            "SELECT * FROM jev_services WHERE id = ?", (service_id,)).fetchone()
        return _from_row(row) if row is not None else None

    def save(self, service: JevService) -> None:
        """Insert/update one non-secret directory row (no key argument exists)."""
        with transaction(self.conn):
            self.conn.execute("""INSERT INTO jev_services
                (id, base_url, model, key_env, timeout_seconds, instructions,
                 state_template, input_tokens, context_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                 base_url=excluded.base_url, model=excluded.model,
                 key_env=excluded.key_env, timeout_seconds=excluded.timeout_seconds,
                 instructions=excluded.instructions, state_template=excluded.state_template,
                 input_tokens=excluded.input_tokens, context_tokens=excluded.context_tokens,
                 updated_at=excluded.updated_at""", (
                service.id, service.base_url, service.model, service.key_env,
                service.timeout_seconds, service.instructions, service.state_template,
                service.input_tokens, service.context_tokens, now_iso(),
            ))

    def delete(self, service_id: str) -> bool:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM jev_services WHERE id = ?", (service_id,))
        return cursor.rowcount > 0
