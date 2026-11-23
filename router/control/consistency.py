"""画布运行快照 vs 数据库目录的只读一致性检查（spec「保存、启用与检查」）。

``/admin/try`` 用本模块把「画布声明了、但数据库目录不同或缺失」的项逐字段报告为
``runtime_mismatch`` hint。它只读数据库、只遍历画布里的资源：

* 数据库存在但画布未声明的额外 provider / model / JEV 服务**不算冲突**；
* 提示不进入保存/启用校验（那里只认画布自身的问题），也不改变执行配置——
  试算/执行仍然严格使用画布快照；
* 缺失的数据库行按字段逐项报告，``database`` 为 ``None``（JSON ``null``）。

密钥纪律：比较只读取白名单里的非密钥字段；``params`` 里的值再经过一次递归脱敏
（与 ``without_runtime_secret_fields`` 相同的固定密钥字段集合）与 JSON 安全
归一化（``NaN``/``Infinity`` 等非 JSON 标量、非 JSON 类型、非字符串对象键都转成
可序列化形态），``api_key`` / ``authorization`` / ``key_present`` / ``key_source``
永远不会出现在 Issue、params、响应或日志中。``key_env`` 是环境变量**名**，不是
密钥值，允许出现。脱敏与深度收口共用同一次递归，病态深嵌套不会在脱敏副本阶段
先抛 ``RecursionError``。
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Mapping

from router.reasoning import ReasoningError, validate_model_profile
from router.routing.graph.validate import Issue
from router.routing.runtime import GraphRuntime
from router.store.jev_services import JevServiceRepo
from router.store.repositories import ModelRepo, ProviderRepo
from router.upstream.catalog import parse_profile_text

RESOURCE_KINDS = ("provider", "model", "jev")
PROVIDER_FIELDS = ("base_url", "key_env", "reasoning_format")
JEV_FIELDS = (
    "id", "base_url", "model", "key_env", "timeout_seconds",
    "instructions", "state_template",
)
JEV_WINDOW_FIELDS = ("input_tokens", "context_tokens")

#: 与 ``router.routing.runtime`` 同一份固定密钥字段集合（小写、去掉 ``-``/``_``）。
#: 只拒绝固定集合，不做「包含 key」的模糊匹配。
_SECRET_KEYS = frozenset({"apikey", "authorization", "keypresent", "keysource"})

_ABSENT = object()

logger = logging.getLogger(__name__)

#: 已记录过失败提示的检查分段（每段只告警一次，避免坏行刷日志）。
_warned_sections: set[str] = set()

#: 递归 JSON 安全化的最大深度：超过后退化为固定标记，绝不让递归把只读检查打挂。
_MAX_SAFE_DEPTH = 64


def _is_secret_name(name: str) -> bool:
    return name.casefold().replace("-", "").replace("_", "") in _SECRET_KEYS


def _secret_path(path: str) -> bool:
    return any(_is_secret_name(part) for part in path.split("."))


def _allowed_field(resource_kind: str, field: str) -> bool:
    """白名单：只有这些字段/路径允许进入 params（绝不接受密钥字段）。"""
    if field and _secret_path(field):
        return False
    if resource_kind == "provider":
        return field in PROVIDER_FIELDS
    if resource_kind == "model":
        return field in ("upstream", "provider", "profile") \
            or field.startswith("profile.")
    if resource_kind == "jev":
        return field in JEV_FIELDS or field in JEV_WINDOW_FIELDS
    return False


def _safe_value(value, depth: int = 0):
    """递归脱敏 + JSON 安全化副本：密钥键被剔除，其余值可安全进响应。

    脱敏与深度收口在**同一次**递归里完成：``without_runtime_secret_fields``
    自己会无限递归，若先把整个值脱敏成副本再做 JSON 安全化，病态深嵌套会在
    建副本时先炸 ``RecursionError``，docstring 承诺的深度上限就形同虚设。超过
    ``_MAX_SAFE_DEPTH`` 的子树直接退化为固定标记（不用 ``repr``：敌意深嵌套下
    repr 也可能递归爆栈）。
    """
    if depth > _MAX_SAFE_DEPTH:
        return "<nested too deep>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {
            key if isinstance(key, str) else str(key): _safe_value(item, depth + 1)
            for key, item in value.items()
            if not (isinstance(key, str) and _is_secret_name(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value]
    return str(value)


def mismatch(resource_kind: str, resource_id: str, field: str,
             canvas_value, database_value) -> Issue:
    """构造一条 ``runtime_mismatch`` hint；只接受白名单字段。

    唯一允许把画布/目录值放进 Issue 的入口：字段不在白名单（或路径里出现密钥
    键名）直接抛 ``ValueError``；值再过一次递归脱敏 + JSON 安全归一化，保证真实
    密钥与旧库里的非 JSON 标量（``NaN``/``Infinity``）都不会进入 params/响应/日志。
    """
    if resource_kind not in RESOURCE_KINDS:
        raise ValueError(f"unknown runtime_mismatch resource kind: {resource_kind!r}")
    if not _allowed_field(resource_kind, field):
        raise ValueError(f"runtime_mismatch field is not allowed: {field!r}")
    return Issue(
        code="runtime_mismatch",
        level="hint",
        params={
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "field": field,
            "canvas": _safe_value(canvas_value),
            "database": _safe_value(database_value),
        },
    )


def _project_profile(profile, provider_format: str) -> dict:
    """与画布 ``runtime._models`` 相同的 profile 投影（校验 + capability 规整）。

    数据库行可能是更新前写入的非法声明：校验失败时退化为原始 dict，让叶子比较
    给出真实差异，而不是让只读检查在 ``/admin/try`` 上抛错。
    """
    raw = dict(profile) if isinstance(profile, Mapping) else {}
    try:
        validated = validate_model_profile(raw, provider_format)
    except (ReasoningError, ValueError, TypeError):
        validated = raw
    capability = validated.get("capability")
    if isinstance(capability, str):
        capability = capability.strip()
        if capability:
            validated["capability"] = capability
        else:
            validated.pop("capability", None)
    elif capability is None:
        validated.pop("capability", None)
    return validated


def _walk_profile_leaves(canvas, database, path: str, out: list) -> None:
    """把两个 profile 投影按叶子字段比较，差异追加到 ``out``（``(path, c, d)``）。"""
    if _secret_path(path):
        return
    if isinstance(canvas, Mapping) and isinstance(database, Mapping):
        keys = set(canvas) | set(database)
        for key in sorted(map(str, keys)):
            if _is_secret_name(key):
                continue
            child = f"{path}.{key}" if path else key
            _walk_profile_leaves(
                canvas.get(key, _ABSENT), database.get(key, _ABSENT), child, out)
        return
    if canvas is _ABSENT and isinstance(database, Mapping):
        for key in sorted(map(str, database)):
            if _is_secret_name(key):
                continue
            _walk_profile_leaves(
                _ABSENT, database[key], f"{path}.{key}" if path else key, out)
        return
    if database is _ABSENT and isinstance(canvas, Mapping):
        for key in sorted(map(str, canvas)):
            if _is_secret_name(key):
                continue
            _walk_profile_leaves(
                canvas[key], _ABSENT, f"{path}.{key}" if path else key, out)
        return
    canvas_value = None if canvas is _ABSENT else canvas
    database_value = None if database is _ABSENT else database
    if (canvas is _ABSENT) != (database is _ABSENT) \
            or canvas_value != database_value:
        out.append((path, canvas_value, database_value))


def _provider_issues(conn: sqlite3.Connection, runtime: GraphRuntime) -> list[Issue]:
    repo = ProviderRepo(conn)
    issues: list[Issue] = []
    for provider in runtime.backends.providers:
        current = repo.get(provider["name"])
        if current is None:
            for field in PROVIDER_FIELDS:
                issues.append(mismatch(
                    "provider", provider["name"], field, provider.get(field), None))
            continue
        for field in PROVIDER_FIELDS:
            canvas_value = provider.get(field)
            database_value = current.get(field)
            if canvas_value != database_value:
                issues.append(mismatch(
                    "provider", provider["name"], field,
                    canvas_value, database_value))
    return issues


def _model_profile_issues(model: dict, current: Mapping | None,
                          provider_format: str) -> list[Issue]:
    """profile 逐叶子比较：画布投影 vs 数据库投影（同一 normalize/validate 规则）。

    两侧都用画布该模型 provider 的 ``reasoning_format``：这正是画布解析 profile 时
    使用的投影。provider 的 format 本身若不同，已由 provider 的逐字段提示单独报告。
    """
    if current is None:
        return []
    try:
        database_raw = parse_profile_text(current.get("profile"))
    except RecursionError:
        # 极端损坏的 JSON 嵌套：按空对象处理，绝不让只读检查把 /admin/try 打挂。
        database_raw = {}
    canvas_profile = _project_profile(model.get("profile"), provider_format)
    database_profile = _project_profile(database_raw, provider_format)
    diffs: list[tuple[str, object, object]] = []
    _walk_profile_leaves(canvas_profile, database_profile, "", diffs)
    return [
        mismatch("model", model["id"], "profile." + path if path else "profile",
                 canvas_value, database_value)
        for path, canvas_value, database_value in diffs
    ]


def _model_issues(conn: sqlite3.Connection, runtime: GraphRuntime) -> list[Issue]:
    repo = ModelRepo(conn)
    canvas_formats = {
        provider["name"]: provider.get("reasoning_format") or "openai"
        for provider in runtime.backends.providers
    }
    issues: list[Issue] = []
    for model in runtime.backends.models:
        current = repo.get(model["id"])
        if current is None:
            for field in ("upstream", "provider", "profile"):
                issues.append(mismatch(
                    "model", model["id"], field, model.get(field), None))
            continue
        for field in ("upstream", "provider"):
            canvas_value = model.get(field)
            database_value = current.get(field)
            if canvas_value != database_value:
                issues.append(mismatch(
                    "model", model["id"], field, canvas_value, database_value))
        # 画布 provider 一定存在（runtime 解析已校验引用），所以这里一定有格式。
        provider_format = canvas_formats.get(model.get("provider")) or "openai"
        issues.extend(_model_profile_issues(model, current, provider_format))
    return issues


def _jev_issues(conn: sqlite3.Connection, runtime: GraphRuntime) -> list[Issue]:
    service = runtime.jev
    if service is None:
        return []
    try:
        current = JevServiceRepo(conn).get(service.id)
    except ValueError:
        # 遗留库里的损坏行：只当作缺失，只读检查不能把 /admin/try 打挂。
        current = None
    canvas_snapshot = service.snapshot_dict()
    window = {
        "input_tokens": runtime.jev_input_tokens,
        "context_tokens": runtime.jev_context_tokens,
    }
    issues: list[Issue] = []
    if current is None:
        for field in JEV_FIELDS:
            issues.append(mismatch(
                "jev", service.id, field, canvas_snapshot.get(field), None))
        for field in JEV_WINDOW_FIELDS:
            issues.append(mismatch(
                "jev", service.id, field, window[field], None))
        return issues
    database_snapshot = current.snapshot_dict()
    for field in JEV_FIELDS:
        canvas_value = canvas_snapshot.get(field)
        database_value = database_snapshot.get(field)
        if canvas_value != database_value:
            issues.append(mismatch(
                "jev", service.id, field, canvas_value, database_value))
    for field in JEV_WINDOW_FIELDS:
        canvas_value = window[field]
        database_value = getattr(current, field)
        if canvas_value != database_value:
            issues.append(mismatch(
                "jev", service.id, field, canvas_value, database_value))
    return issues


def _guarded_section(name: str, check, conn: sqlite3.Connection,
                     runtime: GraphRuntime) -> list[Issue]:
    """执行一段只读比较；意外异常只跳过该段 hint，绝不把 /admin/try 打挂。

    坏行/非 JSON 值应在 ``mismatch()`` 的归一化里被处理；这里是最后一道防线：
    任何未预期错误记录一次 warning（每段仅一次）后返回空列表，执行配置仍来自画布。
    """
    try:
        return check(conn, runtime)
    except Exception:  # noqa: BLE001 - 只读提示的失败绝不能升级为接口错误
        if name not in _warned_sections:
            _warned_sections.add(name)
            logger.warning(
                "runtime catalog drift check %r failed; skipping its hints",
                name, exc_info=True)
        return []


def runtime_mismatch_issues(
        conn: sqlite3.Connection, runtime: GraphRuntime) -> list[Issue]:
    """只读比较「画布声明的资源」与数据库目录；返回 hint 级 Issue 列表。

    遍历顺序固定（provider → model → JEV，各自按画布声明顺序），同一画布/目录
    得到相同输出；数据库额外资源不参与比较，也不会产生 Issue。比较层面的任何
    意外异常都降级为「跳过该段提示」（见 ``_guarded_section``），因为本函数只做
    只读提示，promise 是绝不阻断 ``/admin/try``。
    """
    issues: list[Issue] = []
    for name, check in (
        ("provider", _provider_issues),
        ("model", _model_issues),
        ("jev", _jev_issues),
    ):
        issues.extend(_guarded_section(name, check, conn, runtime))
    return issues
