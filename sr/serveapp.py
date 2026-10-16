"""uvicorn 工厂入口 —— 多 worker 部署（A4/M3-10）。

uvicorn 的 ``workers=N``（N>1）要求传 **import 字符串**而非 app 对象，而工厂函数
不能带参数——因此配置经环境变量传入。``sr serve --workers N``（N>1）设置这些变量后
调用 ``uvicorn.run("sr.serveapp:build_app", factory=True, workers=N, ...)``。

环境变量（由 ``sr serve`` 写入；手工起多 worker 时自行设置）：

- ``SR_DB``（必填）：SQLite 路径；
- ``SR_CONSOLE``：``GET /`` 托管的控制台 HTML（缺省仓库根 console-v7.html）；
- ``SR_CHECKPOINT``：检查点目录（给定时用 checkpoint 打标器）；
- ``SR_DEVICE`` / ``SR_BATCH_WINDOW_MS``：checkpoint 模式参数。

**checkpoint 模式禁止多 worker**：每个 worker 都会各自加载一份模型（显存成倍），
拒绝逻辑在 ``sr/__main__.py`` 的 ``_serve``（rc=2，不静默降级）。

指标跨 worker 聚合见 ``sr/metrics.py``（``metric_counters`` 表）；熔断跨 worker
一致性见 ``sr/models.is_circuit_open``。
"""

from __future__ import annotations

import os


def build_app():
    """按环境变量构造 FastAPI 应用（uvicorn 工厂入口，每 worker 调一次）。"""
    db_path = os.environ.get("SR_DB")
    if not db_path:
        raise RuntimeError(
            "SR_DB 未设置：多 worker 模式经环境变量传配置（sr serve --workers 会自动设置）")

    ckpt = os.environ.get("SR_CHECKPOINT") or None
    if ckpt:
        from sr.checkpoint import CheckpointTagger
        tagger = CheckpointTagger(
            ckpt,
            device=os.environ.get("SR_DEVICE") or None,
            batch_window_ms=int(os.environ.get("SR_BATCH_WINDOW_MS") or 0),
        )
    else:
        from sr.tagger import HeuristicTagger
        tagger = HeuristicTagger()

    import sr.db as db
    db.init_db(db_path).close()          # 幂等（每个 worker 都会走到）

    from sr.server import create_app
    return create_app(db_path, tagger,
                      console_path=os.environ.get("SR_CONSOLE") or None)
