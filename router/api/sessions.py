"""Optional session store (design doc section 3.7).

Holds the previous turn's embedding so similarity/reask can fire. Default OFF
(``settings.sessions``): with it off the hot path never touches the database.
When ON, the request path performs exactly one read (the previous vector); the
new vector is persisted asynchronously by the request logger's flush. This is
the one sanctioned exception to the "routing never touches the DB" rule of
section 4.4, and it is why the module is opt-in.
"""

from __future__ import annotations

from fastapi import Request


def session_id_from(request: Request) -> str | None:
    raw = request.headers.get("x-sr-session")
    if raw and raw.strip():
        return raw.strip()
    return None


def read_last_vec(request: Request, session_id: str | None = None) -> list[float] | None:
    """Previous turn's embedding for this session, or None (feature off/no history)."""
    if not request.app.state.settings.sessions:
        return None
    sid = session_id or session_id_from(request)
    if not sid:
        return None
    from router.store.db import connect
    from router.store.repositories import SessionRepo
    conn = connect(request.app.state.settings.db_path)
    try:
        return SessionRepo(conn).get_vec(sid)
    finally:
        conn.close()
