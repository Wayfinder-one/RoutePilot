"""P0-1: optional static bearer auth on the /admin API.

Design doc section 7 lists a static bearer token middleware as the minimum gate
before exposing the admin plane; the 2026-10-17 review (待修改清单 P0-1) put it
on the required list. Empty ``ROUTER_ADMIN_TOKEN`` keeps the old open behaviour
(loopback/dev); a non-empty token gates every /admin* request while leaving
/healthz, /v1 (data plane) and the static console reachable.
"""

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings

HEADER = {"Authorization": "Bearer sekret"}


def client(tmp_path, token: str = "") -> TestClient:
    return TestClient(create_app(
        Settings(db_path=str(tmp_path / "t.db"), admin_token=token),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": []}))))


def test_admin_open_when_no_token_configured(tmp_path):
    c = client(tmp_path)
    assert c.get("/admin/models").status_code == 200
    assert c.put("/admin/settings", json={"a": "b"}).status_code == 200


def test_admin_requires_the_configured_bearer(tmp_path):
    c = client(tmp_path, "sekret")
    for call in (
        c.get("/admin/models"),
        c.get("/admin/settings"),
        c.post("/admin/try", json={"text": "hello"}),
        c.put("/admin/settings", json={"a": "b"}),
        c.get("/admin/overview"),
    ):
        assert call.status_code == 401, call.request.url
        assert call.headers["www-authenticate"] == "Bearer"

    assert c.get("/admin/models",
                 headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/admin/models",
                 headers={"Authorization": "sekret"}).status_code == 401
    assert c.get("/admin/models", headers=HEADER).status_code == 200
    assert c.put("/admin/settings", json={"a": "b"},
                 headers=HEADER).status_code == 200


def test_healthz_console_and_v1_are_not_admin_gated(tmp_path):
    c = client(tmp_path, "sekret")
    assert c.get("/healthz").status_code == 200
    assert c.get("/console/index.html").status_code == 200
    assert c.get("/console/api.js").status_code == 200
    # /v1 is the data plane: it answers 502 for an unknown model, never 401
    r = c.post("/v1/chat/completions",
               json={"model": "auto",
                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code != 401
