"""模型注册表：供应商解析（spec §8.1）、阶梯（spec §8.2）、熔断（spec §8.3）。

模块职责：
- :class:`Backend` —— §8.1 返回形状（url/headers/proto/upstream_name）。
- :func:`resolve_backend` —— models ⋈ providers 解析出上游 URL / 认证头；
  key_env 缺失抛 ``RuntimeError(f"env {key_env} not set")``。
- :func:`ladder_up` / :func:`ladder_next` —— §8.2 逐字（阶梯从 settings.ladder
  逗号串读，spec §8.2 的 ``settings.get('ladder')`` 在本实现里经 conn 查表）。
- :func:`record_failure` / :func:`record_success` / :func:`is_circuit_open` ——
  §8.3 熔断：连续失败计数在模块级内存 dict（进程内重启即清零，符合 spec 口径），
  达 3 次写库 ``models.ok=0`` + break_reason；成功清零并恢复 ``ok=1``。

约定：写库函数不自行 commit，事务由调用方控制（约定同 sr/db.py）；
半开探测（5 分钟后发一次探测请求，成功则恢复，§8.3 末注释）属 M3 定时器，
本任务只留注释占位。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────────────────
# 1) Backend + resolve_backend —— spec §8.1 逐字（内存 get 落地为 SQL 查询）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Backend:
    """解析后的上游后端（server 层拿它直接转发，spec §8.1）。"""

    url: str                    # 完整上游 URL（base_url + 协议路径）
    headers: dict               # 认证头（openai: Authorization；anthropic: x-api-key…）
    proto: str                  # 'openai' | 'anthropic'（跟随 provider）
    upstream_name: str          # 上游模型名（发给后端的真实名，models.upstream）


def _env(key_env: str | None) -> str:
    """读 key_env 环境变量；缺失抛 RuntimeError（spec §8.1 ``env(...)`` 语义）。"""
    key = os.environ.get(key_env) if key_env else None
    if key is None:
        raise RuntimeError(f"env {key_env} not set")
    return key


def resolve_backend(conn, model_id: str) -> Backend:
    """models ⋈ providers → Backend（spec §8.1 逐字，仅把内存 get 换成 SQL 查询）。

    - §8.1 签名 ``resolve_backend(model_id)`` 的 models/providers 内存对象在本实现
      里是 conn 查询，故形参多一个 conn（Task 6 接口裁定）；查无此行等价于 §8.1
      ``models.get`` 抛 KeyError 的语义。
    - openai → ``{base_url}/chat/completions`` + Bearer；anthropic →
      ``{base_url}/v1/messages`` + x-api-key + anthropic-version（§8.1 逐字）。
    """
    model = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    if model is None:
        raise KeyError(model_id)                      # §8.1 models.get 缺行语义
    provider = conn.execute(
        "SELECT * FROM providers WHERE name = ?", (model["provider"],)).fetchone()
    if provider is None:
        raise KeyError(model["provider"])             # 外键兜底（FK 开启时不可达）
    base_url = provider["base_url"]
    key = _env(provider["key_env"])
    if provider["proto"] == "openai":
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
    elif provider["proto"] == "anthropic":
        url = f"{base_url}/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        raise ValueError(f"unknown provider proto: {provider['proto']}")
    return Backend(url=url, headers=headers, proto=provider["proto"],
                   upstream_name=model["upstream"])


# ─────────────────────────────────────────────────────────────────────────────
# 2) 阶梯 —— spec §8.2 逐字（ladder() 的 settings 读法落地为 conn 查表）
# ─────────────────────────────────────────────────────────────────────────────


def _ladder(conn) -> list[str]:
    """settings.ladder 逗号串 → 档位列表（§8.2 ``ladder()``）。

    逐字差异（记任务报告）：对逗号分段做 strip 并过滤空段——空值 / 尾逗号不产生
    空档位，settings 行缺失或空阶梯返回 []，使 §8.2 ``ladder_next`` 的
    ``lad[0] if lad else None`` 边界与 §4.4 R3「阶梯未配置 → 重新走树」成立。
    """
    row = conn.execute("SELECT value FROM settings WHERE key = 'ladder'").fetchone()
    if row is None:
        return []
    return [s.strip() for s in (row[0] or "").split(",") if s.strip()]


def ladder_up(conn, model_id: str) -> str | None:
    """阶梯里 m 后面一个；到顶 / 不在阶梯 → None（§8.2 逐字）。"""
    lad = _ladder(conn)
    i = lad.index(model_id) if model_id in lad else -1
    return lad[i+1] if 0 <= i < len(lad)-1 else None


def ladder_next(conn, model_id: str) -> str | None:
    """阶梯里跳到下一个不同模型；到顶回第一格；空阶梯 → None（§8.2 逐字）。"""
    lad = _ladder(conn)
    i = lad.index(model_id) if model_id in lad else -1
    return lad[i+1] if i >= 0 and i < len(lad)-1 else lad[0] if lad else None


# ─────────────────────────────────────────────────────────────────────────────
# 3) 熔断 —— spec §8.3 逐字（fail_counts 内存 dict + 阈值 3 + 写库 ok/break_reason）
# ─────────────────────────────────────────────────────────────────────────────

# 连续失败计数（内存 dict；spec §8.3 逐字，模块私有加下划线）
_FAIL_COUNTS: dict[str, int] = {}


def is_circuit_open(model_id: str) -> bool:
    """熔断是否打开：内存连续失败计数 ≥ 3（spec §8.3 逐字）。"""
    return _FAIL_COUNTS.get(model_id, 0) >= 3  # 连续 3 次失败


def record_failure(conn, model_id: str) -> None:
    """失败计数 +1；达 3 次写库 ok=0 + break_reason（spec §8.3 逐字）。"""
    _FAIL_COUNTS[model_id] = _FAIL_COUNTS.get(model_id, 0) + 1
    if _FAIL_COUNTS[model_id] >= 3:
        conn.execute("UPDATE models SET ok = 0, break_reason = ? WHERE id = ?",
                     ("连续失败 · 自动熔断", model_id))


def record_success(conn, model_id: str) -> None:
    """成功清零计数并恢复 ok=1（spec §8.3 逐字）。"""
    _FAIL_COUNTS.pop(model_id, None)
    conn.execute("UPDATE models SET ok = 1, break_reason = NULL WHERE id = ?",
                 (model_id,))

# 半开探测：5 分钟后发一次探测请求，成功则恢复（spec §8.3 末注释逐字）。
# 本任务（M1 Task 6）只留注释占位：需要定时器，M3 再做定时探测。
