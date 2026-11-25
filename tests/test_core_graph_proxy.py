"""Peripheral graph-management proxy: ten explicit routes, verbatim kernel replies.

Task 6 keeps the local graph routes mounted in ``create_app()`` (the mount swap
plus the console two-service harness belongs to Task 8), so this file exercises
the proxy router in a dedicated app that mirrors the peripheral boundary: the
real ``_install_admin_auth`` middleware, one SQLite connection for the audit row
and an injected ``CoreClient`` transport.

Pinned contract:

* exactly ten explicit routes are proxied (no catch-all): method, path and JSON
  body reach the kernel unchanged, and the kernel status/body come back verbatim
  (409 ``graph_conflict``, 422 issue arrays, 503 ``graph_storage_error`` ...);
* a non-dict kernel body is wrapped under ``detail``;
* the browser session credential never becomes a kernel ``Authorization`` header;
* the real admin middleware still rejects unauthenticated proxy calls with 401;
* each successful mutation appends one peripheral ``changes`` row, and an audit
  failure can never fail the proxied write;
* the real ``create_app`` wires ``app.state.core`` on its own HTTP client (the
  browser ``transport`` is not reused), closes it in the lifespan, stops mounting
  the kernel decision endpoint but keeps the decision-log receiver.
"""
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.api.app import _install_admin_auth, create_app
from router.api.routes_core_proxy import router as core_proxy_router
from router.config import Settings
from router.core_client import CoreClient
from router.store.db import connect, migrate
from router.store.repositories import ChangeRepo, SessionRepo, UserRepo
from tests.decision_fixtures import async_test, valid_event
from tests.routing_fixtures import test_settings

TREE = {
    "id": "main", "name": "", "enabled": False, "default": False,
    "departments": [], "tree_revision": "t" * 64, "document_sha256": "a" * 64,
}
TREE_REVISION = TREE["tree_revision"]


def tree_body(**overrides):
    return {"tree": dict(TREE, **overrides)}


class FakeCore:
    """Async transport handler standing in for the kernel HTTP API."""

    def __init__(self, replies=None):
        self.calls: list[dict] = []
        self.replies = dict(replies or {})

    def reply(self, method, path, status, body):
        self.replies[(method, path)] = (status, body)

    async def __call__(self, request):
        self.calls.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
            "body": json.loads(request.content) if request.content else None,
        })
        status, payload = self.replies[(request.method, request.url.path)]
        return httpx.Response(status, json=payload)


def proxy_app(tmp_path, handler, *, user_auth=False) -> FastAPI:
    """Dedicated peripheral app: real admin gate + proxy router mounted at /admin."""
    settings = test_settings(tmp_path, user_auth=user_auth)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        if user_auth:
            UserRepo(conn).seed_admin(settings.admin_email, settings.admin_password)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await app.state.core.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    # Same wiring as create_app: the client owns its HTTP client, fed here by a
    # MockTransport so every kernel call is observable.
    app.state.core = CoreClient(
        settings.core_url,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        timeout_seconds=settings.core_timeout_seconds,
    )
    _install_admin_auth(app, settings)
    app.include_router(core_proxy_router, prefix="/admin")
    return app


def _core_path(peripheral_path: str) -> str:
    return "/internal/v1" + peripheral_path.removeprefix("/admin")


def _new_session_token(app: FastAPI) -> str:
    conn = connect(app.state.settings.db_path)
    try:
        admin = UserRepo(conn).list()[0]
        token, _expires_at = SessionRepo(conn).create(admin["id"], ttl_hours=1)
    finally:
        conn.close()
    return token


def _changes(app: FastAPI) -> list[dict]:
    conn = connect(app.state.settings.db_path)
    try:
        return ChangeRepo(conn).list()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Ten explicit routes: path/method/body in, status/body out — nothing else.
