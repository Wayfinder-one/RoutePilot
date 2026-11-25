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
* ``list_graphs`` reads the current documents for catalog impact scans;
  ``replace_graph_batch`` writes one conditional multi-graph batch.  Both parse
  the kernel contract at this boundary so no other module handles raw kernel
  JSON, and both serialize/deserialize ``RoutingTree`` here: the repository, the
  catalog sync service and the API routes keep working with domain objects.

There are no retries and no local fallbacks: a failed kernel call is a failed
peripheral call, and the caller decides how to expose it.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from router.routing.tree import RoutingTree, tree_from_dict, tree_to_dict

logger = logging.getLogger(__name__)


class CoreDecisionError(RuntimeError):
    """A non-200 kernel decision answer; status and payload are preserved."""

    def __init__(self, status_code: int, payload):
        super().__init__(f"kernel decision failed: {status_code}")
        self.status_code = status_code
        self.payload = payload


class CoreGraphError(RuntimeError):
    """A failed kernel graph-management call; status and payload are preserved.

    Raised by reads (``list_graphs``) and raw administration calls.  Any non-200
    kernel answer keeps its status code and JSON body so the peripheral can map
    the structured ``graph_*`` codes instead of inventing a new vocabulary.
    """

    def __init__(self, status_code: int, payload=None):
        super().__init__(f"kernel graph call failed: {status_code}")
        self.status_code = int(status_code)
        self.payload = payload


class CoreBatchError(CoreGraphError):
    """A failed ``replace_graph_batch`` call (409 conflict / 422 issues / 503).

    ``RemoteCatalogSyncService`` maps the structured kernel answer back to the
    peripheral domain errors; the graph write either happened completely or not
    at all (the kernel verifies every base hash before touching a file).
    """

    def __init__(self, status_code: int, payload=None):
        super().__init__(status_code, payload)
        self.args = (f"graph batch failed: {status_code}",)


def _json_body(response: httpx.Response):
    """Decode a kernel reply; an intermediate proxy may answer non-JSON text."""
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text}


def _unavailable(exc: BaseException) -> CoreGraphError:
    """Kernel unreachable/transport broken → the same 503 class as a 503 answer."""
    return CoreGraphError(
        503, {"detail": {"code": "core_unavailable", "message": str(exc)}})


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
        try:
            response = await self.http.post(
                self.base_url + "/internal/v1/route-decision",
                json=body, headers=headers, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            # Kernel unreachable is a 503 data-plane failure, never a 500: the
            # standalone peripheral keeps the APISIX/route-decision error class.
            raise CoreDecisionError(
                503, {"error": {"message": "core_unavailable"}}) from exc
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

    async def list_graphs(self) -> list[RoutingTree]:
        """Read every current graph document from the kernel.

        The documents keep the kernel's ``document_sha256`` so a catalog write
        can use them as optimistic-concurrency baselines and as the original
        bytes of a reverse batch.  Raises ``CoreGraphError`` for any non-200
        kernel answer or a malformed tree list.
        """
        try:
            status, body = await self.admin_json("GET", "/trees")
        except httpx.HTTPError as exc:
            raise _unavailable(exc) from exc
        if status != 200:
            raise CoreGraphError(status, body)
        raw = body.get("trees") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            raise CoreGraphError(
                503, {"detail": {"code": "core_unavailable",
                                 "message": "kernel tree list is not an array"}})
        try:
            return [tree_from_dict(item) for item in raw]
        except (TypeError, ValueError) as exc:
            raise CoreGraphError(
                503, {"detail": {"code": "core_unavailable",
                                 "message": f"invalid kernel tree document: {exc}"}}
            ) from exc

    async def replace_graph_batch(self, writes) -> dict:
        """Atomically replace a conditional multi-graph batch in the kernel.

        ``writes`` is a list of ``{"tree": RoutingTree, "base_document_sha256":
        str}``: the ``RoutingTree`` objects are serialized **here** (the only
        module that knows the kernel JSON contract).  Returns the kernel reply
        ``{"trees": [tree_to_dict(document), ...]}`` whose documents carry the
        new ``document_sha256`` the reverse batch must use.  Raises
        ``CoreBatchError`` for any non-200 answer (the kernel guarantees it
        either committed every candidate or touched no file).
        """
        payload = {"writes": [
            {
                "tree": tree_to_dict(write["tree"]),
                "base_document_sha256": write["base_document_sha256"],
            }
            for write in writes
        ]}
        try:
            status, body = await self.admin_json(
                "POST", "/graph-batches", payload)
        except httpx.HTTPError as exc:
            raise CoreBatchError(503, {"detail": {
                "code": "core_unavailable", "message": str(exc)}}) from exc
        if status != 200:
            raise CoreBatchError(status, body)
        if not isinstance(body, dict):
            raise CoreBatchError(503, {"detail": {
                "code": "core_unavailable",
                "message": "kernel graph-batch reply is not an object"}})
        return body

    async def aclose(self) -> None:
        await self.http.aclose()
