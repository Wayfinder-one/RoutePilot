"""画布运行配置装配与 JEV 密钥注入。

运行配置只有一个事实源：已保存画布的四个扁平运行字段（``backends`` / ``jev`` /
``jev_window`` / ``time_periods``）。本模块不包含任何「旧图/无启用图 → 数据库」
的过渡回退，也不打开 SQLite、不读取环境变量里的密钥：装配只使用调用方显式传入
的 ``jev_api_key`` 值。

密钥纪律：``JevService`` 只保存非密钥字段；Provider Key 由执行层在调用模型上游时
注入，不进入内核快照。任何密钥都不进入 JSON、快照或 API 出参。
"""
from __future__ import annotations

from router.reasoning import normalize_reasoning
from router.routing.graph.validate import EnableContext
from router.routing.runtime import GraphRuntime
from router.upstream.catalog import Backend


def enable_context_for_runtime(runtime: GraphRuntime) -> EnableContext:
    formats = {
        provider["name"]: provider.get("reasoning_format") or "openai"
        for provider in runtime.backends.providers
    }
    return EnableContext(
        known_models={model["id"] for model in runtime.backends.models},
        model_reasoning={
            model["id"]: normalize_reasoning(
                model.get("profile"), formats.get(model.get("provider"), "openai"))
            for model in runtime.backends.models
        },
        known_periods={period.name for period in runtime.time_periods},
        jev_available=runtime.jev is not None,
    )


def snapshot_parts(runtime: GraphRuntime, *, jev_api_key: str = "") -> dict:
    providers = {provider["name"]: provider for provider in runtime.backends.providers}
    backends = {}
    capabilities = {}
    for model in runtime.backends.models:
        provider = providers[model["provider"]]
        profile = model.get("profile") or {}
        reasoning = normalize_reasoning(profile, provider["reasoning_format"])
        if reasoning is not None and (
                not reasoning.get("declared") or not reasoning.get("supported")):
            reasoning = None
        backends[model["id"]] = Backend(
            url=f"{provider['base_url']}/chat/completions",
            headers={},
            upstream_name=model["upstream"],
            reasoning=reasoning,
            provider=provider["name"],
        )
        capability = profile.get("capability")
        capabilities[model["id"]] = capability.strip() if isinstance(capability, str) else ""
    service = runtime.jev
    return {
        "backends": backends,
        "capabilities": capabilities,
        "jev": service.settings(api_key=jev_api_key) if service is not None else None,
        "jev_input_tokens": runtime.jev_input_tokens,
        "jev_context_tokens": runtime.jev_context_tokens,
        "time_periods": list(runtime.time_periods),
    }