# --------------------------------------------------------------------------- #
PARITY_CASES = [
    ("GET", "/admin/trees", None, 200,
     {"trees": [TREE], "snapshot_revision": "", "graph_status": {"ready": True}}),
    ("POST", "/admin/trees", {"name": "main"}, 201, tree_body()),
    ("GET", "/admin/trees/main", None, 200, tree_body()),
    ("PUT", "/admin/trees/main", {"name": "内核图", "base_document_sha256": "a" * 64},
     200, tree_body(name="内核图")),
    ("POST", "/admin/trees/main/copy", {"id": "copy"}, 201, tree_body(id="copy")),
    ("POST", "/admin/trees/main/enable", None, 200, {"trees": [TREE]}),
    ("POST", "/admin/trees/main/disable", None, 200, {"trees": []}),
    ("PUT", "/admin/trees/main/departments", {"departments": ["财务部"]},
     200, tree_body(departments=["财务部"])),
    ("POST", "/admin/trees/main/default", None, 200, {"trees": [TREE]}),
    ("POST", "/admin/try", {"text": "你好"}, 200,
     {"outcome": {"type": "hit", "model": "real-fast"}, "draft": False}),
]


@pytest.mark.parametrize("method,path,body,status,expected", PARITY_CASES)
def test_proxy_forwards_each_management_route_verbatim(
        tmp_path, method, path, body, status, expected):
    core_path = _core_path(path)
    fake = FakeCore({(method, core_path): (status, expected)})
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        response = client.request(method, path, json=body)
    assert response.status_code == status, response.text
    assert response.json() == expected
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert (call["method"], call["path"]) == (method, core_path)
    assert call["body"] == body


ERROR_CASES = [
    ("PUT", "/admin/trees/main", {"name": "x"},
     409, {"detail": {
         "code": "graph_conflict", "actual_document_sha256": "b" * 64}}),
    ("PUT", "/admin/trees/main/departments", {"departments": ["财务部"]},
     422, {"detail": [{
         "code": "graph_parse_error", "zh": "图解析失败", "en": "Graph parse failed",
         "node_id": None, "edge_id": None, "port": None, "level": "error",
         "params": {}}]}),
    ("POST", "/admin/trees/main/disable", None,
     503, {"detail": {"code": "graph_storage_error", "message": "read only"}}),
    ("POST", "/admin/try", {"text": "你好"}, 503, {"detail": "no_active_tree"}),
]


@pytest.mark.parametrize("method,path,body,status,expected", ERROR_CASES)
def test_proxy_preserves_kernel_error_status_and_detail(
        tmp_path, method, path, body, status, expected):
    core_path = _core_path(path)
    fake = FakeCore({(method, core_path): (status, expected)})
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        response = client.request(method, path, json=body)
    assert response.status_code == status
    assert response.json() == expected


def test_proxy_wraps_a_non_dict_kernel_body_under_detail(tmp_path):
    fake = FakeCore({("GET", "/internal/v1/trees"): (200, ["unexpected"])})
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        response = client.get("/admin/trees")
    assert response.status_code == 200
    assert response.json() == {"detail": ["unexpected"]}


def test_proxy_answers_503_when_the_kernel_is_unreachable(tmp_path):
    async def unreachable(request):
        raise httpx.ConnectError("kernel down", request=request)

    app = proxy_app(tmp_path, unreachable)
    with TestClient(app) as client:
        response = client.get("/admin/trees")
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "core_unavailable"}}


def test_proxy_exposes_exactly_the_ten_management_routes():
    """No catch-all: the mounted surface is the explicit management contract."""
    registered = {
        (method, route.path)
        for route in core_proxy_router.routes
        for method in route.methods
    }
    assert registered == {
        ("GET", "/trees"),
        ("POST", "/trees"),
        ("GET", "/trees/{tree_id}"),
        ("PUT", "/trees/{tree_id}"),
        ("POST", "/trees/{tree_id}/copy"),
        ("POST", "/trees/{tree_id}/enable"),
        ("POST", "/trees/{tree_id}/disable"),
        ("PUT", "/trees/{tree_id}/departments"),
        ("POST", "/trees/{tree_id}/default"),
        ("POST", "/try"),
    }


