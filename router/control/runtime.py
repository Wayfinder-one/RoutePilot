from __future__ import annotations

import json
import os
from dataclasses import replace

from router.jev.config import JevSettings
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MAX_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
)
from router.reasoning import ReasoningError, normalize_reasoning, validate_model_profile
from router.routing.graph.validate import EnableContext
from router.routing.runtime import GraphRuntime, runtime_from_dict
from router.store.jev_config import JevConfigRepo
from router.store.repositories import (
    ModelRepo,
    ProviderRepo,
    SettingsRepo,
    TimePeriodRepo,
)
from router.upstream.catalog import Backend, _headers_for, parse_profile_text


def _int_setting(repo: SettingsRepo, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = repo.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _db_window(conn) -> tuple[int, int]:
    repo = SettingsRepo(conn)
    context = _int_setting(
        repo, "jev_context_tokens", DEFAULT_CONTEXT_TOKENS, MIN_CONTEXT_TOKENS, MAX_TOKENS)
    input_tokens = _int_setting(
        repo, "jev_input_tokens", DEFAULT_INPUT_TOKENS, MIN_INPUT_TOKENS, context)
    return min(input_tokens, context), context


def runtime_from_db(conn) -> GraphRuntime:
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
    settings = JevConfigRepo(conn).load()
    input_tokens, context_tokens = _db_window(conn)
    raw = {
        "backends": {"providers": providers, "models": models},
        "jev": settings.behavior_dict() if settings is not None else None,
        "jev_window": {"input_tokens": input_tokens, "context_tokens": context_tokens},
        "time_periods": TimePeriodRepo(conn).load(),
    }
    return runtime_from_dict(raw)


def sync_db_from_runtime(conn, runtime: GraphRuntime) -> None:
    existing_providers = {
        row["name"]: row for row in ProviderRepo(conn).list()
    }
    existing_jev = JevConfigRepo(conn).load()
    conn.execute("DELETE FROM models")
    conn.execute("DELETE FROM providers")
    for provider in runtime.backends.providers:
        existing = existing_providers.get(provider["name"])
        api_key = ""
        if existing is not None and \
                existing["base_url"].rstrip("/") == provider["base_url"].rstrip("/"):
            api_key = existing.get("api_key") or ""
        ProviderRepo(conn).upsert(
            provider["name"], provider["base_url"],
            key_env=provider.get("key_env") or "",
            reasoning_format=provider.get("reasoning_format") or "openai",
            api_key=api_key,
        )
    for model in runtime.backends.models:
        ModelRepo(conn).create(
            model["id"], model["upstream"], model["provider"],
            json.dumps(model.get("profile") or {}, ensure_ascii=False),
        )
    if runtime.jev is None:
        conn.execute("DELETE FROM jev_config WHERE id = 1")
    else:
        api_key = ""
        if existing_jev is not None and existing_jev.base_url == runtime.jev.base_url \
                and existing_jev.model == runtime.jev.model:
            api_key = existing_jev.api_key
        JevConfigRepo(conn).save(replace(runtime.jev, api_key=api_key))
    settings = SettingsRepo(conn)
    settings.set("jev_input_tokens", str(runtime.jev_input_tokens))
    settings.set("jev_context_tokens", str(runtime.jev_context_tokens))
    TimePeriodRepo(conn).save([period.to_dict() for period in runtime.time_periods])


def runtime_for_tree(conn, tree) -> GraphRuntime:
    if tree is not None and tree.runtime is not None:
        return tree.runtime
    return runtime_from_db(conn)


def effective_runtime(conn, trees) -> GraphRuntime:
    return runtime_for_tree(conn, trees.active())


# ── 管理面有效配置读取：活动图内嵌配置优先，旧图/无活动图回退数据库 ──────
# 配置文件（JEV/窗口/时段）不再是非密钥运行配置的必要来源；管理页面展示与
# 编辑都必须和运行快照消费同一个事实源。
def stored_jev_key(conn, settings: JevSettings) -> str:
    """数据库 JEV 密钥：仅当 base_url 与 model 都匹配时复用（spec §5）。"""
    stored = JevConfigRepo(conn).load()
    if stored is not None and stored.base_url == settings.base_url \
            and stored.model == settings.model:
        return stored.api_key
    return ""


def effective_jev(conn, trees) -> JevSettings | None:
    """管理面 JEV：活动图内嵌配置优先，旧图回退数据库。

    图内配置本身不带密钥；返回前按端点匹配复用数据库密钥（与运行快照规则一致），
    环境变量不在这里展开，只体现在 ``key_source``。
    """
    active = trees.active()
    if active is not None and active.runtime is not None:
        if active.runtime.jev is None:
            return None
        return replace(active.runtime.jev, api_key=stored_jev_key(conn, active.runtime.jev))
    return JevConfigRepo(conn).load()


def effective_window(conn, trees) -> tuple[int, int]:
    """管理面 JEV 输入窗口 (input_tokens, context_tokens)。"""
    active = trees.active()
    if active is not None and active.runtime is not None:
        return active.runtime.jev_input_tokens, active.runtime.jev_context_tokens
    return _db_window(conn)


def effective_periods(conn, trees) -> list[dict]:
    """管理面时段定义：活动图内嵌配置优先，旧图回退数据库。"""
    active = trees.active()
    if active is not None and active.runtime is not None:
        return [period.to_dict() for period in active.runtime.time_periods]
    return TimePeriodRepo(conn).load()


def enable_context_for_runtime(runtime: GraphRuntime) -> EnableContext:
    formats = {
        provider["name"]: provider.get("reasoning_format") or "openai"
        for provider in runtime.backends.providers
    }
    return EnableContext(
        known_models={model["id"] for model in runtime.backends.models},
        model_reasoning={
            model["id"]: normalize_reasoning(
                model.get("profile"), formats.get(model["provider"], "openai"))
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
    if settings is None:
        return None
    stored = JevConfigRepo(conn).load()
    stored_key = ""
    if stored is not None and stored.base_url == settings.base_url and stored.model == settings.model:
        stored_key = stored.api_key
    return replace(
        settings,
        api_key=stored_key or os.environ.get("ROUTER_JEV_API_KEY", ""),
    )


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
