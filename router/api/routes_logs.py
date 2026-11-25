"""Peripheral decision-log read API and the kernel event receiver.

The peripheral keeps only the recent complete decision events in memory
(``app.state.decision_logs``, ``deque(maxlen=20)``): there is no SQLite fallback
and no graph snapshot table any more.  Everything here is a projection of those
retained events:

* ``GET /admin/requests/recent`` returns the list projection (no embedded graph);
* ``GET /admin/requests/{id}/replay`` returns ``tree``/``trace``/``request`` all
  read from the same retained event;
* ``GET /admin/overview`` counts the retained events only;
* ``GET /admin/changes`` keeps reading the peripheral change repository (it is
  not a decision log);
* ``POST /internal/v1/decision-logs`` is the kernel's event feed.  It is mounted
  outside ``/admin`` on purpose: the kernel posts events without an admin login.

Evicted or post-restart replays are 404: the memory log is deliberately volatile
and never rebuilt from SQLite.
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from router.api.deps import get_conn
from router.api.request_presenter import present_record
from router.decision_events import replay_payload, summary_payload
from router.store.repositories import ChangeRepo

router = APIRouter()
#: 内核 → 外围的事件接收口；由外围应用单独挂载到 ``/internal/v1``（不受 /admin 鉴权）。
internal_router = APIRouter()

#: 一条完整事件内嵌整张图；接收上限与数据面请求体一致，避免无界接收。
MAX_DECISION_EVENT_BYTES = 10 * 1024 * 1024
#: 与旧 request logger 相同的置信度分桶（概览只统计保留的事件）。
CONF_BUCKETS = 5


@internal_router.post("/decision-logs")
async def receive_decision_log(request: Request):
    """Validate one kernel event and retain it in the in-memory log store."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_DECISION_EVENT_BYTES:
        raise HTTPException(413, detail="request_too_large")
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > MAX_DECISION_EVENT_BYTES:
            raise HTTPException(413, detail="request_too_large")
    try:
        event = json.loads(bytes(buffer))
    except Exception:
        raise HTTPException(400, detail="invalid_json") from None
    if not isinstance(event, dict):
        raise HTTPException(422, detail="invalid_decision_event")
    try:
        request.app.state.decision_logs.add(event)
    except ValueError:
        # 结构/版本/密钥校验失败的事件整体拒绝，不给最近列表留半条脏事件。
        raise HTTPException(422, detail="invalid_decision_event") from None
    return {"ok": True}


@router.get("/requests/recent")
def recent_requests(request: Request, limit: int = 20):
    """Recent decision events, newest first, as page-ready summaries (no graph)."""
    events = request.app.state.decision_logs.recent(limit)
    return {"requests": [present_record(summary_payload(event)) for event in events]}


@router.get("/requests/{request_id:path}/replay")
def request_replay(request_id: str, request: Request):
    """One replay from the event's own graph/trace/request (never the current tree)."""
    event = request.app.state.decision_logs.get(request_id)
    if event is None:
        raise HTTPException(404, detail="unknown request")
    return replay_payload(event)


@router.get("/requests/stats")
def request_stats(request: Request):
    return request.app.state.decision_logs.stats()


@router.get("/changes")
def get_changes(limit: int = 50, conn: sqlite3.Connection = Depends(get_conn)):
    return {"changes": ChangeRepo(conn).list(limit)}


@router.get("/overview")
def get_overview(request: Request):
    """Memory-only overview: counts/statistics over the retained events.

    The old 14-day SQLite aggregate is gone (Task 9 removes the tables); the
    overview now describes exactly the visible decision-log window.
    """
    events = request.app.state.decision_logs.recent(20)
    daily: dict[str, int] = {}
    routes: dict[str, int] = {}
    confidence = [0] * CONF_BUCKETS
    sticky = 0
    for event in events:
        day = str(event.get("ts") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0) + 1
        decision = event["decision"]
        model = decision.get("model") or "(none)"
        routes[model] = routes.get(model, 0) + 1
        if decision.get("sticky_hit"):
            sticky += 1
        top = event["jev"].get("top")
        if isinstance(top, (int, float)) and not isinstance(top, bool):
            index = min(CONF_BUCKETS - 1, max(0, int(float(top) * CONF_BUCKETS)))
            confidence[index] += 1
    return {
        "daily": [{"d": day, "count": daily[day]} for day in sorted(daily)],
        "routes": [
            {"model": model, "count": count}
            for model, count in sorted(routes.items(), key=lambda item: -item[1])
        ],
        "total": len(events),
        "sticky": sticky,
        "confidence": [
            {"lo": index / CONF_BUCKETS, "hi": (index + 1) / CONF_BUCKETS,
             "count": confidence[index]}
            for index in range(CONF_BUCKETS)
        ],
        "confidence_since": request.app.state.decision_logs.started_at,
    }