def test_proxy_is_not_a_catch_all(tmp_path):
    fake = FakeCore()
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        # A peripheral-only path stays 404 and a non-proxied method stays 405:
        # neither is forwarded to the kernel.
        assert client.get("/admin/models").status_code == 404
        assert client.delete("/admin/trees/main").status_code == 405
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Peripheral audit rows: one ``changes`` row per successful mutation.
# --------------------------------------------------------------------------- #
AUDIT_CASES = [
    ("POST", "/admin/trees", {"name": "main"}, 201, tree_body(),
     "create", {"id": "main"}),
    ("PUT", "/admin/trees/main", {"name": "x"}, 200, tree_body(name="x"),
     "update", {"id": "main", "tree_revision": TREE_REVISION}),
    ("PUT", "/admin/trees/main/departments", {"departments": ["财务部"]}, 200,
     tree_body(departments=["财务部"]),
     "update", {"id": "main", "tree_revision": TREE_REVISION}),
    ("POST", "/admin/trees/main/copy", {"id": "copy"}, 201,
     tree_body(id="copy"), "copy", {"source": "main", "id": "copy"}),
    ("POST", "/admin/trees/main/enable", None, 200, {"trees": [TREE]},
     "enable", {"id": "main"}),
    ("POST", "/admin/trees/main/disable", None, 200, {"trees": []},
     "disable", {"id": "main"}),
    ("POST", "/admin/trees/main/default", None, 200, {"trees": [TREE]},
     "set_default", {"id": "main"}),
]


@pytest.mark.parametrize(
    "method,path,body,status,core_body,action,detail", AUDIT_CASES)
def test_successful_mutation_records_one_peripheral_audit_row(
        tmp_path, method, path, body, status, core_body, action, detail):
    core_path = _core_path(path)
    fake = FakeCore({(method, core_path): (status, core_body)})
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        response = client.request(method, path, json=body)
    assert response.status_code == status
    assert response.json() == core_body
    rows = _changes(app)
    assert len(rows) == 1
    assert rows[0]["kind"] == "tree"
    assert rows[0]["detail"] == {"action": action, **detail}


def test_reads_and_dry_run_write_no_audit_row(tmp_path):
    fake = FakeCore()
    fake.reply("GET", "/internal/v1/trees", 200, {"trees": []})
    fake.reply("GET", "/internal/v1/trees/main", 200, tree_body())
    fake.reply("POST", "/internal/v1/try", 200, {"outcome": {"type": "hit"}})
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        assert client.get("/admin/trees").status_code == 200
        assert client.get("/admin/trees/main").status_code == 200
        assert client.post("/admin/try", json={"text": "你好"}).status_code == 200
    assert _changes(app) == []


def test_kernel_error_writes_no_audit_row(tmp_path):
    fake = FakeCore({("POST", "/internal/v1/trees"): (
        409, {"detail": {"code": "graph_already_exists", "tree_id": "main"}})})
    app = proxy_app(tmp_path, fake)
    with TestClient(app) as client:
        response = client.post("/admin/trees", json={"id": "main"})
    assert response.status_code == 409
    assert _changes(app) == []


def test_audit_failure_never_fails_the_proxied_write(tmp_path):
    fake = FakeCore({("POST", "/internal/v1/trees"): (201, tree_body())})
    app = proxy_app(tmp_path, fake)
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("DROP TABLE changes")
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as client:
        response = client.post("/admin/trees", json={"name": "main"})
    # The peripheral audit row failed, but the kernel write already happened and
    # its reply is returned unchanged.
    assert response.status_code == 201
    assert response.json() == tree_body()
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# Real admin auth gate in front of the proxy: 401 without a session.
# --------------------------------------------------------------------------- #
def test_proxy_requires_a_real_admin_session(tmp_path):
    fake = FakeCore()
    fake.reply("GET", "/internal/v1/trees", 200, {"trees": []})
    app = proxy_app(tmp_path, fake, user_auth=True)
    token = _new_session_token(app)
    with TestClient(app) as client:
        unauthorized = client.get("/admin/trees")
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {
            "detail": "unauthorized: /admin requires login"}
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        assert fake.calls == []

        authorized = client.get(
            "/admin/trees", headers={"Authorization": f"Bearer {token}"})
        assert authorized.status_code == 200
        assert authorized.json() == {"trees": []}
    assert len(fake.calls) == 1


