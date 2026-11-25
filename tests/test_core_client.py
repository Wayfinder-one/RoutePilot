"""Peripheral ``CoreClient``: kernel decision call + graph-management transport.

The client is the only place where the peripheral talks to the core.  Its
contract is deliberately narrow:

* ``decide`` posts the decision body to ``POST /internal/v1/route-decision``
  with the trusted department percent-encoded (the kernel decodes it strictly)
  and **never** forwards a user ``Authorization`` — the kernel holds no user key;
* a non-200 kernel answer raises ``CoreDecisionError`` carrying the kernel status
  code and payload unchanged, so callers preserve 503 ``no_active_tree`` etc.;
* ``admin_json`` proxies one management call under ``/internal/v1`` and returns
  the kernel status/body unchanged — including structured 409/422/503 details
  and a non-JSON error body (an intermediate proxy may answer text/plain).
"""
import json

import httpx
import pytest

from router.core_client import CoreClient, CoreDecisionError
from tests.decision_fixtures import async_test, decision_payload


def make_client(handler, base_url="http://core:8400", *, timeout_seconds=60.0):
    return CoreClient(
        base_url,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        timeout_seconds=timeout_seconds,
    )


@async_test
async def test_decide_never_forwards_authorization():
    seen = {}

    async def handler(request):
        seen.update(request.headers)
        seen["#url"] = str(request.url)
        seen["#body"] = json.loads(request.content)
        return httpx.Response(200, json=decision_payload())

    client = make_client(handler)
    try:
        result = await client.decide(
            {"model": "auto", "messages": []}, department="财务部", session_id="s1")
    finally:
        await client.http.aclose()
    assert result["model"] == "real-fast"
    assert "authorization" not in seen
    assert seen["x-sr-department"] == "%E8%B4%A2%E5%8A%A1%E9%83%A8"
    assert seen["x-sr-session-id"] == "s1"
    assert seen["#url"] == "http://core:8400/internal/v1/route-decision"
    assert seen["#body"] == {"model": "auto", "messages": []}


@async_test
async def test_decide_preserves_kernel_error():
    async def handler(request):
        return httpx.Response(503, json={"error": {"message": "no_active_tree"}})

    client = make_client(handler)
    try:
        with pytest.raises(CoreDecisionError) as caught:
            await client.decide({"model": "auto", "messages": []})
    finally:
        await client.http.aclose()
    assert caught.value.status_code == 503
    assert caught.value.payload["error"]["message"] == "no_active_tree"


@async_test
async def test_decide_forwards_session_and_request_ids():
    seen = {}

    async def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json=decision_payload())

    client = make_client(handler)
    try:
        await client.decide(
            {"model": "auto", "messages": []},
            department="", session_id="sess-9", request_id="req-7")
    finally:
        await client.http.aclose()
    assert seen["x-sr-department"] == ""
    assert seen["x-sr-session-id"] == "sess-9"
    assert seen["x-request-id"] == "req-7"
    assert "authorization" not in seen


@async_test
async def test_admin_json_returns_kernel_status_and_body():
    seen = {}

    async def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen.update(request.headers)
        return httpx.Response(
            200, json={"trees": [], "graph_status": {"ready": True}})

    client = make_client(handler)
    try:
        status, body = await client.admin_json("POST", "/trees", {"id": "main"})
    finally:
        await client.http.aclose()
    assert status == 200
    assert body == {"trees": [], "graph_status": {"ready": True}}
    assert seen["method"] == "POST"
    assert seen["url"] == "http://core:8400/internal/v1/trees"
    assert seen["body"] == {"id": "main"}
    # The kernel management API takes no user credential: none is attached here.
    assert "authorization" not in seen


@async_test
async def test_admin_json_preserves_structured_conflict_detail():
    detail = {
        "code": "graph_conflict",
        "actual_document_sha256": "b" * 64,
    }

    async def handler(request):
        return httpx.Response(409, json={"detail": detail})

    client = make_client(handler)
    try:
        status, body = await client.admin_json(
            "PUT", "/trees/main", {"base_document_sha256": "a" * 64})
    finally:
        await client.http.aclose()
    assert status == 409
    assert body == {"detail": detail}


@async_test
async def test_admin_json_tolerates_non_json_error_body():
    async def handler(request):
        return httpx.Response(
            503, text="upstream connect error or disconnect/reset before headers")

    client = make_client(handler)
    try:
        status, body = await client.admin_json("GET", "/trees")
    finally:
        await client.http.aclose()
    assert status == 503
    assert body == {
        "detail": "upstream connect error or disconnect/reset before headers"}


@async_test
async def test_client_keeps_the_configured_base_url_and_timeout():
    async def handler(request):
        return httpx.Response(200, json={"trees": []})

    client = make_client(handler, "http://kernel:9000/", timeout_seconds=4.5)
    try:
        assert client.base_url == "http://kernel:9000"
        assert client.timeout_seconds == 4.5
        status, _body = await client.admin_json("GET", "/trees")
    finally:
        await client.http.aclose()
    assert status == 200
