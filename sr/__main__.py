"""``sr`` 命令行入口（``python -m sr``）：spec §12 启动流程 + 实施计划 Task 11
Produces 接口。argparse 五个子命令：

- ``sr init --db PATH [--import BOOTSTRAP.YAML]``
      建库（sr.db.init_db：11 表 + 索引 + 默认 settings + balanced 种子树，幂等）；
      ``--import`` 走 sr/config.import_yaml（normalize 校验 + 体检 + 单事务写入，
      控制者裁定 1 同源）。
- ``sr serve --db PATH [--port 8899] [--host HOST] [--checkpoint DIR]
        [--device DEV] [--batch-window-ms MS] [--console FILE]``
      §12 启动序列：显式 ``--checkpoint`` → 按 §2.3 校验 router_config.json /
      model.pt / tags.json（与 sr.tags 逐项断言：名称/类型/标签数/标签顺序），
      通过则注入 ``CheckpointTagger``（torch 前向 + 嵌入头，name="checkpoint"）；
      **校验失败拒绝启动**（§2.5/§15，不静默降级）。未指定 → heuristic 模式
      （注入 ``HeuristicTagger``；A2 打标器是注入的 sr.tagger.Tagger 协议对象）。
      ``--device`` 选 cuda/cpu（缺省自动），``--batch-window-ms`` 是 §4.3 微批窗口。
      ``--console`` 换 GET / 托管的控制台文件（Task 12，缺省仓库根
      console-v7.html）。对未 init 的库先幂等 init（快速开始 ``sr serve`` 一步可达）。
- ``sr fake-upstream [--port 8123] [--host HOST]``
      转调 sr.fake_upstream.run（Task 7/8 的故障注入假上游，§14.4 e2e 底座）。
- ``sr export --db PATH``
      四节 YAML 打到 stdout（转调 sr/config.export_yaml）。
- ``sr import --db PATH FILE``
      YAML 包校验 + 写入（转调 sr/config.import_yaml，actor="cli"）。
- ``sr eval --db PATH --replay-last 7d | --labels FILE.jsonl [--checkpoint DIR]``
      评测（spec §11.4）：历史回放（近 N 天入库标签跑当前树）或标注集（经打标器
      打标）。报告打到 stdout 并落 changes(tbl='eval')。

退出码：``0`` 成功；``1`` import 包被体检 hard findings 拒绝（findings JSON 打到
stdout，整包未写）；``2`` 输入非法（ValueError：YAML/结构/引用，消息在 stderr）
或 ``--checkpoint`` 目录不存在；argparse 用法错误亦为 2。``eval`` 属 M3（实施
计划 §14 文件地图有位，本任务不做）。
"""

from __future__ import annotations

import argparse
import asyncio
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
                           "fake-upstream,export,import,eval}")

    q = sub.add_parser("init", help="建库（幂等）+ 可选导入 bootstrap.yaml")
    q.add_argument("--db", required=True, help="SQLite 路径")
    q.add_argument("--import", dest="import_file", metavar="BOOTSTRAP.YAML",
                   help="导入四节 YAML 配置包（校验+写入，同 sr import）")

    q = sub.add_parser("serve", help="启动路由进程（--checkpoint → checkpoint 模式）")
    q.add_argument("--db", required=True, help="SQLite 路径")
    q.add_argument("--port", type=int, default=8899, help="监听端口（默认 8899）")
    q.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    q.add_argument("--checkpoint", default=None, metavar="DIR",
                   help="检查点目录（显式给定 → 校验 router_config.json/model.pt/"
                        "tags.json，通过则 checkpoint 模式，失败拒绝启动）")
    q.add_argument("--device", default=None, metavar="DEV",
                   help="checkpoint 模式设备（cuda / cpu / cuda:1；缺省自动）")
    q.add_argument("--batch-window-ms", type=int, default=0, metavar="MS",
                   help="checkpoint 微批窗口（§4.3；0 = 不批，直接前向）")
    q.add_argument("--console", default=None, metavar="FILE",
                   help="GET / 托管的控制台 HTML（默认仓库根 console-v7.html）")

    q = sub.add_parser("fake-upstream", help="故障注入假上游（§14.4 e2e 底座）")
    q.add_argument("--port", type=int, default=8123, help="监听端口（默认 8123）")
    q.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")

    q = sub.add_parser("export", help="导出四节 YAML 到 stdout")
    q.add_argument("--db", required=True, help="SQLite 路径")

    q = sub.add_parser("eval", help="评测：历史回放 / 标注集（spec §11.4）")
    q.add_argument("--db", required=True, help="SQLite 路径")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--replay-last", metavar="ND",
                   help="历史回放：近 N 天入库标签跑当前树（如 7d）")
    g.add_argument("--labels", metavar="FILE",
                   help="标注集 jsonl（{text, expect_model|expect_branch}）")
    q.add_argument("--checkpoint", default=None, metavar="DIR",
                   help="标注集评测用的 checkpoint 打标器（缺省 heuristic）")
    q.add_argument("--device", default=None, metavar="DEV",
                   help="checkpoint 打标器设备（cuda / cpu，缺省自动）")
    q.add_argument("--batch-window-ms", type=int, default=0, metavar="MS",
                   help="checkpoint 微批窗口（§4.3；0 = 不批）")

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


