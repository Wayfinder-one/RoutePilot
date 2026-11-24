"""五个演示案例与随仓库发布画布的严格自包含契约。

覆盖（Task 7）：

1. 每份 ``configs/cases/case-*.json`` 都是可直接导入的 ``sr-routing-graph`` v4
   文档：无外部状态 ``id``，生命周期 ``enabled``/``default`` 恒为 False，四个运行字段齐全，
    内存无密钥；
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
import sys
import types
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
SCRIPTS_DIR = REPO_ROOT / "scripts" / "demo"
APPLY_CASES = SCRIPTS_DIR / "apply_cases.py"
RUN_CASES = SCRIPTS_DIR / "run_cases.py"

DEMO_PROVIDER = "demo-tap"
DEMO_PROVIDER_BASE_URL = "http://127.0.0.1:9300/v1"
DEMO_PROVIDER_KEY_ENV = ""
DEMO_PROVIDER_REASONING_FORMAT = "openai"
DEMO_JEV_ID = "demo-jev"
DEMO_JEV_BASE_URL = "http://127.0.0.1:30104"
DEMO_JEV_MODEL = "d1-2B"
DEMO_JEV_TIMEOUT_SECONDS = 10.0
DEMO_INPUT_TOKENS = 2048
DEMO_CONTEXT_TOKENS = 4096

#: 五份案例内嵌的 JEV 指令/状态模板（2026-11-03 线上实例实测值，逐字钉死）。
#: 旧测试只在 gitignore 的 ``docs/.../evidence/platform.json`` 存在时才比对，干净
#: checkout / CI 里等于没测；现在无条件断言，prompt 被改写会直接失败。
DEMO_JEV_INSTRUCTIONS = (
    "你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明，"
    "选择最合适处理这条请求的模型。"
)
DEMO_JEV_STATE_TEMPLATE = (
    "【待判断的用户请求】\n{request}\n\n【任务】请选择最合适处理这条请求的模型。"
)

#: 每个案例必须内嵌的模型 ID 全集（按案例文档实际内容钉死，增删即失败）。
CASE_MODEL_IDS = {
    "case-industry": {
        "industry-medical", "industry-legal", "industry-finance",
        "industry-energy", "industry-code", "industry-general",
    },
    "case-multimodal": {"vl-qwen35-4b", "text-general"},
    "case-reasoning": {"reason-qwen", "reason-openai", "reason-deepseek"},
    "case-logic": {"industry-energy", "ha-general"},
    "case-ha": {"ha-slow-a", "ha-slow-b", "ha-fail", "ha-general"},
}

#: 每个案例实际引用/嵌入的模型数量（设计文档 §案例与脚本）。
CASE_MODEL_COUNTS = {case_id: len(model_ids) for case_id, model_ids in CASE_MODEL_IDS.items()}

#: 五份案例按 ``case-*.json`` 文件名顺序展开的全部 17 条模型条目
#: （``{id, upstream, provider, profile}``，含 ha-general / industry-energy 两处共享）
#: 的规范化指纹：``sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True))``。
#: profile 在迁移/整理中被悄悄改写会在此失败；更新案例后按上面的方式重算此常量。
CASE_MODEL_ENTRIES_SHA256 = (
    "c7ceda8db669da239fed2af9b078803eb45e58606e6a607617b492fc47c0ce69")

#: 15 个唯一模型的 (upstream, provider) 显式表；摘要失败时先查这里是否漂移。
EXPECTED_MODEL_SOURCES = {
    "ha-fail": ("fail-upstream", "demo-tap"),
    "ha-general": ("general-backend", "demo-tap"),
    "ha-slow-a": ("slow-a", "demo-tap"),
    "ha-slow-b": ("slow-b", "demo-tap"),
    "industry-code": ("industry-code", "demo-tap"),
    "industry-energy": ("industry-energy", "demo-tap"),
    "industry-finance": ("industry-finance", "demo-tap"),
    "industry-general": ("industry-general", "demo-tap"),
    "industry-legal": ("industry-legal", "demo-tap"),
    "industry-medical": ("industry-medical", "demo-tap"),
    "reason-deepseek": ("reason-base", "demo-tap"),
    "reason-openai": ("reason-base", "demo-tap"),
    "reason-qwen": ("reason-base", "demo-tap"),
    "text-general": ("general-backend", "demo-tap"),
    "vl-qwen35-4b": ("vl-model", "demo-tap"),
}

#: 发布文档 -> (JEV 服务 id, input_tokens, context_tokens)（§画布快照）。
SHIPPED_DOCUMENTS = {
    REPO_ROOT / "configs" / "default_routing_tree.json": ("default", 1024, 4096),
    REPO_ROOT / "configs" / "production_routing_tree.json": ("default", 1024, 4096),
    REPO_ROOT / "router" / "console" / "routing" / "example.json": ("default", 1024, 4096),
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


def _load_run_cases():
    """按文件路径导入取证脚本（不执行 main）。

    本仓库的测试 venv 不装 ``requests``（脚本只在真实运行时需要它），因此这里注入一个
    最小替身，仅用于让模块级 ``import requests`` 成功；``Api`` 的真实网络调用不在测试范围。
    """
    module_name = "demo_run_cases"
    original_path = list(sys.path)
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        try:  # 真实环境（有 requests）直接用；测试 venv 里退化为空替身
            import requests  # noqa: F401
        except ImportError:
            stub = types.ModuleType("requests")
            stub.Session = object
            stub.Response = object
            sys.modules["requests"] = stub
        spec = importlib.util.spec_from_file_location(module_name, RUN_CASES)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = original_path


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
    assert raw["version"] == 4
    assert "id" not in raw, "部署 ID 由 manifest.json + 文件名提供，案例文件不保存外部状态"
    assert raw["departments"] == []
    assert raw["enabled"] is False and raw["default"] is False
    assert "api_key" not in text

    tree = _parse(path, tree_id=path.stem)

    # JEV 服务属于画布快照：id/端点/模型/窗口都内嵌，Key 只留环境变量名（此处为空）。
    assert tree.runtime.jev is not None
    assert tree.runtime.jev.id == DEMO_JEV_ID
    assert tree.runtime.jev.base_url == DEMO_JEV_BASE_URL
    assert tree.runtime.jev.model == DEMO_JEV_MODEL
    assert tree.runtime.jev.key_env == ""
    assert tree.runtime.jev.timeout_seconds == DEMO_JEV_TIMEOUT_SECONDS
    assert tree.runtime.jev_input_tokens == DEMO_INPUT_TOKENS
    assert tree.runtime.jev_context_tokens == DEMO_CONTEXT_TOKENS

    # 启用级校验只读画布自带运行配置，不依赖数据库目录。
    assert tree_issues(None, tree) == []

    used = _used_target_models(tree)
    embedded = _embedded_models(tree)
    assert sorted(used) == sorted(model["id"] for model in embedded)
    assert {model["id"] for model in embedded} == CASE_MODEL_IDS[path.stem]
    assert len(embedded) == CASE_MODEL_COUNTS[path.stem]
    assert [(provider["name"], provider["base_url"], provider["key_env"],
             provider["reasoning_format"])
            for provider in tree.runtime.backends.providers] == [
        (DEMO_PROVIDER, DEMO_PROVIDER_BASE_URL, DEMO_PROVIDER_KEY_ENV,
         DEMO_PROVIDER_REASONING_FORMAT)]
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
    services = {json.dumps(document["jev"], ensure_ascii=False, sort_keys=True)
                for document in documents}
    windows = {json.dumps(document["jev_window"], ensure_ascii=False, sort_keys=True)
               for document in documents}

    assert providers == {DEMO_PROVIDER}
    assert len(services) == 1
    assert len(windows) == 1
    model_ids = [model["id"] for document in documents for model in document["backends"]["models"]]
    assert len(model_ids) == sum(CASE_MODEL_COUNTS.values()) == 17
    # ha-general（案例四/五）与 industry-energy（案例一/四）各共享一次 → 15 个独立模型。
    assert len(set(model_ids)) == 15
    duplicated = {model_id for model_id in model_ids if model_ids.count(model_id) > 1}
    assert duplicated == {"ha-general", "industry-energy"}


def test_all_cases_embed_byte_identical_jev_prompt():
    """五份案例必须内嵌逐字一致、且与线上实测快照一致的 JEV 指令/状态模板。"""
    documents = _case_documents()
    instructions = {document["jev"]["instructions"] for document in documents}
    templates = {document["jev"]["state_template"] for document in documents}

    assert len(instructions) == 1, "五份案例的 JEV instructions 必须逐字一致"
    assert len(templates) == 1, "五份案例的 JEV state_template 必须逐字一致"
    # 钉死实测字面量：不依赖 gitignore 的 evidence/platform.json，干净 checkout 也生效。
    assert next(iter(instructions)) == DEMO_JEV_INSTRUCTIONS
    assert next(iter(templates)) == DEMO_JEV_STATE_TEMPLATE


def _model_entries(documents: list[dict]) -> list[dict]:
    return [
        {"id": model["id"], "upstream": model["upstream"],
         "provider": model["provider"], "profile": model["profile"]}
        for document in documents
        for model in document["backends"]["models"]
    ]


def test_case_model_profiles_match_pinned_snapshot():
    """15 份模型 profile（含顺序与共享条目）逐字钉死：迁移不得悄悄重写。"""
    entries = _model_entries(_case_documents())
    assert len(entries) == 17
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert digest == CASE_MODEL_ENTRIES_SHA256
    assert {entry["id"]: (entry["upstream"], entry["provider"]) for entry in entries} == (
        EXPECTED_MODEL_SOURCES)


def test_manifest_tree_files_align_with_case_documents():
    manifest = _read_document(CASES_DIR / "manifest.json")
    cases = manifest["cases"]
    assert len(cases) == len(_case_paths()) == 5
    assert {case["tree_file"] for case in cases} == {path.name for path in _case_paths()}
    for case in cases:
        path = CASES_DIR / case["tree_file"]
        assert path.parent == CASES_DIR and path.is_file()
        assert path.stem == case["id"]
        assert _read_document(path)["name"] == case["name"]


# --------------------------------------------------------------------------- #
# 2. 随仓库发布的默认/生产/示例文档
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,expected", SHIPPED_DOCUMENTS.items(), ids=lambda value: str(value))
def test_shipped_document_is_strict_and_carries_jev_identity(path: Path, expected):
    jev_id, input_tokens, context_tokens = expected
    text = path.read_text(encoding="utf-8")
    assert "api_key" not in text

    tree = _parse(path, tree_id=path.stem)
    assert tree.runtime.jev is not None
    assert tree.runtime.jev.id == jev_id
    assert tree.runtime.jev.key_env == SHIPPED_JEV_KEY_ENV
    assert tree.runtime.jev.base_url
    assert tree.runtime.jev.model
    assert tree.runtime.jev.timeout_seconds == DEMO_JEV_TIMEOUT_SECONDS
    assert tree.runtime.jev_input_tokens == input_tokens
    assert tree.runtime.jev_context_tokens == context_tokens
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
    assert service["key_env"] == ""
    assert service["timeout_seconds"] == DEMO_JEV_TIMEOUT_SECONDS
    assert service["input_tokens"] == DEMO_INPUT_TOKENS
    assert service["context_tokens"] == DEMO_CONTEXT_TOKENS
    assert "api_key" not in service

    provider = runtime["providers"][0]
    assert (provider["base_url"], provider["key_env"], provider["reasoning_format"]) == (
        DEMO_PROVIDER_BASE_URL, DEMO_PROVIDER_KEY_ENV, DEMO_PROVIDER_REASONING_FORMAT)

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
    for stripped in ("kind", "version", "enabled", "default"):
        assert stripped not in body
    assert body["departments"] == document["departments"]
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

GRAPH_CONFLICT_DETAIL = {
    "message": "画布已被其它请求修改，基线哈希过期",
    "code": "graph_conflict",
    "expected_document_sha256": "stale-hash",
    "actual_document_sha256": "actual-hash",
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


def test_first_attempt_graph_conflict_raises_without_retry():
    """首次（非重试）响应就是 409 graph_conflict：必须走专用停止路径，绝不重试写入。"""
    module = _load_apply_cases()
    api, seen = _api_with_responses(module, [(409, {"detail": GRAPH_CONFLICT_DETAIL})])

    with pytest.raises(module.GraphConflictError) as info:
        api.request("PUT", "/admin/trees/case-industry", {"name": "案例一"})

    assert len(seen) == 1, "首次冲突不得重试（重试会拿过期基线再写一次）"
    assert info.value.detail["actual_document_sha256"] == "actual-hash"
    assert info.value.detail["expected_document_sha256"] == "stale-hash"


def test_main_stops_cleanly_on_first_attempt_graph_conflict(monkeypatch, capsys):
    """main() 对首次冲突只打印一行停止信息并返回非零，不出 traceback、不再写入。"""
    module = _load_apply_cases()
    calls: list[tuple] = []

    class FakeApi(module.Api):
        def _raw(self, method, path, body=None, *, auth=True):
            calls.append((method, path))
            if path == "/admin/login":
                return 200, {"token": "fake-token"}
            if path == "/admin/trees":
                return 200, {"trees": []}
            return 409, {"detail": GRAPH_CONFLICT_DETAIL}

    monkeypatch.setattr(module, "Api", FakeApi)

    code = module.main(["--base-url", "http://127.0.0.1:1"])
    output = capsys.readouterr().out

    assert code == 1
    assert "Traceback" not in output
    assert "画布已变化" in output
    assert "actual-hash" in output
    assert "请重新运行" in output
    # 登录 → 读部署前 enabled 基线（纯读）→ 第一个目录写（provider PUT）就停止：
    # 没有后续模型/JEV/画布写入。
    assert calls == [
        ("POST", "/admin/login"),
        ("GET", "/admin/trees"),
        ("PUT", "/admin/providers/demo-tap"),
    ]


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

    # 全新组合根没有任何 enabled 决策组：最终状态基线必须记录为空。
    enabled_before = module.enabled_tree_ids(api)
    assert enabled_before == [], "全新实例部署前不应有 enabled 决策组"

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
    # 全新实例的收尾状态：enabled 仍为空是正确结果，脚本不得误报失败（回归点）。
    assert module.enabled_tree_ids(api) == []
    assert module.step_final_state(api, manifest["cases"], enabled_before) == []
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
# 3b. apply_cases.py 最终状态：以部署前 enabled 集合为基线（全新实例不误报）
# --------------------------------------------------------------------------- #
def _manifest_cases() -> list[dict]:
    return json.loads((CASES_DIR / "manifest.json").read_text(encoding="utf-8"))["cases"]


def _tree_row(tree_id: str, *, enabled: bool) -> dict:
    return {
        "id": tree_id,
        "enabled": enabled,
        "document_sha256": hashlib.sha256(tree_id.encode("utf-8")).hexdigest(),
    }


def _deployed_rows(*, enabled: set[str] = frozenset()) -> dict:
    rows = {case["id"]: _tree_row(case["id"], enabled=case["id"] in enabled)
            for case in _manifest_cases()}
    for tree_id in enabled - set(rows):
        rows[tree_id] = _tree_row(tree_id, enabled=True)
    return rows


def _final_state_api(module, rows: dict):
    api = module.Api("http://127.0.0.1:1")
    api.trees = lambda: rows
    return api


def test_final_state_accepts_fresh_instance_without_enabled_group(capsys):
    """全新实例部署前没有 enabled 决策组 -> 部署后仍为空是正确结果，不是失败。"""
    module = _load_apply_cases()
    cases = _manifest_cases()
    api = _final_state_api(module, _deployed_rows())

    assert module.step_final_state(api, cases, []) == []
    output = capsys.readouterr().out
    assert "部署前没有 enabled 决策组" in output
    assert "仍为空" in output
    assert "5 棵演示树保持未启用" in output


def test_final_state_preserves_preexisting_enabled_group(capsys):
    """既有实例（如 default-jev enabled）部署后必须原样保持。"""
    module = _load_apply_cases()
    cases = _manifest_cases()
    api = _final_state_api(module, _deployed_rows(enabled={"default-jev"}))

    assert module.step_final_state(api, cases, ["default-jev"]) == []
    assert "default-jev" in capsys.readouterr().out


def test_final_state_rejects_newly_enabled_demo_tree():
    """最终状态仍必须抓住「演示树被启用」：全新实例的 enabled 集合不得新增成员。"""
    module = _load_apply_cases()
    cases = _manifest_cases()
    api = _final_state_api(module, _deployed_rows(enabled={"case-ha"}))

    problems = module.step_final_state(api, cases, [])
    assert problems
    assert "不应新增" in problems[0]
    assert "case-ha" in problems[0]


def test_final_state_rejects_lost_preexisting_enabled_group():
    """既有 enabled 决策组被停用/删除同样必须失败，不能静默接受空集。"""
    module = _load_apply_cases()
    cases = _manifest_cases()
    api = _final_state_api(module, _deployed_rows())

    problems = module.step_final_state(api, cases, ["default-jev"])
    assert any("default-jev" in problem for problem in problems)


def test_final_state_rejects_missing_case_tree():
    module = _load_apply_cases()
    cases = _manifest_cases()
    rows = _deployed_rows()
    rows.pop("case-logic")
    problems = module.step_final_state(_final_state_api(module, rows), cases, [])
    assert any("case-logic" in problem for problem in problems)


def test_main_succeeds_on_fresh_instance_without_false_final_state_failure(monkeypatch, capsys):
    """main() 在全新实例（部署前无 enabled）上必须退出 0，不得因最终状态误报失败。"""
    module = _load_apply_cases()
    rows = _deployed_rows()

    class FakeApi(module.Api):
        def _raw(self, method, path, body=None, *, auth=True):
            if path == "/admin/login":
                return 200, {"token": "fake-token"}
            if path == "/admin/trees":
                return 200, {"trees": list(rows.values())}
            raise AssertionError(f"unexpected 管理请求 {method} {path}")

    monkeypatch.setattr(module, "Api", FakeApi)
    monkeypatch.setattr(module, "step_providers", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "step_models", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "step_jev_services", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "step_time_periods", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "step_trees", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "step_dry_run", lambda *args, **kwargs: 0)

    assert module.main(["--base-url", "http://127.0.0.1:1"]) == 0
    output = capsys.readouterr().out
    assert "部署前没有 enabled 决策组" in output
    assert "全部通过" in output


# --------------------------------------------------------------------------- #
# 4. run_cases.py：root-only 试算携带运行字段、JEV 窗口按服务读取
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """requests.Response 的最小替身：admin_write 只用到 status_code/json()/text。"""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body, ensure_ascii=False)


def _run_cases_api(module, responses, monkeypatch, *, confirm_sync: bool = False):
    api = module.Api("http://127.0.0.1:1")
    monkeypatch.setattr(module, "CONFIRM_SYNC", confirm_sync)
    seen: list[tuple] = []

    def fake_request(method, path, *, json_body=None, timeout=120.0, **kwargs):
        seen.append((method, path, json_body))
        return _FakeResponse(*responses.pop(0))

    api.request = fake_request
    return api, seen


def test_run_cases_has_no_legacy_single_jev_window_endpoint():
    source = RUN_CASES.read_text(encoding="utf-8")
    assert "/admin/jev/window" not in source, "单 JEV 窗口端点已删除"
    assert "/admin/jev" in source


def test_jev_window_of_reads_window_from_multi_service_catalog():
    module = _load_run_cases()
    api = module.Api("http://127.0.0.1:1")
    api.get_json = lambda path, timeout=60.0: {"status": 200, "body": {"services": [
        {"id": "demo-jev", "input_tokens": 2048, "context_tokens": 4096},
        {"id": "other", "input_tokens": 1, "context_tokens": 2},
    ]}}

    assert module.jev_window_of(api, "demo-jev") == {
        "input_tokens": 2048, "context_tokens": 4096}
    with pytest.raises(module.Fail, match="missing"):
        module.jev_window_of(api, "missing")


def test_tree_runtime_document_requires_all_four_runtime_fields():
    """root-only 试算的来源树必须四字段齐全：缺字段直接 Fail，不静默少传。"""
    module = _load_run_cases()
    tree = {
        "id": "case-logic",
        "backends": {"providers": [], "models": []},
        "jev": {"id": "demo-jev"},
        "jev_window": {"input_tokens": 2048, "context_tokens": 4096},
        "time_periods": [],
    }

    runtime = module.tree_runtime_document(tree)
    assert tuple(runtime) == module.RUNTIME_FIELDS
    assert runtime == {field: tree[field] for field in module.RUNTIME_FIELDS}

    with pytest.raises(module.Fail, match="jev_window"):
        module.tree_runtime_document({key: value for key, value in tree.items()
                                      if key != "jev_window"})
    with pytest.raises(module.Fail, match="期望 /admin/trees"):
        module.tree_runtime_document(None)


def test_admin_try_payload_carries_runtime_or_tree_id_in_process():
    """直接跑真实的 payload 构造：root-only 必带四字段；带 tree_id 时由服务端继承快照。"""
    module = _load_run_cases()
    tree = {
        "id": "case-logic",
        "backends": {"providers": [], "models": []},
        "jev": {"id": "demo-jev"},
        "jev_window": {"input_tokens": 2048, "context_tokens": 4096},
        "time_periods": [],
    }
    runtime = module.tree_runtime_document(tree)
    root = {"id": "root", "nodes": [], "edges": []}
    calls: list[tuple] = []
    api = module.Api("http://127.0.0.1:1")
    api.request = lambda method, path, *, json_body=None, **kwargs: (
        calls.append((method, path, json_body)) or _FakeResponse(200, {"ok": True}))

    result = module.admin_try(api, text="10kV 告警", root=root, runtime=runtime)
    assert result["status"] == 200
    body = calls[0][2]
    assert body["root"] == root
    assert "tree_id" not in body
    assert {field: body[field] for field in module.RUNTIME_FIELDS} == runtime

    # 缺任一运行字段：payload 构造阶段就 Fail（不把残缺 runtime 发给服务端）。
    with pytest.raises(module.Fail, match="time_periods"):
        module.admin_try(api, text="x", root=root,
                         runtime={field: runtime[field] for field in module.RUNTIME_FIELDS
                                  if field != "time_periods"})

    calls.clear()
    module.admin_try(api, text="10kV 告警", tree_id="case-logic", root=root)
    body = calls[0][2]
    assert body["tree_id"] == "case-logic"
    assert not any(field in body for field in module.RUNTIME_FIELDS)


def test_run_cases_admin_write_stops_on_first_attempt_graph_conflict(monkeypatch):
    module = _load_run_cases()
    api, seen = _run_cases_api(
        module, [(409, {"detail": GRAPH_CONFLICT_DETAIL})], monkeypatch)

    with pytest.raises(module.GraphConflictError) as info:
        api.admin_write("POST", "/admin/trees/case-industry/enable")

    assert len(seen) == 1, "首次冲突不得重试写入"
    assert info.value.detail["actual_document_sha256"] == "actual-hash"


def test_run_cases_admin_write_stops_on_impact_without_flag(monkeypatch):
    module = _load_run_cases()
    api, seen = _run_cases_api(
        module, [(409, {"detail": IMPACT_DETAIL})], monkeypatch)

    with pytest.raises(module.ImpactConfirmationRequired):
        api.admin_write("POST", "/admin/trees/case-logic/enable")

    assert len(seen) == 1, "默认遇影响确认必须停止，不得重试或继续写入"


def test_run_cases_admin_write_confirm_sync_retries_with_baseline_hashes(monkeypatch):
    module = _load_run_cases()
    api, seen = _run_cases_api(
        module,
        [(409, {"detail": IMPACT_DETAIL}), (200, {"ok": True})],
        monkeypatch,
        confirm_sync=True,
    )

    result = api.admin_write("POST", "/admin/trees/case-logic/enable")
    assert result == {"status": 200, "body": {"ok": True}}
    assert len(seen) == 2
    retry_body = seen[1][2]
    assert retry_body["impact_confirmation"] == {
        "trees": [{"id": "case-logic", "document_sha256": "abc123"}],
    }


def test_run_cases_evidence_probe_re_raises_stop_signals():
    module = _load_run_cases()
    evidence = module.Evidence()

    def raising(exc):
        def func():
            raise exc
        return func

    with pytest.raises(module.ImpactConfirmationRequired):
        evidence.probe("impact", raising(
            module.ImpactConfirmationRequired("PUT", "/admin/x", IMPACT_DETAIL)))
    with pytest.raises(module.GraphConflictError):
        evidence.probe("conflict", raising(
            module.GraphConflictError("PUT", "/admin/x", GRAPH_CONFLICT_DETAIL)))

    # 普通异常仍只记为探针失败，不中断全流程。
    record = evidence.probe("boom", raising(RuntimeError("boom")))
    assert record["ok"] is False
    assert "boom" in record["error"]
