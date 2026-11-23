"""五个演示案例与随仓库发布画布的严格自包含契约。

覆盖（Task 7）：

1. 每份 ``configs/cases/case-*.json`` 都是可直接导入的 ``sr-routing-graph`` v3
   文档：无外部状态 ``id``/``enabled``，四个运行字段齐全，内存无密钥；
2. 每个案例只嵌入自身 target 实际使用的模型与完整 profile，provider 统一为
   ``demo-tap``，JEV 统一为 ``demo-jev`` / 窗口 ``2048 / 4096``；
3. ``configs/default_routing_tree.json``、``configs/production_routing_tree.json``、
   ``router/console/routing/example.json`` 均严格可解析，``jev`` 带 ``id`` 与
   ``key_env``；
4. ``scripts/demo/apply_cases.py`` 不再自带 ``MODEL_SPECS`` 等硬编码目录：纯函数
   ``case_runtime()`` 从案例文档合并数据库目录，冲突必须报错；``upsert_tree()``
   剥离信封字段并携带四个运行字段；409 ``impact_confirmation_required`` 默认停止、
   ``--confirm-sync`` 才带基线哈希重试，``graph_conflict`` 停止。
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from router.api.app import create_app
from router.control.validation import tree_issues
from router.routing.document import parse_graph_document
from router.routing.graph.model import TargetNode
from router.routing.tree import tree_from_dict
from tests.routing_fixtures import test_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = REPO_ROOT / "configs" / "cases"
APPLY_CASES = REPO_ROOT / "scripts" / "demo" / "apply_cases.py"
RUN_CASES = REPO_ROOT / "scripts" / "demo" / "run_cases.py"

DEMO_PROVIDER = "demo-tap"
DEMO_JEV_ID = "demo-jev"
DEMO_JEV_BASE_URL = "http://127.0.0.1:30104"
DEMO_JEV_MODEL = "d1-2B"
DEMO_INPUT_TOKENS = 2048
DEMO_CONTEXT_TOKENS = 4096

#: 每个案例实际引用/嵌入的模型数量（设计文档 §案例与脚本）。
CASE_MODEL_COUNTS = {
    "case-industry": 6,
    "case-multimodal": 2,
    "case-reasoning": 3,
    "case-logic": 2,
    "case-ha": 4,
}

#: 发布文档 -> JEV 服务 id（§画布快照：默认/生产/示例统一 ``default``）。
SHIPPED_DOCUMENTS = {
    REPO_ROOT / "configs" / "default_routing_tree.json": "default",
    REPO_ROOT / "configs" / "production_routing_tree.json": "default",
    REPO_ROOT / "router" / "console" / "routing" / "example.json": "default",
}

SHIPPED_JEV_KEY_ENV = "ROUTER_JEV_API_KEY"


def _case_paths() -> list[Path]:
    return sorted(CASES_DIR.glob("case-*.json"))


def _read_document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse(path: Path, tree_id: str):
    content = path.read_bytes()
    return parse_graph_document(
        content,
        tree_id=tree_id,
        enabled=False,
        updated_at="",
        document_sha256=hashlib.sha256(content).hexdigest(),
    )


def _used_target_models(tree) -> list[str]:
    """root 里全部 target 引用的模型 id（保持首次出现顺序、去重）。"""
    used: list[str] = []
    for node in tree.root.nodes:
        if not isinstance(node, TargetNode):
            continue
        for item in node.models:
            if item.model not in used:
                used.append(item.model)
    return used


def _embedded_models(tree) -> list[dict]:
    return list(tree.runtime.backends.models)


def _load_apply_cases():
    """按文件路径导入部署脚本（不执行 main）。"""
    spec = importlib.util.spec_from_file_location("demo_apply_cases", APPLY_CASES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case_documents() -> list[dict]:
    return [_read_document(path) for path in _case_paths()]


# --------------------------------------------------------------------------- #
# 1. 五个案例：严格自包含文档
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _case_paths(), ids=lambda path: path.stem)
def test_case_document_is_strict_self_contained(path: Path):
    raw = _read_document(path)
    text = path.read_text(encoding="utf-8")

    assert raw["kind"] == "sr-routing-graph"
    assert raw["version"] == 3
    assert "id" not in raw, "部署 ID 由 manifest.json + 文件名提供，案例文件不保存外部状态"
    assert "enabled" not in raw
    assert "api_key" not in text

    tree = _parse(path, tree_id=path.stem)

    # JEV 服务属于画布快照：id/端点/模型/窗口都内嵌，Key 只留环境变量名（此处为空）。
    assert tree.runtime.jev is not None
    assert tree.runtime.jev.id == DEMO_JEV_ID
    assert tree.runtime.jev.base_url == DEMO_JEV_BASE_URL
    assert tree.runtime.jev.model == DEMO_JEV_MODEL
    assert tree.runtime.jev.key_env == ""
    assert tree.runtime.jev_input_tokens == DEMO_INPUT_TOKENS
    assert tree.runtime.jev_context_tokens == DEMO_CONTEXT_TOKENS

    # 启用级校验只读画布自带运行配置，不依赖数据库目录。
    assert tree_issues(None, tree) == []

    used = _used_target_models(tree)
    embedded = _embedded_models(tree)
    assert sorted(used) == sorted(model["id"] for model in embedded)
    assert len(embedded) == CASE_MODEL_COUNTS[path.stem]
    assert {provider["name"] for provider in tree.runtime.backends.providers} == {DEMO_PROVIDER}
    assert all(model["provider"] == DEMO_PROVIDER for model in embedded)
    # profile 必须完整随文档内嵌（不依赖数据库行），至少带能力说明。
    assert all(model["profile"].get("capability") for model in embedded)

    period_names = [period.name for period in tree.runtime.time_periods]
    if path.stem == "case-logic":
        assert period_names == ["白天", "夜间"]
    else:
        assert period_names == []


def test_case_catalog_is_one_shared_demo_provider_and_jev():
    documents = _case_documents()
    providers = {
        provider["name"]
        for document in documents
        for provider in document["backends"]["providers"]
    }
    services = {json.dumps(document["jev"], ensure_ascii=False) for document in documents}
    windows = {json.dumps(document["jev_window"], sort_keys=True) for document in documents}

    assert providers == {DEMO_PROVIDER}
    assert len(services) == 1
    assert len(windows) == 1
    model_ids = [model["id"] for document in documents for model in document["backends"]["models"]]
    assert len(model_ids) == sum(CASE_MODEL_COUNTS.values()) == 17
    # ha-general（案例四/五）与 industry-energy（案例一/四）各共享一次 → 15 个独立模型。
    assert len(set(model_ids)) == 15
    duplicated = {model_id for model_id in model_ids if model_ids.count(model_id) > 1}
    assert duplicated == {"ha-general", "industry-energy"}


# --------------------------------------------------------------------------- #
# 2. 随仓库发布的默认/生产/示例文档
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,jev_id", SHIPPED_DOCUMENTS.items(), ids=lambda value: str(value))
def test_shipped_document_is_strict_and_carries_jev_identity(path: Path, jev_id: str):
    text = path.read_text(encoding="utf-8")
    assert "api_key" not in text

    tree = _parse(path, tree_id=path.stem)
    assert tree.runtime.jev is not None
    assert tree.runtime.jev.id == jev_id
    assert tree.runtime.jev.key_env == SHIPPED_JEV_KEY_ENV
    assert tree.runtime.jev.base_url
    assert tree.runtime.jev.model
    # 四个运行字段成组出现且启用级校验通过（严格解析已保证字段齐全）。
    assert tree_issues(None, tree) == []


# --------------------------------------------------------------------------- #
# 3. apply_cases.py：案例 JSON 是唯一事实源
# --------------------------------------------------------------------------- #
def test_apply_cases_has_no_hardcoded_catalog():
    source = APPLY_CASES.read_text(encoding="utf-8")
    for forbidden in (
        "MODEL_SPECS",
        "PROVIDER_SPEC",
        "REASONING_EXPECT",
        "demo-tap",
        "demo-jev",
        "30104",
        "9300",
        "2048",
        "4096",
    ):
        assert forbidden not in source, f"apply_cases.py 不应再硬编码 {forbidden!r}"


def test_case_runtime_merges_the_five_case_documents():
    module = _load_apply_cases()
    documents = _case_documents()
    runtime = module.case_runtime(documents)

    assert set(runtime) == {"providers", "models", "jev_services", "time_periods"}
    assert [provider["name"] for provider in runtime["providers"]] == [DEMO_PROVIDER]

    document_models = {
        model["id"]: model
        for document in documents
        for model in document["backends"]["models"]
    }
    assert [model["id"] for model in runtime["models"]] == list(document_models)
    assert len(runtime["models"]) == 15  # 6+2+3+2+4 去重共享模型（ha-general/industry-energy）
    assert all(model == document_models[model["id"]] for model in runtime["models"])

    assert len(runtime["jev_services"]) == 1
    service = runtime["jev_services"][0]
    assert service["id"] == DEMO_JEV_ID
    assert service["base_url"] == DEMO_JEV_BASE_URL
    assert service["model"] == DEMO_JEV_MODEL
    assert service["input_tokens"] == DEMO_INPUT_TOKENS
    assert service["context_tokens"] == DEMO_CONTEXT_TOKENS
    assert "api_key" not in service

    assert {period["name"] for period in runtime["time_periods"]} == {"白天", "夜间"}


@pytest.mark.parametrize(
    "group,key",
    [
        ("models", "ha-general"),
        ("jev_services", DEMO_JEV_ID),
        ("providers", DEMO_PROVIDER),
        ("time_periods", "白天"),
    ],
)
def test_case_runtime_rejects_conflicting_duplicates(group: str, key: str):
    module = _load_apply_cases()
    documents = _case_documents()
    # 在「案例五」副本里改写一个与其它案例共享的键（合并顺序里它先出现）。
    ha = copy.deepcopy(next(document for document in documents
                            if document["name"].startswith("案例五")))
    if group == "models":
        model = next(item for item in ha["backends"]["models"] if item["id"] == key)
        model["profile"]["capability"] += "（冲突版本）"
    elif group == "jev_services":
        ha["jev"]["timeout_seconds"] = 11.0
    elif group == "providers":
        ha["backends"]["providers"][0]["reasoning_format"] = "deepseek"
    else:
        ha["time_periods"] = [{"name": key, "days": [6], "start": "00:00", "end": "01:00"}]
    mutated = [ha] + [document for document in documents if document is not ha]

    with pytest.raises(ValueError, match=key):
        module.case_runtime(mutated)


def test_upsert_tree_strips_envelope_and_carries_runtime():
    module = _load_apply_cases()
    document = _read_document(CASES_DIR / "case-multimodal.json")
    api = module.Api("http://127.0.0.1:1")
    calls: list[tuple] = []
    api.request = lambda method, path, body=None, **kwargs: calls.append((method, path, body)) or {}
    api.trees = lambda: {}

    assert api.upsert_tree("case-multimodal", document) == "created"
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/admin/trees")
    assert body["id"] == "case-multimodal"
    for stripped in ("kind", "version", "enabled"):
        assert stripped not in body
    for runtime_field in ("backends", "jev", "jev_window", "time_periods"):
        assert body[runtime_field] == document[runtime_field]
    assert body["name"] == document["name"]
    assert body["root"] == document["root"]
    assert body["canvas"] == document["canvas"]

    calls.clear()
    api.trees = lambda: {"case-multimodal": {
        "id": "case-multimodal", "document_sha256": "current-sha"}}
    assert api.upsert_tree("case-multimodal", document) == "updated"
    method, path, body = calls[0]
    assert (method, path) == ("PUT", "/admin/trees/case-multimodal")
    assert "id" not in body
    assert body["base_document_sha256"] == "current-sha"
    assert body["jev"] == document["jev"]


IMPACT_DETAIL = {
    "message": "该资源仍被画布引用，确认影响后才可写入",
    "code": "impact_confirmation_required",
    "resource": {"kind": "model", "id": "ha-general"},
    "affected_trees": [{
        "id": "case-logic",
        "name": "案例四 · 请求条件与逻辑门",
        "enabled": False,
        "document_sha256": "abc123",
        "usage": {"targets": ["t-normal"], "runtime_catalog": True},
    }],
    "replacements": [],
}


def _api_with_responses(module, responses, *, confirm_sync: bool = False):
    api = module.Api("http://127.0.0.1:1")
    api.confirm_sync = confirm_sync
    seen: list[tuple] = []

    def fake_raw(method, path, body=None, *, auth=True):
        seen.append((method, path, body))
        return responses.pop(0) if len(responses) > 1 else responses[0]

    api._raw = fake_raw
    return api, seen


def test_impact_confirmation_stops_without_the_flag():
    module = _load_apply_cases()
    api, seen = _api_with_responses(module, [(409, {"detail": IMPACT_DETAIL})])

    with pytest.raises(module.ImpactConfirmationRequired) as info:
        api.request("PUT", "/admin/models/ha-general", {"upstream": "ha-general"})

    assert len(seen) == 1, "未确认时不得重试或继续写入"
    assert info.value.detail["code"] == "impact_confirmation_required"
    lines = module.impact_report_lines(info.value.detail)
    assert any("case-logic" in line for line in lines)
    assert any("--confirm-sync" in line for line in lines)


def test_confirm_sync_retries_the_exact_request_with_baseline_hashes():
    module = _load_apply_cases()
    api, seen = _api_with_responses(
        module,
        [(409, {"detail": IMPACT_DETAIL}), (200, {"ok": True})],
        confirm_sync=True,
    )

    assert api.request("PUT", "/admin/models/ha-general", {"upstream": "ha-general"}) == {"ok": True}
    assert len(seen) == 2
    assert seen[1][:2] == ("PUT", "/admin/models/ha-general")
    retry_body = seen[1][2]
    assert retry_body["upstream"] == "ha-general"
    assert retry_body["impact_confirmation"] == {
        "trees": [{"id": "case-logic", "document_sha256": "abc123"}],
    }


def test_stale_graph_conflict_stops_with_actual_hash():
    module = _load_apply_cases()
    api, seen = _api_with_responses(
        module,
        [
            (409, {"detail": IMPACT_DETAIL}),
            (409, {"detail": {"code": "graph_conflict", "actual_document_sha256": "actual-hash"}}),
        ],
        confirm_sync=True,
    )

    with pytest.raises(module.GraphConflictError) as info:
        api.request("PUT", "/admin/models/ha-general", {"upstream": "ha-general"})

    assert len(seen) == 2
    assert info.value.detail["actual_document_sha256"] == "actual-hash"


def test_case_runtime_deploys_through_the_real_admin_api(tmp_path):
    """真实组合根：按案例文档部署目录 + 画布，再走两阶段影响确认重跑。"""
    module = _load_apply_cases()
    manifest = json.loads((CASES_DIR / "manifest.json").read_text(encoding="utf-8"))
    documents = module.load_case_documents(manifest["cases"])
    runtime = module.case_runtime(list(documents.values()))

    client = TestClient(create_app(test_settings(tmp_path)))
    api = module.Api("http://testserver")

    def fake_raw(method, path, body=None, *, auth=True):
        response = client.request(method, path, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        return response.status_code, payload

    api._raw = fake_raw

    def deploy():
        module.step_providers(api, runtime, False)
        module.step_models(api, runtime, False)
        module.step_jev_services(api, runtime, False)
        module.step_time_periods(api, runtime, False)
        module.step_trees(api, manifest["cases"], documents, False)

    deploy()
    rows = api.trees()
    assert len(rows) == len(manifest["cases"])
    assert not any(row.get("enabled") for row in rows.values())
    for case in manifest["cases"]:
        tree = tree_from_dict(rows[case["id"]])
        assert tree.runtime.jev.id == DEMO_JEV_ID
        assert tree.runtime.jev_input_tokens == DEMO_INPUT_TOKENS
        assert tree.runtime.jev_context_tokens == DEMO_CONTEXT_TOKENS
        assert tree_issues(None, tree) == []

    # 第二次部署：目录条目已被画布引用 → 默认必须停在影响确认，不得静默覆盖。
    with pytest.raises(module.ImpactConfirmationRequired) as info:
        deploy()
    affected = {item["id"] for item in info.value.detail["affected_trees"]}
    assert affected == {case["id"] for case in manifest["cases"]}

    # --confirm-sync：用 409 里的基线哈希显式确认，同一批请求可重复部署。
    api.confirm_sync = True
    deploy()
    restored = api.trees()
    assert {tree_id: row["document_sha256"] for tree_id, row in restored.items()} == {
        tree_id: row["document_sha256"] for tree_id, row in rows.items()}
    assert all(tree_from_dict(row).runtime.jev.id == DEMO_JEV_ID for row in restored.values())


# --------------------------------------------------------------------------- #
# 4. run_cases.py：root-only 试算携带运行字段、JEV 窗口按服务读取
# --------------------------------------------------------------------------- #
def test_run_cases_uses_multi_jev_catalog_and_self_contained_probes():
    source = RUN_CASES.read_text(encoding="utf-8")
    assert "/admin/jev/window" not in source, "单 JEV 窗口端点已删除"
    assert 'runtime=' in source and "def admin_try" in source, "root-only 探针必须携带四字段运行配置"
    assert "--confirm-sync" in source
    assert "impact_confirmation" in source
    assert "services" in source