def test_browser_session_credential_is_never_forwarded_to_the_kernel(tmp_path):
    fake = FakeCore()
    fake.reply("POST", "/internal/v1/trees", 201, tree_body())
    app = proxy_app(tmp_path, fake, user_auth=True)
    token = _new_session_token(app)
    with TestClient(app) as client:
        response = client.post(
            "/admin/trees",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "main"},
        )
    assert response.status_code == 201
    assert len(fake.calls) == 1
    headers = fake.calls[0]["headers"]
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert token not in json.dumps(headers)


# --------------------------------------------------------------------------- #
# Real create_app wiring (local graph mounts stay until Task 8's swap).
# --------------------------------------------------------------------------- #
def test_create_app_builds_an_independent_core_client(tmp_path):
    core_calls = []

    async def core_handler(request):
        core_calls.append(str(request.url))
        return httpx.Response(200, json={"trees": []})

    app = create_app(
        test_settings(
            tmp_path, core_url="http://kernel:8400", core_timeout_seconds=7.5),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": []})),
        core_transport=httpx.MockTransport(core_handler),
    )
    assert isinstance(app.state.core, CoreClient)
    assert app.state.core.base_url == "http://kernel:8400"
    assert app.state.core.timeout_seconds == 7.5
    # The core client owns a separate AsyncClient: the browser/provider transport
    # (and its pool) is never shared with the kernel management plane.
    assert app.state.core.http is not app.state.http
    with TestClient(app) as client:
        # Task 6 keeps the local graph mount; the proxy mount is Task 8's swap.
        assert client.get("/admin/trees").json()["trees"] == []
        assert core_calls == []
    assert app.state.core.http.is_closed
    assert app.state.http.is_closed


@async_test
async def test_create_app_core_client_uses_the_injected_core_transport(tmp_path):
    """The production wiring really routes management calls through core_transport."""
    core_calls = []

    async def core_handler(request):
        core_calls.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"trees": [], "graph_status": {"ready": True}})

    app = create_app(
        test_settings(tmp_path, core_url="http://kernel:8400"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": []})),
        core_transport=httpx.MockTransport(core_handler),
    )
    try:
        status, body = await app.state.core.admin_json("GET", "/trees")
    finally:
        await app.state.core.aclose()
        await app.state.http.aclose()
    assert status == 200
    assert body == {"trees": [], "graph_status": {"ready": True}}
    assert core_calls == [("GET", "/internal/v1/trees")]


def test_peripheral_does_not_mount_the_kernel_decision_endpoint(tmp_path):
    app = create_app(
        test_settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": []})),
    )
    client = TestClient(app)
    decision = client.post(
        "/internal/v1/route-decision",
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]})
    assert decision.status_code == 404
    # The decision-log receiver stays mounted outside /admin: the kernel posts
    # complete events to the peripheral without an admin login.
    received = client.post("/internal/v1/decision-logs", json=valid_event())
    assert received.status_code == 200
    assert received.json() == {"ok": True}


def test_settings_core_defaults_and_environment(monkeypatch):
    for name in ("ROUTER_CORE_URL", "ROUTER_CORE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.core_url == "http://127.0.0.1:8400"
    assert settings.core_timeout_seconds == 60.0

    monkeypatch.setenv("ROUTER_CORE_URL", "http://kernel:9000")
    monkeypatch.setenv("ROUTER_CORE_TIMEOUT_SECONDS", "4.5")
    settings = Settings.from_env()
    assert settings.core_url == "http://kernel:9000"
    assert settings.core_timeout_seconds == 4.5
