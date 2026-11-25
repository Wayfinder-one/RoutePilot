"""Internal decision boundary: no gateway token, no Authorization forwarding.

The ``X-SR-Gateway-Token`` era is over.  APISIX and the peripheral authenticate
the user *before* the decision sub-request; the kernel itself accepts no token,
reads no ``Authorization`` header, and therefore cannot leak the user key into
the decision event it emits.  The old internal chat forwarding is gone.
"""
import json

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.core_app import create_core_app
from tests.decision_fixtures import core_settings
from tests.routing_fixtures import test_settings
from tests.test_core_app import (
    RecordingSink,
    forbidden_http,
    publish_enabled_graph,
)


def kernel_client(tmp_path):
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    sink = RecordingSink()
    app.state.decision_sink = sink
    publish_enabled_graph(app, "main", upstream="real-fast")
    return TestClient(app), sink


def peripheral_client(tmp_path):
    app = create_app(
        test_settings(tmp_path, jev_url="", jev_model=""),
        transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    return TestClient(app)


def test_internal_decision_needs_no_gateway_token(tmp_path):
    client, sink = kernel_client(tmp_path)
    response = client.post("/internal/v1/route-decision", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    })
    assert response.status_code == 200
    assert response.json()["model"] == "real-fast"
    assert response.json()["logical_model"] == "fast"
    assert len(sink.events) == 1


def test_authorization_never_enters_the_decision_event(tmp_path):
    client, sink = kernel_client(tmp_path)
    response = client.post(
        "/internal/v1/route-decision",
        headers={"Authorization": "Bearer user-key-must-not-leak"},
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]},
    )
    assert response.status_code == 200
    event = json.dumps(sink.events[0], ensure_ascii=False)
    assert "user-key-must-not-leak" not in event
    assert "Authorization" not in event
    assert "authorization" not in event.lower()


def test_old_internal_chat_forwarding_is_gone(tmp_path):
    client = peripheral_client(tmp_path)
    response = client.post("/internal/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    })
    assert response.status_code == 404


def test_public_v1_rejects_forged_department_header(tmp_path):
    """The public /v1 entry must keep refusing X-SR-Department."""
    client = peripheral_client(tmp_path)
    response = client.post(
        "/v1/chat/completions",
        headers={"X-SR-Department": "%E8%B4%A2%E5%8A%A1%E9%83%A8"},
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]})
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "department_header_not_allowed"}}
