"""Peripheral client for the decision kernel's internal APIs.

Only the peripheral owns user credentials and SQLite; the core owns the graph
JSON and every routing decision (global constraints 1-3).  This module is the
single HTTP boundary between them:

* ``decide`` posts one decision request to ``POST /internal/v1/route-decision``
  with the trusted department percent-encoded (the kernel decodes that header
  strictly).  The kernel never reads an ``Authorization`` header, so this client
  never sends one: the peripheral authenticates the user and resolves the
  provider credential for the execution it performs itself.
* ``admin_json`` forwards one graph-management call under ``/internal/v1`` and
  returns the kernel status code and JSON body untouched; the peripheral proxy
  returns them verbatim (409 conflicts, 422 issue arrays, 503 ``graph_*`` codes).

There are no retries and no local fallbacks: a failed kernel call is a failed
peripheral call, and the caller decides how to expose it.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class CoreDecisionError(RuntimeError):
    """A non-200 kernel decision answer; status and payload are preserved."""

    def __init__(self, status_code: int, payload):
        super().__init__(f"kernel decision failed: {status_code}")
        self.status_code = status_code
        self.payload = payload


def _json_body(response: httpx.Response):
    """Decode a kernel reply; an intermediate proxy may answer non-JSON text."""
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text}


class CoreClient:
    """One kernel base URL, one dedicated ``httpx.AsyncClient``."""

    def __init__(self, base_url: str, *, http: httpx.AsyncClient,
                 timeout_seconds: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.timeout_seconds = timeout_seconds

    async def decide(self, body, *, department: str = "",
                     session_id: str = "", request_id: str = ""):
        """POST one decision request; return the non-secret decision contract.

        Raises ``CoreDecisionError`` for any non-200 answer (the kernel's 503
        ``no_active_tree``/``routing_error`` payloads are carried unchanged).
        """
        headers = {
            "X-SR-Department": quote(department, safe=""),
            "X-SR-Session-Id": session_id,
            "X-Request-ID": request_id,
        }
        response = await self.http.post(
            self.base_url + "/internal/v1/route-decision",
            json=body, headers=headers, timeout=self.timeout_seconds)
        payload = _json_body(response)
        if response.status_code != 200:
            raise CoreDecisionError(response.status_code, payload)
        return payload

    async def admin_json(self, method: str, path: str, payload=None
                         ) -> tuple[int, object]:
        """One graph-management call under ``/internal/v1`` → (status, body).

        ``path`` is the kernel-side path (``/trees``, ``/trees/{id}/enable``,
        ``/try`` ...); no user credential is attached to the request.
        """
        kwargs = {"timeout": self.timeout_seconds}
        if payload is not None:
            kwargs["json"] = payload
        response = await self.http.request(
            method, self.base_url + "/internal/v1" + path, **kwargs)
        return response.status_code, _json_body(response)

    async def aclose(self) -> None:
        await self.http.aclose()
