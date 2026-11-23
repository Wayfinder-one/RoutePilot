"""运行配置装配与密钥解析（过渡层）。

本模块只剩两条路径：

- 图内嵌运行配置（``tree.runtime is not None``）：严格从画布快照读取；
- 旧图/无活动图：过渡性数据库回退（``runtime_from_db``），由下一单元删除。

密钥纪律：``JevSettings`` 只用于运行态，``resolve_jev_settings`` 按服务端点匹配
数据库里的已存密钥或读取 ``key_env`` 指向的环境变量；任何密钥都不进入 JSON。
"""
from __future__ import annotations

import os
from dataclasses import replace

from router.jev.config import JevSettings
from router.jev.service import JevService
from router.reasoning import ReasoningError, normalize_reasoning, validate_model_profile
from router.routing.graph.validate import EnableContext
from router.routing.runtime import GraphRuntime, runtime_from_dict
from router.store.jev_services import JevServiceRepo
from router.store.repositories import (
    ModelRepo,
    ProviderRepo,
    TimePeriodRepo,
)
from router.upstream.catalog import Backend, _headers_for, parse_profile_text


def first_jev_settings(conn) -> JevSettings | None:
    """过渡性回退：目录中 id 最小的 JEV 服务（不含密钥）。

    旧图/无活动图没有内嵌 JEV 时的唯一来源；多服务画布不在本路径上。
    """
    services: list[JevService] = JevServiceRepo(conn).list()
    return services[0].settings() if services else None


def runtime_from_db(conn) -> GraphRuntime:
    """过渡性数据库回退运行配置（无密钥）；下一单元随旧图兼容路径一并删除。"""
    # 延迟导入：``router.routing.snapshot`` 在模块级依赖本模块，直接导入会成环。
    from router.routing.snapshot import read_jev_window

    providers = [
        {key: value for key, value in row.items() if key != "api_key"}
        for row in ProviderRepo(conn).list()
    ]
    formats = {
        row["name"]: row.get("reasoning_format") or "openai"
        for row in providers
    }
    models = []
    for row in ModelRepo(conn).list():
        profile = parse_profile_text(row.get("profile"))
        try:
            profile = validate_model_profile(
                profile, formats.get(row["provider"], "openai"))
        except ReasoningError:
            profile = dict(profile)
            profile.pop("reasoning", None)
        models.append({**row, "profile": profile})
    settings = first_jev_settings(conn)
    input_tokens, context_tokens = read_jev_window(conn)
    raw = {
        "backends": {"providers": providers, "models": models},
        "jev": settings.behavior_dict() if settings is not None else None,
        "jev_window": {"input_tokens": input_tokens, "context_tokens": context_tokens},
        "time_periods": TimePeriodRepo(conn).load(),
    }
    return runtime_from_dict(raw)


def runtime_for_tree(conn, tree) -> GraphRuntime:
    if tree is not None and tree.runtime is not None:
        return tree.runtime
    return runtime_from_db(conn)


def effective_runtime(conn, trees) -> GraphRuntime:
    return runtime_for_tree(conn, trees.active())


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


def resolve_jev_settings(conn, settings: JevSettings | None) -> JevSettings | None:
    """按端点匹配解析 JEV 密钥（过渡语义，spec §5）。

    1. ``base_url`` 与 ``model`` 都匹配的目录服务 → 该服务的已存密钥；
    2. 未匹配时沿用该服务的 ``key_env`` 环境变量；
    3. 目录为空时保留旧 ``ROUTER_JEV_API_KEY`` 环境回退。

    环境变量不会被写成 stored，也不进入任何 JSON。
    """
    if settings is None:
        return None
    repo = JevServiceRepo(conn)
    services = repo.list()
    matched = next(
        (service for service in services
         if service.base_url == settings.base_url and service.model == settings.model),
        None,
    )
    if matched is not None:
        key = repo.stored_api_key(matched)
        if not key and matched.key_env:
            key = os.environ.get(matched.key_env, "")
        return replace(
            settings, api_key=key or os.environ.get("ROUTER_JEV_API_KEY", ""))
    for service in services:
        if service.key_env:
            key = os.environ.get(service.key_env, "")
            if key:
                return replace(settings, api_key=key)
    return replace(settings, api_key=os.environ.get("ROUTER_JEV_API_KEY", ""))


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
