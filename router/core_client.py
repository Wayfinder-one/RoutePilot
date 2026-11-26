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
    """A failed ``replace_graph_batch`` call (409 conflict / 422 issues / 500+).

    ``RemoteCatalogSyncService`` maps the structured kernel answer back to the
    peripheral domain errors and decides whether to probe the kernel.

    ``uncertain`` marks an answer that does **not** prove the batch was rejected
    before commit.  The kernel contract (``POST /internal/v1/graph-batches``) is
    binary at the storage layer — it either commits every candidate or leaves
    every file untouched — but a failure *after* the commit (e.g. building the
    success response) can answer 500 ``graph_batch_committed``.  Classification:

    * **definite, not committed** (``uncertain=False``): 409 ``graph_conflict``
      and 422 issue arrays (rejected before a single file is written), plus 503
      with detail code ``graph_storage_error`` or ``core_unavailable`` (the
      kernel's verified pre-commit paths: read-only storage, invalid route set,
      staging failure, failed commit rolled back; or the kernel never answered);
    * **uncertain** (``uncertain=True``): 500/502/504, any body code
      ``graph_batch_committed``, any other/unrecognised non-200 answer, and any
      200 whose body cannot be parsed as the committed tree list.  The caller
      must probe the kernel and compensate if the candidates are committed.

    Transport failures are classified by ``_batch_transport_error``: errors
    raised before the request is sent (``ConnectError`` / ``ConnectTimeout`` /
    pool/protocol setup) leave ``uncertain`` false; read timeouts and dropped
    connections are ambiguous and set it true.
    """

    def __init__(self, status_code: int, payload=None, *, uncertain: bool = False):
        super().__init__(status_code, payload)
        self.args = (f"graph batch failed: {status_code}",)
        self.uncertain = bool(uncertain)


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


#: Transport failures raised before any byte of the request can reach the kernel:
#: the graph batch is definitely not committed, so the catalog sync only has to
#: roll back SQLite. Every other transport error is ambiguous: the request may
#: have been sent and committed while the response was lost.
_NOT_SENT_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
)


def _batch_transport_error(exc: httpx.HTTPError) -> CoreBatchError:
    """Classify a transport failure of ``POST /internal/v1/graph-batches``.

    ``ConnectError``/``ConnectTimeout``/pool/protocol setup failures happen
    before the request is sent: the kernel state is unchanged and the caller
    can roll back SQLite without compensating. Read timeouts, dropped
    connections and protocol errors on the reply are **unknown**: the kernel may
    already have committed the batch (the response was lost), so the caller must
    probe and compensate.
    """
    return CoreBatchError(
        503, {"detail": {"code": "core_unavailable", "message": str(exc)}},
        uncertain=not isinstance(exc, _NOT_SENT_TRANSPORT_ERRORS))


#: Kernel 503 body codes that prove the batch was rejected/rolled back before
#: any graph file stayed written.  ``graph_storage_error`` covers the kernel's
#: verified pre-commit paths (read-only repo, invalid route set, staging failure,
#: failed commit with a successful rollback); ``core_unavailable`` is the
#: transport-level "kernel never answered" class.
_PRE_COMMIT_BATCH_CODES = frozenset({"graph_storage_error", "core_unavailable"})

#: Kernel body code for "committed but the response could not be built".
_COMMITTED_BATCH_CODE = "graph_batch_committed"


def _batch_answer_uncertain(status_code: int, payload) -> bool:
    """Whether a non-200 batch answer leaves the kernel commit state unknown.

    Only a *verified* pre-commit rejection is definite: 409 ``graph_conflict``,
    422 issue arrays and 503 with a known pre-commit body code.  Everything else
    (5xx, ``graph_batch_committed``, unknown codes/statuses, non-JSON proxy
    answers) may have committed the batch, so the caller probes.
    """
    detail = payload.get("detail") if isinstance(payload, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    if code == _COMMITTED_BATCH_CODE:
        return True
    if status_code in (409, 422):
        return False
    if status_code == 503 and code in _PRE_COMMIT_BATCH_CODES:
        return False
    return True


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
        new ``document_sha256`` the reverse batch must use.

        Raises ``CoreBatchError`` for any non-200 answer and for a 200 whose body
        is not the committed tree list.  ``uncertain`` is false only for the
        definite pre-commit rejections (409/422, 503 ``graph_storage_error`` /
        ``core_unavailable``, definitely-not-sent transport errors); 5xx,
        ``graph_batch_committed`` and ambiguous transport failures mark the
        kernel state unknown, so ``RemoteCatalogSyncService`` probes and
        compensates instead of rolling SQLite back as if nothing was written.
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
            raise _batch_transport_error(exc) from exc
        if status != 200:
            raise CoreBatchError(
                status, body, uncertain=_batch_answer_uncertain(status, body))
        if not isinstance(body, dict) or not isinstance(body.get("trees"), list):
            # The kernel answers 200 only after the commit: an unusable reply is
            # "committed but unconfirmed", never a definite not-committed answer.
            raise CoreBatchError(
                500, {"detail": {
                    "code": _COMMITTED_BATCH_CODE,
                    "message": (
                        "kernel graph-batch reply has no committed tree list"),
                }}, uncertain=True)
        return body

    async def jev_test(self, payload: dict) -> dict:
        """Ask the kernel to run one credentialed JEV connection test (I6).

        ``payload`` carries only the **non-secret** catalog service description
        and the probe text: the key never crosses this boundary because only the
        kernel holds it (its own ``jev.api_key_env`` environment variable).  The
        kernel status/body are preserved: 422 (invalid service values), 502 (JEV
        failure) and 503 (kernel unavailable) reach the peripheral route, which
        maps them onto its public contract.
        """
        try:
            status, body = await self.admin_json("POST", "/jev-test", payload)
        except httpx.HTTPError as exc:
            raise _unavailable(exc) from exc
        if status != 200:
            raise CoreGraphError(status, body)
        if not isinstance(body, dict):
            raise CoreGraphError(502, {"detail": "kernel JEV test reply is not an object"})
        return body

    async def aclose(self) -> None:
        await self.http.aclose()
