from __future__ import annotations

import os
from dataclasses import dataclass


#: 公开已知的默认管理员凭据：只在 loopback 本机绑定上允许作为种子值；
#: 任何非 loopback 绑定（如容器里的 0.0.0.0）都必须在启动时显式覆盖。
DEFAULT_ADMIN_EMAIL = "admin@test.com"
DEFAULT_ADMIN_PASSWORD = "1qaz@WSX"

#: 允许使用默认管理员凭据的绑定主机（仅本机回环）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    db_path: str = "router.db"
    graph_dir: str = "./routing-graph"
    graph_writer: bool = True
    #: 公开监听（控制台 + /admin + /v1），默认只绑定本机回环。
    host: str = "127.0.0.1"
    port: int = 8400
    #: 内部监听（仅内核事件接收口 /internal/v1/decision-logs + /healthz），
    #: 与公开监听分离，部署时不应发布到主机。
    internal_host: str = "127.0.0.1"
    internal_port: int = 8402
    jev_url: str = ""
    jev_model: str = ""
    # I6：JEV Key 只属于内核（CoreSettings.jev_api_key_env / 内核进程环境变量），
    # 外围 Settings 不读取、也不持有任何 JEV 密钥或密钥变量名。
    jev_timeout: float = 10.0
    session_max_entries: int = 10000
    user_auth: bool = True
    admin_email: str = DEFAULT_ADMIN_EMAIL
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    session_ttl_hours: int = 168
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
            internal_host=os.environ.get("ROUTER_INTERNAL_HOST", "127.0.0.1"),
            internal_port=int(os.environ.get("ROUTER_INTERNAL_PORT", "8402")),
            jev_url=os.environ.get("ROUTER_JEV_URL", ""),
            jev_model=os.environ.get("ROUTER_JEV_MODEL", ""),
            jev_timeout=float(os.environ.get("ROUTER_JEV_TIMEOUT", "10")),
            session_max_entries=int(os.environ.get("ROUTER_SESSION_MAX_ENTRIES", "10000")),
            user_auth=_flag("ROUTER_USER_AUTH", True),
            admin_email=os.environ.get(
                "ROUTER_ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL),
            admin_password=os.environ.get(
                "ROUTER_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
            session_ttl_hours=int(os.environ.get("ROUTER_SESSION_TTL_HOURS", "168")),
            core_url=os.environ.get("ROUTER_CORE_URL", "http://127.0.0.1:8400"),
            core_timeout_seconds=float(
                os.environ.get("ROUTER_CORE_TIMEOUT_SECONDS", "60")),
        )
