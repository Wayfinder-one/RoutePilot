import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.control.validation import build_enable_context
from router.routing.policy import policy_from_dict
from router.routing.policy.validate import validate_policy_for_enable
from router.store.db import connect, migrate
from router.store.policies import PolicyRepo
from router.store.repositories import seed_default
from tests.routing_fixtures import test_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
JEV_ENV = {"jev_url": "http://jev.test", "jev_model": "NeoHorse-Jev-4B"}
SPEC_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "policy_v4.json"


def catalog_policy(model="qwen3-8b"):
    """无问题策略：文本条件直连 target(model)，启用校验只需要模型存在。"""
    return {
        "kind": "sr-routing-policy", "version": 4, "name": "直连策略",
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "questions": {},
        "targets": {"direct": {"models": [{"model": model}]}},
        "rules": [{"name": "直连", "when": {"text_contains_any": ["请求"]}, "target": "direct"}],
        "fallback": "direct",
    }


def question_policy(model="qwen3-8b"):
    """带问题的策略：rules+questions 同时存在，启用校验要求 JEV 配置。"""
    return {
        "kind": "sr-routing-policy", "version": 4, "name": "问题策略",
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "questions": {
            "route": {"type": "choice", "instructions": "选择模型", "options": [
                {"key": "fast", "label": "轻量", "capability": "简单请求"},
                {"key": "strong", "label": "增强", "capability": "复杂请求"},
            ]},
        },
        "targets": {"direct": {"models": [{"model": model}]}},
        "rules": [{"name": "轻量", "when": {"answer": "route.selected", "eq": "fast"},
                   "target": "direct"}],
        "fallback": "direct",
    }


def test_health_console_and_empty_policy_boot(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/console/index.html").status_code == 200
    assert app.state.snapshot.policy is None
    assert app.state.snapshot.policy_id is None
    # 旧 /admin/trees 不再提供（设计 §6.1：不保留 graph v3 兼容接口）。
    assert client.get("/admin/trees").status_code == 404
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "no_active_policy"
    assert not hasattr(app.state, "tagger")


def prepare_active_policy(tmp_path, raw):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    seed_default(conn)
    conn.close()
    repo = PolicyRepo(settings.policy_dir)
    repo.initialize()
    repo.create("main", policy_from_dict(raw))
    repo.enable("main")
    return settings


def test_missing_snapshot_is_not_ready_and_recovered_pointer_is_ready(tmp_path):
    settings = test_settings(tmp_path)
    repo = PolicyRepo(settings.policy_dir)
    repo.initialize()
    (repo.current).write_text("missing\n", encoding="utf-8")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        assert client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
        }).status_code == 503
        (repo.current).write_bytes(b"")
        asyncio.run(app.state.policy_reloader.poll_once())
        assert client.get("/readyz").status_code == 200


def test_startup_accepts_valid_direct_policy(tmp_path):
    settings = prepare_active_policy(tmp_path, catalog_policy())
    app = create_app(settings)
    assert app.state.snapshot.policy_id == "main"
    direct = app.state.snapshot.policy.targets["direct"]
    assert [(item.model, item.reasoning) for item in direct.models] == [
        ("qwen3-8b", None),
    ]


def test_startup_with_invalid_active_policy_is_not_ready(tmp_path):
    # 目录外模型：启用校验失败，快照保持缺失而不是带病发布。
    settings = prepare_active_policy(tmp_path, catalog_policy(model="ghost-model"))
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_valid_snapshot"


def test_policy_with_questions_without_jev_config_is_not_ready(tmp_path):
    settings = prepare_active_policy(tmp_path, question_policy())
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert "decision_service_unavailable" in ready.json()["last_load_error"]


def test_environment_jev_config_is_seeded_once(tmp_path):
    settings = test_settings(tmp_path, jev_url="http://jev.test", jev_model="NeoHorse-Jev-4B", jev_api_key="secret")
    app = create_app(settings)
    assert app.state.snapshot.jev.model == "NeoHorse-Jev-4B"
    assert app.state.snapshot.jev.api_key == "secret"


def test_spec_example_policy_document_parses_cleanly():
    """示例 v4 文档（设计 §4.1）必须始终可解析为行为模型。"""
    policy = policy_from_dict(json.loads(SPEC_EXAMPLE.read_text(encoding="utf-8")))
    assert set(policy.questions) == {"route", "power_business", "adversarial"}
    assert [rule.name for rule in policy.rules] == [
        "对抗请求优先走安全目标", "电力业务优先", "主问题选择轻量模型", "主问题选择增强模型",
    ]
    assert policy.fallback == "safe"


def test_spec_example_enable_issues_pin_seeded_catalog(tmp_path):
    """钉住示例策略引用的 seed 目录模型 id：目录里改名/删除会在这里失败。"""
    conn = connect(str(tmp_path / "shipped.db"))
    migrate(conn)
    seed_default(conn)
    try:
        context = replace(build_enable_context(conn), jev_available=True)  # JEV 打桩：只校验目录存在性
    finally:
        conn.close()
    policy = policy_from_dict(json.loads(SPEC_EXAMPLE.read_text(encoding="utf-8")))
    issues = validate_policy_for_enable(policy, context)
    # qwen3-8b 在 seed 目录里直接通过；示例文档钉住两个目录事实：
    # qwen3-32b 的 reasoning 档位与电力专用模型 qwen3.8-flash-sft 都未登记。
    assert [(issue.code, issue.params) for issue in issues] == [
        ("reasoning_unsupported",
         {"model": "qwen3-32b", "reasoning": "high", "efforts": []}),
        ("unknown_model", {"model": "qwen3.8-flash-sft"}),
    ]


class TryHandler:
    """MockTransport 分流：jev.test 回答策略问题，其余请求落到假 OpenAI 上游。"""

    def __init__(self):
        self.jev_paths = []

    def __call__(self, request):
        if request.url.host == "jev.test":
            self.jev_paths.append(request.url.path)
            return httpx.Response(200, json={
                "answers": {"route": {"type": "choice", "probabilities": {"轻量": 0.85, "增强": 0.15}}},
                "usage": {"input_tokens": 10},
            })
        return httpx.Response(200, json={"choices": [], "usage": {}})


def composed_try_app(tmp_path, handler):
    """真实组合根：evaluator/jev_client/breaker 全部由 create_app 构造，测试不注入 evaluator。"""
    settings = prepare_active_policy(tmp_path, question_policy())
    return create_app(replace(settings, **JEV_ENV), transport=httpx.MockTransport(handler))


def test_try_through_app_composition_routes_policy(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"policy_id": "main", "text": "你好"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert handler.jev_paths == ["/v1/systemone"]
    assert body["target"] == "direct"
    assert body["models"] == ["qwen3-8b"]
    assert body["matched_rule"] == "轻量"
    assert body["fallback"] is False
    assert body["reason"] is None
    assert body["answers"]["route"]["ranking"] == ["fast", "strong"]
    assert [event["event"] for event in body["trace"]].count("jev") == 1


def test_try_through_app_composition_target_without_backend_is_503(tmp_path):
    handler = TryHandler()
    app = composed_try_app(tmp_path, handler)
    for _ in range(app.state.breaker.threshold):
        app.state.breaker.failure("qwen3-8b")
    with TestClient(app) as client:
        response = client.post("/admin/try", json={"policy_id": "main", "text": "你好"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "target 'direct' has no available backend"
    assert handler.jev_paths == ["/v1/systemone"]
