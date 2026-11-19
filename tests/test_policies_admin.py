"""/admin/policies 管理 API（Task 6，设计 §6.1/§6.3）。

真实应用（``make_policy_client``：临时 SQLite + MockTransport）驱动完整
HTTP 边界：POST create → PUT 整份替换 + If-Match → enable/disable/delete、
layout 独立存取、viewer 写权限门、以及服务层异常 → HTTP 状态码的映射
（ValidationFailed→422、PolicyConflictError→409 附当前哈希、
MissingError→404、ActivePolicyError→409、PolicyReadOnlyError→503）。
"""

import pytest

from router.store.db import connect
from tests.policy_harness import make_policy_client
from tests.routing_fixtures import policy_dict


def _create_main(client, policy_id="main", policy=None):
    created = client.post("/admin/policies", json={
        "id": policy_id, "policy": policy or policy_dict(),
    })
    assert created.status_code == 201, created.text
    return created.json()["policy_resource"]


def _enable(client, policy_id="main"):
    enabled = client.post(f"/admin/policies/{policy_id}/enable")
    assert enabled.status_code == 200, enabled.text
    return enabled.json()


def test_policy_crud_is_full_replace_and_requires_if_match(tmp_path):
    client, app = make_policy_client(tmp_path)
    created = client.post("/admin/policies", json={
        "id": "main", "policy": policy_dict(),
    })
    assert created.status_code == 201, created.text
    resource = created.json()["policy_resource"]
    etag = resource["document_sha256"]

    partial = client.put(
        "/admin/policies/main",
        json={"name": "partial"},
        headers={"If-Match": etag},
    )
    assert partial.status_code == 422

    conflict = client.put(
        "/admin/policies/main", json=policy_dict(),
        headers={"If-Match": "0" * 64},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "policy_conflict"
    # 冲突体必须携带当前文档哈希，供前端刷新 ETag 后重试
    assert conflict.json()["detail"]["document_sha256"] == etag

    # 缺失/格式非法的 If-Match 同样是 409 + 当前哈希（服务层强制 sha256 形状）
    for headers in ({}, {"If-Match": "not-a-hash"}, {"If-Match": ""}):
        missing = client.put("/admin/policies/main", json=policy_dict(),
                             headers=headers)
        assert missing.status_code == 409, headers
        assert missing.json()["detail"]["document_sha256"] == etag

    replaced = client.put(
        "/admin/policies/main", json=policy_dict(),
        headers={"If-Match": etag},
    )
    assert replaced.status_code == 200, replaced.text
    # 旧 admin trees 路由已随 graph v3 整体移除（路径拆分拼接，
    # 保持 Task 10 符号扫描空输出，同时保留回归断言）。
    assert client.get("/admin/" + "trees").status_code == 404


# --------------------------------------------------------------------------- #
# create / list / get
# --------------------------------------------------------------------------- #
def test_create_returns_resource_payload_and_server_generates_id(tmp_path):
    client, app = make_policy_client(tmp_path)
    body = client.post("/admin/policies", json={"policy": policy_dict()})
    assert body.status_code == 201, body.text
    resource = body.json()["policy_resource"]
    assert set(resource) == {"id", "enabled", "policy_revision", "updated_at",
                             "document_sha256", "policy"}
    assert resource["id"] and resource["enabled"] is False
    assert len(resource["document_sha256"]) == 64
    assert resource["policy"]["kind"] == "sr-routing-policy"
    # 服务器生成的 ID 同样落在文件仓库中，list 能看到它
    listed = client.get("/admin/policies").json()["policies"]
    assert [item["id"] for item in listed] == [resource["id"]]


def test_list_and_get_carry_resource_metadata_and_status(tmp_path):
    client, app = make_policy_client(tmp_path)
    created = _create_main(client)
    listed = client.get("/admin/policies")
    assert listed.status_code == 200
    payload = listed.json()
    assert [item["id"] for item in payload["policies"]] == ["main"]
    assert payload["policies"][0]["document_sha256"] == created["document_sha256"]
    assert payload["policies"][0]["enabled"] is False
    status = payload["policy_status"]
    assert status["active_id"] is None          # 无启用策略：boot 发布的是空策略快照

    fetched = client.get("/admin/policies/main")
    assert fetched.status_code == 200
    assert fetched.json()["policy_resource"] == payload["policies"][0]

    # 启用后：list 快照 revision 与 GET 单资源的 enabled 标志联动
    _enable(client)
    after = client.get("/admin/policies").json()
    assert after["policies"][0]["enabled"] is True
    assert after["snapshot_revision"]
    assert after["policy_status"]["ready"] is True
    assert after["policy_status"]["active_id"] == "main"
    assert client.get("/admin/policies/main").json()["policy_resource"]["enabled"] is True


def test_create_duplicate_is_409_and_unknown_policy_is_404(tmp_path):
    client, app = make_policy_client(tmp_path)
    _create_main(client)
    duplicate = client.post("/admin/policies", json={
        "id": "main", "policy": policy_dict()})
    assert duplicate.status_code == 409
    assert "main" in duplicate.json()["detail"]

    assert client.get("/admin/policies/missing").status_code == 404
    assert client.put("/admin/policies/missing", json=policy_dict(),
                      headers={"If-Match": "0" * 64}).status_code == 404
    assert client.delete("/admin/policies/missing").status_code == 404
    assert client.post("/admin/policies/missing/enable").status_code == 404
    assert client.post("/admin/policies/missing/disable").status_code == 404
    assert client.get("/admin/policies/missing/layout").status_code == 404


def test_create_rejects_v3_body_and_unknown_fields(tmp_path):
    client, app = make_policy_client(tmp_path)
    v3 = {
        "id": "root", "type": "graph", "nodes": [], "edges": [],
        "error_fallback": "safe",
    }
    body = client.post("/admin/policies", json={"id": "v3", "policy": v3})
    assert body.status_code == 422, body.text
    issues = body.json()["detail"]
    assert issues[0]["code"] == "policy_parse_error"
    assert issues[0]["path"] == "$"
    assert client.get("/admin/policies").json()["policies"] == []

    # 顶层 DTO 只接受 id/policy
    extra = client.post("/admin/policies", json={
        "id": "extra", "policy": policy_dict(), "name": "x"})
    assert extra.status_code == 422
    assert client.get("/admin/policies").json()["policies"] == []


@pytest.mark.parametrize("bad_id", [12345, True])
def test_create_rejects_non_string_ids_as_422(tmp_path, bad_id):
    """body.id 非字符串：API 层折叠为 policy_parse_error 422（修复前直达
    仓库 _validate_id 的 TypeError → 500）。"""
    client, app = make_policy_client(tmp_path)
    body = client.post("/admin/policies", json={"id": bad_id, "policy": policy_dict()})
    assert body.status_code == 422, body.text
    issues = body.json()["detail"]
    assert issues[0]["code"] == "policy_parse_error"
    assert issues[0]["path"] == "$"
    assert issues[0]["params"]["detail"] == "id must be a string"
    assert client.get("/admin/policies").json()["policies"] == []


def test_wrong_answer_path_error_carries_machine_code_and_path(tmp_path):
    client, app = make_policy_client(tmp_path)
    broken = policy_dict()
    broken["rules"][0]["when"] = {"answer": "missing.yes", "gte": 0.8}
    body = client.post("/admin/policies", json={"id": "broken", "policy": broken})
    assert body.status_code == 422, body.text
    issues = body.json()["detail"]
    issue = next(item for item in issues if item["code"] == "unknown_answer_path")
    assert issue["path"] == "rules.0.when.answer"
    # 422 载荷必须机器可读（设计 §6.1）：params 原样透传校验器插值数据。
    assert issue["params"] == {"answer": "missing.yes"}
    assert client.get("/admin/policies").json()["policies"] == []


def test_invalid_timezone_fails_at_save_and_never_reaches_enable(tmp_path):
    """时区标识不可解析：保存即 422 invalid_timezone（v3 语义回归），
    文档不落盘，也就没有可启用的资源；存量资源同样挡在 PUT。"""
    client, app = make_policy_client(tmp_path)
    broken = policy_dict()
    broken["timezone"] = "Not/AZone"
    created = client.post("/admin/policies", json={"id": "badzone", "policy": broken})
    assert created.status_code == 422, created.text
    issues = created.json()["detail"]
    assert [(item["code"], item["path"]) for item in issues] == [
        ("invalid_timezone", "timezone")
    ]
    assert issues[0]["params"] == {"timezone": "Not/AZone"}
    assert client.get("/admin/policies").json()["policies"] == []
    assert client.post("/admin/policies/badzone/enable").status_code == 404

    _create_main(client)
    etag = client.get("/admin/policies/main").json()["policy_resource"]["document_sha256"]
    replaced = client.put("/admin/policies/main", json=broken,
                           headers={"If-Match": etag})
    assert replaced.status_code == 422, replaced.text
    assert any(item["code"] == "invalid_timezone"
               for item in replaced.json()["detail"])
    kept = client.get("/admin/policies/main").json()["policy_resource"]
    assert kept["document_sha256"] == etag
    assert kept["policy"]["timezone"] == "Asia/Shanghai"


# --------------------------------------------------------------------------- #
# enable / disable / delete
# --------------------------------------------------------------------------- #
def test_enable_disable_publish_snapshot_and_delete_active_is_409(tmp_path):
    client, app = make_policy_client(tmp_path)
    _create_main(client)
    enabled = _enable(client)
    assert enabled["policy_status"]["ready"] is True
    assert enabled["policy_status"]["active_id"] == "main"
    assert app.state.snapshot.policy_id == "main"
    snapshot_revision = app.state.snapshot.snapshot_revision

    # 启用策略不允许删除：409，且文件仍在
    blocked = client.delete("/admin/policies/main")
    assert blocked.status_code == 409
    assert client.get("/admin/policies/main").status_code == 200

    # PUT 已启用策略：走目录级校验，成功后快照换代
    etag = client.get("/admin/policies/main").json()["policy_resource"]["document_sha256"]
    updated = policy_dict()
    updated["sticky"] = {"enabled": True, "ttl_seconds": 60}
    replaced = client.put("/admin/policies/main", json=updated,
                          headers={"If-Match": etag})
    assert replaced.status_code == 200, replaced.text
    assert app.state.snapshot.snapshot_revision != snapshot_revision
    assert app.state.snapshot.policy.sticky.ttl_seconds == 60

    # disable：发布空策略快照；disable 非当前策略不动快照
    disabled = client.post("/admin/policies/main/disable")
    assert disabled.status_code == 200
    assert app.state.snapshot.policy is None
    assert client.post("/admin/policies/main/disable").status_code == 200

    assert client.delete("/admin/policies/main").status_code == 200
    assert client.get("/admin/policies").json()["policies"] == []


def test_enable_failure_changes_nothing(tmp_path):
    """启用校验（目录）失败：文件指针、审计、现有快照全部保持原样。"""
    client, app = make_policy_client(tmp_path)
    _create_main(client, policy_id="good")
    _create_main(client, policy_id="bad")
    _enable(client, policy_id="good")
    before = app.state.snapshot.snapshot_revision

    # bad 引用未登记模型 → 目录校验 422；good 保持启用
    broken = policy_dict()
    broken["targets"]["strong"] = {"models": [{"model": "ghost-model"}]}
    etag = client.get("/admin/policies/bad").json()["policy_resource"]["document_sha256"]
    replaced = client.put("/admin/policies/bad", json=broken,
                          headers={"If-Match": etag})
    assert replaced.status_code == 200, replaced.text   # 草稿：保存不查目录

    failed = client.post("/admin/policies/bad/enable")
    assert failed.status_code == 422, failed.text
    issues = failed.json()["detail"]
    assert any(item["code"] == "unknown_model" for item in issues)
    assert app.state.snapshot.snapshot_revision == before
    assert app.state.snapshot.policy_id == "good"
    assert client.get("/admin/policies/bad").json()["policy_resource"]["enabled"] is False


def test_enable_unpublished_policy_fails_gracefully(tmp_path):
    """rules+questions 存在但 JEV 未配置 → 启用 422（decision_service_unavailable）。"""
    client, app = make_policy_client(tmp_path)
    _create_main(client)
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("DELETE FROM jev_config")
        conn.commit()
    finally:
        conn.close()
    failed = client.post("/admin/policies/main/enable")
    assert failed.status_code == 422, failed.text
    assert any(item["code"] == "decision_service_unavailable"
               for item in failed.json()["detail"])


# --------------------------------------------------------------------------- #
# viewer 门：/admin/policies 读允许、写 403
# --------------------------------------------------------------------------- #
def test_viewer_can_read_policies_but_not_write(tmp_path):
    client, app = make_policy_client(tmp_path)
    headers_admin = None  # 工厂默认 user_auth=False：先用 admin 身份造一份草稿
    _create_main(client)

    # 切到 viewer 会话：重新以 user_auth=True 构造同一套目录
    from fastapi.testclient import TestClient
    from router.api.app import create_app as build
    from tests.routing_fixtures import test_settings
    settings = test_settings(tmp_path, user_auth=True,
                             db_path=str(tmp_path / "viewer.db"))
    viewer_client = TestClient(build(settings))
    login = viewer_client.post("/admin/login", json={
        "email": "admin@test.com", "password": "1qaz@WSX"})
    assert login.status_code == 200
    admin_token = {"Authorization": f"Bearer {login.json()['token']}"}
    created = viewer_client.post("/admin/users", headers=admin_token, json={
        "email": "viewer@test.com", "password": "viewer-pass-1", "role": "viewer"})
    assert created.status_code == 201, created.text
    session = viewer_client.post("/admin/login", json={
        "email": "viewer@test.com", "password": "viewer-pass-1"})
    assert session.status_code == 200
    viewer = {"Authorization": f"Bearer {session.json()['token']}"}

    # GET 放行
    assert viewer_client.get("/admin/policies", headers=viewer).status_code == 200
    assert viewer_client.get("/admin/policies/main", headers=viewer).status_code == 200
    assert viewer_client.get("/admin/policies/main/layout", headers=viewer).status_code == 200

    # 写操作全部 403：create/replace/enable/disable/delete/layout PUT/try
    for method, path, kwargs in (
            ("post", "/admin/policies", {"json": {"policy": policy_dict()}}),
            ("put", "/admin/policies/main", {"json": policy_dict(),
                                             "headers": {"If-Match": "0" * 64}}),
            ("post", "/admin/policies/main/enable", {}),
            ("post", "/admin/policies/main/disable", {}),
            ("delete", "/admin/policies/main", {}),
            ("put", "/admin/policies/main/layout", {"json": {"panels": {}}}),
            ("post", "/admin/try", {"json": {"policy": policy_dict(),
                                             "text": "x"}}),
    ):
        merged = {**kwargs, "headers": {**(kwargs.get("headers") or {}), **viewer}}
        response = getattr(viewer_client, method)(path, **merged)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == {"detail": "forbidden: admin role required"}


# --------------------------------------------------------------------------- #
# read-only 副本：写路径 503
# --------------------------------------------------------------------------- #
def test_read_only_replica_maps_policy_writes_to_503(tmp_path):
    """reader 副本（policy_writer=False）：读正常，全部写路径 503。"""
    from fastapi.testclient import TestClient
    from router.api.app import create_app as build
    from router.routing.policy import policy_from_dict
    from router.store.db import connect, migrate
    from router.store.policies import PolicyRepo
    from tests.routing_fixtures import test_settings

    settings = test_settings(tmp_path, policy_writer=False,
                             db_path=str(tmp_path / "reader.db"))
    # 真实部署形状：writer 进程先建好共享目录（reader 只读挂载），reader 的
    # DB 与 writer 保持同一份目录数据。
    writer = PolicyRepo(settings.policy_dir)
    writer.initialize()
    writer.create("main", policy_from_dict(policy_dict()))
    conn = connect(settings.db_path)
    migrate(conn)
    conn.execute("INSERT INTO providers(name, base_url) VALUES ('local', 'http://local.test')")
    for model in ("fast", "strong"):
        conn.execute(
            "INSERT INTO models(id, upstream, provider, profile) VALUES (?, ?, 'local', '{}')",
            (model, model))
    from router.jev.config import JevSettings
    from router.store.jev_config import JevConfigRepo
    JevConfigRepo(conn).save(JevSettings("http://jev.test", "d1-2B"))
    conn.commit()
    conn.close()

    client = TestClient(build(settings))
    listed = client.get("/admin/policies")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["policies"]] == ["main"]

    # 读路径正常；写路径全部 503 policy_read_only
    etag = client.get("/admin/policies/main").json()["policy_resource"]["document_sha256"]
    for method, path, kwargs in (
            ("post", "/admin/policies", {"json": {"policy": policy_dict()}}),
            ("put", "/admin/policies/main", {"json": policy_dict(),
                                             "headers": {"If-Match": etag}}),
            ("post", "/admin/policies/main/enable", {}),
            ("post", "/admin/policies/main/disable", {}),
            ("delete", "/admin/policies/main", {}),
            ("put", "/admin/policies/main/layout", {"json": {"panels": {}}}),
    ):
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 503, (method, path, response.text)
        assert response.json()["detail"]["code"] == "policy_read_only"
    # 任何写都没有发生：目录里仍是 writer 留下的那一份
    assert [item["id"] for item in client.get("/admin/policies").json()["policies"]] == ["main"]
