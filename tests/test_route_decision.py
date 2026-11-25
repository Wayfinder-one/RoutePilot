"""decision-only kernel API: contract fields, validation, events and lease release.

These tests pin the kernel's single data-plane endpoint.  The endpoint must:

* return exactly the non-secret decision contract both APISIX and the
  peripheral consume;
* accept no gateway token and never read the user ``Authorization`` (the
  decision event must not carry it either);
* keep the existing request-size/JSON/messages/department/session validation;
* emit one self-contained decision event per selected graph — including
  ``status=routing_error`` for a failure after graph selection;
* release the scheduling lease on every return path.
"""
import json
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from router.api.core_app import create_core_app
from router.api.routes_decision import provider_payload
from router.routing.runtime import runtime_from_dict
from router.upstream.runtime_keys import runtime_model_key, tree_scope
from tests.decision_fixtures import core_settings
from tests.routing_fixtures import runtime_dict
from tests.test_core_app import (
    RecordingSink,
    forbidden_http,
    publish_department_graph,
    publish_enabled_graph,
)

DECISION_FIELDS = {
    "decision_id", "model", "logical_model", "provider", "reasoning",
    "reasoning_warning", "tree_id", "tree_revision", "target_id",
    "selection_reason", "graph_selection",
}


def make_kernel(tmp_path):
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    sink = RecordingSink()
    app.state.decision_sink = sink
    return app, TestClient(app), sink


def decision_request(client, *, headers=None, json_body=None):
    body = {"model": "auto", "messages": [{"role": "user", "content": "你好"}]}
    if json_body is not None:
        body = json_body
    return client.post("/internal/v1/route-decision", json=body, headers=headers or {})


