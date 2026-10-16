"""settings 表读写与值校验（spec §3.1 settings 行 / §9.2 PUT /admin/settings）。

A1（audit §3）：自 sr/server.py 拆出（原「1) 小工具」段的设置部分）。消费点对值
形态有硬要求——engine 用 int(session_idle)、server 用 int(max_retries)/
int(batch_window_ms)、budget_water/budget_daily_limit_usd 直接进预算比较、
ladder/budget_route 是模型 id 引用。旧实现只校验键集，一次 PUT 200 写入 "abc"
就会让**每个** /v1 请求 500 且持久不复原。

依赖方向：本模块 → sr.db（取 DEFAULT_SETTINGS 键集）；**不** import sr.server——
这正是 A1 要消掉的反向依赖：config.import_yaml 现在直接 import 本模块，
不再 `import sr.server`（config.py 顶部注释同步更新）。
"""

from __future__ import annotations

import re

import sr.db as db


def _load_settings(conn) -> dict:
    """settings 表 → ``{key: value}``（值一律字符串，§3.1）。"""
    return {r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM settings")}


def _settings_positive_int(v) -> bool:
    """正整数（字符串数字形态；bool 不算——它在 Python 里是 int 的子类）。"""
    return re.fullmatch(r"[1-9][0-9]*", str(v).strip()) is not None


def _settings_flag(v) -> bool:
    """0/1 开关（字符串数字形态）。"""
    return str(v).strip() in ("0", "1")


def _settings_water(v) -> bool:
    """预算水位：浮点且 ∈ (0,1)（budget_protect 触发线，开区间与 bands 切点同口径）。"""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return False
    return 0.0 < f < 1.0


def _settings_positive_float(v) -> bool:
    """正浮点（字符串数字形态；bool 不算——同 _settings_positive_int 口径）。"""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return False
    return f > 0.0


def validate_settings(settings, models) -> list[str]:
    """按 key 白名单校验设置值 → 非法消息列表（空列表 = 合法）。

    :param settings: ``{key: value}``（值可为字符串或 YAML 原生标量）。
    :param models: 可用模型 id 集合（ladder / budget_route 的引用校验）。
    :returns: 逐条可读错误（定位到 key 与当前值）；不抛异常——调用方决定 400
              还是 ValueError（I2 要求 /admin/settings 与 config.import_yaml 共用）。
    """
    if not isinstance(settings, dict):
        return ["settings must be a mapping"]
    errs: list[str] = []
    want, got = set(db.DEFAULT_SETTINGS), set(settings)
    if got != want:
        return [f"settings key set must equal DEFAULT_SETTINGS; "
                f"unknown: {sorted(got - want)}, missing: {sorted(want - got)}"]
    mids = {str(m) for m in (models or ())}
    for key, val in settings.items():
        if key in ("session_idle", "max_retries", "batch_window_ms",
                   "retention_days", "tree_version"):
            if not _settings_positive_int(val):
                errs.append(f"{key} must be a positive integer: {val!r}")
        elif key == "budget_water":
            if not _settings_water(val):
                errs.append(f"budget_water must be a float in (0,1): {val!r}")
        elif key == "budget_daily_limit_usd":
            if not _settings_positive_float(val):
                errs.append(f"budget_daily_limit_usd must be a positive float: "
                            f"{val!r}")
        elif key in ("session_sticky", "sticky_detect_feedback", "budget_protect"):
            if not _settings_flag(val):
                errs.append(f"{key} must be 0 or 1: {val!r}")
        elif key == "ladder":
            parts = [p.strip() for p in str(val).split(",") if p.strip()]
            if not parts:
                errs.append(f"ladder must be a non-empty comma-separated "
                            f"model list: {val!r}")
            for m in parts:
                if m not in mids:
                    errs.append(f"ladder references unknown model: {m!r}")
        elif key == "budget_route":
            if str(val).strip() not in mids:
                errs.append(f"budget_route references unknown model: {val!r}")
    return errs
