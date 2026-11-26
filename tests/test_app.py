import asyncio
import hashlib
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from router import auth
from router.api.app import create_app
from router.config import (
    DEFAULT_ADMIN_PASSWORD,
    MIN_ADMIN_PASSWORD_LENGTH,
    Settings,
)
from router.jev.service import JevService
from router.routing.document import graph_document_bytes, parse_graph_document
from router.routing.graph.model import TargetNode
from router.routing.graph.validate import validate_graph_for_enable, validate_graph_structure
from router.control.runtime import enable_context_for_runtime
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.repositories import (
    SessionRepo,
    SettingsRepo,
    UserRepo,
    seed_default,
)
from router.store.routing_trees import RoutingTreeRepo
from router.upstream.runtime_keys import runtime_model_key, tree_scope
from tests.routing_fixtures import (
    DEFAULT_PROVIDER_BASE_URL,
    core_app,
    graph_dict,
    kernel_state,
    portable_tree_dict,
    split_app_for,
    test_settings,
    tree_dict,
)
from tests.test_db import build_v6_database, create_v6_jev_service

REPO_ROOT = Path(__file__).resolve().parent.parent
JEV_ENV = {"jev_url": "http://jev.test", "jev_model": "NeoHorse-Jev-4B"}


def direct_graph(models=("qwen3-8b",)):
    """start → target 的直连图（无问题节点，不需要决策服务）。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": None} for model in models]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def test_health_console_and_empty_tree_boot(tmp_path):
    # 外围 + 进程内内核：图管理经代理读取内核的空调度集。
    app = split_app_for(tmp_path)
    client = TestClient(app)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/console/index.html").status_code == 200
    assert kernel_state(app).snapshot_set.snapshots == {}
    assert kernel_state(app).snapshot_set.default_snapshot is None
    assert client.get("/admin/trees").json()["trees"] == []
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "no_active_tree"
    assert not hasattr(app.state, "tagger")


def prepare_active_tree(tmp_path, raw):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_default(conn)
    conn.close()
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict({**raw, "enabled": True, "default": True}))
    return settings


def test_missing_snapshot_is_not_ready_and_disabled_snapshot_is_ready(tmp_path):
    """损坏的启用文档 -> 内核启动快照不可用；改为关闭态文档 -> 空快照就绪。"""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    path = repo.graphs / "main.json"
    path.write_bytes(b"{broken")
    app = core_app(graph_dir=settings.graph_dir)
    client = TestClient(app)
    assert client.get("/readyz").status_code == 503
    assert client.post("/internal/v1/route-decision", json={
        "messages": [{"role": "user", "content": "hello"}],
    }).status_code == 503
    path.write_bytes(graph_document_bytes(tree_from_dict(tree_dict())))
    asyncio.run(app.state.graph_reloader.poll_once())
    assert client.get("/readyz").status_code == 200


def test_startup_accepts_valid_direct_target(tmp_path):
    settings = prepare_active_tree(tmp_path, tree_dict(root=direct_graph()))
    app = core_app(graph_dir=settings.graph_dir)
    assert app.state.snapshot_set.default_snapshot.tree_id == "main"
    targets = [node for node in app.state.snapshot_set.default_snapshot.tree.root.nodes if isinstance(node, TargetNode)]
    assert [(node.id, [item.model for item in node.models]) for node in targets] == [
        ("direct", ["qwen3-8b"]),
    ]


def test_startup_with_invalid_active_tree_is_not_ready(tmp_path):
    raw = tree_dict(root=direct_graph())
    del raw["root"]["error_fallback"]
    settings = prepare_active_tree(tmp_path, raw)
    app = core_app(graph_dir=settings.graph_dir)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        response = client.post("/internal/v1/route-decision", json={"messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_valid_snapshot"


def test_graph_tree_with_questions_without_service_config_is_not_ready(tmp_path):
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][2]["models"] = [{"model": "qwen3-8b", "reasoning": None}]
    raw["jev"] = None  # 画布显式声明“不使用 JEV”：问题节点必须被启用校验拒绝
    settings = prepare_active_tree(tmp_path, raw)
    app = core_app(graph_dir=settings.graph_dir)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert "decision_service_unavailable" in ready.json()["last_load_error"]


def test_environment_jev_service_is_seeded_once(tmp_path):
    """空目录首次启动：环境 URL/model/api_key 创建 ``default`` 服务。"""
    settings = test_settings(tmp_path, jev_url="http://jev.test", jev_model="NeoHorse-Jev-4B")
    app = split_app_for(settings=settings)
    # 无启用图：内核路由集为空；种子只写外围数据库目录，不进入运行快照。
    assert kernel_state(app).snapshot_set.snapshots == {}
    assert kernel_state(app).snapshot_set.default_snapshot is None
    conn = connect(settings.db_path)
    try:
        services = JevServiceRepo(conn).list()
        assert [(service.id, service.model) for service in services] == [
            ("default", "NeoHorse-Jev-4B")]
        # v7：目录不再保存 JEV Key；种子只写非密钥字段，key_env 固定内核变量名。
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jev_services)")}
        assert "api_key" not in columns
        assert services[0].snapshot_dict()["key_env"] == "ROUTER_JEV_API_KEY"
    finally:
        conn.close()

    # 第二次启动不重复种子/不覆盖管理结果。
    create_app(settings)
    conn = connect(settings.db_path)
    try:
        assert len(JevServiceRepo(conn).list()) == 1
    finally:
        conn.close()


def test_first_boot_without_environment_seeds_when_environment_arrives_later(tmp_path):
    """首启无环境：不写标记；之后带环境启动仍会播种一次。"""
    settings = test_settings(tmp_path)
    create_app(settings)
    conn = connect(settings.db_path)
    try:
        assert JevServiceRepo(conn).list() == []
        assert SettingsRepo(conn).get("jev_catalog_seeded") is None
    finally:
        conn.close()

    create_app(replace(settings, **JEV_ENV))

    conn = connect(settings.db_path)
    try:
        assert [service.id for service in JevServiceRepo(conn).list()] == ["default"]
        assert SettingsRepo(conn).get("jev_catalog_seeded") == "1"
    finally:
        conn.close()


def test_deleting_all_jev_services_never_resurrects_the_environment_seed(tmp_path):
    """环境种子只发生一次：全部删除后重启也不会把 ``default`` 复活。"""
    settings = test_settings(tmp_path, **JEV_ENV)
    create_app(settings)
    conn = connect(settings.db_path)
    try:
        assert [service.id for service in JevServiceRepo(conn).list()] == ["default"]
        assert JevServiceRepo(conn).delete("default") is True
        assert JevServiceRepo(conn).list() == []
    finally:
        conn.close()

    create_app(settings)  # 目录为空但标记仍在 → 不再播种

    conn = connect(settings.db_path)
    try:
        assert JevServiceRepo(conn).list() == []
    finally:
        conn.close()


def test_partial_environment_jev_is_a_configuration_error(tmp_path):
    """URL/model 只配一半仍是启动配置错误（保持原语义）。"""
    with pytest.raises(RuntimeError, match="configured together"):
        create_app(test_settings(tmp_path, jev_url="http://jev.test"))


def test_environment_seed_never_overwrites_an_existing_catalog(tmp_path):
    settings = test_settings(tmp_path, **JEV_ENV)
    conn = connect(settings.db_path)
    migrate(conn)
    JevServiceRepo(conn).save(JevService("custom", "http://custom.test", "custom-model"))
    conn.close()

    app = split_app_for(settings=settings)

    conn = connect(settings.db_path)
    try:
        assert [service.id for service in JevServiceRepo(conn).list()] == ["custom"]
    finally:
        conn.close()
    # 目录没有被环境种子覆盖；内核空路由集不读取外围目录。
    assert kernel_state(app).snapshot_set.snapshots == {}


def test_embedded_runtime_jev_wins_over_environment_seed(tmp_path):
    """环境种子只写目录；活动图内嵌 JEV 仍是运行时事实源。"""
    settings = test_settings(tmp_path, **JEV_ENV)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    document = portable_tree_dict("main")
    repo.create(tree_from_dict({**document, "enabled": True, "default": True}))

    app = split_app_for(settings=settings)

    assert kernel_state(app).snapshot_set.default_snapshot.jev.base_url == "http://jev-json.test"
    assert kernel_state(app).snapshot_set.default_snapshot.jev.model == "json-jev"
    conn = connect(settings.db_path)
    try:
        services = JevServiceRepo(conn).list()
        assert [(service.id, service.model) for service in services] == [
            ("default", "NeoHorse-Jev-4B")]
    finally:
        conn.close()
    stored = kernel_state(app).graph_repo.get("main")
    assert stored.runtime.jev.model == "json-jev"


def shipped_document(name):
    """读取并严格解析随仓库发布的图文档（不改写任何字段）。"""
    content = (REPO_ROOT / "configs" / name).read_bytes()
    return parse_graph_document(
        content,
        tree_id="default-jev",
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_shipped_configs_are_v3_export_documents():
    for name in ("default_routing_tree.json", "production_routing_tree.json"):
        assert validate_graph_structure(shipped_document(name)) == [], name


def test_shipped_graph_configs_pass_enable_validation_with_own_runtime():
    """发布配置必须自包含通过启用校验：模型/provider/时段/JEV 都来自文档自身。"""
    for name in ("default_routing_tree.json", "production_routing_tree.json"):
        tree = shipped_document(name)
        assert validate_graph_for_enable(
            tree, enable_context_for_runtime(tree.runtime)) == [], name


class TryHandler:
    """MockTransport 分流：jev.test 回答图问题，其余请求落到假 OpenAI 上游。"""

    def __init__(self):
        self.jev_paths = []

    def __call__(self, request):
        if request.url.host == "jev.test":
            self.jev_paths.append(request.url.path)
            return httpx.Response(200, json={
                "answers": {"q": {"type": "choice", "probabilities": {"轻量": 0.85, "增强": 0.15}}},
                "usage": {"input_tokens": 10},
            })
        return httpx.Response(200, json={"choices": [], "usage": {}})


def question_graph(model="qwen3-8b"):
    """start → choice(轻量/增强) → safe target；target 引用已登记的默认模型。"""
    raw = graph_dict()
    for node in raw["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": model, "reasoning": None}]
    return raw


def composed_try_app(tmp_path, handler):
    """真实组合根：外围代理 + 进程内内核（JEV/上游都由 mock handler 承接）。"""
    settings = replace(
        prepare_active_tree(tmp_path, tree_dict(root=question_graph())), **JEV_ENV)
    app = split_app_for(
        settings=settings, transport=httpx.MockTransport(handler),
        kernel_transport=httpx.MockTransport(handler))
    # 拆分后 Auto 执行目录属于外围：把种子 provider 指到画布声明的 mock 上游。
    conn = connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE providers SET base_url = ? WHERE name = 'local'",
                (DEFAULT_PROVIDER_BASE_URL,))
    finally:
        conn.close()
    return app


def test_try_through_app_composition_routes_graph(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"tree_id": "main", "text": "你好"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is False
    assert handler.jev_paths == ["/v1/systemone"]
    assert [(item["target_id"], item["priority"], item["wave"]) for item in body["outcome"]["candidates"]] == [
        ("safe", 1, 1),
    ]
    assert body["outcome"]["waves"][0]["questions"] == ["q"]
    assert body["answers"]["q"]["ranking"] == ["easy", "strong"]


def test_try_through_app_composition_target_without_backend_is_503(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    # /admin/try 是 Auto 图路径：熔断键必须带 try 快照的图作用域。
    key = runtime_model_key(tree_scope("main"), "qwen3-8b")
    for _ in range(kernel_state(app).breaker.threshold):
        kernel_state(app).breaker.failure(key)
    assert kernel_state(app).breaker.is_open("qwen3-8b") is False
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"tree_id": "main", "text": "你好"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "target 'safe' has no available backend"
    assert handler.jev_paths == ["/v1/systemone"]


# ---------------------------------------------------------------------------
# C1：首次部署默认凭据 admin@test.com / 1qaz@WSX 在非 loopback 绑定上直接可用；
# 只有空/纯空白/过短的口令才会被启动守卫拒绝。
# ---------------------------------------------------------------------------

def test_default_admin_credentials_are_allowed_on_a_non_loopback_bind(tmp_path):
    settings = test_settings(tmp_path, host="0.0.0.0", user_auth=True)
    app = create_app(settings)
    assert app.state.settings.admin_email == "admin@test.com"
    assert app.state.settings.admin_password == DEFAULT_ADMIN_PASSWORD


def test_default_admin_credentials_survive_a_restart(tmp_path):
    """默认口令播种后，非 loopback 的第二次启动不能被 stale 扫描拦住。"""
    settings = test_settings(tmp_path, host="0.0.0.0", user_auth=True)
    create_app(settings)
    app = create_app(settings)
    assert app.state.settings.admin_password == DEFAULT_ADMIN_PASSWORD


def test_explicit_admin_credentials_are_allowed_on_a_non_loopback_bind(tmp_path):
    settings = test_settings(
        tmp_path, host="0.0.0.0", user_auth=True,
        admin_email="ops@example.com", admin_password="explicit-strong-pass",
    )
    app = create_app(settings)
    assert app.state.settings.admin_email == "ops@example.com"


def test_default_admin_password_is_allowed_with_a_custom_email(tmp_path):
    """默认口令本身是首次部署的期望值；改邮箱不影响它可用。"""
    settings = test_settings(
        tmp_path, host="0.0.0.0", user_auth=True,
        admin_email="ops@example.com", admin_password="1qaz@WSX",
    )
    app = create_app(settings)
    assert app.state.settings.admin_email == "ops@example.com"


def test_custom_admin_password_is_allowed_with_the_default_email(tmp_path):
    """只把默认密码换掉就足够：公开口令才是要拒绝的东西，默认邮箱不是。"""
    settings = test_settings(
        tmp_path, host="0.0.0.0", user_auth=True,
        admin_password="explicit-strong-pass",
    )
    app = create_app(settings)
    assert app.state.settings.admin_password == "explicit-strong-pass"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_default_admin_credentials_stay_allowed_on_loopback(tmp_path, host):
    settings = test_settings(tmp_path, host=host, user_auth=True)
    app = create_app(settings)
    assert app.state.settings.host == host


def test_default_admin_guard_does_not_fire_when_console_auth_is_off(tmp_path):
    """No admin is seeded without user auth: a non-loopback still must not trip."""
    settings = test_settings(
        tmp_path, host="0.0.0.0", user_auth=False,
        admin_email="admin@test.com", admin_password="1qaz@WSX",
    )
    app = create_app(settings)
    assert app.state.settings.user_auth is False


# ---------------------------------------------------------------------------
# C1 补充：非 loopback 绑定拒绝「空 / 空白 / 短」口令（不只默认口令）。
# 这些用例在旧代码上会启动成功并可用空口令登录（见 fix-h 报告 RED 证据）。
# ---------------------------------------------------------------------------

def _legacy_user_db(tmp_path, users, *, name="legacy.db"):
    """迁移 + 预置用户行，模拟上一版本/手写部署留下的 SQLite 卷。

    ``users`` 是 ``(email, password, role)`` 列表；返回数据库路径。
    """
    db_path = str(tmp_path / name)
    conn = connect(db_path)
    try:
        migrate(conn)
        seed_default(conn)
        repo = UserRepo(conn)
        for email, password, role in users:
            repo.create(email, password, role=role)
    finally:
        conn.close()
    return db_path


def test_empty_admin_password_is_refused_on_a_non_loopback_bind(tmp_path):
    """C1: 空 env 口令 + 旧库里已用空口令播种的管理员都必须启动即拒。"""
    db_path = _legacy_user_db(
        tmp_path, [("admin@test.com", "", "admin")], name="empty.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="")
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    assert "ROUTER_ADMIN_PASSWORD" in message
    assert f"at least {MIN_ADMIN_PASSWORD_LENGTH} characters" in message
    assert "0.0.0.0" in message


@pytest.mark.parametrize("password", ["   ", "        ", "\t \n", "short7",
                                     "1234567"])
def test_weak_admin_password_is_refused_on_a_non_loopback_bind(
        tmp_path, password):
    """空白（含恰好 8 个空格）与短于 8 字符的口令都不能通过启动守卫。"""
    settings = test_settings(
        tmp_path, host="0.0.0.0", user_auth=True, admin_password=password)
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    assert "ROUTER_ADMIN_PASSWORD" in message
    assert f"at least {MIN_ADMIN_PASSWORD_LENGTH} characters" in message
    assert "0.0.0.0" in message


def test_non_loopback_bind_with_a_strong_password_serves_a_real_login(tmp_path):
    """正向对照：显式强口令 + 空库在非 loopback 上照常启动并可真实登录。"""
    settings = test_settings(
        tmp_path, host="0.0.0.0", user_auth=True,
        admin_email="ops@example.com", admin_password="explicit-strong-pass")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/admin/login", json={
            "email": "ops@example.com",
            "password": "explicit-strong-pass",
        })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "ops@example.com"
    assert body["user"]["role"] == "admin"


# ---------------------------------------------------------------------------
# C2：旧 SQLite 卷里仍接受公开默认口令（或空口令）的管理员账号 —— 非
# loopback 启动必须拒绝，除非操作员显式授权 ROUTER_ADMIN_FORCE_RESET=1。
# ---------------------------------------------------------------------------

def test_stale_default_admin_password_refuses_a_non_loopback_bind(tmp_path):
    """C2: 重启换了强 env 口令也不能掩盖旧库里公开默认口令仍可登录的事实。"""
    db_path = _legacy_user_db(
        tmp_path, [("admin@test.com", DEFAULT_ADMIN_PASSWORD, "admin")])
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password")
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    # 发现事实（邮箱列表）+ 两条修复路径 + 点名的环境变量。
    assert "admin@test.com" in message
    assert "ROUTER_HOST=127.0.0.1" in message
    assert "ROUTER_ADMIN_FORCE_RESET" in message
    assert "ROUTER_ADMIN_PASSWORD" in message
    assert "revoked" in message


def test_force_reset_rotates_the_stale_default_admin_and_revokes_sessions(
        tmp_path, caplog):
    """显式授权后：旧默认口令 401、新 env 口令 200、旧会话 401、普通账号不动。"""
    db_path = str(tmp_path / "stale.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        seed_default(conn)
        repo = UserRepo(conn)
        admin = repo.create(
            "admin@test.com", DEFAULT_ADMIN_PASSWORD, role="admin")
        repo.create("viewer@example.com", "ordinary-secret-1", role="viewer")
        stale_token, _ = SessionRepo(conn).create(admin["id"], 168)
    finally:
        conn.close()

    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password", admin_force_reset=True)
    with caplog.at_level(logging.CRITICAL, logger="router.api.app"):
        app = create_app(settings)

    reset_warnings = [record.getMessage() for record in caplog.records
                      if record.levelno >= logging.CRITICAL]
    assert any("admin@test.com" in message for message in reset_warnings), (
        reset_warnings)

    with TestClient(app) as client:
        old_default = client.post("/admin/login", json={
            "email": "admin@test.com", "password": DEFAULT_ADMIN_PASSWORD})
        assert old_default.status_code == 401, old_default.text
        new_password = client.post("/admin/login", json={
            "email": "admin@test.com", "password": "new-strong-password"})
        assert new_password.status_code == 200, new_password.text
        # 重置之前签发的会话必须已被撤销。
        revoked = client.get("/admin/me", headers={
            "Authorization": f"Bearer {stale_token}"})
        assert revoked.status_code == 401, revoked.text
        # 普通（非默认）口令的账号不得被重置。
        untouched = client.post("/admin/login", json={
            "email": "viewer@example.com",
            "password": "ordinary-secret-1"})
        assert untouched.status_code == 200, untouched.text


def test_stale_empty_password_refuses_a_non_loopback_bind(tmp_path):
    """C2: 旧库把空口令持久化后，即便新 env 口令很强也必须先拒绝启动。"""
    db_path = _legacy_user_db(
        tmp_path, [("admin@test.com", "", "admin")], name="stale-empty.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password")
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    assert "admin@test.com" in message
    assert "ROUTER_ADMIN_FORCE_RESET" in message
    assert "ROUTER_HOST=127.0.0.1" in message


def test_force_reset_clears_a_persisted_empty_password(tmp_path):
    """C1 隐患落进旧库时同样处理：空口令 401，新 env 口令 200。"""
    db_path = _legacy_user_db(
        tmp_path, [("admin@test.com", "", "admin")], name="empty-reset.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password", admin_force_reset=True)
    app = create_app(settings)
    with TestClient(app) as client:
        empty = client.post("/admin/login", json={
            "email": "admin@test.com", "password": ""})
        assert empty.status_code == 401, empty.text
        new_password = client.post("/admin/login", json={
            "email": "admin@test.com", "password": "new-strong-password"})
        assert new_password.status_code == 200, new_password.text


def test_force_reset_with_an_unsafe_password_still_refuses_startup(tmp_path):
    """授权重置标志不能绕过 C1：重置目标口令本身必须强。"""
    db_path = _legacy_user_db(
        tmp_path, [("admin@test.com", DEFAULT_ADMIN_PASSWORD, "admin")],
        name="bad-reset.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="short7", admin_force_reset=True)
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    assert "ROUTER_ADMIN_PASSWORD" in message
    assert f"at least {MIN_ADMIN_PASSWORD_LENGTH} characters" in message


def test_force_reset_flag_is_read_from_the_environment(monkeypatch):
    """C2 的授权开关必须走既有的 ``_flag`` 解析（env 名固定）。"""
    monkeypatch.setenv("ROUTER_ADMIN_FORCE_RESET", "1")
    assert Settings.from_env().admin_force_reset is True
    monkeypatch.setenv("ROUTER_ADMIN_FORCE_RESET", "0")
    assert Settings.from_env().admin_force_reset is False


def test_force_reset_is_atomic_when_session_revocation_fails(tmp_path):
    """C2 原子性：吊销会话失败时密码重置必须一起回滚，不能留下半状态。

    ``set_password`` 与 ``delete_for_user`` 若各写一个事务，第二步失败会留下
    「密码已换、旧会话仍在」的中间态；下次启动不再把这行判为 stale，旧 token
    继续有效。这里用 SQLite 触发器让会话删除失败，断言两件事都没发生，且再次
    启动仍然拒绝（说明这行依旧是 stale）。
    """
    db_path = str(tmp_path / "atomic.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        seed_default(conn)
        repo = UserRepo(conn)
        admin = repo.create(
            "admin@test.com", DEFAULT_ADMIN_PASSWORD, role="admin")
        SessionRepo(conn).create(admin["id"], 168)
        before_hash = repo.get(admin["id"])["password_hash"]
        conn.execute(
            "CREATE TRIGGER fail_session_delete BEFORE DELETE ON user_sessions "
            "BEGIN SELECT RAISE(ABORT, 'session revoke failed'); END")
        conn.commit()
    finally:
        conn.close()

    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password", admin_force_reset=True)
    with pytest.raises(Exception) as excinfo:
        create_app(settings)
    assert "session revoke failed" in str(excinfo.value)

    conn = connect(db_path)
    try:
        after_hash = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (admin["id"],)).fetchone()["password_hash"]
        assert after_hash == before_hash, "密码不得在会话吊销失败时被改掉"
        assert conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?",
            (admin["id"],)).fetchone()[0] == 1
        conn.execute("DROP TRIGGER fail_session_delete")
        conn.commit()
    finally:
        conn.close()

    # 仍是 stale：没有授权标志的下一次启动必须继续拒绝，而不是悄悄放行旧 token。
    with pytest.raises(RuntimeError) as excinfo:
        create_app(test_settings(
            tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
            admin_password="new-strong-password"))
    assert "admin@test.com" in str(excinfo.value)


def test_peripheral_does_not_mount_the_kernel_jev_test(tmp_path):
    """I6: the credentialed JEV test belongs to the kernel internal listener."""
    app = split_app_for(tmp_path)
    response = TestClient(app).post("/internal/v1/jev-test", json={
        "base_url": "http://jev.test", "model": "m", "text": "t"})
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# 第四轮复审 Important 1：v6→v7 是破坏性迁移，未授权的 stale admin 必须在
# 任何迁移/种子/写入之前拒绝（只读 preflight）。旧实现先 migrate 再拒绝，
# 拒绝时 request_logs/graph_snapshots/request_snapshots 已被删、jev_services
# 的 api_key 已丢。
# ---------------------------------------------------------------------------

def _build_v6_with_stale_admin(tmp_path, password, *, name="v6-stale.db"):
    """真实 v6 库 + 一条 active admin + 三张日志表/旧 JEV 密钥各一行。"""
    db_path = str(tmp_path / name)
    conn = build_v6_database(db_path)
    try:
        UserRepo(conn).create("admin@test.com", password, role="admin")
        conn.execute(
            "INSERT INTO request_logs (id, ts, decision) VALUES "
            "('old-request', '2026-01-01T00:00:00+08:00', 'route')")
        conn.execute(
            "INSERT INTO graph_snapshots "
            "(snapshot_id, tree_id, snapshot_revision, graph, created_at) "
            "VALUES ('snap-1', 'main', 'r1', '{}', "
            "'2026-01-01T00:00:00+08:00')")
        conn.execute(
            "INSERT INTO request_snapshots "
            "(request_id, graph_snapshot_id, trace, created_at) "
            "VALUES ('req-1', 'snap-1', '{}', '2026-01-01T00:00:00+08:00')")
        create_v6_jev_service(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _v6_state(db_path):
    """用裸连接读 v6 库：不跑 WAL pragma、不写任何东西。"""
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(jev_services)")}
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("request_logs", "graph_snapshots", "request_snapshots")
        }
        api_key = conn.execute(
            "SELECT api_key FROM jev_services WHERE id = 'j1'").fetchone()[0]
        admin_hash = conn.execute(
            "SELECT password_hash FROM users WHERE email = 'admin@test.com'"
        ).fetchone()[0]
        settings_rows = [tuple(row) for row in conn.execute(
            "SELECT key, value FROM settings ORDER BY key")]
    finally:
        conn.close()
    return {"version": version, "tables": tables, "columns": columns,
            "counts": counts, "api_key": api_key, "admin_hash": admin_hash,
            "settings": settings_rows}


def _assert_v6_untouched(db_path):
    state = _v6_state(db_path)
    assert state["version"] == 6
    assert {"request_logs", "graph_snapshots", "request_snapshots"} \
        <= state["tables"]
    assert state["columns"] and "api_key" in state["columns"]
    assert state["counts"] == {
        "request_logs": 1, "graph_snapshots": 1, "request_snapshots": 1}
    assert state["api_key"] == "old-secret"
    # Residual 1b(iv)：preflight 是严格只读的，迁移前的拒绝不得写入任何
    # settings 行（尤其不能写扫描缓存）。
    assert state["settings"] == [], "拒绝启动不得在迁移前写 settings"
    return state


def test_refused_stale_admin_leaves_a_v6_database_untouched(tmp_path):
    """Important 1: 拒绝启动时 v6 库必须原封不动（迁移根本不能开始）。"""
    db_path = _build_v6_with_stale_admin(tmp_path, DEFAULT_ADMIN_PASSWORD)
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password")
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    assert "admin@test.com" in message
    assert "ROUTER_HOST=127.0.0.1" in message
    assert "ROUTER_ADMIN_FORCE_RESET" in message

    state = _assert_v6_untouched(db_path)
    # 未授权的启动也不得偷偷重置口令。
    assert auth.verify_password(
        DEFAULT_ADMIN_PASSWORD, state["admin_hash"]) is True


def test_authorized_force_reset_still_migrates_a_v6_database(tmp_path):
    """Important 1 正向对照：显式授权后 v6→v7 照常完成并重置旧默认口令。"""
    db_path = _build_v6_with_stale_admin(
        tmp_path, DEFAULT_ADMIN_PASSWORD, name="v6-reset.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password", admin_force_reset=True)
    app = create_app(settings)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not ({"request_logs", "graph_snapshots",
                     "request_snapshots"} & tables)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(jev_services)")}
        assert "api_key" not in columns
        # Residual 1b：preflight 不能写（迁移前只读），迁移后的可写连接负责把
        # 重置后的 clean 结论落进 settings 缓存。
        cached = conn.execute(
            "SELECT value FROM settings WHERE key = 'admin_weak_scan_state'"
        ).fetchone()
        assert cached is not None, "重置成功后的 clean 指纹必须在迁移后写入"
    finally:
        conn.close()

    with TestClient(app) as client:
        old = client.post("/admin/login", json={
            "email": "admin@test.com", "password": DEFAULT_ADMIN_PASSWORD})
        assert old.status_code == 401, old.text
        new = client.post("/admin/login", json={
            "email": "admin@test.com", "password": "new-strong-password"})
        assert new.status_code == 200, new.text


# ---------------------------------------------------------------------------
# 第四轮复审 Important 2：纯空白口令也是「信息为零」的公开凭据。
# ---------------------------------------------------------------------------

def test_whitespace_only_legacy_admin_password_refuses_before_migration(
        tmp_path):
    """旧库把 8 个空格持久化后，非 loopback 启动必须拒绝且不迁移。"""
    db_path = _build_v6_with_stale_admin(
        tmp_path, "        ", name="v6-whitespace.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password")
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    message = str(excinfo.value)
    assert "admin@test.com" in message
    assert "ROUTER_ADMIN_FORCE_RESET" in message
    state = _assert_v6_untouched(db_path)
    assert auth.verify_password("        ", state["admin_hash"]) is True


def test_force_reset_clears_a_persisted_whitespace_password(tmp_path):
    """显式授权后：8 空格口令 401、旧会话 401、新 env 口令 200。"""
    db_path = str(tmp_path / "whitespace-reset.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        seed_default(conn)
        admin = UserRepo(conn).create("admin@test.com", "        ",
                                      role="admin")
        stale_token, _ = SessionRepo(conn).create(admin["id"], 168)
    finally:
        conn.close()

    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password", admin_force_reset=True)
    app = create_app(settings)
    with TestClient(app) as client:
        blank = client.post("/admin/login", json={
            "email": "admin@test.com", "password": "        "})
        assert blank.status_code == 401, blank.text
        revoked = client.get("/admin/me", headers={
            "Authorization": f"Bearer {stale_token}"})
        assert revoked.status_code == 401, revoked.text
        new = client.post("/admin/login", json={
            "email": "admin@test.com", "password": "new-strong-password"})
        assert new.status_code == 200, new.text


def test_legacy_short_non_whitespace_password_is_not_auto_flagged(tmp_path):
    """策略决定：旧的短（非空白）口令不像空白/默认口令那样公开可知，不拦启动。

    启动门只拦「公开已知 / 空白 / 纯空白」；操作员应主动轮换旧的短口令。
    """
    db_path = _legacy_user_db(
        tmp_path, [("admin@test.com", "1234567", "admin")],
        name="short-legacy.db")
    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post("/admin/login", json={
            "email": "admin@test.com",
            "password": "1234567"}).status_code == 200


def test_preflight_scan_is_read_only_and_tolerates_old_schemas(tmp_path):
    """preflight 辅助：只读、缺库/缺表返回空、缺 status 列时按 active 处理。"""
    from router.api.app import _preflight_stale_admin_users

    missing = str(tmp_path / "does-not-exist.db")
    assert _preflight_stale_admin_users(missing) == []
    assert not Path(missing).exists(), "preflight 不得创建数据库文件"

    # 有文件但没有 users 表（例如更旧的库/别的 SQLite）。
    other = str(tmp_path / "other.db")
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    assert _preflight_stale_admin_users(other) == []

    # 非常旧的 users 表没有 status 列：所有行都当作 active。
    legacy = str(tmp_path / "legacy-schema.db")
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL, "
        "password_hash TEXT NOT NULL)")
    conn.execute("INSERT INTO users VALUES ('old', 'old@test.com', ?)",
                 (auth.hash_password(DEFAULT_ADMIN_PASSWORD),))
    conn.commit()
    conn.close()
    stale = _preflight_stale_admin_users(legacy)
    assert [user["email"] for user in stale] == ["old@test.com"]
    assert auth.verify_password(
        DEFAULT_ADMIN_PASSWORD, stale[0]["password_hash"]) is True

    # v7 库：只报 active 且接受公开口令的行；强口令/disabled 不报。
    db_path = str(tmp_path / "v7.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        repo = UserRepo(conn)
        repo.create("stale@test.com", DEFAULT_ADMIN_PASSWORD, role="admin")
        repo.create("strong@test.com", "strong-enough-1", role="admin")
        disabled = repo.create(
            "disabled@test.com", DEFAULT_ADMIN_PASSWORD, role="admin")
        repo.update(disabled["id"], status="disabled")
    finally:
        conn.close()
    assert [user["email"] for user in _preflight_stale_admin_users(db_path)] \
        == ["stale@test.com"]


def test_stale_scan_tolerates_a_users_table_without_status(tmp_path):
    """Residual 3：缺 status 列的（手工/极旧）用户表按 active 处理，不抛 KeyError。

    preflight 一直容忍这种表；post-migration 扫描（``_stale_admin_users``）也必须
    同样防御——迁移链本身不会补 status 列。
    """
    from router.api.app import _stale_admin_users

    conn = connect(str(tmp_path / "hand-made.db"))
    try:
        conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO users VALUES ('stale', 'stale@test.com', ?, "
            "'2026-01-01T00:00:00+08:00')",
            (auth.hash_password(DEFAULT_ADMIN_PASSWORD),))
        conn.execute(
            "INSERT INTO users VALUES ('strong', 'strong@test.com', ?, "
            "'2026-01-01T00:00:00+08:00')",
            (auth.hash_password("strong-password-1"),))
        conn.commit()
        stale = _stale_admin_users(conn)
    finally:
        conn.close()
    assert [user["email"] for user in stale] == ["stale@test.com"]
    assert stale[0]["status"] == "active", "缺列时扫描按 active 处理"


# ---------------------------------------------------------------------------
# 第四轮复审 Residual 1：非 loopback 启动对同一批哈希只扫描一次，并把「已证实
# clean」的指纹写进 settings，后续启动在哈希逐字节相同时跳过整轮 scrypt 扫描。
# 任何口令/账号变化 → 指纹变化 → 重扫；stale 结论永不落缓存。
# ---------------------------------------------------------------------------

def _account_db(tmp_path, accounts, *, name="accounts.db"):
    """v7 库 + 给定 ``(email, password, role)`` 账号（直接落库，绕过启动）。"""
    db_path = str(tmp_path / name)
    conn = connect(db_path)
    try:
        migrate(conn)
        seed_default(conn)
        for email, password, role in accounts:
            UserRepo(conn).create(email, password, role=role)
    finally:
        conn.close()
    return db_path


def _non_loopback_settings(tmp_path, db_path, **overrides):
    values = dict(
        db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password")
    values.update(overrides)
    return test_settings(tmp_path, **values)


def _count_weak_scans(monkeypatch):
    """统计 ``is_weak_stored_password`` 调用（每个被扫描的 active 账号一次）。"""
    from router import password_policy

    calls = []
    real = password_policy.is_weak_stored_password

    def counting(stored_hash, **kwargs):
        calls.append(stored_hash)
        return real(stored_hash, **kwargs)

    monkeypatch.setattr(password_policy, "is_weak_stored_password", counting)
    return calls


def test_clean_scan_cache_skips_scrypt_on_the_second_non_loopback_start(
        tmp_path, monkeypatch):
    """Residual 1b(i)：两次连续的非 loopback clean 启动，第二次 0 次扫描。"""
    db_path = _account_db(tmp_path, [
        ("admin@test.com", "strong-password-1", "admin"),
        ("viewer@test.com", "strong-password-2", "viewer"),
    ])
    settings = _non_loopback_settings(tmp_path, db_path)
    calls = _count_weak_scans(monkeypatch)

    create_app(settings)
    assert len(calls) == 2, "首次启动应扫描两个 active 账号"
    conn = connect(db_path)
    try:
        cached = SettingsRepo(conn).get("admin_weak_scan_state")
    finally:
        conn.close()
    assert cached, "clean 扫描结论必须写入 settings 指纹缓存"

    calls.clear()
    create_app(settings)
    assert calls == [], "第二次启动必须命中 clean 指纹缓存，不做任何 scrypt 扫描"


def test_credential_change_invalidates_the_clean_scan_cache(
        tmp_path, monkeypatch):
    """Residual 1b(ii)：改口令 / 直接插入 stale 账号都会让缓存失效并重扫。"""
    db_path = _account_db(
        tmp_path, [("admin@test.com", "strong-password-1", "admin")])
    settings = _non_loopback_settings(tmp_path, db_path)
    calls = _count_weak_scans(monkeypatch)
    create_app(settings)
    assert len(calls) == 1

    # 改一条口令（哈希变化）→ 缓存失效 → 重扫；仍 clean → 缓存更新。
    conn = connect(db_path)
    try:
        admin = UserRepo(conn).get_by_email("admin@test.com")
        UserRepo(conn).set_password(admin["id"], "another-strong-pass")
    finally:
        conn.close()
    calls.clear()
    create_app(settings)
    assert len(calls) == 1, "口令哈希变化后必须重新扫描"
    calls.clear()
    create_app(settings)
    assert calls == [], "重扫 clean 后缓存更新，第三次启动再次跳过"

    # 直接插入一个 stale 账号（绕过 API）→ 指纹变化 → 重扫并拒绝启动。
    conn = connect(db_path)
    try:
        UserRepo(conn).create("late@test.com", DEFAULT_ADMIN_PASSWORD)
    finally:
        conn.close()
    calls.clear()
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)
    assert "late@test.com" in str(excinfo.value)
    assert calls, "缓存失效后必须重扫，才能发现新插入的 stale 账号"


def test_force_reset_reuses_the_preflight_scan(tmp_path, monkeypatch):
    """Residual 1a/1b(iii)：授权重置路径只扫描一次，重置后写入 clean 指纹。"""
    db_path = _account_db(tmp_path, [
        ("first@test.com", DEFAULT_ADMIN_PASSWORD, "admin"),
        ("second@test.com", "        ", "viewer"),
    ])
    settings = _non_loopback_settings(
        tmp_path, db_path, admin_force_reset=True)
    calls = _count_weak_scans(monkeypatch)
    app = create_app(settings)
    assert len(calls) == 2, \
        "两个 stale 账号各扫描一次；post-migration 复用 preflight 结果，不得重扫"

    conn = connect(db_path)
    try:
        assert SettingsRepo(conn).get("admin_weak_scan_state"), \
            "重置后的 clean 结论必须落缓存（post-reset 哈希 = 已验证的 env 口令）"
        for email in ("first@test.com", "second@test.com"):
            assert auth.verify_password(
                "new-strong-password",
                UserRepo(conn).get_by_email(email)["password_hash"]) is True
    finally:
        conn.close()
    with TestClient(app) as client:
        assert client.post("/admin/login", json={
            "email": "first@test.com",
            "password": DEFAULT_ADMIN_PASSWORD}).status_code == 401
        assert client.post("/admin/login", json={
            "email": "first@test.com",
            "password": "new-strong-password"}).status_code == 200


def test_guard_returns_the_preflight_result_or_none_when_it_did_not_scan(
        tmp_path):
    """Residual 1a 契约：loopback / user_auth off → None（没有扫描）；非 loopback
    → preflight 列表（``[]`` 表示已证实干净）。"""
    from router.api.app import _guard_stale_admin_credentials

    db_path = _account_db(
        tmp_path, [("admin@test.com", "strong-password-1", "admin")])
    assert _guard_stale_admin_credentials(
        test_settings(tmp_path, db_path=db_path)) is None
    assert _guard_stale_admin_credentials(
        test_settings(tmp_path, db_path=db_path, user_auth=True,
                      host="0.0.0.0",
                      admin_password="new-strong-password")) == []

    stale_db = _account_db(
        tmp_path, [("stale@test.com", DEFAULT_ADMIN_PASSWORD, "admin")],
        name="stale.db")
    with pytest.raises(RuntimeError):
        _guard_stale_admin_credentials(
            test_settings(tmp_path, db_path=stale_db, user_auth=True,
                          host="0.0.0.0",
                          admin_password="new-strong-password"))


# ---------------------------------------------------------------------------
# 第四轮复审 Minor：多账号强制重置必须是整批原子（不是逐账号事务）。
# ---------------------------------------------------------------------------

def test_force_reset_is_batch_atomic_across_multiple_stale_accounts(tmp_path):
    """第二个账号吊销会话失败时，第一个账号也必须回滚；修好后重试两个都重置。"""
    db_path = str(tmp_path / "batch-atomic.db")
    conn = connect(db_path)
    try:
        migrate(conn)
        seed_default(conn)
        repo = UserRepo(conn)
        first = repo.create("first@test.com", DEFAULT_ADMIN_PASSWORD,
                            role="admin")
        second = repo.create("second@test.com", DEFAULT_ADMIN_PASSWORD,
                             role="viewer")
        SessionRepo(conn).create(first["id"], 168)
        SessionRepo(conn).create(second["id"], 168)
        # 固定处理顺序：旧实现逐账号提交，first 先成功、second 失败会留下半状态。
        conn.execute(
            "UPDATE users SET created_at = '2026-01-01T00:00:00+08:00' "
            "WHERE id = ?", (first["id"],))
        conn.execute(
            "UPDATE users SET created_at = '2026-01-02T00:00:00+08:00' "
            "WHERE id = ?", (second["id"],))
        before = {user["id"]: user["password_hash"] for user in repo.list()}
        conn.execute(
            "CREATE TRIGGER fail_second_session_delete "
            "BEFORE DELETE ON user_sessions "
            f"WHEN OLD.user_id = '{second['id']}' "
            "BEGIN SELECT RAISE(ABORT, 'second session revoke failed'); END")
        conn.commit()
    finally:
        conn.close()

    settings = test_settings(
        tmp_path, db_path=db_path, host="0.0.0.0", user_auth=True,
        admin_password="new-strong-password", admin_force_reset=True)
    with pytest.raises(Exception) as excinfo:
        create_app(settings)
    assert "second session revoke failed" in str(excinfo.value)

    conn = connect(db_path)
    try:
        hashes = {row["id"]: row["password_hash"]
                  for row in conn.execute("SELECT id, password_hash FROM users")}
        assert hashes == before, "整批重置必须回滚：任何账号都不得被改掉"
        assert conn.execute(
            "SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 2
        conn.execute("DROP TRIGGER fail_second_session_delete")
        conn.commit()
    finally:
        conn.close()

    # 修好触发器后重试：整批成功，两个账号的口令都换、会话都吊销。
    app = create_app(settings)
    conn = connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 0
        assert auth.verify_password(
            DEFAULT_ADMIN_PASSWORD,
            UserRepo(conn).get_by_email("first@test.com")["password_hash"]
        ) is False
        assert auth.verify_password(
            DEFAULT_ADMIN_PASSWORD,
            UserRepo(conn).get_by_email("second@test.com")["password_hash"]
        ) is False
    finally:
        conn.close()
    with TestClient(app) as client:
        for email in ("first@test.com", "second@test.com"):
            assert client.post("/admin/login", json={
                "email": email,
                "password": DEFAULT_ADMIN_PASSWORD}).status_code == 401
            assert client.post("/admin/login", json={
                "email": email,
                "password": "new-strong-password"}).status_code == 200
