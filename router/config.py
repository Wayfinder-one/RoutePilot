from __future__ import annotations

import os
from dataclasses import dataclass


#: 首次部署的默认管理员凭据：未设置 ROUTER_ADMIN_EMAIL / ROUTER_ADMIN_PASSWORD
#: 时直接写入新库，容器里的 0.0.0.0 绑定也一样可用。生产可显式覆盖；
#: 旧库若仍保留**另一套**公开口令，由启动时的 stale 账号扫描处理。
DEFAULT_ADMIN_EMAIL = "admin@test.com"
DEFAULT_ADMIN_PASSWORD = "1qaz@WSX"

#: 用户口令最小长度：唯一来源在 ``router.password_policy``，这里 re-export 以
#: 保持既有的 ``from router.config import MIN_ADMIN_PASSWORD_LENGTH`` 不变
#: （启动守卫 / 用户 API / 测试都依赖这个公开名，数字只定义一次）。
#: import 必须放在 DEFAULT_ADMIN_PASSWORD 之后：password_policy 反过来从本
#: 模块读取公开默认口令，两个导入顺序都要能完成初始化。
from router.password_policy import (  # noqa: E402
    MIN_PASSWORD_LENGTH as MIN_ADMIN_PASSWORD_LENGTH,
)

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
    #: C2：显式授权把旧库里仍接受公开默认/空口令的管理员账号重置为
    #: ``admin_password`` 并吊销其会话（仅在非 loopback 绑定启动时生效）。
    admin_force_reset: bool = False
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
            admin_force_reset=_flag("ROUTER_ADMIN_FORCE_RESET"),
            session_ttl_hours=int(os.environ.get("ROUTER_SESSION_TTL_HOURS", "168")),
            core_url=os.environ.get("ROUTER_CORE_URL", "http://127.0.0.1:8400"),
            core_timeout_seconds=float(
                os.environ.get("ROUTER_CORE_TIMEOUT_SECONDS", "60")),
        )
