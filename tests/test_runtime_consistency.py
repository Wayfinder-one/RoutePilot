"""画布快照 vs 数据库目录：只读差异提示（spec「保存、启用与检查」）。

覆盖：
1. 空目录时 ``/admin/try`` 仍 200，只对「画布声明了」的资源逐字段报告
   ``runtime_mismatch`` hint，执行完全来自画布（mock JEV 请求）；
2. 数据库里多出来的 provider/model/JEV 不算冲突；
3. 逐字段精确比较（JEV base_url / input_tokens、provider base_url、model upstream、
   model profile.capability / 多叶子 profile）以及 ``database: null`` 的缺失项；
4. 有/无差异执行结果一致；
5. 真实密钥不出现在响应、差异 params 或画布文件字节里；
6. ``issues_payload`` 暴露 ``params``，差异永远是 hint；
7. 旧库里的非 JSON 值（``NaN``/``Infinity``）不能把 ``/admin/try`` 打成 500：
   ``_safe_value``/``mismatch`` 输出 JSON 安全，比较段意外异常只跳过该段 hint；
8. 无 ``tree_id`` 的完整草稿同样报告草稿运行资源的差异并严格从草稿执行。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from pathlib import Path

import pytest

import router.control.consistency as consistency
from router.api.issue_presenter import issues_payload
from router.control.consistency import _safe_value, mismatch, runtime_mismatch_issues
from router.jev.service import JevService
from router.routing.graph.validate import Issue
from router.store.db import connect
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ModelRepo, ProviderRepo
from tests.routing_fixtures import default_tree, portable_tree_dict
from tests.test_self_contained_e2e import json_only, self_contained_root

TRY_BODY = {
    "tree_id": "portable",
    "text": "帮我分析这段复杂代码",
    "now": "2026-11-20T10:00:00+08:00",
}


def _runtime(app):
    return default_tree(app.state.graph_repo).runtime


def _seed_catalog(conn, runtime) -> None:
    """把数据库目录写成与画布快照完全一致（此后不应有任何 mismatch）。"""
    for provider in runtime.backends.providers:
        ProviderRepo(conn).upsert(
            provider["name"], provider["base_url"],
            key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"])
    for model in runtime.backends.models:
        ModelRepo(conn).create(
            model["id"], model["upstream"], model["provider"],
            json.dumps(model["profile"], ensure_ascii=False))
    if runtime.jev is not None:
        JevServiceRepo(conn).save(runtime.jev)


def _seed_providers(conn, runtime) -> None:
    for provider in runtime.backends.providers:
        ProviderRepo(conn).upsert(
            provider["name"], provider["base_url"],
            key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"])


def _drift_issues(body) -> list[dict]:
    return [item for item in body["warnings"] if item["code"] == "runtime_mismatch"]


def _issue_keys(issues) -> set[tuple[str, str, str]]:
    """兼容机器 Issue（.params）与 /admin/try 的 JSON 载荷（["params"]）。"""
    params_list = [issue if isinstance(issue, dict) else {"params": issue.params}
                   for issue in issues]
    return {
        (item["params"]["resource_kind"], item["params"]["resource_id"],
         item["params"]["field"])
        for item in params_list
    }


# ── 1. 空目录：提示缺失项，执行仍来自画布 ────────────────────────────────

def test_empty_catalog_reports_canvas_only_drift_and_uses_canvas(json_only):
    app, client, _settings, calls = json_only
    response = client.post("/admin/try", json=TRY_BODY)
    assert response.status_code == 200, response.text
    body = response.json()

    assert {item["code"] for item in body["warnings"]} == {"runtime_mismatch"}
    drifts = _drift_issues(body)
    assert all(item["level"] == "hint" for item in drifts)
    # 只报告画布声明过的三个资源，且空目录里每个被比较字段都是 database: null。
    assert {item["params"]["resource_id"] for item in drifts} == {
        "local", "fast", "json-jev-service"}
    assert all(item["params"]["database"] is None for item in drifts)
    assert _issue_keys(drifts) == {
        ("provider", "local", "base_url"),
        ("provider", "local", "key_env"),
        ("provider", "local", "reasoning_format"),
        ("model", "fast", "upstream"),
        ("model", "fast", "provider"),
        ("model", "fast", "profile"),
        ("jev", "json-jev-service", "id"),
        ("jev", "json-jev-service", "base_url"),
        ("jev", "json-jev-service", "model"),
        ("jev", "json-jev-service", "key_env"),
        ("jev", "json-jev-service", "timeout_seconds"),
        ("jev", "json-jev-service", "instructions"),
        ("jev", "json-jev-service", "state_template"),
        ("jev", "json-jev-service", "input_tokens"),
        ("jev", "json-jev-service", "context_tokens"),
    }

    # 执行仍严格来自画布：模型引用 fast 与 JSON JEV，dry-run 不调用模型上游。
    assert body["outcome"]["model"] == "fast"
    assert body["outcome"]["target_id"] == "fast-target"
    assert calls["jev"] and calls["jev"][0]["model"] == "json-jev"
    assert calls["upstream"] == []
    # 提示不改变画布本身，也不物化目录。
    conn = connect(_settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM jev_services").fetchone()[0] == 0
    finally:
        conn.close()


# ── 2. 数据库额外资源不误报 ────────────────────────────────────────────

def test_extra_database_resources_do_not_warn(json_only):
    _app, client, settings, _calls = json_only
    conn = connect(settings.db_path)
    try:
        ProviderRepo(conn).upsert("extra", "http://extra.test/v1")
        ModelRepo(conn).create("unused-model", "unused", "extra", "{}")
        JevServiceRepo(conn).save(JevService(
            id="extra-jev", base_url="http://extra-jev.test", model="extra-jev"))
    finally:
        conn.close()

    response = client.post("/admin/try", json=TRY_BODY)
    assert response.status_code == 200, response.text
    warnings = response.json()["warnings"]
    assert not [
        item for item in warnings
        if item["code"] == "runtime_mismatch"
        and item["params"]["resource_id"] in {"extra", "unused-model", "extra-jev"}
    ]
    # 画布资源仍然缺失 → 仍然提示（过滤掉额外资源后集合与空目录一致）。
    assert {item["params"]["resource_id"] for item in _drift_issues(response.json())} == {
        "local", "fast", "json-jev-service"}


# ── 3. 逐字段精确比较 ─────────────────────────────────────────────────

def _mutate(conn, runtime, resource_kind, field, database_value) -> None:
    if resource_kind == "jev":
        service = runtime.jev
        if field == "base_url":
            JevServiceRepo(conn).save(replace(service, base_url=database_value))
        elif field == "input_tokens":
            JevServiceRepo(conn).save(replace(service, input_tokens=database_value))
        else:  # pragma: no cover - 参数化用例之外没有别的 JEV 变更
            raise AssertionError(field)
        return
    if resource_kind == "provider":
        provider = runtime.backends.providers[0]
        ProviderRepo(conn).upsert(
            provider["name"], database_value,
            key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"])
        return
    # model
    if field == "upstream":
        ModelRepo(conn).update("fast", upstream=database_value)
        return
    if field == "profile.capability":
        row = ModelRepo(conn).get("fast")
        profile = json.loads(row["profile"])
        profile["capability"] = database_value
        ModelRepo(conn).update(
            "fast", profile=json.dumps(profile, ensure_ascii=False))
        return
    raise AssertionError(field)  # pragma: no cover


@pytest.mark.parametrize(
    ("resource_kind", "resource_id", "field", "canvas_value", "database_value"),
    [
        ("jev", "json-jev-service", "base_url",
         "http://jev-json.test", "http://database-jev.test"),
        ("jev", "json-jev-service", "input_tokens", 1024, 2048),
        ("provider", "local", "base_url",
         "http://json-only.test/v1", "http://database.test/v1"),
        ("model", "fast", "upstream", "json-upstream", "database-upstream"),
        ("model", "fast", "profile.capability", "来自 JSON 的能力", "数据库能力"),
    ],
)
def test_mismatch_reports_exact_field_values(
        json_only, resource_kind, resource_id, field, canvas_value, database_value):
    app, _client, settings, _calls = json_only
    runtime = _runtime(app)
    conn = connect(settings.db_path)
    try:
        _seed_catalog(conn, runtime)
        # 基线：数据库目录与画布完全一致 → 无任何差异提示。
        assert runtime_mismatch_issues(conn, runtime) == []

        _mutate(conn, runtime, resource_kind, field, database_value)
        issues = runtime_mismatch_issues(conn, runtime)
        # 只有一个字段被改，提示必须精确到这一条，不多不少。
        assert len(issues) == 1, [
            (issue.params["resource_id"], issue.params["field"]) for issue in issues]
        issue = issues[0]
        assert issue.code == "runtime_mismatch"
        assert issue.level == "hint"
        assert issue.params["resource_kind"] == resource_kind
        assert issue.params["resource_id"] == resource_id
        assert issue.params["field"] == field
        assert issue.params["canvas"] == canvas_value
        assert issue.params["database"] == database_value
    finally:
        conn.close()


# ── 4. 缺失 JEV 服务 / 模型 → database: null，仍从画布执行 ───────────────

def test_missing_service_and_model_are_null_drift_hints(json_only):
    app, client, settings, calls = json_only
    runtime = _runtime(app)
    conn = connect(settings.db_path)
    try:
        _seed_providers(conn, runtime)   # provider 一致 → 只剩 model/JEV 缺失
        issues = runtime_mismatch_issues(conn, runtime)
    finally:
        conn.close()

    assert _issue_keys([issue for issue in issues
                        if issue.params["resource_kind"] == "model"]) == {
        ("model", "fast", "upstream"),
        ("model", "fast", "provider"),
        ("model", "fast", "profile"),
    }
    jev_issues = [issue for issue in issues if issue.params["resource_kind"] == "jev"]
    assert len(jev_issues) == 9
    assert all(issue.params["database"] is None for issue in issues)
    assert all(issue.level == "hint" for issue in issues)

    response = client.post("/admin/try", json=TRY_BODY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"]["model"] == "fast"
    assert calls["jev"] and calls["jev"][0]["model"] == "json-jev"


# ── 5. 提示不改变执行 ─────────────────────────────────────────────────

def test_drift_does_not_change_execution(json_only):
    app, client, settings, calls = json_only
    drift_response = client.post("/admin/try", json=TRY_BODY)
    assert drift_response.status_code == 200, drift_response.text
    drift_body = drift_response.json()
    assert _drift_issues(drift_body), "empty catalog must report drift"
    assert calls["jev"], "drifted run did not call the canvas JEV"

    runtime = _runtime(app)
    conn = connect(settings.db_path)
    try:
        _seed_catalog(conn, runtime)
    finally:
        conn.close()
    clean_response = client.post("/admin/try", json=TRY_BODY)
    assert clean_response.status_code == 200, clean_response.text
    clean_body = clean_response.json()
    assert clean_body["warnings"] == []

    assert drift_body["outcome"]["model"] == clean_body["outcome"]["model"] == "fast"
    assert drift_body["outcome"]["target_id"] == clean_body["outcome"]["target_id"]
    assert drift_body["outcome"]["candidates"] == clean_body["outcome"]["candidates"]
    assert len(calls["jev"]) == 2
    assert [call["model"] for call in calls["jev"]] == ["json-jev", "json-jev"]
    assert calls["jev"][0]["state"] == calls["jev"][1]["state"]


# ── 6. 真实密钥永不进入响应 / params / 画布文件 ─────────────────────────

def test_secrets_never_leak_into_drift_payloads(json_only):
    app, client, settings, _calls = json_only
    runtime = _runtime(app)
    provider = runtime.backends.providers[0]
    env_secret = os.environ["JSON_PROVIDER_API_KEY"]
    stored_secret = "sk-db-jev-secret-value"

    conn = connect(settings.db_path)
    try:
        # 差异 + 已存真实密钥：provider 的 api_key 与环境变量同值，JEV 再存一个密钥。
        ProviderRepo(conn).upsert(
            provider["name"], "http://drifted-provider.test/v1",
            key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"],
            api_key=env_secret)
        JevServiceRepo(conn).save(
            replace(runtime.jev, base_url="http://drifted-jev.test"),
            api_key=stored_secret)
    finally:
        conn.close()

    response = client.post("/admin/try", json=TRY_BODY)
    assert response.status_code == 200, response.text
    text = response.text
    assert env_secret not in text
    assert stored_secret not in text

    drifts = _drift_issues(response.json())
    assert drifts, "drifted catalog must produce hints"
    for item in drifts:
        dumped = json.dumps(item, ensure_ascii=False)
        assert env_secret not in dumped
        assert stored_secret not in dumped
        assert "api_key" not in dumped
        assert "key_present" not in dumped
        assert "key_source" not in dumped

    graph_files = list(Path(settings.graph_dir, "graphs").glob("*.json"))
    assert graph_files, "no canvas file was written"
    for path in graph_files:
        raw = path.read_bytes()
        assert env_secret.encode() not in raw
        assert stored_secret.encode() not in raw


# ── 7. 呈现层：params 透出，level 永远是 hint ──────────────────────────

def test_issues_payload_exposes_params_and_drift_is_hint():
    issue = Issue(
        code="runtime_mismatch", level="hint",
        params={
            "resource_kind": "provider", "resource_id": "local",
            "field": "base_url",
            "canvas": "http://canvas.test/v1",
            "database": None,
        })
    payload = issues_payload([issue])
    assert len(payload) == 1
    presented = payload[0]
    # 既有字段保持原样，只新增 params。
    assert presented["code"] == "runtime_mismatch"
    assert presented["level"] == "hint"
    assert presented["node_id"] == ""
    assert presented["edge_id"] == ""
    assert presented["port"] == ""
    assert presented["zh"] and presented["en"]
    assert "local" in presented["zh"] and "base_url" in presented["zh"]
    assert "local" in presented["en"] and "base_url" in presented["en"]
    assert presented["params"] == {
        "resource_kind": "provider", "resource_id": "local",
        "field": "base_url",
        "canvas": "http://canvas.test/v1",
        "database": None,
    }


# ── 8. 非 JSON 合规的旧库值：hint 必须归一化，不能把 /admin/try 打成 500 ────

@pytest.mark.parametrize(
    ("profile_text", "normalized"),
    [
        # Python json.loads 接受这三种扩展字面量：旧库/直接写库都能制造非 JSON 值。
        ('{"tier":"large","domains":NaN}', "NaN"),
        ('{"tier":"large","domains":Infinity}', "Infinity"),
        ('{"tier":"large","domains":-Infinity}', "-Infinity"),
    ],
)
def test_non_json_database_profile_does_not_break_try(
        json_only, profile_text, normalized):
    app, client, settings, calls = json_only
    runtime = _runtime(app)
    provider = runtime.backends.providers[0]
    env_secret = os.environ["JSON_PROVIDER_API_KEY"]
    conn = connect(settings.db_path)
    try:
        _seed_catalog(conn, runtime)
        # 目录值与画布完全一致，只把模型 profile 改成非 JSON 值；provider 再存一个
        # 真实密钥，验证归一化路径不会把密钥带进响应。
        ProviderRepo(conn).upsert(
            provider["name"], provider["base_url"],
            key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"],
            api_key=env_secret)
        ModelRepo(conn).update("fast", profile=profile_text)
    finally:
        conn.close()

    response = client.post("/admin/try", json=TRY_BODY)
    # 关键判别：不归一化时 FastAPI 的 JSONResponse 在序列化 params.database 时
    # 抛 ValueError → 500（或在 TestClient 里直接抛出），根本走不到这里。
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"]["model"] == "fast"
    assert body["outcome"]["target_id"] == "fast-target"
    # 执行仍严格来自画布：问题节点调用了画布 JEV，dry-run 不调用模型上游。
    assert calls["jev"] and calls["jev"][0]["model"] == "json-jev"
    assert calls["upstream"] == []

    drifts = _drift_issues(body)
    assert drifts and all(item["level"] == "hint" for item in drifts)
    domains = [
        item for item in drifts
        if item["params"]["resource_id"] == "fast"
        and item["params"]["field"] == "profile.domains"
    ]
    assert len(domains) == 1, domains
    assert domains[0]["params"]["canvas"] == ["json"]
    assert domains[0]["params"]["database"] == normalized
    assert normalized in response.text

    dumped = json.dumps(body, ensure_ascii=False, allow_nan=False)
    assert env_secret not in dumped
    for item in drifts:
        payload = json.dumps(item, ensure_ascii=False)
        assert env_secret not in payload
        assert "api_key" not in payload


# ── 9. `_safe_value` / `mismatch` 输出 JSON 安全（单元钉） ──────────────────

def test_safe_value_normalizes_to_json_safe_primitives():
    raw = {
        "domains": [float("nan"), float("inf"), float("-inf")],
        "bytes": b"\x00\x01",
        "tuple": ("a", 1),
        "ratio": 1.5,
        "api_key": "sk-canvas-secret",
        "nested": {"authorization": "Bearer canvas-secret", "value": 1},
        1: "int-key",
    }
    safe = _safe_value(raw)

    encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    assert safe["domains"] == ["NaN", "Infinity", "-Infinity"]
    assert safe["bytes"] == str(b"\x00\x01")
    assert safe["tuple"] == ["a", 1]
    assert safe["ratio"] == 1.5
    assert safe["1"] == "int-key"
    # 脱敏规则保持：嵌套密钥键在归一化前被剔除，敏感值不出现在编码结果里。
    assert "api_key" not in safe
    assert safe["nested"] == {"value": 1}
    assert "canvas-secret" not in encoded

    # 深嵌套不会让归一化递归爆栈（超深部分退化为固定标记）。
    deep = "leaf"
    for _ in range(200):
        deep = [deep]
    json.dumps(_safe_value(deep), allow_nan=False)


def test_mismatch_issue_params_are_json_safe_and_redacted():
    issue = mismatch(
        "model", "fast", "profile",
        {"capability": "canvas", "scores": [float("nan")]},
        {"domains": [float("inf")], "api_key": "sk-db-secret"})
    assert issue.code == "runtime_mismatch"
    assert issue.level == "hint"
    # 不归一化时 json.dumps(allow_nan=False) 会抛 ValueError，正好钉住修复。
    encoded = json.dumps(issue.params, ensure_ascii=False, allow_nan=False)
    assert issue.params["canvas"] == {"capability": "canvas", "scores": ["NaN"]}
    assert issue.params["database"] == {"domains": ["Infinity"]}
    assert "sk-db-secret" not in encoded
    assert "api_key" not in encoded


# ── 10. `mismatch()` 字段白名单：密钥路径拒绝，普通字段允许 ────────────────

@pytest.mark.parametrize(
    ("resource_kind", "field"),
    [
        ("provider", "api_key"),
        ("provider", "authorization"),
        ("provider", "key_present"),
        ("provider", "key_source"),
        ("model", "profile.api_key"),
        ("model", "profile.reasoning.key_source"),
        ("jev", "api_key"),
        ("model", "unknown_field"),
        ("unknown-kind", "base_url"),
    ],
)
def test_mismatch_rejects_secret_or_unknown_fields(resource_kind, field):
    with pytest.raises(ValueError):
        mismatch(resource_kind, "local", field, "canvas-secret", "database-secret")


@pytest.mark.parametrize(
    ("resource_kind", "field"),
    [
        ("provider", "base_url"),
        ("provider", "key_env"),
        ("provider", "reasoning_format"),
        ("model", "upstream"),
        ("model", "provider"),
        ("model", "profile"),
        ("model", "profile.capability"),
        ("model", "profile.reasoning.efforts.high"),
        ("jev", "instructions"),
        ("jev", "state_template"),
        ("jev", "input_tokens"),
        ("jev", "context_tokens"),
    ],
)
def test_mismatch_allows_whitelisted_non_secret_fields(resource_kind, field):
    issue = mismatch(resource_kind, "id-1", field, "canvas-value", None)
    assert issue.params["field"] == field
    assert issue.params["canvas"] == "canvas-value"
    assert issue.params["database"] is None


def test_mismatch_never_places_sensitive_values_in_params():
    issue = mismatch(
        "model", "fast", "profile",
        {"capability": "ok", "api_key": "sk-canvas-secret",
         "nested": {"authorization": "Bearer canvas-secret", "value": 1}},
        {"key_source": "env", "key_present": True, "domains": [float("nan")]})
    dumped = json.dumps(issue.params, ensure_ascii=False, allow_nan=False)
    for needle in ("sk-canvas-secret", "Bearer canvas-secret",
                   "api_key", "authorization", "key_present", "key_source"):
        assert needle not in dumped
    assert issue.params["canvas"] == {"capability": "ok", "nested": {"value": 1}}
    assert issue.params["database"] == {"domains": ["NaN"]}


# ── 11. 意外比较异常：只跳过该段 hint，/admin/try 仍 200 且执行不变 ──────────

def test_unexpected_comparison_error_skips_only_that_section(
        json_only, monkeypatch, caplog):
    _app, client, _settings, _calls = json_only

    def boom(conn, runtime):
        raise RuntimeError("corrupt comparison")

    monkeypatch.setattr(consistency, "_model_issues", boom)
    caplog.set_level(logging.WARNING, logger="router.control.consistency")
    for _ in range(2):
        response = client.post("/admin/try", json=TRY_BODY)
        assert response.status_code == 200, response.text
        body = response.json()
        # 坏掉的分段被跳过，其余分段照常提示；执行仍来自画布。
        assert {item["params"]["resource_kind"]
                for item in _drift_issues(body)} == {"provider", "jev"}
        assert body["outcome"]["model"] == "fast"
        assert body["outcome"]["target_id"] == "fast-target"

    failures = [
        record for record in caplog.records
        if "failed; skipping its hints" in record.getMessage()
    ]
    assert len(failures) == 1, "同一比较分段只应告警一次"


# ── 12. 无 tree_id 的完整草稿：提示引用草稿运行资源，执行严格来自草稿 ────────

def test_full_draft_without_tree_id_reports_draft_runtime_drift(json_only):
    _app, client, _settings, calls = json_only
    document = portable_tree_dict("portable", root=self_contained_root())
    # 不传 tree_id，也不依赖已存画布：四个运行字段完整给出（自包含草稿）。
    body = {
        "root": document["root"],
        "timezone": document["timezone"],
        "sticky": document["sticky"],
        "backends": document["backends"],
        "jev": document["jev"],
        "jev_window": document["jev_window"],
        "time_periods": document["time_periods"],
        "text": "帮我分析这段复杂代码",
        "now": "2026-11-20T10:00:00+08:00",
    }
    response = client.post("/admin/try", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["draft"] is True
    assert payload["tree_id"] == "draft"
    drifts = _drift_issues(payload)
    assert drifts and all(item["level"] == "hint" for item in drifts)
    # 空数据库：草稿声明的三个运行资源全部以 database: null 报告。
    assert {item["params"]["resource_id"] for item in drifts} == {
        "local", "fast", "json-jev-service"}
    assert all(item["params"]["database"] is None for item in drifts)

    # 执行严格来自草稿：模型/JEV/目标都使用草稿运行字段，dry-run 不调用上游。
    assert payload["outcome"]["model"] == "fast"
    assert payload["outcome"]["target_id"] == "fast-target"
    assert calls["jev"] and calls["jev"][0]["model"] == "json-jev"
    assert calls["upstream"] == []


# ── 13. 多叶子 profile 差异：每条叶子都要独立提示 ──────────────────────────

def test_multi_leaf_profile_mutation_reports_every_leaf(json_only):
    app, _client, settings, _calls = json_only
    runtime = _runtime(app)
    conn = connect(settings.db_path)
    try:
        _seed_catalog(conn, runtime)
        assert runtime_mismatch_issues(conn, runtime) == []
        row = ModelRepo(conn).get("fast")
        profile = json.loads(row["profile"])
        # 同时改两个不同层级的叶子：capability 与 reasoning.efforts.high。
        profile["capability"] = "数据库能力"
        profile["reasoning"]["efforts"]["high"] = "数据库-high"
        ModelRepo(conn).update(
            "fast", profile=json.dumps(profile, ensure_ascii=False))
        issues = runtime_mismatch_issues(conn, runtime)
    finally:
        conn.close()

    assert {(issue.params["field"], issue.params["canvas"],
             issue.params["database"]) for issue in issues} == {
        ("profile.capability", "来自 JSON 的能力", "数据库能力"),
        ("profile.reasoning.efforts.high", "high", "数据库-high"),
    }
    assert all(issue.level == "hint" for issue in issues)
