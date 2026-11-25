"""The standalone decision kernel: no SQLite, no console, no model upstream.

Every assertion here is a kernel boundary held by the core/peripheral split:

* the decision endpoint never calls a model upstream (no ``/chat/completions``
  request leaves the kernel, whatever the request body asks for);
* constructing and serving the kernel never opens SQLite — including the
  complete graph-management API (create/update/departments/copy/enable/default/
  disable/try), which the kernel owns without any database;
* the console and the people/provider/log APIs do not exist on the kernel;
* the configured ``logging.routepilot`` sink is wired to the kernel's HTTP
  client, so one decision really produces one event POST.
"""
import json
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from router.api.core_app import create_core_app
from router.core_config import CoreSettings, LogSinkSettings
from router.routing.tree import tree_from_dict
from tests.decision_fixtures import core_settings
from tests.routing_fixtures import tree_dict


class RecordingSink:
    """Deterministic in-memory decision sink for kernel tests (synchronous)."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def forbidden_connect(*args, **kwargs):
    raise AssertionError("kernel opened SQLite")


async def forbidden_http(request):
    raise AssertionError("kernel made an unexpected external request")


def _graph_document(tree_id, *, upstream, enabled=True, default=True,
                    departments=()):
    root = {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "target", "type": "target", "priority": 1,
             "models": [{"model": "fast", "reasoning": None}]},
        ],
        "edges": [{"id": "e", "source": "start", "source_port": "out",
                   "target": "target", "target_port": "in_1", "condition": None}],
        "error_fallback": "target",
    }
    document = tree_dict(
        tree_id, root=root, enabled=enabled, default=default,
        departments=list(departments))
    document["backends"]["models"][0]["upstream"] = upstream
    return document


def publish_enabled_graph(app, tree_id, *, upstream):
    document = _graph_document(tree_id, upstream=upstream)
    app.state.graph_repo.create(tree_from_dict(document))
    app.state.publisher.refresh()


#: ``POST /internal/v1/trees`` DTO fields: life cycle (enabled/default) is
#: server state, so the input document deliberately excludes it.
TREE_INPUT_FIELDS = (
    "id", "name", "root", "canvas", "sticky", "timezone", "departments",
    "backends", "jev", "jev_window", "time_periods",
)


def tree_input(tree_id, *, upstream="real-fast"):
    """A self-contained graph document accepted by the kernel's graph API."""
    document = _graph_document(tree_id, upstream=upstream)
    return {key: document[key] for key in TREE_INPUT_FIELDS if key in document}
def publish_department_graph(app, tree_id, *, upstream, departments):
    """Extra enabled graph selected by department, never the default one."""
    document = _graph_document(
        tree_id, upstream=upstream, default=False, departments=departments)
    app.state.graph_repo.create(tree_from_dict(document))
    app.state.publisher.refresh()


def test_core_decision_does_not_call_model_upstream(tmp_path, monkeypatch):
    upstream_calls = []

    async def transport(request):
        if request.url.path.endswith("/chat/completions"):
            upstream_calls.append(request)
            return httpx.Response(500, json={"error": "must not execute"})
        return httpx.Response(200, json={"answers": {}})

    app = create_core_app(core_settings(tmp_path), transport=httpx.MockTransport(transport))
    publish_enabled_graph(app, "main", upstream="real-fast")
    client = TestClient(app)
    response = client.post("/internal/v1/route-decision", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    })
    assert response.status_code == 200
    assert response.json()["model"] == "real-fast"
    assert response.json()["logical_model"] == "fast"
    assert upstream_calls == []
    assert client.get("/console/index.html").status_code == 404
    assert client.get("/admin/users").status_code == 404


def test_core_needs_no_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr("router.store.db.connect", forbidden_connect)
    app = create_core_app(core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    assert TestClient(app).get("/readyz").status_code in {200, 503}


def test_core_decision_path_never_touches_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr("router.store.db.connect", forbidden_connect)
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    publish_enabled_graph(app, "main", upstream="real-fast")
    response = TestClient(app).post("/internal/v1/route-decision", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    })
    assert response.status_code == 200
    assert response.json()["model"] == "real-fast"


