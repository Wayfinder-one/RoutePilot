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
    host: str = "127.0.0.1"
    port: int = 8400
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
    # JEV input window "<head>:<tail>" in tokenizer tokens (ROUTER_JEV_WINDOW).
    # "" = not configured: JevTagger derives the legacy head-only window
    # "<max_length>:0" ("1024:0" for the deployed model), so a custom
    # ROUTER_JEV_MAX_LENGTH keeps the old behavior without also setting the
    # window. Explicit arms: "0:1024" = tail-only; "256:768"/"512:512" keep
    # both ends (docs/JEV-改进-实施计划-2026-10-24.md §2.1). Validated at
    # startup by router.signals.jev.parse_window.
    jev_window: str = ""
    # Long-text PII chunking (2026-10-24 P2-A, opt-in). 0 = off (default:
    # behavior identical to the single-window tagger). >0 = score the PII
    # multi-signal on that many planned token segments for requests longer than
    # max_length (head + tail + rule-span windows + uniform fillers).
    jev_chunk_segments: int = 0          # ROUTER_JEV_CHUNK_SEGMENTS
    jev_chunk_size: int = 512            # ROUTER_JEV_CHUNK_SIZE (<= max_length)
    jev_chunk_agg: str = "noisy_or"      # ROUTER_JEV_CHUNK_AGG (noisy_or|logsumexp|max)
    jev_chunk_calibration: str = ""      # ROUTER_JEV_CHUNK_CALIBRATION (JSON path)
    # Pipeline-aware candidate trimming: score only the JEV questions the
    # current pipeline tree can read (docs/pipeline-aware-trimming.md). The
    # default is on; ROUTER_JEV_TRIM=0 restores the full pack.
    jev_trim: bool = True
    # Console accounts (2026-10-26 user management, design doc §6). NOTE the
    # deliberate asymmetry between the two defaults:
    #   * dataclass default False — tests and code that build Settings(...)
    #     without this field keep the pre-2026-10-26 behavior (/admin open),
    #     so existing deployments/tests do not suddenly need a login;
    #   * from_env() default True — a service started from the environment
    #     (production, systemd unit, deploy script) DOES require login by
    #     default, because an open /admin used to be reachable on the network.
    #     ROUTER_USER_AUTH=0 explicitly restores the open behavior for dev.
    # The static ROUTER_ADMIN_TOKEN was removed on 2026-10-26: scripts now log
    # in with ROUTER_ADMIN_EMAIL/ROUTER_ADMIN_PASSWORD and reuse the session
    # token (see scripts/configure_upstream.py).
    # The default admin account is seeded once into an empty users table only.
    user_auth: bool = False
    admin_email: str = "admin@test.com"
    admin_password: str = "1qaz@WSX"
    session_ttl_hours: int = 168

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("ROUTER_DB", "router.db"),
            tagger=os.environ.get("ROUTER_TAGGER", "heuristic"),
            checkpoint_dir=os.environ.get("ROUTER_CHECKPOINT", ""),
            device=os.environ.get("ROUTER_DEVICE", ""),
            host=os.environ.get("ROUTER_HOST", "127.0.0.1"),
            port=int(os.environ.get("ROUTER_PORT", "8400")),
            judge_url=os.environ.get("ROUTER_JUDGE_URL", ""),
            judge_model=os.environ.get("ROUTER_JUDGE_MODEL", ""),
            judge_api_key=os.environ.get("ROUTER_JUDGE_API_KEY", ""),
            signal_prompts=os.environ.get("ROUTER_SIGNAL_PROMPTS", ""),
            jev_model=os.environ.get("ROUTER_JEV_MODEL", ""),
            signal_questions=os.environ.get("ROUTER_SIGNAL_QUESTIONS", ""),
            jev_max_length=int(os.environ.get("ROUTER_JEV_MAX_LENGTH", "0")),
            jev_window=os.environ.get("ROUTER_JEV_WINDOW", ""),
            jev_chunk_segments=int(os.environ.get("ROUTER_JEV_CHUNK_SEGMENTS", "0")),
            jev_chunk_size=int(os.environ.get("ROUTER_JEV_CHUNK_SIZE", "512")),
            jev_chunk_agg=os.environ.get("ROUTER_JEV_CHUNK_AGG", "noisy_or"),
            jev_chunk_calibration=os.environ.get(
                "ROUTER_JEV_CHUNK_CALIBRATION", ""),
            jev_trim=_flag("ROUTER_JEV_TRIM", True),
            user_auth=_flag("ROUTER_USER_AUTH", True),
            admin_email=os.environ.get("ROUTER_ADMIN_EMAIL", "admin@test.com"),
            admin_password=os.environ.get("ROUTER_ADMIN_PASSWORD", "1qaz@WSX"),
            session_ttl_hours=int(os.environ.get("ROUTER_SESSION_TTL_HOURS",
                                                 "168")),
        )
