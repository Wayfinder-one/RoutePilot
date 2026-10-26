"""P0-1: optional static bearer auth on the /admin API.

Design doc section 7 lists a static bearer token middleware as the minimum gate
before exposing the admin plane; the 2026-10-17 review (待修改清单 P0-1) put it
on the required list. Empty ``ROUTER_ADMIN_TOKEN`` keeps the old open behaviour
(loopback/dev); a non-empty token gates every /admin* request while leaving
/healthz, /v1 (data plane) and the static console reachable.

2026-10-26 用户管理：``user_auth=True`` 时 /admin 还要求登录会话（见文件末尾
的用例）。本文件前面这些旧用例**故意不传 user_auth**：dataclass 默认 False
是既有测试/脚本的兼容路径，只有 ``Settings.from_env()`` 才默认开启。
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


def user_auth_client(tmp_path, token: str = "") -> TestClient:
    """user_auth=True（生产默认）：/admin 需要登录，静态 token 仍是后门。"""
    return TestClient(create_app(
        Settings(db_path=str(tmp_path / "t.db"), admin_token=token,
                 user_auth=True),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": []}))))


def test_user_auth_enabled_requires_login(tmp_path):
    c = user_auth_client(tmp_path)
    for call in (c.get("/admin/models"), c.get("/admin/settings"),
                 c.get("/admin/users"), c.get("/admin/me"),
                 c.put("/admin/settings", json={"a": "b"})):
        assert call.status_code == 401, call.request.url
        # 统一的登录提示（区别于旧 token 文案），前端据此跳登录页
        assert call.json() == {"detail": "unauthorized: /admin requires login"}
        assert call.headers["www-authenticate"] == "Bearer"

    # /admin/login 免鉴权：错误凭据是 401 invalid email or password，不是"未登录"
    r = c.post("/admin/login", json={"email": "admin@test.com",
                                     "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}

    # 首次启动 seed 的默认管理员可登录，之后 /admin 可访问
    r = c.post("/admin/login", json={"email": "admin@test.com",
                                     "password": "1qaz@WSX"})
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert c.get("/admin/models", headers=headers).status_code == 200

    # 静态 token 仍然可用（脚本/部署不回归）；未带任何凭据仍 401
    service = user_auth_client(tmp_path, "sekret")
    assert service.get("/admin/models", headers=HEADER).status_code == 200
    assert service.get("/admin/models").status_code == 401


def test_user_auth_does_not_gate_healthz_console_or_v1(tmp_path):
    c = user_auth_client(tmp_path, "sekret")
    assert c.get("/healthz").status_code == 200
    assert c.get("/console/index.html").status_code == 200
    assert c.get("/console/api.js").status_code == 200
    r = c.post("/v1/chat/completions",
               json={"model": "auto",
                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code != 401
