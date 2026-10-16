"""``sr`` 命令行入口（``python -m sr``）：spec §12 启动流程 + 实施计划 Task 11
Produces 接口。argparse 五个子命令：

- ``sr init --db PATH [--import BOOTSTRAP.YAML]``
      建库（sr.db.init_db：11 表 + 索引 + 默认 settings + balanced 种子树，幂等）；
      ``--import`` 走 sr/config.import_yaml（normalize 校验 + 体检 + 单事务写入，
      控制者裁定 1 同源）。
- ``sr serve --db PATH [--port 8899] [--host HOST] [--checkpoint DIR]
        [--console FILE]``
      §12 启动序列：显式 ``--checkpoint`` 时本任务只校验**目录存在性**——
      存在则打印「M2 才支持，当前以 heuristic 启动」警告并继续（不静默失败），
      不存在则拒绝启动（§15「校验失败拒绝启动，不静默降级」）；未指定 →
      heuristic。打标器注入 ``HeuristicTagger()``（A2：sr.tagger.Tagger 协议
      对象；M2 的 --checkpoint 分支加载 CheckpointTagger 后派第二实现）。
      ``--console`` 换 GET / 托管的控制台文件（Task 12，缺省仓库根
      console-v7.html）。
      对未 init 的库先幂等 init（快速开始 ``sr serve`` 一步可达）。
- ``sr fake-upstream [--port 8123] [--host HOST]``
      转调 sr.fake_upstream.run（Task 7/8 的故障注入假上游，§14.4 e2e 底座）。
- ``sr export --db PATH``
      四节 YAML 打到 stdout（转调 sr/config.export_yaml）。
- ``sr import --db PATH FILE``
      YAML 包校验 + 写入（转调 sr/config.import_yaml，actor="cli"）。

退出码：``0`` 成功；``1`` import 包被体检 hard findings 拒绝（findings JSON 打到
stdout，整包未写）；``2`` 输入非法（ValueError：YAML/结构/引用，消息在 stderr）
或 ``--checkpoint`` 目录不存在；argparse 用法错误亦为 2。``eval`` 属 M3（实施
计划 §14 文件地图有位，本任务不做）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sr.db as db
from sr import config


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sr", description="Smart Router 命令行（spec §12 启动流程；"
                               "M1：heuristic 模式闭环）")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{init,serve,"
                           "fake-upstream,export,import}")

    q = sub.add_parser("init", help="建库（幂等）+ 可选导入 bootstrap.yaml")
    q.add_argument("--db", required=True, help="SQLite 路径")
    q.add_argument("--import", dest="import_file", metavar="BOOTSTRAP.YAML",
                   help="导入四节 YAML 配置包（校验+写入，同 sr import）")

    q = sub.add_parser("serve", help="启动路由进程（无检查点 → heuristic）")
    q.add_argument("--db", required=True, help="SQLite 路径")
    q.add_argument("--port", type=int, default=8899, help="监听端口（默认 8899）")
    q.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    q.add_argument("--checkpoint", default=None, metavar="DIR",
                   help="检查点目录（M2 才支持加载；本任务只校验目录存在）")
    q.add_argument("--console", default=None, metavar="FILE",
                   help="GET / 托管的控制台 HTML（默认仓库根 console-v7.html）")

    q = sub.add_parser("fake-upstream", help="故障注入假上游（§14.4 e2e 底座）")
    q.add_argument("--port", type=int, default=8123, help="监听端口（默认 8123）")
    q.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")

    q = sub.add_parser("export", help="导出四节 YAML 到 stdout")
    q.add_argument("--db", required=True, help="SQLite 路径")

    q = sub.add_parser("import", help="导入四节 YAML 配置包（校验+写入）")
    q.add_argument("--db", required=True, help="SQLite 路径")
    q.add_argument("file", help="YAML 配置包路径")
    return p


def _do_import(conn, path: str) -> int:
    """读 YAML 文件 → config.import_yaml；按退出码约定返回（0/1/2）。

    I3（终审 Important）：结构类异常（TypeError/KeyError，如「节存在但值为 null」
    的旧行为）必须落到 rc=2 + 一行消息，**不得**让原始 traceback 逃出——
    §12 的 0/1/2 退出码契约是 CLI 的对外接口。config.import_yaml 现在已就地把
    这类输入转 ValueError，这里再兜一层防未来回归。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"sr: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        findings = config.import_yaml(conn, text, actor="cli")
    except ValueError as exc:
        print(f"sr: invalid package: {exc}", file=sys.stderr)
        return 2
    except (TypeError, KeyError, AttributeError) as exc:   # 输入形状问题 → rc=2
        print(f"sr: invalid package: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if findings is not None:
        print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))
        return 1
    return 0


def _serve(args) -> int:
    """§12 serve 启动序列（M1 口径）：checkpoint 只验存在性 + 警告可见；heuristic。"""
    if args.checkpoint is not None:
        cp = Path(args.checkpoint)
        if not cp.is_dir():
            # §15：显式 --checkpoint 但校验失败 → 拒绝启动，不静默降级
            print(f"sr serve: --checkpoint 目录不存在，拒绝启动（不静默降级，"
                  f"spec §15）: {cp}", file=sys.stderr, flush=True)
            return 2
        # §12：检查点加载是 M2；本任务显式给定时校验目录存在并警告（不静默失败）
        print(f"sr serve: --checkpoint {cp}：M2 才支持，当前以 heuristic 启动",
              file=sys.stderr, flush=True)
    conn = db.init_db(args.db)             # 幂等：未 init 的库直接 serve 也可用
    conn.close()
    import uvicorn
    from sr.server import create_app
    from sr.tagger import HeuristicTagger
    # A2：打标器是注入的协议对象（sr.tagger.Tagger）。M1 恒 HeuristicTagger；
    # M2 在 --checkpoint 分支加载 CheckpointTagger（torch 前向 + 嵌入头，
    # name="checkpoint"）替换此处注入——警告分支维持 heuristic 不变。
    uvicorn.run(create_app(args.db, HeuristicTagger(), console_path=args.console),
                host=args.host, port=args.port)
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "init":
        conn = db.init_db(args.db)
        try:
            if args.import_file:
                code = _do_import(conn, args.import_file)
                if code:
                    return code
        finally:
            conn.close()
        done = f"sr init: {args.db} 就绪"
        if args.import_file:
            done += f"（已导入 {args.import_file}）"
        print(done)
        return 0
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "fake-upstream":
        from sr import fake_upstream
        fake_upstream.run(host=args.host, port=args.port)
        return 0
    if args.cmd == "export":
        if not db.is_initialized(args.db):
            # backlog 修：未初始化库曾先静默建空文件、再在 config 层裸 traceback。
            # 这里显式拒绝（rc=2，同「输入非法」），不建文件、不打栈。
            print(f"sr export: 库未初始化，请先 `sr init --db {args.db}`"
                  f"（不静默建库）", file=sys.stderr)
            return 2
        conn = db.get(args.db)
        try:
            sys.stdout.write(config.export_yaml(conn))
        finally:
            conn.close()
        return 0
    if args.cmd == "import":
        if not db.is_initialized(args.db):
            print(f"sr import: 库未初始化，请先 `sr init --db {args.db}`"
                  f"（不静默建库）", file=sys.stderr)
            return 2
        conn = db.get(args.db)
        try:
            return _do_import(conn, args.file)
        finally:
            conn.close()
    return 2                               # required=True 保证不可达（防呆）


if __name__ == "__main__":
    sys.exit(main())
