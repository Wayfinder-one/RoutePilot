"""Optional session continuation (M4, design doc section 3.7).

A pre-stage before the pipeline: within one session, keep the previous turn's
model unless the user says it was wrong, in which case escalate a tier. Off by
default (``settings.sessions``); when on it reads the session row once (this is
the second of the two session reads on the hot path — ``sessions.py`` reads the
previous embedding first). The session writes happen in the async logger, so
this stage is read-only.

Returned state is surfaced as the ``x-sr-session-state`` response header:
``sticky`` (reused last model), ``unlocked`` (escalated after wrong_answer) or
``new`` (no continuation applied).
"""

from __future__ import annotations

from fastapi import Request

from router.pipeline.engine import Decision

# Escalation ladder by profile tier (design 2: profile.tier is small/large).
TIER_ORDER = ["small", "medium", "large"]


def _tier_rank(tier: str | None) -> int:
    try:
        return TIER_ORDER.index(str(tier))
    except ValueError:
        return -1


def _pick_escalation(models: list[dict], last_model: str | None) -> str | None:
    """Next model up the tier ladder; None when already at the top."""
    last = next((m for m in models if m["id"] == last_model), None)
    if last is None:
        return None
    import json
    last_tier = _tier_rank(json.loads(last.get("profile") or "{}").get("tier"))
    candidates = []
    for m in models:
        import json as _json
        rank = _tier_rank(_json.loads(m.get("profile") or "{}").get("tier"))
        if rank > last_tier:
            candidates.append((rank, m["id"]))
    if not candidates:
        return None
    # lowest tier above the current one (one step up, not a jump to the top)
    best_rank = min(r for r, _ in candidates)
    return next(mid for r, mid in candidates if r == best_rank)


def continuation(request: Request, session_id: str | None) -> tuple[Decision, str] | None:
    """(Decision, state) to short-circuit the pipeline, or None to run it."""
    if not request.app.state.settings.sessions or not session_id:
        return None
    from router.store.db import connect
    from router.store.repositories import ModelRepo, SessionRepo
    conn = connect(request.app.state.settings.db_path)
    try:
        session = SessionRepo(conn).get(session_id)
        last_model = session["model"]
        if not last_model:
            return None
        feedback = (request.headers.get("x-sr-feedback") or "").strip().lower()
        if feedback == "wrong_answer":
            models = ModelRepo(conn).list()
            up = _pick_escalation(models, last_model)
            if up and up != last_model:
                return (Decision(kind="route", model=up,
                                 station_id="session", node_id="unlocked"),
                        "unlocked")
        return (Decision(kind="route", model=last_model,
                         station_id="session", node_id="sticky"), "sticky")
    finally:
        conn.close()
