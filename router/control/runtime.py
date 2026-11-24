"""画布运行配置装配与密钥解析。

运行配置只有一个事实源：已保存画布的四个扁平运行字段（``backends`` / ``jev`` /
``jev_window`` / ``time_periods``）。本模块不包含任何「旧图/无启用图 → 数据库」
的过渡回退；数据库目录只用于管理面读取与密钥存储。

密钥纪律：``JevService`` 只保存非密钥字段；``resolve_jev_settings`` 按 JEV 服务
``id`` + 端点匹配 ``jev_services`` 表里已存密钥，或读取 ``key_env`` 指向的环境
变量。任何密钥都不进入 JSON、快照或 API 出参。
"""
from __future__ import annotations

import os

from router.jev.config import JevSettings
from router.jev.service import JevService
from router.reasoning import normalize_reasoning
from router.routing.graph.validate import EnableContext
from router.routing.runtime import GraphRuntime
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ProviderRepo
from router.upstream.catalog import Backend, _headers_for


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


def _stored_provider_key(conn, provider: dict) -> str:
    stored = ProviderRepo(conn).get(provider["name"])
    if stored is None:
        return ""
    if stored["base_url"].rstrip("/") != provider["base_url"].rstrip("/"):
        return ""
    return stored.get("api_key") or ""


def resolve_jev_settings(conn, service: JevService | None) -> JevSettings | None:
    """解析所选 JEV 服务的运行设置（含密钥）。

    密钥来源只有两个：``jev_services`` 表里 ``id`` + 端点都匹配的已存密钥，或
    ``key_env`` 指向的环境变量。绝不跨服务复用环境变量或端点密钥。
    """
    if service is None:
        return None
    stored = JevServiceRepo(conn).stored_api_key(service)
    environment = os.environ.get(service.key_env, "") if service.key_env else ""
    return service.settings(api_key=stored or environment)


def snapshot_parts(conn, runtime: GraphRuntime) -> dict:
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
            headers=_headers_for(
                provider.get("key_env") or "", _stored_provider_key(conn, provider)),
            upstream_name=model["upstream"],
            reasoning=reasoning,
            provider=provider["name"],
        )
        capability = profile.get("capability")
        capabilities[model["id"]] = capability.strip() if isinstance(capability, str) else ""
    return {
        "backends": backends,
        "capabilities": capabilities,
        "jev": resolve_jev_settings(conn, runtime.jev),
        "jev_input_tokens": runtime.jev_input_tokens,
        "jev_context_tokens": runtime.jev_context_tokens,
        "time_periods": list(runtime.time_periods),
    }