def test_core_takes_the_jev_key_only_from_its_configured_environment_variable(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TASK4_JEV_KEY", "jev-secret")
    settings = CoreSettings(
        graph_dir=str(tmp_path / "graphs"), jev_api_key_env="TASK4_JEV_KEY")
    app = create_core_app(
        settings, transport=httpx.MockTransport(forbidden_http))
    assert app.state.jev_api_key == "jev-secret"


def test_core_has_no_jev_key_when_the_environment_variable_is_unset(
        tmp_path, monkeypatch):
    monkeypatch.delenv("ROUTER_JEV_API_KEY", raising=False)
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    assert app.state.jev_api_key == ""


def test_core_decision_serves_no_peripheral_routes(tmp_path):
    """Only the kernel surface exists: health, readiness and the decision API."""
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    publish_enabled_graph(app, "main", upstream="real-fast")
    client = TestClient(app)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").status_code == 200
    for method, path in (
        ("get", "/console/index.html"),
        ("get", "/admin/users"),
        ("get", "/admin/providers"),
        ("get", "/admin/models"),
        ("get", "/admin/requests/recent"),
        ("get", "/admin/logs"),
        ("post", "/v1/chat/completions"),
        ("post", "/internal/v1/chat/completions"),
    ):
        if method == "get":
            response = client.get(path)
        else:
            response = client.post(
                path, json={"model": "auto", "messages": []})
        assert response.status_code == 404, (method, path)


def test_core_app_delivers_one_decision_event_to_the_configured_sink(tmp_path):
    posts = []

    async def transport(request):
        posts.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = CoreSettings(
        graph_dir=str(tmp_path / "graphs"),
        logging=LogSinkSettings(
            type="routepilot",
            url="http://console:8401/internal/v1/decision-logs",
            timeout_ms=100,
        ),
    )
    app = create_core_app(settings, transport=httpx.MockTransport(transport))
    publish_enabled_graph(app, "main", upstream="real-fast")
    # The sink worker only runs under the app lifespan: one event drains on exit.
    with TestClient(app) as client:
        response = client.post("/internal/v1/route-decision", json={
            "model": "auto", "messages": [{"role": "user", "content": "你好"}],
        })
        assert response.status_code == 200
    assert len(posts) == 1
    assert str(posts[0].url) == "http://console:8401/internal/v1/decision-logs"
    event = json.loads(posts[0].content)
    assert event["event_type"] == "routepilot.decision"
    assert event["graph"]["id"] == "main"
    assert event["request"]["text"] == "你好"
    assert app.state.decision_sink.sent_events == 1


def test_kernel_owns_graph_management_without_sqlite(tmp_path, monkeypatch):
    """create → list/get → update → departments → enable, then decide on it."""
    monkeypatch.setattr("router.store.db.connect", forbidden_connect)
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    client = TestClient(app)

    created = client.post("/internal/v1/trees", json=tree_input("main"))
    assert created.status_code == 201, created.text
    created_tree = created.json()["tree"]
    assert created_tree["id"] == "main"
    assert created_tree["enabled"] is False
    assert created_tree["default"] is False

    listed = client.get("/internal/v1/trees")
    assert listed.status_code == 200
    assert [tree["id"] for tree in listed.json()["trees"]] == ["main"]
    assert listed.json()["graph_status"]["ready"] is True

    fetched = client.get("/internal/v1/trees/main")
    assert fetched.status_code == 200
    assert fetched.json()["tree"]["id"] == "main"

    updated = client.put("/internal/v1/trees/main", json={
        "name": "内核图", "base_document_sha256": created_tree["document_sha256"]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["tree"]["name"] == "内核图"
    updated_tree = updated.json()["tree"]

    departments = client.put("/internal/v1/trees/main/departments", json={
        "departments": ["财务部"],
        "base_document_sha256": updated_tree["document_sha256"]})
    assert departments.status_code == 200, departments.text
    assert departments.json()["tree"]["departments"] == ["财务部"]

    enabled = client.post("/internal/v1/trees/main/enable")
    assert enabled.status_code == 200, enabled.text
    trees = {tree["id"]: tree for tree in enabled.json()["trees"]}
    assert trees["main"]["enabled"] is True
    assert trees["main"]["default"] is True

    decision = client.post("/internal/v1/route-decision", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    })
    assert decision.status_code == 200
    assert decision.json()["tree_id"] == "main"
    assert decision.json()["model"] == "real-fast"
    assert decision.json()["graph_selection"] == "default"

    department_decision = client.post(
        "/internal/v1/route-decision",
        headers={"X-SR-Department": quote("财务部", safe="")},
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]})
    assert department_decision.status_code == 200
    assert department_decision.json()["tree_id"] == "main"
    assert department_decision.json()["graph_selection"] == "department"


