"""用户体系（2026-10-26）：口令哈希、会话仓储、/admin 鉴权与用户 CRUD。

设计依据 docs/superpowers/specs/2026-10-26-user-management-design.md；
实现计划 Task 1（存储与原语）+ Task 2（鉴权中间件/API）。本文件分两段：
前面是仓储/原语级用例（直接开会话连接），后面是 API 级用例（TestClient）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from router import auth
from router.api.app import create_app
from router.store.db import connect, migrate
from router.store.repositories import (ChangeRepo, EmailExists, SessionRepo,
                                       UserRepo)
from tests.routing_fixtures import test_settings

# --------------------------------------------------------------------------- #
# Task 1：口令哈希 / email 规范化 / UserRepo / SessionRepo
# --------------------------------------------------------------------------- #


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    migrate(c)
    yield c
    c.close()


def test_hash_password_roundtrip():
    stored = auth.hash_password("1qaz@WSX")
    parts = stored.split("$")
    # 存储格式是 spec §2 冻结的：scrypt$n$r$p$salt_hex$hash_hex
    assert parts[:4] == ["scrypt", "16384", "8", "1"]
    assert len(parts) == 6
    assert len(bytes.fromhex(parts[4])) == 16          # 16 字节随机盐
    assert auth.verify_password("1qaz@WSX", stored) is True
    # 每次哈希都换盐：同样的口令不会产生同一串（彩虹表/等值判断防不住）
    assert auth.hash_password("1qaz@WSX") != stored


def test_verify_password_rejects_wrong_and_malformed():
    stored = auth.hash_password("right-password")
    assert auth.verify_password("wrong-password", stored) is False
    # 坏格式必须返回 False 而不是抛异常（登录接口不能 500）
    for bad in ("", "nonsense", "scrypt$16384$8$1$zz$zz",
                "scrypt$notint$8$1$00$00", "bcrypt$16384$8$1$00$00",
                "scrypt$16384$8$1$00", None, 123):
        assert auth.verify_password("right-password", bad) is False
    # 合法前缀但摘要长度对不上（被截断的行）同样只是校验失败
    salt_hex, hash_hex = stored.split("$")[4:6]
    assert auth.verify_password(
        "right-password", f"scrypt$16384$8$1${salt_hex}${hash_hex[:8]}") is False


def test_normalize_email_and_validation():
    assert auth.normalize_email("  Admin@Test.COM ") == "admin@test.com"
    assert auth.normalize_email("") == ""
    assert auth.normalize_email(None) == ""
    assert auth.is_valid_email("admin@test.com") is True
    assert auth.is_valid_email("a.b+c@sub.example.cn") is True
    assert auth.is_valid_email("admin@test") is False        # 缺顶级域
    assert auth.is_valid_email("a b@test.com") is False      # 空白
    assert auth.is_valid_email("a@@b.com") is False
    assert auth.is_valid_email("") is False
    assert auth.is_valid_email(None) is False


def test_seed_admin_only_when_empty(conn):
    repo = UserRepo(conn)
    assert repo.seed_admin("admin@test.com", "1qaz@WSX") is True
    # 第二次调用（等价于重启）不再插入、也不覆盖已改过的密码
    assert repo.seed_admin("other@test.com", "other-pass") is False
    users = repo.list()
    assert len(users) == 1
    assert users[0]["email"] == "admin@test.com"
    assert users[0]["role"] == "admin"
    assert users[0]["status"] == "active"
    row = repo.get_by_email("ADMIN@test.com")            # 大小写不敏感
    assert row is not None
    assert auth.verify_password("1qaz@WSX", row["password_hash"]) is True


def test_seed_admin_uses_configured_email(tmp_path):
    db = str(tmp_path / "t.db")
    create_app(test_settings(tmp_path, db_path=db, user_auth=True,
                             admin_email="root@example.com",
                             admin_password="root-pass-1"))

    def rows():
        c = connect(db)
        try:
            return UserRepo(c).list()
        finally:
            c.close()

    users = rows()
    assert [u["email"] for u in users] == ["root@example.com"]
    assert users[0]["role"] == "admin"
    # 再次启动（幂等）：不重复插入、不覆盖已经改过的密码
    create_app(test_settings(tmp_path, db_path=db, user_auth=True,
                             admin_email="root@example.com",
                             admin_password="changed-pass-2"))
    conn = connect(db)
    try:
        users = UserRepo(conn).list()
        assert len(users) == 1
        row = UserRepo(conn).get_by_email("root@example.com")
        assert auth.verify_password("root-pass-1", row["password_hash"]) is True
        assert auth.verify_password("changed-pass-2", row["password_hash"]) is False
    finally:
        conn.close()
    # user_auth=False 不 seed（连哈希都不做：这是给旧行为部署的兼容路径）
    db2 = str(tmp_path / "legacy.db")
    create_app(test_settings(tmp_path, db_path=db2, user_auth=False))
    conn = connect(db2)
    try:
        assert UserRepo(conn).list() == []
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        conn.close()


def test_user_create_conflict_is_case_insensitive(conn):
    repo = UserRepo(conn)
    created = repo.create("User@Test.com", "password-1")
    assert created["email"] == "user@test.com"           # 存前 normalize
    with pytest.raises(EmailExists):
        repo.create("user@test.com", "password-2")
    with pytest.raises(EmailExists):
        repo.create("  USER@TEST.COM  ", "password-3")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_session_create_and_lookup(conn):
    user = UserRepo(conn).create("a@test.com", "password-1", role="admin")
    sessions = SessionRepo(conn)
    token, expires_at = sessions.create(user["id"], 168)
    assert token and len(token) >= 32
    # 明文 token 不落库，只存 sha256 hex
    rows = conn.execute("SELECT token_hash, user_id FROM user_sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == auth.session_token_hash(token)
    assert token not in rows[0]["token_hash"]
    found = sessions.user_for_token(token)
    assert found is not None
    assert found["id"] == user["id"]
    assert found["email"] == "a@test.com"
    assert found["role"] == "admin"
    assert found["expires_at"] == expires_at
    assert "password_hash" not in found
    assert sessions.user_for_token("bogus-token") is None
    assert sessions.user_for_token("") is None
    assert sessions.user_for_token(token + "x") is None


def test_session_expired_returns_none(conn):
    user = UserRepo(conn).create("b@test.com", "password-1")
    sessions = SessionRepo(conn)
    # TTL 0 / 负数 = 立即过期（测试用；生产默认 168h）
    token0, _ = sessions.create(user["id"], 0)
    assert sessions.user_for_token(token0) is None
    token_neg, _ = sessions.create(user["id"], -1)
    assert sessions.user_for_token(token_neg) is None
    # 惰性清理：过期行在校验时被删掉
    assert conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 0
    # purge_expired 独立可用（清历史遗留的过期行）
    token_ok, _ = sessions.create(user["id"], 168)
    conn.execute(
        "INSERT INTO user_sessions (token_hash, user_id, created_at, expires_at) "
        "VALUES ('deadbeef', ?, '2000-01-01T00:00:00+08:00', "
        "'2000-01-01T00:00:01+08:00')", (user["id"],))
    sessions.purge_expired()
    assert sessions.user_for_token(token_ok) is not None
    assert conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 1


def test_session_disabled_user_returns_none(conn):
    repo = UserRepo(conn)
    sessions = SessionRepo(conn)
    user = repo.create("c@test.com", "password-1", role="viewer")
    token, _ = sessions.create(user["id"], 168)
    assert sessions.user_for_token(token) is not None
    repo.update(user["id"], status="disabled")          # 停用立即生效
    assert sessions.user_for_token(token) is None


def test_delete_for_user(conn):
    repo = UserRepo(conn)
    sessions = SessionRepo(conn)
    u1 = repo.create("d@test.com", "password-1")
    u2 = repo.create("e@test.com", "password-2")
    t1, _ = sessions.create(u1["id"], 168)
    t2, _ = sessions.create(u1["id"], 168)
    t3, _ = sessions.create(u2["id"], 168)
    sessions.delete_for_user(u1["id"])                   # 改密/删用户吊销全部会话
    assert sessions.user_for_token(t1) is None
    assert sessions.user_for_token(t2) is None
    assert sessions.user_for_token(t3) is not None
    sessions.delete(t3)                                  # 登出只吊销当前会话
    assert sessions.user_for_token(t3) is None


def test_public_hides_password_hash(conn):
    repo = UserRepo(conn)
    user = repo.create("f@test.com", "password-1")
    assert "password_hash" in user                        # 内部行含哈希
    public = repo.public(user)
    assert set(public) == {"id", "email", "role", "status",
                           "created_at", "last_login_at"}
    assert "password_hash" not in public


def test_update_and_delete(conn):
    repo = UserRepo(conn)
    user = repo.create("g@test.com", "password-1")
    assert repo.count_active_admins() == 0
    updated = repo.update(user["id"], role="admin", email="G2@test.com")
    assert updated["role"] == "admin" and updated["email"] == "g2@test.com"
    assert repo.count_active_admins() == 1
    repo.update(user["id"], status="disabled")
    assert repo.count_active_admins() == 0
    assert repo.update("missing", role="viewer") is None
    assert repo.delete("missing") is False
    assert repo.delete(user["id"]) is True
    assert repo.get(user["id"]) is None


# --------------------------------------------------------------------------- #
# Task 2：/admin 鉴权中间件 + 登录 / 用户管理 API
# --------------------------------------------------------------------------- #
def api_client(tmp_path, *, name="api.db", user_auth=True, **overrides):
    """TestClient with the login-based /admin gate (user_auth=True by default)."""
    settings = test_settings(tmp_path, db_path=str(tmp_path / name), user_auth=user_auth,
                             **overrides)
    return TestClient(create_app(settings))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def login(c, email="admin@test.com", password="1qaz@WSX"):
    r = c.post("/admin/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def admin_headers(c):
    """登录一次拿会话 token（静态 token 已移除；测试库每次新会话无所谓）。"""
    return bearer(login(c))


def create_user(c, email, password="password-1", role="viewer"):
    r = c.post("/admin/users", json={"email": email, "password": password,
                                     "role": role}, headers=admin_headers(c))
    assert r.status_code == 201, r.text
    return r.json()["user"]


def audit_rows(c, kind="user"):
    conn = connect(c.app.state.settings.db_path)
    try:
        return [r for r in ChangeRepo(conn).list(limit=200) if r["kind"] == kind]
    finally:
        conn.close()


def test_login_me_logout_flow(tmp_path):
    c = api_client(tmp_path)
    # 无凭据：不是 403，而是统一的 401 登录提示
    unauth = c.get("/admin/me")
    assert unauth.status_code == 401
    assert unauth.json() == {"detail": "unauthorized: /admin requires login"}
    assert unauth.headers["www-authenticate"] == "Bearer"

    r = c.post("/admin/login", json={"email": "Admin@Test.com",  # 邮箱大小写不敏感
                                     "password": "1qaz@WSX"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token", "expires_at", "user"}
    assert set(body["user"]) == {"id", "email", "role"}
    assert body["user"]["email"] == "admin@test.com"
    assert body["user"]["role"] == "admin"
    headers = bearer(body["token"])

    me = c.get("/admin/me", headers=headers)
    assert me.status_code == 200
    assert me.json() == {"id": body["user"]["id"], "email": "admin@test.com",
                         "role": "admin", "expires_at": body["expires_at"]}
    # 会话能读管理面
    assert c.get("/admin/models", headers=headers).status_code == 200
    assert c.get("/admin/users", headers=headers).status_code == 200
    # 未认证仍是 401
    assert c.get("/admin/models").status_code == 401

    out = c.post("/admin/logout", headers=headers)
    assert out.status_code == 200 and out.json() == {"ok": True}
    assert c.get("/admin/me", headers=headers).status_code == 401


def test_login_wrong_password_and_unknown_email_are_401(tmp_path):
    c = api_client(tmp_path)
    for payload in ({"email": "admin@test.com", "password": "wrong-pass"},
                    {"email": "ghost@test.com", "password": "1qaz@WSX"},
                    {"email": "not-an-email", "password": "1qaz@WSX"}):
        r = c.post("/admin/login", json=payload)
        assert r.status_code == 401, payload
        # 未注册/密码错/格式非法统一文案，不泄露账号是否存在
        assert r.json() == {"detail": "invalid email or password"}


def test_disabled_user_login_401(tmp_path):
    c = api_client(tmp_path)
    viewer = create_user(c, "viewer@test.com", "viewer-pass-1", "viewer")
    token = login(c, "viewer@test.com", "viewer-pass-1")
    assert c.get("/admin/me", headers=bearer(token)).status_code == 200
    # 停用后：登录 401，已签发的会话立即失效
    assert c.put(f"/admin/users/{viewer['id']}", json={"status": "disabled"},
                 headers=admin_headers(c)).status_code == 200
    assert c.get("/admin/me", headers=bearer(token)).status_code == 401
    r = c.post("/admin/login", json={"email": "viewer@test.com",
                                     "password": "viewer-pass-1"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}


def test_login_updates_last_login(tmp_path):
    c = api_client(tmp_path)
    db = c.app.state.settings.db_path

    def last_login():
        conn = connect(db)
        try:
            return conn.execute(
                "SELECT last_login_at FROM users WHERE email = ?",
                ("admin@test.com",)).fetchone()[0]
        finally:
            conn.close()

    assert last_login() is None
    login(c)
    assert last_login() is not None


def test_static_admin_token_is_gone(tmp_path):
    """2026-10-26: 静态 ROUTER_ADMIN_TOKEN 已移除 —— Bearer 只能是登录会话。"""
    c = api_client(tmp_path)
    assert not hasattr(c.app.state.settings, "admin_token")
    # 旧静态 token 的字面值不再被接受
    assert c.get("/admin/models",
                 headers={"Authorization": "Bearer sekret"}).status_code == 401
    assert c.get("/admin/users",
                 headers={"Authorization": "Bearer wrong"}).status_code == 401
    # 不带凭据 401；登录会话可以访问
    assert c.get("/admin/users").status_code == 401
    assert c.get("/admin/users", headers=admin_headers(c)).status_code == 200


def test_viewer_can_read_trees_but_not_write(tmp_path):
    """viewer 门（Task 9 起）：树列表只读放行；树管理/启停等写操作 403。"""
    c = api_client(tmp_path)
    create_user(c, "viewer@test.com", "viewer-pass-1", "viewer")
    headers = bearer(login(c, "viewer@test.com", "viewer-pass-1"))
    # 只读面照常
    assert c.get("/admin/trees", headers=headers).status_code == 200
    assert c.get("/admin/models", headers=headers).status_code == 200
    # 非白名单写操作 → 403（含统一树管理与启停）
    tree_root = {"id": "r", "type": "route", "model": "qwen3-8b"}
    for method, path, payload in (
            ("post", "/admin/trees", {"name": "t", "root": tree_root}),
            ("put", "/admin/trees/tree-a", {"name": "t", "root": tree_root}),
            ("post", "/admin/trees/tree-a/copy",
             {"id": "tree-b", "name": "b"}),
            ("post", "/admin/trees/tree-a/enable", None),
            ("post", "/admin/trees/tree-a/disable", None),
            ("post", "/admin/try", {"tree_id": "tree-a", "text": "hello"}),
            ("put", "/admin/settings", {"a": "b"}),
            ("post", "/admin/models", {"id": "m1", "upstream": "u",
                                       "provider": "p"})):
        kwargs = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        r = getattr(c, method)(path, **kwargs)
        assert r.status_code == 403, (method, path)
        assert r.json() == {"detail": "forbidden: admin role required"}
    # 白名单：查自己/登出可用（logout 会吊销会话，放最后）
    assert c.get("/admin/me", headers=headers).status_code == 200
    assert c.post("/admin/logout", headers=headers).status_code == 200
    assert c.get("/admin/me", headers=headers).status_code == 401


def test_viewer_cannot_touch_users_api(tmp_path):
    c = api_client(tmp_path)
    viewer = create_user(c, "viewer@test.com", "viewer-pass-1", "viewer")
    headers = bearer(login(c, "viewer@test.com", "viewer-pass-1"))
    for method, path, payload in (
            ("get", "/admin/users", None),
            ("post", "/admin/users", {"email": "x@test.com",
                                      "password": "password-1"}),
            ("put", f"/admin/users/{viewer['id']}", {"role": "admin"}),
            ("post", f"/admin/users/{viewer['id']}/password",
             {"new_password": "password-2"}),
            ("delete", f"/admin/users/{viewer['id']}", None)):
        kwargs = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        r = getattr(c, method)(path, **kwargs)
        # viewer 连用户列表都不给：GET 也是 403（不是 401）
        assert r.status_code == 403, (method, path)
        assert r.json() == {"detail": "forbidden: admin role required"}
    # 自己没有被降级/删除
    by_email = {u["email"]: u for u in c.get("/admin/users",
                                             headers=admin_headers(c)).json()["users"]}
    assert by_email["viewer@test.com"]["role"] == "viewer"


def test_viewer_can_change_own_password(tmp_path):
    """白名单里的 POST /admin/me/password：viewer 也可以改自己的密码。"""
    c = api_client(tmp_path)
    create_user(c, "viewer@test.com", "viewer-pass-1", "viewer")
    headers = bearer(login(c, "viewer@test.com", "viewer-pass-1"))
    r = c.post("/admin/me/password",
               json={"old_password": "viewer-pass-1",
                     "new_password": "viewer-pass-2"}, headers=headers)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.get("/admin/me", headers=headers).status_code == 401  # 会话被吊销
    assert login(c, "viewer@test.com", "viewer-pass-2")


def test_admin_user_crud_flow(tmp_path):
    c = api_client(tmp_path)
    admin = bearer(login(c))

    created = c.post("/admin/users", json={"email": "New@Test.com",
                                           "password": "new-pass-1",
                                           "role": "admin"}, headers=admin)
    assert created.status_code == 201
    assert set(created.json()) == {"user"}
    user = created.json()["user"]
    assert set(user) == {"id", "email", "role", "status", "created_at",
                         "last_login_at"}
    assert user["email"] == "new@test.com"
    assert user["role"] == "admin" and user["status"] == "active"

    listed = c.get("/admin/users", headers=admin).json()["users"]
    assert {u["email"] for u in listed} == {"admin@test.com", "new@test.com"}

    updated = c.put(f"/admin/users/{user['id']}",
                    json={"email": "renamed@test.com", "role": "viewer"},
                    headers=admin)
    assert updated.status_code == 200
    assert updated.json()["user"]["email"] == "renamed@test.com"
    assert updated.json()["user"]["role"] == "viewer"

    reset = c.post(f"/admin/users/{user['id']}/password",
                   json={"new_password": "reset-pass-1"}, headers=admin)
    assert reset.status_code == 200 and reset.json() == {"ok": True}
    # 新密码生效、旧密码失效
    token = login(c, "renamed@test.com", "reset-pass-1")
    assert token
    assert c.post("/admin/login",
                  json={"email": "renamed@test.com",
                        "password": "new-pass-1"}).status_code == 401

    deleted = c.delete(f"/admin/users/{user['id']}", headers=admin)
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    assert c.get("/admin/me", headers=bearer(token)).status_code == 401  # 会话一并删
    assert [u["email"] for u in c.get("/admin/users",
                                      headers=admin).json()["users"]] \
        == ["admin@test.com"]

    # 404 / 422 边界
    for method, path, payload in (
            ("delete", f"/admin/users/{user['id']}", None),
            ("put", "/admin/users/nope", {"role": "viewer"}),
            ("post", "/admin/users/nope/password", {"new_password": "password-1"})):
        kwargs = {"headers": admin}
        if payload is not None:
            kwargs["json"] = payload
        r = getattr(c, method)(path, **kwargs)
        assert r.status_code == 404, (method, path)
        assert r.json() == {"detail": "user not found"}
    for payload in ({"email": "bad", "password": "password-1"},
                    {"email": "x@test.com", "password": "short"},
                    {"email": "x@test.com", "password": "password-1",
                     "role": "root"}):
        assert c.post("/admin/users", json=payload,
                      headers=admin).status_code == 422
    # 重置密码同样要求 >=8（422 来自请求体校验）
    other = create_user(c, "keep@test.com")
    assert c.post(f"/admin/users/{other['id']}/password",
                  json={"new_password": "short"},
                  headers=admin).status_code == 422


def test_create_user_duplicate_email_409_case_insensitive(tmp_path):
    c = api_client(tmp_path)
    create_user(c, "Dup@Test.com")
    for email in ("dup@test.com", "DUP@TEST.COM", "  dup@test.com  "):
        r = c.post("/admin/users", json={"email": email,
                                         "password": "password-1",
                                         "role": "viewer"}, headers=admin_headers(c))
        assert r.status_code == 409, email
        assert r.json() == {"detail": "email already exists"}
    # 与 seed 管理员冲突也是 409
    r = c.post("/admin/users", json={"email": "ADMIN@test.com",
                                     "password": "password-1"},
               headers=admin_headers(c))
    assert r.status_code == 409
    # 改名撞已有邮箱同样是 409
    other = create_user(c, "other@test.com")
    r = c.put(f"/admin/users/{other['id']}", json={"email": "dup@test.com"},
              headers=admin_headers(c))
    assert r.status_code == 409
    assert r.json() == {"detail": "email already exists"}


def test_cannot_delete_self(tmp_path):
    c = api_client(tmp_path)
    headers = bearer(login(c))
    me = c.get("/admin/me", headers=headers).json()
    r = c.delete(f"/admin/users/{me['id']}", headers=headers)
    assert r.status_code == 400
    assert r.json() == {"detail": "cannot delete yourself"}
    assert len(c.get("/admin/users", headers=headers).json()["users"]) == 1


def test_cannot_delete_last_active_admin(tmp_path):
    """DB 层的「不能删最后一个 active admin」守卫：用开放模式（user_auth=False）
    的请求方验证 —— 它没有 user row，self 检查不适用，能真正打到 last-admin 分支。
    （会话模式下 actor 自己就是一个 admin：删别人时至少还有 2 个 active admin，
    最后一个只能是自己，会先被 "cannot delete yourself" 拦住。）"""
    c = TestClient(create_app(test_settings(tmp_path, user_auth=False)))
    admin = c.post("/admin/users", json={"email": "solo@test.com",
                                         "password": "solo-pass-1",
                                         "role": "admin"}).json()["user"]
    r = c.delete(f"/admin/users/{admin['id']}")
    assert r.status_code == 400
    assert r.json() == {"detail": "cannot delete the last active admin"}
    # 加第二个 active admin 后允许删一个；再删最后一个又拦住
    second = c.post("/admin/users", json={"email": "second@test.com",
                                          "password": "second-pass-1",
                                          "role": "admin"}).json()["user"]
    assert c.delete(f"/admin/users/{admin['id']}").status_code == 200
    r = c.delete(f"/admin/users/{second['id']}")
    assert r.status_code == 400
    assert r.json() == {"detail": "cannot delete the last active admin"}


def test_cannot_demote_or_disable_last_admin(tmp_path):
    c = api_client(tmp_path)
    admin = bearer(login(c))                 # 当前会话就是 seed admin
    me = c.get("/admin/me", headers=admin).json()
    for payload in ({"role": "viewer"}, {"status": "disabled"},
                    {"role": "viewer", "status": "disabled"}):
        r = c.put(f"/admin/users/{me['id']}", json=payload, headers=admin)
        assert r.status_code == 400, payload
        assert r.json() == {"detail": "cannot demote or disable the last "
                                      "active admin"}
    unchanged = c.get("/admin/users", headers=admin).json()["users"][0]
    assert unchanged["role"] == "admin" and unchanged["status"] == "active"
    # 有了第二个 active admin 后：second 可以把 seed admin 降级/停用；
    # 最后 second 再降自己 → 又被 last-admin 拦住。
    create_user(c, "second@test.com", "second-pass-1", "admin")
    second = bearer(login(c, "second@test.com", "second-pass-1"))
    assert c.put(f"/admin/users/{me['id']}", json={"role": "viewer"},
                 headers=second).status_code == 200
    # seed admin 现在是 viewer：它的旧会话写权限也随之消失（角色每次现查）
    assert c.put("/admin/settings", json={"a": "b"},
                 headers=admin).status_code == 403
    assert c.put(f"/admin/users/{me['id']}", json={"status": "disabled"},
                 headers=second).status_code == 200
    me2 = c.get("/admin/me", headers=second).json()
    r = c.put(f"/admin/users/{me2['id']}", json={"role": "viewer"},
              headers=second)
    assert r.status_code == 400
    assert r.json() == {"detail": "cannot demote or disable the last active admin"}
    # 非降级/停用的更新（重复设置同值）不受影响
    assert c.put(f"/admin/users/{me2['id']}",
                 json={"role": "admin", "status": "active"},
                 headers=second).status_code == 200


def test_password_change_revokes_old_session(tmp_path):
    c = api_client(tmp_path)
    t1, t2 = login(c), login(c)                     # 两个会话，模拟两台浏览器
    r = c.post("/admin/me/password",
               json={"old_password": "1qaz@WSX",
                     "new_password": "brand-new-1"}, headers=bearer(t1))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.get("/admin/me", headers=bearer(t1)).status_code == 401  # 含当前会话
    assert c.get("/admin/me", headers=bearer(t2)).status_code == 401
    # 旧密码失效、新密码可登录
    assert c.post("/admin/login", json={"email": "admin@test.com",
                                        "password": "1qaz@WSX"}
                  ).status_code == 401
    t3 = login(c, "admin@test.com", "brand-new-1")
    # 旧密码错 → 400；短密码 → 422
    assert c.post("/admin/me/password",
                  json={"old_password": "wrong", "new_password": "another-1"},
                  headers=bearer(t3)).status_code == 400
    assert c.post("/admin/me/password",
                  json={"old_password": "brand-new-1", "new_password": "short"},
                  headers=bearer(t3)).status_code == 422
    # 会话还在：失败的改密不应该吊销任何东西
    assert c.get("/admin/me", headers=bearer(t3)).status_code == 200


def test_user_writes_audited_without_secrets(tmp_path):
    c = api_client(tmp_path)
    headers = bearer(login(c))
    user = c.post("/admin/users", json={"email": "audit@test.com",
                                        "password": "user-secret-1",
                                        "role": "viewer"}, headers=headers)
    assert user.status_code == 201
    uid = user.json()["user"]["id"]
    assert c.put(f"/admin/users/{uid}", json={"role": "admin"},
                 headers=headers).status_code == 200
    assert c.post(f"/admin/users/{uid}/password",
                  json={"new_password": "reset-secret-1"},
                  headers=headers).status_code == 200
    assert c.delete(f"/admin/users/{uid}", headers=headers).status_code == 200

    rows = audit_rows(c)
    assert {"create", "update", "reset_password", "delete"} <= {
        row["detail"]["action"] for row in rows}
    text = json.dumps([row["detail"] for row in rows], ensure_ascii=False,
                      sort_keys=True)
    # 审计只记 metadata：口令明文、哈希、salt、password 字段一个都不能有
    for secret in ("user-secret-1", "reset-secret-1", "1qaz@WSX"):
        assert secret not in text
    assert "scrypt$" not in text
    assert "password_hash" not in text
    assert '"password"' not in text
    assert "salt" not in text


def test_responses_never_contain_password_hash(tmp_path):
    c = api_client(tmp_path)
    responses = [c.post("/admin/login", json={"email": "admin@test.com",
                                              "password": "1qaz@WSX"})]
    token = responses[0].json()["token"]
    headers = bearer(token)
    responses.append(c.get("/admin/me", headers=headers))
    responses.append(c.get("/admin/users", headers=headers))
    created = c.post("/admin/users", json={"email": "resp@test.com",
                                          "password": "resp-secret-1"},
                     headers=headers)
    responses.append(created)
    uid = created.json()["user"]["id"]
    responses.append(c.put(f"/admin/users/{uid}", json={"role": "admin"},
                           headers=headers))
    responses.append(c.post(f"/admin/users/{uid}/password",
                            json={"new_password": "resp-secret-2"},
                            headers=headers))
    responses.append(c.get("/admin/users", headers=admin_headers(c)))
    responses.append(c.get("/admin/changes", headers=admin_headers(c)))
    responses.append(c.post("/admin/logout", headers=headers))
    for response in responses:
        body = response.text
        assert "password_hash" not in body, response.request.url
        assert "scrypt$" not in body, response.request.url
        for secret in ("1qaz@WSX", "resp-secret-1", "resp-secret-2"):
            assert secret not in body, (response.request.url, secret)


def test_login_without_user_auth_flag_is_legacy_open(tmp_path):
    """ROUTER_USER_AUTH=0 + 空 token = 2026-10-26 之前的行为：/admin 完全开放。"""
    db = str(tmp_path / "legacy.db")
    c = TestClient(create_app(test_settings(tmp_path, db_path=db, user_auth=False)))
    assert c.get("/admin/models").status_code == 200
    assert c.put("/admin/settings", json={"a": "b"}).status_code == 200
    assert c.get("/admin/users").json() == {"users": []}
    me = c.get("/admin/me")
    assert me.status_code == 200
    assert me.json()["role"] == "admin" and me.json()["service"] is True
    # 用户体系关闭 → 不 seed 管理员，所以也没法用用户名口令登录
    r = c.post("/admin/login", json={"email": "admin@test.com",
                                     "password": "1qaz@WSX"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}
    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        conn.close()

    # 开放模式不看 Authorization 头（静态 token 已移除，也没有会话概念）
    c2 = TestClient(create_app(test_settings(tmp_path, db_path=str(tmp_path / "legacy2.db"),
                                             user_auth=False)))
    assert c2.get("/admin/models",
                  headers={"Authorization": "Bearer whatever"}).status_code == 200
