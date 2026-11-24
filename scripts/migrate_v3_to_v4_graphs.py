#!/usr/bin/env python3
"""把 `sr-routing-graph` v3 图目录迁移为 v4（部门 + 生命周期字段）。

v4 文件契约：必须同时含 ``departments`` / ``enabled`` / ``default``，且
``default => enabled``、启用图恰好一张默认图。旧 v3 文件由 ``current`` 指针
指定唯一活动图；新代码不再读取指针，因此迁移时把原来指针指向的图写成
``enabled: true, default: true``，其余图写成关闭草稿。

用法（在部署目录上，使用新代码的 PYTHONPATH）::

    PYTHONPATH=/opt/smart-router-v3 /opt/smart-router-v3/.venv/bin/python \\
        migrate_v3_to_v4_graphs.py --graph-dir /opt/smart-router-v3/routing-graph

脚本默认从 ``<graph-dir>/current`` 读取活动图 id（可用 ``--active-id`` 覆盖），
``--dry-run`` 只打印迁移计划不写盘。迁移后用新代码的解析/启用校验逐份复核，
任何一份失败则整体不写盘（先全部校验，再统一替换）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

KIND = "sr-routing-graph"
TARGET_VERSION = 4


def _read_active_id(graph_dir: Path, override: str | None) -> str:
    if override:
        return override
    current = graph_dir / "current"
    if not current.exists():
        raise SystemExit(f"no active id: {current} is missing; pass --active-id")
    value = current.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"{current} is empty; pass --active-id")
    return value


def _plan(graphs_dir: Path, active_id: str) -> list[dict]:
    plans = []
    for path in sorted(graphs_dir.glob("*.json")):
        tree_id = path.stem
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("kind") != KIND:
            raise SystemExit(f"{path}: not a {KIND} document")
        version = raw.get("version")
        if version == TARGET_VERSION:
            plans.append({"path": path, "tree_id": tree_id, "raw": raw, "changed": False})
            continue
        if version != 3:
            raise SystemExit(f"{path}: unsupported version {version!r}")
        document = dict(raw)
        document["version"] = TARGET_VERSION
        document["departments"] = sorted(set(document.get("departments") or []))
        enabled = tree_id == active_id
        document["enabled"] = enabled
        document["default"] = enabled
        plans.append({"path": path, "tree_id": tree_id, "raw": document, "changed": True})
    enabled_ids = [item["tree_id"] for item in plans if item["raw"].get("enabled") is True]
    default_ids = [item["tree_id"] for item in plans if item["raw"].get("default") is True]
    if enabled_ids != [active_id] or default_ids != [active_id]:
        raise SystemExit(
            f"lifecycle invariant broken: enabled={enabled_ids} default={default_ids} active={active_id}")
    return plans


def _bytes(document: dict) -> bytes:
    text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return text.encode("utf-8")


def _validate(plans: list[dict]) -> None:
    """用目标版本的解析与启用校验逐份复核；任何失败都在写盘前终止。"""
    from router.control.validation import tree_issues
    from router.routing.document import parse_graph_document
    from router.routing.tree import tree_revision_of

    for item in plans:
        content = _bytes(item["raw"])
        tree = parse_graph_document(
            content,
            tree_id=item["tree_id"],
            updated_at="",
            document_sha256="",
        )
        issues = tree_issues(None, tree)
        if item["raw"].get("enabled") and issues:
            codes = ",".join(sorted({issue.code for issue in issues}))
            raise SystemExit(f"{item['path']}: enabled graph fails validation: {codes}")
        item["content"] = content
        item["tree"] = tree
        item["revision"] = tree_revision_of(tree)


def _write(plans: list[dict]) -> None:
    for item in plans:
        if not item["changed"]:
            continue
        path: Path = item["path"]
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(item["content"])
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-dir", required=True, help="routing-graph directory")
    parser.add_argument("--active-id", default=None, help="override the active tree id")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    graph_dir = Path(args.graph_dir)
    graphs_dir = graph_dir / "graphs"
    if not graphs_dir.is_dir():
        raise SystemExit(f"missing graph directory: {graphs_dir}")
    active_id = _read_active_id(graph_dir, args.active_id)
    plans = _plan(graphs_dir, active_id)
    _validate(plans)

    changed = [item for item in plans if item["changed"]]
    print(f"active={active_id} trees={len(plans)} to_migrate={len(changed)}")
    for item in plans:
        print(
            f"  {item['tree_id']}: enabled={item['raw'].get('enabled')} "
            f"default={item['raw'].get('default')} "
            f"departments={item['raw'].get('departments')} "
            f"revision={item['revision'][:12]}{'' if item['changed'] else ' (already v4)'}"
        )
    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    _write(changed)
    print(f"migrated {len(changed)} document(s) to v{TARGET_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
