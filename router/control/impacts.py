"""目录资源（模型 / provider / JEV）对自包含画布的影响清单与纯转换函数。

本模块不读写数据库、不依赖 FastAPI：输入是已解析的 ``RoutingTree`` 列表与
非密钥目录投影，输出是影响清单或新的 ``RoutingTree``。写用例先在 API 层用
这里的影响扫描生成 409 预览；用户确认后，再用这里的 ``sync_*`` / ``delete_*``
构造候选画布，交给 ``router.control.catalog_sync`` 在单事务 + 单写锁内提交。

纪律：
- 影响清单只含非密钥的目录事实（id / name / enabled / document_sha256 /
  使用位置）；替代目录同样只投影非密钥字段（模型 ``{id, upstream, provider}``；
  JEV ``{id, base_url, model, input_tokens, context_tokens}``）。
- 所有转换函数深拷贝输入，绝不修改调用方传入的树、root 或 runtime。
- JEV 的 ``sync_jev`` 同时替换 ``runtime.jev`` 与 ``jev_input_tokens`` /
  ``jev_context_tokens``：窗口属于服务，绝不允许出现「旧服务配置 + 新窗口」
  或反过来的混合体。
- 过期确认必须报告**当前**画布哈希：``require_confirmation()`` 从当前影响
  清单里取第一棵不匹配画布的 ``document_sha256`` 抛 ``GraphConflictError``，
  绝不把客户端提交的期望值当实际值回显。
"""
from __future__ import annotations

import copy
from dataclasses import replace

from router.control.errors import ReferencedByError
from router.jev.service import JevService
from router.reasoning import normalize_reasoning
from router.routing.runtime import BackendCatalog, GraphRuntime
from router.store.routing_trees import GraphConflictError

__all__ = [
    "confirmation_mismatch",
    "delete_jev",
    "delete_model",
    "jev_impacts",
    "merge_providers",
    "model_impacts",
    "provider_impacts",
    "reasoning_default",
    "reasoning_levels",
    "replace_runtime_catalog",
    "replace_target_models",
    "require_confirmation",
    "sync_jev",
    "sync_model",
    "sync_provider",
]


def _impact(tree, usage: dict) -> dict:
    """单幅画布的影响条目（纯非密钥投影）。"""
    return {
        "id": tree.id,
        "name": tree.name,
        "enabled": tree.enabled,
        "document_sha256": tree.document_sha256,
        "usage": usage,
    }


def model_impacts(trees, model_id: str) -> list[dict]:
    """模型影响：target 引用（返回节点 id）和/或运行时目录包含该模型。"""
    impacts: list[dict] = []
    for tree in trees:
        targets = [
            node.id for node in tree.root.nodes
            if any(item.model == model_id for item in getattr(node, "models", ()) or ())
        ]
        catalog = any(
            entry["id"] == model_id for entry in tree.runtime.backends.models)
        if targets or catalog:
            impacts.append(_impact(tree, {
                "targets": targets,
                "runtime_catalog": catalog,
            }))
    return impacts


def provider_impacts(trees, provider_name: str) -> list[dict]:
    """provider 影响：运行时 provider 目录和/或运行时模型引用该 provider。"""
    impacts: list[dict] = []
    for tree in trees:
        catalog = any(
            entry["name"] == provider_name
            for entry in tree.runtime.backends.providers)
        used_by_models = [
            entry["id"] for entry in tree.runtime.backends.models
            if entry.get("provider") == provider_name
        ]
        if catalog or used_by_models:
            impacts.append(_impact(tree, {
                "runtime_catalog": catalog,
                "models": {"use": used_by_models},
            }))
    return impacts


def jev_impacts(trees, service_id: str) -> list[dict]:
    """JEV 影响：运行时 ``runtime.jev.id`` 等于该服务 id 的画布。

    影响位置固定是 JEV（``usage.jev``）：JEV 同步同时替换服务快照与窗口，不像
    模型那样还有 target 引用；被引用即必须整体同步。
    """
    return [
        _impact(tree, {"jev": service_id})
        for tree in trees
        if tree.runtime.jev is not None and tree.runtime.jev.id == service_id
    ]


def _confirmation_map(confirmation) -> dict[str, str]:
    """把确认载荷归一成 ``{tree_id: document_sha256}``；结构非法时返回空映射。"""
    if not isinstance(confirmation, dict):
        return {}
    trees = confirmation.get("trees")
    if not isinstance(trees, list):
        return {}
    supplied: dict[str, str] = {}
    for item in trees:
        if not isinstance(item, dict):
            return {}
        tree_id = item.get("id")
        sha = item.get("document_sha256")
        if not isinstance(tree_id, str) or not isinstance(sha, str):
            return {}
        supplied[tree_id] = sha
    return supplied


