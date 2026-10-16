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
半开探测（M3）：连续失败达阈值后经 HALF_OPEN_AFTER_S 放行一次探测（见
:func:`is_circuit_open`）；人工复位 :func:`reset_circuit`（M6）。
"""

from __future__ import annotations

import os
import threading
import time
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

# 连续失败计数（内存 dict；spec §8.3 逐字，模块私有加下划线）。
# 半开探测（M3）：连续失败达阈值后置「打开时刻」，超过 HALF_OPEN_AFTER_S 允许一次
# 探测（is_circuit_open 回 False）；成功/复位清计数与时刻。多线程（uvicorn 线程 /
# 测试线程）共享，读写一律持锁。
_FAIL_COUNTS: dict[str, int] = {}
_FAIL_LOCK = threading.Lock()
_CIRCUIT_OPENED_AT: dict[str, float] = {}

# 半开探测窗口（spec §8.3 末注「5 分钟后发一次探测请求」）
HALF_OPEN_AFTER_S = 300.0


def is_circuit_open(model_id: str, conn=None) -> bool:
    """熔断是否打开（spec §8.3 + A4/M3-10 跨 worker 一致）。

    两级判定：

    1. **本进程**有完整状态（连续失败计数 ≥3）：未过半开窗口 → 打开；过了窗口 →
       放行一次探测（返回 False）。半开语义与 M3 一致。
    2. 本进程**没有**状态时查 DB ``models.ok == 0``（另一 worker 打开的熔断，或本
       进程重启后 DB 里仍记着隔离）：打开，并在本进程 latch 出「计数 3 + 打开时刻
       = 现在」，使半开窗口在该 worker 同样生效——否则它会永久跳过这个模型，只能
       靠人工 reset（M3 半开的本意是自愈）。DB 里没有打开时刻，故窗口从**本进程
       首次观察到**起算（偏保守，不误放）。``conn`` 省略（None）时退化为纯进程内
       判定，供单测与无库调用。
    """
    with _FAIL_LOCK:
        if _FAIL_COUNTS.get(model_id, 0) >= 3:
            opened = _CIRCUIT_OPENED_AT.get(model_id, 0.0)
            if (time.monotonic() - opened) < HALF_OPEN_AFTER_S:
                return True
            return False                  # 半开：放行本次探测
    if conn is not None:
        row = conn.execute("SELECT ok FROM models WHERE id = ?",
                           (model_id,)).fetchone()
        if row is not None and row["ok"] == 0:
            with _FAIL_LOCK:
                if model_id not in _FAIL_COUNTS:      # 首次观察到 → latch 窗口起点
                    _FAIL_COUNTS[model_id] = 3
                    _CIRCUIT_OPENED_AT[model_id] = time.monotonic()
            return True
    return False


def record_failure(conn, model_id: str) -> None:
    """失败计数 +1；达 3 次写库 ok=0 + break_reason（spec §8.3 逐字）+ 记打开时刻。

    打开熔断时**立即提交**该 UPDATE（A4/M3-10）：其它 worker 靠 DB 的 ok=0 得知
    隔离状态，若等调用方后续提交，跨 worker 会有窗口期内继续打到坏模型。
    """
    with _FAIL_LOCK:
        _FAIL_COUNTS[model_id] = _FAIL_COUNTS.get(model_id, 0) + 1
        if _FAIL_COUNTS[model_id] >= 3:
            _CIRCUIT_OPENED_AT[model_id] = time.monotonic()
    if _FAIL_COUNTS[model_id] >= 3:
        conn.execute("UPDATE models SET ok = 0, break_reason = ? WHERE id = ?",
                     ("连续失败 · 自动熔断", model_id))
        conn.commit()


def record_success(conn, model_id: str) -> None:
    """成功清零计数并恢复 ok=1（spec §8.3 逐字）。"""
    with _FAIL_LOCK:
        _FAIL_COUNTS.pop(model_id, None)
        _CIRCUIT_OPENED_AT.pop(model_id, None)
    conn.execute("UPDATE models SET ok = 1, break_reason = NULL WHERE id = ?",
                 (model_id,))


def reset_circuit(conn, model_id: str) -> None:
    """人工复位（M6，audit Major）：清内存连续失败计数 + 恢复 ok=1 /
    break_reason=NULL。半开探测定时器见 :func:`is_circuit_open`（M3）。"""
    with _FAIL_LOCK:
        _FAIL_COUNTS.pop(model_id, None)
        _CIRCUIT_OPENED_AT.pop(model_id, None)
    conn.execute("UPDATE models SET ok = 1, break_reason = NULL WHERE id = ?",
                 (model_id,))


def clear_circuit_state() -> None:
    """清空全部熔断内存状态（测试隔离用；生产不调用）。"""
    with _FAIL_LOCK:
        _FAIL_COUNTS.clear()
        _CIRCUIT_OPENED_AT.clear()
