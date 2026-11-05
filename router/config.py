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
    jev_timeout: float = 10.0
    session_max_entries: int = 10000
    user_auth: bool = False
    admin_email: str = "admin@test.com"
    admin_password: str = "1qaz@WSX"
    session_ttl_hours: int = 168

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
        )