def test_kernel_copy_default_and_disable_graphs_without_sqlite(tmp_path, monkeypatch):
    """copy → set_default → decision follows it → disable rules + 409 mapping."""
    monkeypatch.setattr("router.store.db.connect", forbidden_connect)
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    client = TestClient(app)
    assert client.post(
        "/internal/v1/trees", json=tree_input("main")).status_code == 201
    assert client.post("/internal/v1/trees/main/enable").status_code == 200

    copied = client.post(
        "/internal/v1/trees/main/copy", json={"id": "copy", "name": "副本"})
    assert copied.status_code == 201, copied.text
    assert copied.json()["tree"]["id"] == "copy"
    assert copied.json()["tree"]["enabled"] is False

    default = client.post("/internal/v1/trees/copy/default")
    assert default.status_code == 200, default.text
    trees = {tree["id"]: tree for tree in default.json()["trees"]}
    assert trees["copy"]["default"] is True
    assert trees["copy"]["enabled"] is True
    assert trees["main"]["default"] is False
    assert trees["main"]["enabled"] is True

    decision = client.post("/internal/v1/route-decision", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    })
    assert decision.status_code == 200
    assert decision.json()["tree_id"] == "copy"

    # The default graph cannot be disabled while another enabled graph remains.
    blocked = client.post("/internal/v1/trees/copy/disable")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "default_graph_required"

    assert client.post("/internal/v1/trees/main/disable").status_code == 200
    assert client.post("/internal/v1/trees/copy/disable").status_code == 200
    assert client.post("/internal/v1/route-decision", json={
        "model": "auto", "messages": [{"role": "user", "content": "你好"}],
    }).status_code == 503


def test_kernel_try_dry_run_without_sqlite(tmp_path, monkeypatch):
    """Draft try and route-set try both work without SQLite; neither logs events."""
    monkeypatch.setattr("router.store.db.connect", forbidden_connect)
    app = create_core_app(
        core_settings(tmp_path), transport=httpx.MockTransport(forbidden_http))
    sink = RecordingSink()
    app.state.decision_sink = sink
    client = TestClient(app)

    # No graph published yet: the route-set path answers the designed 503.
    assert client.post("/internal/v1/try", json={"text": "你好"}).status_code == 503

    document = _graph_document("main", upstream="real-fast")
    draft = client.post("/internal/v1/try", json={
        "root": document["root"],
        "backends": document["backends"],
        "jev": document["jev"],
        "jev_window": document["jev_window"],
        "time_periods": document["time_periods"],
        "text": "你好",
    })
    assert draft.status_code == 200, draft.text
    payload = draft.json()
    assert payload["outcome"]["type"] == "hit"
    assert payload["outcome"]["model"] == "fast"
    assert payload["draft"] is True
    assert payload["warnings"] == []
    # 管理面试算不是真实决策：不产生决策事件。
    assert sink.events == []

    assert client.post(
        "/internal/v1/trees", json=tree_input("main")).status_code == 201
    assert client.post("/internal/v1/trees/main/enable").status_code == 200
    route_set = client.post("/internal/v1/try", json={"text": "你好"})
    assert route_set.status_code == 200, route_set.text
    assert route_set.json()["outcome"]["model"] == "fast"
    assert route_set.json()["graph_selection"] == "default"
    assert sink.events == []
