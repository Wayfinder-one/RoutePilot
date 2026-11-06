from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from router.control.validation import build_enable_context
from router.routing.graph.validate import validate_graph_for_enable, validate_graph_structure
from router.routing.tree import tree_from_dict, tree_revision_of, tree_storage_dict
from router.store.routing_trees import RoutingTreeRepo


def _reject_issues(tree, issues) -> None:
    if issues:
        codes = ",".join(sorted({issue.code for issue in issues}))
        raise ValueError(f"invalid graph {tree.id}: {codes}")


def export_sqlite_graphs(db_path: str, out_dir: str) -> dict:
    output = Path(out_dir)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM routing_trees ORDER BY id").fetchall()
        trees = []
        for row in rows:
            document = json.loads(row["root"])
            tree = tree_from_dict({
                **document,
                "id": row["id"], "name": row["name"],
                "enabled": bool(row["enabled"]),
                "tree_revision": row["tree_revision"],
                "updated_at": row["updated_at"],
            })
            _reject_issues(tree, validate_graph_structure(tree))
            trees.append(tree)

        active_ids = [tree.id for tree in trees if tree.enabled]
        if len(active_ids) > 1:
            raise ValueError("legacy database has multiple active graphs")
        if active_ids:
            active_tree = next(tree for tree in trees if tree.id == active_ids[0])
            _reject_issues(
                active_tree,
                validate_graph_for_enable(active_tree, build_enable_context(conn)),
            )

        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.export-", dir=output.parent))
        try:
            repo = RoutingTreeRepo(staging)
            repo.initialize()
            for tree in trees:
                repo.create(tree)
            expected_active = active_ids[0] if active_ids else None
            if expected_active is not None:
                repo.enable(expected_active)
            if repo.active_id() != expected_active:
                raise ValueError("exported active graph pointer differs from the legacy database")

            loaded = {tree.id: tree for tree in repo.list()}
            expected_ids = {tree.id for tree in trees}
            if set(loaded) != expected_ids:
                raise ValueError("exported graph ids do not match the legacy database")
            for tree in trees:
                exported = loaded[tree.id]
                _reject_issues(exported, validate_graph_structure(exported))
                if tree_storage_dict(exported) != tree_storage_dict(tree):
                    raise ValueError(f"exported graph differs: {tree.id}")
                if exported.tree_revision != tree_revision_of(tree):
                    raise ValueError(f"exported graph revision differs: {tree.id}")
            if expected_active is not None:
                _reject_issues(
                    loaded[expected_active],
                    validate_graph_for_enable(loaded[expected_active], build_enable_context(conn)),
                )
            os.replace(staging, output)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {"tree_ids": sorted(loaded), "active_id": expected_active}
    finally:
        conn.close()