def test_route_decision_returns_exactly_the_decision_contract(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(client)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == DECISION_FIELDS
    assert payload["model"] == "real-fast"
    assert payload["logical_model"] == "fast"
    assert payload["provider"] == {
        "name": "local",
        "base_url": "http://upstream.test/v1",
        "reasoning_format": "openai",
    }
    assert payload["reasoning"] is None
    assert payload["reasoning_warning"] is None
    assert payload["tree_id"] == "main"
    assert payload["tree_revision"]
    assert payload["target_id"] == "target"
    assert payload["selection_reason"] == "target"
    assert payload["graph_selection"] == "default"
    assert isinstance(payload["decision_id"], str) and payload["decision_id"]
    assert len(sink.events) == 1
    # Provider credentials are never part of the decision contract.
    assert "key_env" not in json.dumps(payload)


def test_route_decision_ignores_requested_model_but_records_it(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(
        client, json_body={
            "model": "requested-strong",
            "messages": [{"role": "user", "content": "你好"}],
        })
    assert response.status_code == 200
    assert response.json()["model"] == "real-fast"
    assert response.json()["logical_model"] == "fast"
    assert sink.events[0]["request"]["requested_model"] == "requested-strong"


def test_route_decision_still_decides_with_a_non_string_requested_model(tmp_path):
    """The kernel ignores the request model, so a malformed one cannot 500."""
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(
        client, json_body={
            "model": {"not": "a string"},
            "messages": [{"role": "user", "content": "你好"}],
        })
    assert response.status_code == 200
    assert response.json()["model"] == "real-fast"
    assert sink.events[0]["request"]["requested_model"] == ""


def test_route_decision_never_reads_authorization(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(
        client, headers={"Authorization": "Bearer super-secret-user-token"})
    assert response.status_code == 200
    event = json.dumps(sink.events[0], ensure_ascii=False)
    assert "super-secret-user-token" not in event
    assert "authorization" not in event.lower()
    assert "api_key" not in event


def test_route_decision_decodes_trusted_department_header(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(
        client, headers={"X-SR-Department": quote("财务部", safe="")})
    assert response.status_code == 200
    assert response.json()["graph_selection"] == "default"
    assert sink.events[0]["request"]["department"] == "财务部"


def test_route_decision_selects_the_department_graph(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    publish_department_graph(
        app, "finance", upstream="real-finance", departments=["财务部"])
    response = decision_request(
        client, headers={"X-SR-Department": quote("财务部", safe="")})
    assert response.status_code == 200
    payload = response.json()
    assert payload["graph_selection"] == "department"
    assert payload["tree_id"] == "finance"
    assert payload["model"] == "real-finance"
    event = sink.events[0]
    assert event["graph"]["id"] == "finance"
    assert event["decision"]["graph_selection"] == "department"


def test_route_decision_rejects_invalid_json_and_non_object_bodies(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    for content, message in ((b"{not json", "invalid_json"),
                             (b"[]", "body_must_be_object")):
        response = client.post("/internal/v1/route-decision", content=content)
        assert response.status_code == 400
        assert response.json() == {"error": {"message": message}}
    assert sink.events == []


def test_route_decision_requires_a_messages_list(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(client, json_body={"messages": "not-a-list"})
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "messages_must_be_list"}}
    assert sink.events == []


def test_route_decision_rejects_overlong_session_id(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(client, headers={"X-SR-Session-Id": "s" * 257})
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "invalid_session_id"}}
    assert sink.events == []


def test_route_decision_rejects_invalid_department(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = decision_request(client, headers={"X-SR-Department": "%zz"})
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "invalid_department"}}
    assert sink.events == []


def test_route_decision_rejects_bodies_over_ten_mebibytes(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    oversized = {
        "model": "auto",
        "messages": [{"role": "user", "content": "x" * (10 * 1024 * 1024)}],
    }
    response = client.post("/internal/v1/route-decision", json=oversized)
    assert response.status_code == 413
    assert response.json() == {"error": {"message": "request_too_large"}}
    assert sink.events == []


def test_route_decision_returns_503_before_selecting_any_graph(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    response = decision_request(client)
    assert response.status_code == 503
    assert response.json() == {"error": {"message": "no_active_tree"}}
    # No graph was selected: there is nothing replayable to emit.
    assert sink.events == []


def _open_breaker(app, tree_id="main", logical_model="fast"):
    key = runtime_model_key(tree_scope(tree_id), logical_model)
    for _ in range(app.state.breaker.threshold):
        app.state.breaker.failure(key)


def test_route_decision_emits_routing_error_event_after_graph_selection(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    _open_breaker(app)
    response = decision_request(client)
    assert response.status_code == 503
    assert response.json() == {"error": {"message": "no_available_backend"}}
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["decision"]["status"] == "routing_error"
    assert event["decision"]["model"] is None
    assert event["graph"]["id"] == "main"
    assert event["decision"]["graph_selection"] == "default"
    assert any(item.get("event") == "routing_error" for item in event["trace"])


def test_route_decision_releases_the_scheduling_lease_on_success(tmp_path):
    app, client, _sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    assert decision_request(client).status_code == 200
    assert app.state.balancer.inflight("fast", scope="tree:main") == 0


def test_route_decision_releases_the_scheduling_lease_on_routing_error(tmp_path):
    app, client, _sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    _open_breaker(app)
    assert decision_request(client).status_code == 503
    assert app.state.balancer.inflight("fast", scope="tree:main") == 0


def test_route_decision_releases_the_scheduling_lease_on_response_failure(tmp_path):
    app, _client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")

    def explode(event):
        raise RuntimeError("event sink exploded")

    sink.emit = explode
    client = TestClient(app, raise_server_exceptions=False)
    response = decision_request(client)
    assert response.status_code == 500
    assert app.state.balancer.inflight("fast", scope="tree:main") == 0


def test_decision_event_trace_is_presented_for_replay(tmp_path):
    app, client, sink = make_kernel(tmp_path)
    publish_enabled_graph(app, "main", upstream="real-fast")
    assert decision_request(client).status_code == 200
    event = sink.events[0]
    assert event["trace"]
    assert all("phase" in item for item in event["trace"])
    assert "snapshot_revision" not in event["decision"]
    assert "graph_snapshot_id" not in json.dumps(event)


def test_provider_payload_returns_only_non_secret_provider_fields():
    runtime = runtime_from_dict(runtime_dict())
    assert provider_payload(runtime, "local") == {
        "name": "local",
        "base_url": "http://json-only.test/v1",
        "reasoning_format": "openai",
    }
