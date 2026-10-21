"""``router/presets.py`` 内置目录的结构测试。

契约：``docs/模型目录与推理配置-2026-10-21.md`` §3.2 / §4 / §5.3 / §8。

只依赖 pytest + 标准库 + ``router.reasoning`` 的**常量**（``LEVELS`` / ``FORMAT_IDS`` /
``FORMATS``）——不调用 backend 尚未实现的函数，避免与并行任务互相阻塞。

覆盖点：

1. 提供方/模型的结构、必填字段、数量与唯一性；
2. ``format`` / 档位 id 的合法集合与 ``efforts`` 的有序性；
3. §4 的 ``needs_wire`` / ``supports_sre`` 规则以及各家 ``off`` 档语义；
4. ``base_url`` 是 OpenAI 兼容前缀（router 会拼 ``/chat/completions``、``/models``）；
5. 推理声明三态（未声明 / ``false`` / 已声明）都有覆盖，且目录里没有对
   ``string-thinking`` / ``ant-ling`` 等未收录提供方的臆造。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from router import presets
from router.presets import PROVIDER_PRESETS, get_preset, provider_ids
from router.reasoning import FORMATS, FORMAT_IDS, LEVELS

# ---------------------------------------------------------------- 契约常量

#: 契约 §8 要求「至少」覆盖的提供方（外加本地 OpenAI 兼容服务）。
REQUIRED_PROVIDER_IDS = (
    "openai", "anthropic", "deepseek", "moonshot", "qwen", "zai",
    "openrouter", "gemini", "xai", "together", "groq", "mistral", "minimax",
    "local",
)

MIN_PROVIDERS, MAX_PROVIDERS = 8, 14
MIN_MODELS, MAX_MODELS = 3, 6

#: 少数提供方在 2026-10 的官方模型列表不足 3 个可用模型，按「宁缺毋滥」放宽下限：
#: MiniMax 只剩 MiniMax-M3.1-Flash / MiniMax-M3（M2.x 已退役）。
PROVIDER_MIN_MODELS_OVERRIDES = {"minimax": 2}

PROVIDER_FIELDS = frozenset(
    {"id", "name", "base_url", "key_env", "reasoning_format", "models"})
MODEL_FIELDS = frozenset(
    {"id", "name", "context_window", "max_tokens", "tier", "domains", "profile"})
REASONING_FIELDS = frozenset(
    {"format", "efforts", "default", "supports_reasoning_effort"})

TIERS = ("small", "medium", "large")
#: 本目录里实际声明的写法；string-thinking / ant-ling 没有对应提供方，不允许出现。
EXPECTED_FORMATS = frozenset(
    {"openai", "deepseek", "qwen", "qwen-chat-template", "openrouter", "zai",
     "together"})

PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: 上游模型 id：允许 ``a/b``、``gpt-5.6-luna``、``MiniMax-M3.1-Flash`` 这类真实写法，
#: 只禁止空白/控制字符与超长（POST /admin/models 的 upstream 上限 200）。
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+~-]{0,199}$")


def _all_providers() -> list[dict]:
    return list(PROVIDER_PRESETS)


def _all_models() -> list[tuple[dict, dict]]:
    return [(provider, model)
            for provider in PROVIDER_PRESETS
            for model in provider["models"]]


def _model_ids() -> list[str]:
    return [f"{provider['id']}/{model['id']}" for provider, model in _all_models()]


def _effective_format(provider: dict, model: dict) -> str:
    """模型级 ``format`` 覆盖提供方默认值（§3.2 / §4）。"""
    reasoning = (model.get("profile") or {}).get("reasoning")
    if isinstance(reasoning, dict) and "format" in reasoning:
        return reasoning["format"]
    return provider["reasoning_format"]


def _declared_models() -> list[tuple[dict, dict]]:
    """返回有推理声明（dict 形态，不含 ``false``）的 (provider, model)。"""
    out = []
    for provider, model in _all_models():
        reasoning = (model.get("profile") or {}).get("reasoning")
        if isinstance(reasoning, dict):
            out.append((provider, model))
    return out


# ---------------------------------------------------------------- 提供方结构

def test_catalog_is_pure_data_module():
    """§5.1/§8：presets 是纯数据模块，不 import ``router.reasoning``（避免循环依赖）。"""
    source = Path(presets.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [node for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in imports:
        if isinstance(node, ast.ImportFrom):
            assert node.module == "__future__", f"意外的 import：from {node.module}"
        else:
            raise AssertionError("presets.py 不应 import 任何模块")
    assert "from router.reasoning" not in source
    assert "import router.reasoning" not in source


def test_provider_breadth_and_order():
    ids = list(provider_ids())
    assert MIN_PROVIDERS <= len(ids) <= MAX_PROVIDERS, ids
    assert len(set(ids)) == len(ids), f"提供方 id 重复：{ids}"
    missing = set(REQUIRED_PROVIDER_IDS) - set(ids)
    assert not missing, f"缺少契约要求的提供方：{sorted(missing)}"
    # provider_ids() 必须与目录顺序一致。
    assert ids == [provider["id"] for provider in PROVIDER_PRESETS]


@pytest.mark.parametrize("provider", _all_providers(), ids=provider_ids())
def test_provider_entry(provider):
    assert isinstance(provider, dict)
    assert set(provider) == PROVIDER_FIELDS, set(provider) ^ PROVIDER_FIELDS

    assert isinstance(provider["id"], str)
    assert PROVIDER_ID_RE.match(provider["id"]), provider["id"]
    assert isinstance(provider["name"], str) and provider["name"].strip()
    assert provider["name"] == provider["name"].strip()

    base_url = provider["base_url"]
    assert isinstance(base_url, str) and base_url
    parts = urlsplit(base_url)
    assert parts.scheme in {"http", "https"}, base_url
    assert parts.netloc, base_url
    assert not base_url.endswith("/"), "base_url 不能带尾斜杠（router 直接拼路径）"
    assert not base_url.endswith("/chat/completions"), base_url
    assert not base_url.endswith("/models"), base_url
    assert not any(ch.isspace() for ch in base_url), base_url

    key_env = provider["key_env"]
    assert isinstance(key_env, str)
    if provider["id"] == "local":
        assert key_env == "", "本地服务的 key_env 必须为空串（无 key）"
    else:
        assert KEY_ENV_RE.match(key_env), key_env

    assert provider["reasoning_format"] in FORMAT_IDS, provider["reasoning_format"]

    models = provider["models"]
    assert isinstance(models, (tuple, list)) and models, "models 不能为空"


# ---------------------------------------------------------------- 模型结构

@pytest.mark.parametrize("provider", _all_providers(), ids=provider_ids())
def test_models_per_provider(provider):
    models = list(provider["models"])
    low = PROVIDER_MIN_MODELS_OVERRIDES.get(provider["id"], MIN_MODELS)
    assert low <= len(models) <= MAX_MODELS, (
        f"{provider['id']} 有 {len(models)} 个模型（要求 {low}~{MAX_MODELS}）")
    ids = [model["id"] for model in models]
    assert len(set(ids)) == len(ids), f"{provider['id']} 内模型 id 重复：{ids}"


@pytest.mark.parametrize("provider, model", _all_models(), ids=_model_ids())
def test_model_entry(provider, model):
    assert isinstance(model, dict)
    assert set(model) <= MODEL_FIELDS, f"未知字段：{set(model) - MODEL_FIELDS}"
    for field in ("id", "name", "tier", "domains", "profile"):
        assert field in model, f"{provider['id']}/{model.get('id')} 缺少 {field}"

    assert isinstance(model["id"], str) and MODEL_ID_RE.match(model["id"]), model["id"]
    assert isinstance(model["name"], str) and model["name"].strip()

    context = model.get("context_window")
    max_tokens = model.get("max_tokens")
    for field, value in (("context_window", context), ("max_tokens", max_tokens)):
        if value is not None:
            assert isinstance(value, int) and not isinstance(value, bool), field
            assert value > 0, f"{field} 必须为正：{value}"
    if context is not None and max_tokens is not None:
        assert max_tokens <= context, (
            f"{model['id']} 的 max_tokens({max_tokens}) 大于 context_window({context})")

    assert model["tier"] in TIERS, model["tier"]
    domains = model["domains"]
    assert isinstance(domains, (tuple, list))
    assert all(isinstance(domain, str) and domain for domain in domains)
    assert len(set(domains)) == len(domains)

    profile = model["profile"]
    assert isinstance(profile, dict)
    # profile 与落库的 models.profile 同构：domains/tier 必须与顶层一致。
    assert profile.get("tier") == model["tier"]
    assert list(profile.get("domains") or []) == list(domains)
    assert set(profile) <= {"domains", "tier", "reasoning"}


# ------------------------------------------------------------ 推理声明（§3.2/§4）

@pytest.mark.parametrize("provider, model", _all_models(), ids=_model_ids())
def test_reasoning_declaration(provider, model):
    profile = model["profile"]
    if "reasoning" not in profile:
        return  # 未声明：UI 显示「未声明推理能力」，只允许透传
    reasoning = profile["reasoning"]
    if reasoning is False:
        return  # 明确不支持推理
    assert isinstance(reasoning, dict), "reasoning 只能是 false / 对象 / 缺失"
    assert set(reasoning) <= REASONING_FIELDS, (
        f"未知 reasoning 字段：{set(reasoning) - REASONING_FIELDS}")

    fmt = _effective_format(provider, model)
    assert fmt in FORMAT_IDS, f"{model['id']} 的 format 非法：{fmt}"
    spec = FORMATS[fmt]

    efforts = reasoning.get("efforts")
    assert isinstance(efforts, dict) and efforts, "efforts 必填且非空"
    assert set(efforts) <= set(LEVELS), f"非法档位：{set(efforts) - set(LEVELS)}"
    # 插入顺序 = UI 展示顺序，必须与 LEVELS 的固定顺序一致。
    assert list(efforts) == [level for level in LEVELS if level in efforts]
    # §3.2：至少 1 个非 off 档位，否则请用 reasoning: false。
    assert [level for level in efforts if level != "off"], "至少要有一个非 off 档位"

    # wire 的类型与 needs_wire 规则：非 off 档在 needs_wire 方言下必须给非空字符串。
    for level, wire in efforts.items():
        if wire is not None:
            assert isinstance(wire, str) and wire, f"{level} 的 wire 必须是非空字符串"
            assert wire == wire.strip(), f"{level} 的 wire 不能带首尾空白"
        if level != "off" and spec.needs_wire:
            assert isinstance(wire, str) and wire, (
                f"{fmt} 方言的 {level} 档必须声明协议拼写（needs_wire）")
        if level == "off" and spec.needs_wire:
            assert isinstance(wire, str) and wire, (
                f"{fmt} 方言的 off 档必须声明禁用拼写（如 OpenAI 系用 none）")

    # 各家 off 语义：format 自身能表达关闭的方言不应再带 wire。
    if fmt in {"deepseek", "zai", "together", "qwen", "qwen-chat-template"}:
        if "off" in efforts:
            assert efforts["off"] is None, (
                f"{fmt} 方言的 off 由 format 自身表达（thinking.type=disabled / "
                f"reasoning.enabled=false / enable_thinking=false），wire 必须是 null")
    if fmt in {"qwen", "qwen-chat-template"}:
        # 只有开关两态：除 off 外只允许 on 档。
        assert set(efforts) - {"off"} == {"on"}, (
            f"{fmt} 方言只有开关两态，非 off 档只能是 on：{set(efforts)}")

    # supports_reasoning_effort：仅 openai/deepseek/zai/together 允许出现；出现即须为 bool。
    if "supports_reasoning_effort" in reasoning:
        value = reasoning["supports_reasoning_effort"]
        assert isinstance(value, bool)
        assert spec.supports_sre, (
            f"{fmt} 方言不允许 supports_reasoning_effort（§3.2 → 422）")
        if value:
            # sre 会把 wire ?? level 写进 reasoning_effort，所以非 off 档必须显式给 wire，
            # 否则会把 off/on 这类档位 id 当作协议值发出去。
            for level, wire in efforts.items():
                if level != "off":
                    assert isinstance(wire, str) and wire, (
                        f"{fmt} 且 supports_reasoning_effort=true 时 {level} 档必须给 wire")
    # deepseek/zai/together 的开关档（wire=null）必须显式关掉 sre，避免发出非法档位 id。
    non_off = [level for level in efforts if level != "off"]
    if fmt in {"deepseek", "zai", "together"} and any(
            efforts[level] is None for level in non_off):
        assert reasoning.get("supports_reasoning_effort") is False, (
            "开关式声明必须 supports_reasoning_effort: false")

    default = reasoning.get("default")
    if default is not None:
        assert isinstance(default, str)
        assert default in efforts, f"default({default}) 不在 efforts 里"


def test_reasoning_off_wire_for_openai_family():
    """OpenAI 系 off 用 "none" 拼写；DeepSeek/Z.ai 用 off:null + 关闭开关。"""
    checked = 0
    for provider, model in _declared_models():
        fmt = _effective_format(provider, model)
        efforts = model["profile"]["reasoning"]["efforts"]
        if "off" not in efforts:
            continue
        checked += 1
        if fmt in {"openai", "openrouter"}:
            assert efforts["off"] == "none", (
                f"{provider['id']}/{model['id']} 的 {fmt} off 应为 none")
        elif fmt == "deepseek":
            assert efforts["off"] is None
    assert checked >= 5


def test_reasoning_states_are_all_represented():
    """三态都要有真实样本：未声明 / false / 已声明。"""
    undeclared, unsupported, declared = [], [], []
    for provider, model in _all_models():
        reasoning = model["profile"].get("reasoning")
        if reasoning is None:
            undeclared.append(model["id"])
        elif reasoning is False:
            unsupported.append(model["id"])
        else:
            declared.append(model["id"])
    assert len(undeclared) >= 3, "应有「未声明推理能力」的模型（拿不准就不写）"
    assert len(unsupported) >= 2, "应有「明确不支持推理」（reasoning: false）的模型"
    assert len(declared) >= 20, "推理声明覆盖面太窄"


def test_reasoning_format_coverage():
    """目录要覆盖各家协议方言（§4），且只用 FORMAT_IDS 里的写法。"""
    covered = set()
    for provider, model in _declared_models():
        fmt = _effective_format(provider, model)
        assert fmt in FORMAT_IDS
        covered.add(fmt)
    assert EXPECTED_FORMATS <= covered, (
        f"未覆盖的方言：{sorted(EXPECTED_FORMATS - covered)}")
    # 没有对应提供方的方言不该出现在预置目录里（用户可自定义）。
    assert not {"string-thinking", "ant-ling"} & covered


def test_format_override_only_where_verifiable():
    """模型级 format 覆盖只用于同一提供方内协议不同的模型（Kimi K3 / Together GPT-OSS /
    本地 Qwen 模板），且覆盖后的方言必须有显式理由（这里用固定清单钉住）。"""
    overrides = {
        (provider["id"], model["id"], model["profile"]["reasoning"]["format"])
        for provider, model in _declared_models()
        if "format" in model["profile"]["reasoning"]
    }
    assert overrides == {
        ("moonshot", "kimi-k3", "openai"),
        ("together", "openai/gpt-oss-120b", "openai"),
        ("local", "qwen3-8b", "qwen-chat-template"),
        ("local", "qwen3-32b", "qwen-chat-template"),
    }


# ------------------------------------------------------------------ 辅助函数与规模

def test_helper_functions():
    assert provider_ids() == tuple(provider["id"] for provider in PROVIDER_PRESETS)
    deepseek = get_preset("deepseek")
    assert deepseek is not None
    assert deepseek["reasoning_format"] == "deepseek"
    assert get_preset("no-such-provider") is None
    assert get_preset("") is None


def test_catalog_size():
    total = sum(len(provider["models"]) for provider in PROVIDER_PRESETS)
    assert total >= 40, f"预置模型总数 {total}，覆盖面不足"


def test_provider_base_urls_are_unique():
    """同一目录里不应该有两个提供方指向同一个 OpenAI 兼容前缀。"""
    urls = [provider["base_url"].rstrip("/") for provider in PROVIDER_PRESETS]
    assert len(set(urls)) == len(urls)
