"""``jev_services`` 多服务目录表的 SQLite 存储。

领域 DTO（:class:`router.jev.service.JevService`）与窗口校验保持纯数据；本模块
是唯一持有这张表的 SQL 与连接的地方。密钥只存在本表 ``api_key`` 列，读取接口
``stored_api_key()`` 只在服务端点与模型都匹配时返回它，避免把旧服务的 Key
复用到另一个端点。
"""
from __future__ import annotations

import sqlite3

from router.jev.service import JevService
from router.store.repositories import now_iso, transaction


def _from_row(row) -> JevService:
    return JevService(
        id=row["id"], base_url=row["base_url"], model=row["model"],
        key_env=row["key_env"], timeout_seconds=row["timeout_seconds"],
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

    def save(self, service: JevService, *, api_key: str | None = None) -> None:
        previous = self.conn.execute(
            "SELECT * FROM jev_services WHERE id = ?", (service.id,)).fetchone()
        preserve = api_key is None and previous is not None and all(
            previous[field] == getattr(service, field)
            for field in ("base_url", "model")
        )
        key = previous["api_key"] if preserve else api_key or ""
        with transaction(self.conn):
            self.conn.execute("""INSERT INTO jev_services
                (id, base_url, model, key_env, api_key, timeout_seconds,
                 instructions, state_template, input_tokens, context_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                 base_url=excluded.base_url, model=excluded.model,
                 key_env=excluded.key_env, api_key=excluded.api_key,
                 timeout_seconds=excluded.timeout_seconds,
                 instructions=excluded.instructions, state_template=excluded.state_template,
                 input_tokens=excluded.input_tokens, context_tokens=excluded.context_tokens,
                 updated_at=excluded.updated_at""", (
                service.id, service.base_url, service.model, service.key_env, key,
                service.timeout_seconds, service.instructions, service.state_template,
                service.input_tokens, service.context_tokens, now_iso(),
            ))

    def delete(self, service_id: str) -> bool:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM jev_services WHERE id = ?", (service_id,))
        return cursor.rowcount > 0

    def stored_api_key(self, service: JevService) -> str:
        row = self.conn.execute(
            "SELECT base_url, model, api_key FROM jev_services WHERE id = ?",
            (service.id,),
        ).fetchone()
        if row is None or row["base_url"] != service.base_url or row["model"] != service.model:
            return ""
        return row["api_key"] or ""
