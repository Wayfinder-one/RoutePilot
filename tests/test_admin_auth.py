"""2026-10-26: /admin 鉴权 = 登录会话（静态 ROUTER_ADMIN_TOKEN 已移除）。

历史：2026-10-17 的 P0-1 给 /admin 加了静态 Bearer token；2026-10-26 用户管理
把它换成了邮箱+密码登录的会话 token（``POST /admin/login`` → opaque token，
DB 只存 hash）。
本文件只覆盖"门"本身；登录/角色/CRUD 的细节在 ``tests/test_users.py``。

两种模式：
* ``user_auth=False``（旧测试/本地 dev 兼容路径；fixtures 显式设置，dataclass 默认已是 True）：
  /admin 完全开放，Authorization 头被忽略。
* ``user_auth=True``（生产默认）：/admin/login 免鉴权，
  其余 /admin* 需要未过期会话。
"""

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from tests.routing_fixtures import test_settings

#: 旧静态 token 的字面值：移除后它不能通过任何校验。
OLD_STATIC_TOKEN = {"Authorization": "Bearer sekret"}


def _client(tmp_path, **overrides) -> TestClient:
    return TestClient(create_app(
        test_settings(tmp_path, **overrides),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": []}))))


def _login(c: TestClient, email="admin@test.com",
           password="1qaz@WSX") -> dict:
    r = c.post("/admin/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_admin_open_when_user_auth_disabled(tmp_path):
    """ROUTER_USER_AUTH=0：旧行为，/admin 完全开放（仅限本地 dev）。"""
    c = _client(tmp_path, user_auth=False)
    assert c.get("/admin/models").status_code == 200
    assert c.put("/admin/time-periods", json={"periods": []}).status_code == 200
    # 开放模式不看 Authorization 头（也不存在静态 token 了）
    assert c.get("/admin/models", headers=OLD_STATIC_TOKEN).status_code == 200
    me = c.get("/admin/me")
    assert me.status_code == 200
    assert me.json()["role"] == "admin" and me.json()["service"] is True


def test_user_auth_enabled_requires_login(tmp_path):
    """生产默认（user_auth=True）：无会话一律 401；默认管理员登录后放行。"""
    c = _client(tmp_path, user_auth=True)
    for call in (c.get("/admin/models"), c.get("/admin/changes"),
                 c.get("/admin/users"), c.get("/admin/me"),
                 c.put("/admin/jev/window", json={})):
        assert call.status_code == 401, call.request.url
        assert call.json() == {"detail": "unauthorized: /admin requires login"}
        assert call.headers["www-authenticate"] == "Bearer"

    # 旧静态 token 已移除：这个 Bearer 不是会话，必须 401
    assert c.get("/admin/models",
                 headers=OLD_STATIC_TOKEN).status_code == 401
    assert c.get("/admin/models",
                 headers={"Authorization": "Bearer wrong"}).status_code == 401

    # /admin/login 免鉴权：错误凭据是 401 invalid email or password，不是"未登录"
    r = c.post("/admin/login", json={"email": "admin@test.com",
                                     "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}

    # 首次启动 seed 的默认管理员可登录，之后 /admin 可访问
    headers = _login(c)
    assert c.get("/admin/models", headers=headers).status_code == 200
    assert c.get("/admin/users", headers=headers).status_code == 200
    me = c.get("/admin/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "admin@test.com"
    assert me.json()["role"] == "admin"
    assert "service" not in me.json()


def test_user_auth_does_not_gate_healthz_console_or_v1(tmp_path):
    c = _client(tmp_path, user_auth=True)
    assert c.get("/healthz").status_code == 200
    assert c.get("/console/index.html").status_code == 200
    assert c.get("/console/api.js").status_code == 200
    # /v1 is the data plane: it answers 502 for an unknown model, never 401
    r = c.post("/v1/chat/completions",
               json={"model": "auto",
                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code != 401