def confirmation_mismatch(impacts, confirmation) -> str | None:
    """返回第一棵与确认不匹配的受影响画布的**当前**哈希；一致时返回 None。"""
    expected = {item["id"]: item["document_sha256"] for item in impacts}
    supplied = _confirmation_map(confirmation)
    if supplied == expected:
        return None
    for item in impacts:
        if supplied.get(item["id"]) != item["document_sha256"]:
            return item["document_sha256"]
    # 只多报/少报画布 id：仍以第一棵受影响画布的当前哈希作为 actual。
    return impacts[0]["document_sha256"]


def require_confirmation(impacts, confirmation, *, resource: dict,
                         replacements=()) -> None:
    """强制两阶段写入。

    - 无影响：直接返回（普通目录写路径）。
    - ``confirmation is None``：409 ``impact_confirmation_required`` 预览，
      携带资源标识、受影响画布清单与删除时可选的替代目录。
    - 确认与当前影响清单不一致（过期/删改）：409 ``graph_conflict``，报告
      当前画布的真实哈希，要求重新读取影响清单。
    """
    if not impacts:
        return
    if confirmation is None:
        raise ReferencedByError({
            "message": "该资源仍被画布引用，确认影响后才可写入",
            "code": "impact_confirmation_required",
            "resource": resource,
            "affected_trees": impacts,
            "replacements": list(replacements),
        })
    actual = confirmation_mismatch(impacts, confirmation)
    if actual is not None:
        raise GraphConflictError(actual)


# ── 纯运行时目录转换 ───────────────────────────────────────────────────
def merge_providers(existing, new) -> list[dict]:
    """按 name 合并 provider 目录：新条目胜出，既有顺序保留、新条目追加。"""
    merged: dict[str, dict] = {}
    for entry in existing:
        merged[entry["name"]] = copy.deepcopy(entry)
    for entry in new:
        merged[entry["name"]] = copy.deepcopy(entry)
    return list(merged.values())


def replace_runtime_catalog(runtime: GraphRuntime, *, models=None,
                            providers=None) -> GraphRuntime:
    """返回替换模型/provider 目录后的新冻结 runtime（深拷贝目录项）。"""
    current = runtime.backends
    next_providers = current.providers if providers is None else providers
    next_models = current.models if models is None else models
    return replace(runtime, backends=BackendCatalog(
        providers=tuple(copy.deepcopy(list(next_providers))),
        models=tuple(copy.deepcopy(list(next_models))),
    ))


def _provider_format(providers, provider_name: str) -> str:
    for entry in providers:
        if entry.get("name") == provider_name:
            value = entry.get("reasoning_format")
            return value if isinstance(value, str) else "openai"
    return "openai"


