from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from router.routing.model import tree_from_dict, tree_revision_of
from router.store.routing_trees import RoutingTreeRepo


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
            trees.append(tree)
    finally:
        conn.close()
    active_ids = [tree.id for tree in trees if tree.enabled]
    if len(active_ids) > 1:
        raise ValueError("legacy database has multiple active graphs")

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.export-", dir=output.parent))
    try:
        repo = RoutingTreeRepo(staging)
        repo.initialize()
        for tree in trees:
            repo.create(tree)
        if active_ids:
            repo.enable(active_ids[0])
        loaded = {tree.id: tree for tree in repo.list()}
        expected_ids = {tree.id for tree in trees}
        if set(loaded) != expected_ids:
            raise ValueError("exported graph ids do not match the legacy database")
        for tree in trees:
            if loaded[tree.id].root != tree.root:
                raise ValueError(f"exported graph differs: {tree.id}")
            if loaded[tree.id].tree_revision != tree_revision_of(tree):
                raise ValueError(f"exported graph revision differs: {tree.id}")
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"tree_ids": sorted(loaded), "active_id": active_ids[0] if active_ids else None}
