from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    db_path: str = "router.db"
    graph_dir: str = "./routing-graph"
    graph_writer: bool = True
    host: str = "127.0.0.1"
    port: int = 8400
    jev_url: str = ""
    jev_model: str = ""
    jev_api_key: str = ""
    # 内核 JEV Key 的环境变量名（与 CoreSettings.jev_api_key_env 同义）；
    # 图运行装配只读该变量，不再从 SQLite 解析已存密钥。
    jev_api_key_env: str = "ROUTER_JEV_API_KEY"
    jev_timeout: float = 10.0
    session_max_entries: int = 10000
    user_auth: bool = True
    admin_email: str = "admin@test.com"
    admin_password: str = "1qaz@WSX"
    session_ttl_hours: int = 168
    gateway_token: str = ''
    # 决策内核的内部 API（图管理 + 决策）；外围不再直接写图 JSON。
    core_url: str = "http://127.0.0.1:8400"
    core_timeout_seconds: float = 60.0

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("ROUTER_DB", "router.db"),
            graph_dir=os.environ.get("ROUTER_GRAPH_DIR", "./routing-graph"),
            graph_writer=_flag("ROUTER_GRAPH_WRITER", True),
            host=os.environ.get("ROUTER_HOST", "127.0.0.1"),
            port=int(os.environ.get("ROUTER_PORT", "8400")),
            jev_url=os.environ.get("ROUTER_JEV_URL", ""),
            jev_model=os.environ.get("ROUTER_JEV_MODEL", ""),
            jev_api_key=os.environ.get("ROUTER_JEV_API_KEY", ""),
            jev_timeout=float(os.environ.get("ROUTER_JEV_TIMEOUT", "10")),
            session_max_entries=int(os.environ.get("ROUTER_SESSION_MAX_ENTRIES", "10000")),
            user_auth=_flag("ROUTER_USER_AUTH", True),
            admin_email=os.environ.get("ROUTER_ADMIN_EMAIL", "admin@test.com"),
            admin_password=os.environ.get("ROUTER_ADMIN_PASSWORD", "1qaz@WSX"),
            session_ttl_hours=int(os.environ.get("ROUTER_SESSION_TTL_HOURS", "168")),
            gateway_token=os.environ.get("ROUTER_GATEWAY_TOKEN", ""),
            core_url=os.environ.get("ROUTER_CORE_URL", "http://127.0.0.1:8400"),
            core_timeout_seconds=float(
                os.environ.get("ROUTER_CORE_TIMEOUT_SECONDS", "60")),
        )