def reasoning_levels(model_entry, provider_format: str = "openai") -> set[str]:
    """模型目录项声明的可选推理档位 id；未声明/不支持推理时为空集合。"""
    info = normalize_reasoning((model_entry or {}).get("profile"), provider_format)
    if not isinstance(info, dict) or not info.get("declared") \
            or not info.get("supported"):
        return set()
    return {
        entry["id"] for entry in info.get("efforts") or ()
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def reasoning_default(model_entry, provider_format: str = "openai"):
    """模型目录项的默认推理档位；无声明/不支持时为 None。"""
    info = normalize_reasoning((model_entry or {}).get("profile"), provider_format)
    if not isinstance(info, dict) or not info.get("declared") \
            or not info.get("supported"):
        return None
    return info.get("default")


def replace_target_models(root, model_id: str, replacement_model, *,
                          default_reasoning, supported_reasoning):
    """把所有 target 槽位里的 ``model_id`` 替换成替代模型并按模型 id 去重。

    - 替代模型的推理档位受支持时保留槽位原档位；否则使用替代模型默认档位
      （可为 None）。槽位原本没有档位（None，即「不指定」）时继续保持 None：
      这是有意的语义——画布作者显式选择「跟随模型默认」，替换模型后也不该被
      改写成某个具体档位。
    - 每个 target 内按模型 id 去重并保留首次出现的槽位顺序。
    - 没有任何槽位引用 ``model_id`` 时返回深拷贝的 root（不修改输入）。
    """
    new_root = copy.deepcopy(root)
    referenced = any(
        item.model == model_id
        for node in new_root.nodes
        for item in getattr(node, "models", ()) or ())
    if not referenced:
        return new_root
    if not isinstance(replacement_model, dict) or "id" not in replacement_model:
        raise ValueError("replacement_model is required when a target references the model")
    replacement_id = replacement_model["id"]
    supported = set(supported_reasoning or ())
    changed = False

    def convert(node):
        nonlocal changed
        slots = getattr(node, "models", ()) or ()
        if not any(item.model == model_id for item in slots):
            return node
        changed = True
        converted = []
        seen: set[str] = set()
        for slot in slots:
            if slot.model == model_id:
                if slot.reasoning is None:
                    level = None
                elif slot.reasoning in supported:
                    level = slot.reasoning
                else:
                    level = default_reasoning
                model = replacement_id
            else:
                model = slot.model
                level = slot.reasoning
            if model in seen:
                continue
            seen.add(model)
            converted.append(type(slot)(model=model, reasoning=level))
        return replace(node, models=tuple(converted))

    nodes = tuple(convert(node) for node in new_root.nodes)
    if not changed:
        return new_root
    return replace(new_root, nodes=nodes)


def _merged_models(models, entry: dict | None, removed_id: str) -> list[dict]:
    """删除 ``removed_id`` 后按 id 合并 ``entry``（已存在则保留画布版本）。"""
    result: list[dict] = []
    present = False
    for item in models:
        if item["id"] == removed_id:
            continue
        if entry is not None and item["id"] == entry["id"]:
            present = True
        result.append(copy.deepcopy(item))
    if entry is not None and not present:
        result.append(copy.deepcopy(entry))
    return result


def sync_model(tree, model_entry: dict, providers=()):
    """用目录模型项替换/追加画布运行时模型，并合并 provider 目录。

    target 的模型引用不变；快照里其它模型/时段/JEV 保持原样。
    """
    models: list[dict] = []
    replaced = False
    for item in tree.runtime.backends.models:
        if item["id"] == model_entry["id"]:
            models.append(copy.deepcopy(model_entry))
            replaced = True
        else:
            models.append(copy.deepcopy(item))
    if not replaced:
        models.append(copy.deepcopy(model_entry))
    runtime = replace_runtime_catalog(
        tree.runtime, models=models,
        providers=merge_providers(tree.runtime.backends.providers, providers))
    return replace(tree, runtime=runtime)


def sync_provider(tree, provider_entry: dict):
    """用目录 provider 项替换/追加画布运行时 provider 目录。"""
    providers: list[dict] = []
    replaced = False
    for item in tree.runtime.backends.providers:
        if item["name"] == provider_entry["name"]:
            providers.append(copy.deepcopy(provider_entry))
            replaced = True
        else:
            providers.append(copy.deepcopy(item))
    if not replaced:
        providers.append(copy.deepcopy(provider_entry))
    runtime = replace_runtime_catalog(tree.runtime, providers=providers)
    return replace(tree, runtime=runtime)


def sync_jev(tree, service: JevService):
    """用目录 JEV 服务整体替换画布运行时 JEV 与窗口（同一服务、原子替换）。

    只替换 ``runtime.jev`` 与两个窗口计数；backends / 时段 / graph 结构与
    ``document_sha256``（提交基线哈希）保持原样。``service`` 深拷贝后写入，
    调用方的对象不会被候选画布共享或修改。
    """
    runtime = replace(
        tree.runtime,
        jev=copy.deepcopy(service),
        jev_input_tokens=service.input_tokens,
        jev_context_tokens=service.context_tokens,
    )
    return replace(tree, runtime=runtime)


def delete_jev(tree, service_id: str, *, replacement: JevService | None):
    """删除画布引用的 JEV 服务：必须用 ``replacement`` 整体替换，不能置空。

    - 画布未引用该服务 → 原样返回（不产生任何变化）。
    - 引用该服务且 ``replacement is None`` → ``ValueError``（调用方先做 422）。
    - 否则等价于 ``sync_jev(tree, replacement)``：服务快照与窗口一起换掉，避免
      留下「无决策服务」的问题节点。
    """
    if tree.runtime.jev is None or tree.runtime.jev.id != service_id:
        return tree
    if replacement is None:
        raise ValueError("replacement_jev_id is required when a canvas references the JEV service")
    return sync_jev(tree, replacement)


def delete_model(tree, model_id: str, *, replacement=None, providers=()):
    """从画布运行时目录删除模型；target 引用时替换为 ``replacement``。

    - target 引用且 ``replacement is None`` → ``ValueError``（调用方先做 422）。
    - 替代模型及其 provider 在目录缺失时补入；已存在时保留画布版本。
    - 推理档位：替代模型支持则保留，否则落到替代模型默认档位（可为 None）。
    """
    target_referenced = any(
        item.model == model_id
        for node in tree.root.nodes
        for item in getattr(node, "models", ()) or ())
    if target_referenced and replacement is None:
        raise ValueError("replacement is required when targets reference the model")

    merged_providers = merge_providers(tree.runtime.backends.providers, providers)
    existing = next(
        (item for item in tree.runtime.backends.models
         if replacement is not None and item["id"] == replacement["id"]), None)
    replacement_entry = existing if existing is not None else replacement
    provider_format = "openai"
    if replacement_entry is not None:
        provider_format = _provider_format(
            merged_providers, replacement_entry.get("provider"))

    root = replace_target_models(
        tree.root, model_id,
        replacement_entry if target_referenced else None,
        default_reasoning=reasoning_default(replacement_entry, provider_format),
        supported_reasoning=reasoning_levels(replacement_entry, provider_format),
    )
    runtime = replace_runtime_catalog(
        tree.runtime,
        models=_merged_models(
            tree.runtime.backends.models,
            replacement_entry if target_referenced else None,
            model_id),
        providers=merged_providers,
    )
    return replace(tree, root=root, runtime=runtime)
