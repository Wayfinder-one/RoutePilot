from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass

from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.jev.service import JevService
from router.jev.window import JevWindow
from router.reasoning import FORMAT_IDS, validate_model_profile
from router.routing.codec import reject_unknown_fields
from router.routing.conditions import TimePeriod

RUNTIME_FIELDS = frozenset({"backends", "jev", "jev_window", "time_periods"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class BackendCatalog:
    providers: tuple[dict, ...]
    models: tuple[dict, ...]


@dataclass(frozen=True)
class GraphRuntime:
    backends: BackendCatalog
    jev: JevService | None
    jev_input_tokens: int
    jev_context_tokens: int
    time_periods: tuple[TimePeriod, ...]


def _text(value, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value or len(value) > max_len or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} is empty, too long, or contains control characters")
    return value


def _id(value, field: str) -> str:
    value = _text(value, field, max_len=64)
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


#: 密钥与派生的密钥状态字段名（小写、去掉 ``-``/``_`` 后比较）。
#: 只拒绝固定集合，不做「包含 key」的模糊匹配：``monkey``、``keychain`` 之类的
#: 正常元数据必须原样保留。
_SECRET_KEYS = frozenset({"apikey", "authorization", "keypresent", "keysource"})


def _secret_key(key: str) -> bool:
    return key.casefold().replace("-", "").replace("_", "") in _SECRET_KEYS


def reject_runtime_secret_fields(value, field: str = "runtime") -> None:
    """递归拒绝运行配置里任何层级的密钥字段（逐层给出来源路径）。

    显式栈遍历：导入内容不可信，深度嵌套不能把解析变成栈溢出。
    """
    stack = [(field, value)]
    while stack:
        path, current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field} object keys must be strings")
                if _secret_key(key):
                    raise ValueError(f"secret field is not allowed: {path}.{key}")
                stack.append((f"{path}.{key}", item))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                stack.append((f"{path}[{index}]", item))


def without_runtime_secret_fields(value):
    """返回递归脱敏副本；用于兼容旧库的只读出参，不校验或修改输入。

    与 ``reject_runtime_secret_fields()`` 共用同一固定字段规则，但职责不同：
    读取投影只剔除，写入与图解析仍然拒绝，绝不静默清洗后保存。
    """
    if isinstance(value, dict):
        return {
            key: without_runtime_secret_fields(item)
            for key, item in value.items()
            if not isinstance(key, str) or not _secret_key(key)
        }
    if isinstance(value, list):
        return [without_runtime_secret_fields(item) for item in value]
    return value


def _json_copy(value, field: str):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must contain finite numbers")
        return value
    if isinstance(value, list):
        return [_json_copy(item, field) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{field} object keys must be strings")
        return {key: _json_copy(item, field) for key, item in value.items()}
    raise ValueError(f"{field} must contain only JSON values")


def _providers(raw) -> tuple[dict, ...]:
    if not isinstance(raw, list):
        raise ValueError("backends.providers must be a list")
    providers = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("provider must be an object")
        reject_unknown_fields(
            "provider", item, {"name", "base_url", "key_env", "reasoning_format"})
        name = _id(item.get("name"), "provider name")
        if name in seen:
            raise ValueError(f"duplicate provider: {name}")
        seen.add(name)
        key_env = item.get("key_env", "")
        if not isinstance(key_env, str) or key_env and not _ENV_RE.fullmatch(key_env):
            raise ValueError("invalid key_env")
        reasoning_format = item.get("reasoning_format", "openai")
        if reasoning_format not in FORMAT_IDS:
            raise ValueError("invalid reasoning_format")
        providers.append({
            "name": name,
            "base_url": _text(item.get("base_url"), "base_url", max_len=500).rstrip("/"),
            "key_env": key_env,
            "reasoning_format": reasoning_format,
        })
    return tuple(providers)


def _models(raw, providers: tuple[dict, ...]) -> tuple[dict, ...]:
    if not isinstance(raw, list):
        raise ValueError("backends.models must be a list")
    formats = {provider["name"]: provider["reasoning_format"] for provider in providers}
    models = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("model must be an object")
        reject_unknown_fields("model", item, {"id", "upstream", "provider", "profile"})
        model_id = _id(item.get("id"), "model id")
        if model_id in seen:
            raise ValueError(f"duplicate model: {model_id}")
        seen.add(model_id)
        provider = _id(item.get("provider"), "model provider")
        if provider not in formats:
            raise ValueError(f"unknown provider: {provider}")
        profile = item.get("profile")
        if not isinstance(profile, dict):
            raise ValueError("profile must be an object")
        profile = _json_copy(profile, "profile")
        validated = validate_model_profile(profile, formats[provider])
        if "capability" in validated:
            capability = validated["capability"]
            if capability is not None and not isinstance(capability, str):
                raise ValueError("capability must be a string or null")
            capability = (capability or "").strip()
            if len(capability) > 2000:
                raise ValueError("capability must not exceed 2000 characters")
            if capability:
                validated["capability"] = capability
            else:
                validated.pop("capability", None)
        models.append({
            "id": model_id,
            "upstream": _text(item.get("upstream"), "upstream", max_len=200),
            "provider": provider,
            "profile": validated,
        })
    return tuple(models)


def _jev(raw, window: JevWindow) -> JevService | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("jev must be an object or null")
    reject_unknown_fields(
        "jev", raw,
        {"id", "base_url", "model", "key_env", "timeout_seconds",
         "instructions", "state_template"})
    if "id" not in raw:
        raise ValueError("jev.id is required")
    return JevService(
        id=raw["id"],
        base_url=raw.get("base_url"),
        model=raw.get("model"),
        key_env=raw.get("key_env", ""),
        timeout_seconds=raw.get("timeout_seconds", 10.0),
        instructions=raw.get("instructions", DEFAULT_INSTRUCTIONS),
        state_template=raw.get("state_template", DEFAULT_STATE_TEMPLATE),
        input_tokens=window.input_tokens,
        context_tokens=window.context_tokens,
    )


def _window(raw) -> JevWindow:
    if not isinstance(raw, dict):
        raise ValueError("jev_window must be an object")
    reject_unknown_fields("jev_window", raw, {"input_tokens", "context_tokens"})
    return JevWindow(
        input_tokens=raw.get("input_tokens"),
        context_tokens=raw.get("context_tokens"),
    )


def _periods(raw) -> tuple[TimePeriod, ...]:
    if not isinstance(raw, list):
        raise ValueError("time_periods must be a list")
    periods = tuple(TimePeriod.from_dict(item) for item in raw)
    names = [period.name for period in periods]
    if len(names) != len(set(names)):
        raise ValueError("period names must be unique")
    return periods


def runtime_from_dict(raw: dict) -> GraphRuntime:
    # 密钥通道只封在 runtime 的四个字段上：图其它 envelope 元数据不归这里管。
    reject_runtime_secret_fields({name: raw[name] for name in RUNTIME_FIELDS})
    backends = raw["backends"]
    if not isinstance(backends, dict):
        raise ValueError("backends must be an object")
    reject_unknown_fields("backends", backends, {"providers", "models"})
    providers = _providers(backends.get("providers"))
    window = _window(raw["jev_window"])
    return GraphRuntime(
        backends=BackendCatalog(
            providers=providers,
            models=_models(backends.get("models"), providers),
        ),
        jev=_jev(raw["jev"], window),
        jev_input_tokens=window.input_tokens,
        jev_context_tokens=window.context_tokens,
        time_periods=_periods(raw["time_periods"]),
    )


def runtime_to_dict(runtime: GraphRuntime) -> dict:
    return {
        "backends": {
            "providers": copy.deepcopy(list(runtime.backends.providers)),
            "models": copy.deepcopy(list(runtime.backends.models)),
        },
        "jev": runtime.jev.snapshot_dict() if runtime.jev is not None else None,
        "jev_window": {
            "input_tokens": runtime.jev_input_tokens,
            "context_tokens": runtime.jev_context_tokens,
        },
        "time_periods": [period.to_dict() for period in runtime.time_periods],
    }


def _behavior_numbers(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_behavior_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _behavior_numbers(item) for key, item in value.items()}
    return value


def runtime_behavior_dict(runtime: GraphRuntime) -> dict:
    return _behavior_numbers(runtime_to_dict(runtime))