def _build_tagger(args):
    """按 ``--checkpoint`` 决定打标器：显式给定 → 校验 + CheckpointTagger（§2.3/§2.5）；
    未指定 → HeuristicTagger（heuristic 模式，§2.5）。校验失败抛 :class:`CheckpointError`。"""
    if args.checkpoint is None:
        from sr.tagger import HeuristicTagger
        return HeuristicTagger()
    from sr.checkpoint import CheckpointTagger
    return CheckpointTagger(args.checkpoint, device=args.device,
                            batch_window_ms=args.batch_window_ms)


def _serve(args) -> int:
    """§12 serve 启动序列：--checkpoint 显式给定 → 内容校验（§2.3），通过建
    CheckpointTagger，失败拒绝启动（§2.5/§15「校验失败不静默降级」）。"""
    if args.checkpoint is not None:
        # 显式指定即用户认为它有效：目录/文件/标签清单任一不符 → 拒绝启动
        from sr.checkpoint import CheckpointError
        try:
            tagger = _build_tagger(args)
        except CheckpointError as exc:
            print(f"sr serve: --checkpoint 校验失败，拒绝启动"
                  f"（不静默降级，spec §2.5/§15）: {exc}", file=sys.stderr,
                  flush=True)
            return 2
        except ImportError as exc:              # torch / transformers 未安装
            print(f"sr serve: checkpoint 模式需要 torch / transformers，"
                  f"当前环境缺失，拒绝启动: {exc}", file=sys.stderr, flush=True)
            return 2
        print(f"sr serve: checkpoint 模式（{tagger.device}，"
              f"max_length={tagger.max_length}）", file=sys.stderr, flush=True)
    else:
        tagger = _build_tagger(args)
    conn = db.init_db(args.db)             # 幂等：未 init 的库直接 serve 也可用
    conn.close()
    import uvicorn
    from sr.server import create_app
    uvicorn.run(create_app(args.db, tagger, console_path=args.console),
                host=args.host, port=args.port)
    return 0


def _do_eval(args) -> int:
    """评测入口（spec §11.4）：历史回放 / 标注集 → 报告打到 stdout，落 changes。

    退出码同约定：0 成功；2 输入非法（库未初始化 / 标注集不可读 / YAML 行坏）。
    """
    if not db.is_initialized(args.db):
        print(f"sr eval: 库未初始化，请先 `sr init --db {args.db}`", file=sys.stderr)
        return 2
    import sr.eval as eval_mod
    conn = db.get(args.db)
    try:
        if args.replay_last is not None:
            raw = str(args.replay_last).strip().rstrip("dD")
            try:
                days = int(raw or 7)
            except ValueError:
                print(f"sr eval: --replay-last 需形如 7d：{args.replay_last!r}",
                      file=sys.stderr)
                return 2
            report = eval_mod.replay_eval(conn, args.db, days=days)
        else:
            try:
                lines = Path(args.labels).read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                print(f"sr eval: cannot read {args.labels}: {exc}", file=sys.stderr)
                return 2
            records = []
            for ln in lines:
                if ln.strip():
                    try:
                        records.append(json.loads(ln))
                    except ValueError as exc:
                        print(f"sr eval: bad jsonl line: {exc}", file=sys.stderr)
                        return 2
            try:
                tagger = _build_tagger(args)
            except Exception as exc:               # noqa: BLE001（checkpoint 校验等）
                print(f"sr eval: tagger 构建失败：{exc}", file=sys.stderr)
                return 2
            report = asyncio.run(
                eval_mod.labels_eval(conn, args.db, records, tagger))
        eval_mod.log_eval(conn, report, actor="cli")
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
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
    if args.cmd == "eval":
        return _do_eval(args)
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
