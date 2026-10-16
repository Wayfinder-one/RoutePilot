"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "router.db"
    tagger: str = "heuristic"        # 'heuristic' | 'checkpoint' (M2)
    checkpoint_dir: str = ""
    host: str = "127.0.0.1"
    port: int = 8400

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("ROUTER_DB", "router.db"),
            tagger=os.environ.get("ROUTER_TAGGER", "heuristic"),
            checkpoint_dir=os.environ.get("ROUTER_CHECKPOINT", ""),
            host=os.environ.get("ROUTER_HOST", "127.0.0.1"),
            port=int(os.environ.get("ROUTER_PORT", "8400")),
        )
