from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace

from router.routing.model import (
    RoutingTree,
    canonical_json,
    tree_from_dict,
    tree_revision_of,
    tree_storage_dict,
)
from router.store.repositories import _now_iso, _tx, record_revision, write_transaction

REVISION_TREE = "tree"


def _canonical_tree(tree: RoutingTree) -> str:
    return canonical_json(tree_storage_dict(tree))


def _row_to_tree(row: sqlite3.Row) -> RoutingTree:
    document = json.loads(row["root"])
    return tree_from_dict({
        **document,
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "tree_revision": row["tree_revision"],
        "updated_at": row["updated_at"],
    })


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    if conn.in_transaction:
        yield
        return
    with write_transaction(conn):
        yield


class RoutingTreeRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, tree_id: str) -> RoutingTree | None:
        row = self.conn.execute("SELECT * FROM routing_trees WHERE id = ?", (tree_id,)).fetchone()
        return _row_to_tree(row) if row else None

    def list(self) -> list[RoutingTree]:
        return [_row_to_tree(row) for row in self.conn.execute(
            "SELECT * FROM routing_trees ORDER BY updated_at, id"
        )]

    def create(self, tree: RoutingTree) -> RoutingTree:
        content = _canonical_tree(tree)
        revision = tree_revision_of(tree)
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO routing_trees (id, name, enabled, root, tree_revision, updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (tree.id, tree.name, content, revision, _now_iso()),
            )
            record_revision(self.conn, REVISION_TREE, content)
        return self._require(tree.id)

    def update(self, tree: RoutingTree) -> RoutingTree:
        content = _canonical_tree(tree)
        revision = tree_revision_of(tree)
        with _tx(self.conn):
            cursor = self.conn.execute(
                "UPDATE routing_trees SET name = ?, root = ?, tree_revision = ?, updated_at = ? WHERE id = ?",
                (tree.name, content, revision, _now_iso(), tree.id),
            )
            if cursor.rowcount == 0:
                raise KeyError(tree.id)
            record_revision(self.conn, REVISION_TREE, content)
        return self._require(tree.id)

    def copy(self, source_id: str, new_id: str, new_name: str) -> RoutingTree:
        source = self.get(source_id)
        if source is None:
            raise KeyError(source_id)
        return self.create(replace(source, id=new_id, name=new_name, enabled=False, updated_at=""))

    def enable(self, tree_id: str) -> RoutingTree:
        with _write_tx(self.conn):
            exists = self.conn.execute("SELECT 1 FROM routing_trees WHERE id = ?", (tree_id,)).fetchone()
            if exists is None:
                raise KeyError(tree_id)
            self.conn.execute("UPDATE routing_trees SET enabled = 0 WHERE enabled = 1 AND id != ?", (tree_id,))
            self.conn.execute("UPDATE routing_trees SET enabled = 1 WHERE id = ?", (tree_id,))
        return self._require(tree_id)

    def disable(self, tree_id: str) -> bool:
        with _tx(self.conn):
            exists = self.conn.execute("SELECT 1 FROM routing_trees WHERE id = ?", (tree_id,)).fetchone()
            if exists is None:
                return False
            self.conn.execute("UPDATE routing_trees SET enabled = 0 WHERE id = ?", (tree_id,))
        return True

    def _require(self, tree_id: str) -> RoutingTree:
        tree = self.get(tree_id)
        if tree is None:
            raise KeyError(tree_id)
        return tree
