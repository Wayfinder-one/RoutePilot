"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    db_path: str = "router.db"
    tagger: str = "heuristic"        # 'heuristic' | 'checkpoint'
    checkpoint_dir: str = ""
    device: str = ""                 # checkpoint device; '' = auto (cuda if available)
    sessions: bool = False           # optional session store (last_vec for similarity)
    host: str = "127.0.0.1"
    port: int = 8400
    # Static bearer token for the /admin API (design doc section 7). Empty =
    # no auth (loopback/dev only); set ROUTER_ADMIN_TOKEN to require
    # "Authorization: Bearer <token>" on every /admin* request.
    admin_token: str = ""
    # PromptJudgeTagger (ROUTER_TAGGER=prompt): OpenAI-compatible chat endpoint
    # used to judge all model signals from one configurable prompt.
    judge_url: str = ""
    judge_model: str = ""
    judge_api_key: str = ""
    signal_prompts: str = ""         # JSON prompt config path ('' = shipped default)
    # JevTagger (ROUTER_TAGGER=jev): merged Open-Jev single model loaded in
    # process; the question wording comes from an editable JSON config.
    jev_model: str = ""              # merged model dir (ROUTER_JEV_MODEL)
    signal_questions: str = ""       # JSON question config ('' = shipped default)
    jev_max_length: int = 0          # 0 = use the model.json max_length
    # Pipeline-aware candidate trimming: score only the JEV questions the
    # current pipeline tree can read (docs/pipeline-aware-trimming.md). The
    # default is on; ROUTER_JEV_TRIM=0 restores the full pack.
    jev_trim: bool = True

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("ROUTER_DB", "router.db"),
            tagger=os.environ.get("ROUTER_TAGGER", "heuristic"),
            checkpoint_dir=os.environ.get("ROUTER_CHECKPOINT", ""),
            device=os.environ.get("ROUTER_DEVICE", ""),
            sessions=_flag("ROUTER_SESSIONS", False),
            host=os.environ.get("ROUTER_HOST", "127.0.0.1"),
            port=int(os.environ.get("ROUTER_PORT", "8400")),
            admin_token=os.environ.get("ROUTER_ADMIN_TOKEN", ""),
            judge_url=os.environ.get("ROUTER_JUDGE_URL", ""),
            judge_model=os.environ.get("ROUTER_JUDGE_MODEL", ""),
            judge_api_key=os.environ.get("ROUTER_JUDGE_API_KEY", ""),
            signal_prompts=os.environ.get("ROUTER_SIGNAL_PROMPTS", ""),
            jev_model=os.environ.get("ROUTER_JEV_MODEL", ""),
            signal_questions=os.environ.get("ROUTER_SIGNAL_QUESTIONS", ""),
            jev_max_length=int(os.environ.get("ROUTER_JEV_MAX_LENGTH", "0")),
            jev_trim=_flag("ROUTER_JEV_TRIM", True),
        )
