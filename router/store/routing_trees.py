"""Routing tree repository (migration v19): one embedded routing tree per row.

The unified routing tree (design doc §7) replaces the four independent station
trees: a row holds the canonical JSON of one finite tree plus its
content-addressed ``tree_revision``. This repository owns the two invariants
the rest of the system relies on:

* **At most one enabled tree.** :meth:`RoutingTreeRepo.enable` closes every
  other enabled row and enables the target inside ONE ``BEGIN IMMEDIATE``
  transaction, so two connections racing to enable different trees cannot both
  win; the v19 partial unique index is the last line of defence (§7.1/§12.6).
  All trees closed is a legal state.
* **Append-only content history.** Every content write appends the canonical
  tree JSON to ``config_revisions`` under ``kind='tree'`` keyed by sha256;
  re-saving identical content keeps the original row (``UNIQUE(kind, sha256)``
  via :func:`record_revision`), so a later replay can re-hash the exact content
  a request was routed with (§8.2).

Metadata rules from the brief: ``create``/``copy`` start closed, the generic
``update`` never writes ``enabled`` (enable/disable are separate operations),
``copy`` does not inherit enabled/updated_at, and ``tree_revision`` covers tree
content only — never name, enabled or updated_at.

Transaction convention: writes join an outer transaction when one exists
(``_tx``/``write_transaction`` composition) and otherwise own theirs; a nested
``BEGIN`` would fail and ``with self.conn:`` would commit a composer's
transaction early.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from router.pipeline.tree import (Node, RoutingTree, node_from_dict,
                                  node_to_dict, tree_revision_of)
from router.store.repositories import (_now_iso, _tx, record_revision,
                                       write_transaction)

#: ``config_revisions.kind`` of a routing tree document (v19, design §8.2).
REVISION_TREE = "tree"


def _canonical_tree(root: Node) -> str:
    """Canonical tree JSON stored in ``routing_trees.root`` and revisions.

    Byte-identical to the text :func:`tree_revision_of` hashes, so stored
    rows keep the offline-verifiable invariant ``sha256(content) == revision``.
    """
    return json.dumps(node_to_dict(root), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _row_to_tree(row: sqlite3.Row) -> RoutingTree:
    return RoutingTree(
        id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
        root=node_from_dict(json.loads(row["root"])),
        tree_revision=row["tree_revision"], updated_at=row["updated_at"])


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE for the single-active guard; join an outer transaction.

    ``enable`` must hold the write lock across "close the others + enable the
    target". A composer that already owns a transaction (e.g. a publisher
    batching a config write) must not be broken by a nested BEGIN, so there we
    simply join that outer transaction.
    """
    if conn.in_transaction:
        yield
        return
    with write_transaction(conn):
        yield


class RoutingTreeRepo:
    """Read/write access to ``routing_trees`` (v19)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, tree_id: str) -> RoutingTree | None:
        row = self.conn.execute(
            "SELECT * FROM routing_trees WHERE id = ?", (tree_id,)).fetchone()
        return _row_to_tree(row) if row else None

    def list(self) -> list[RoutingTree]:
        """All trees in stable ``updated_at, id`` order (the v19 list index)."""
        return [_row_to_tree(row) for row in self.conn.execute(
            "SELECT * FROM routing_trees ORDER BY updated_at, id")]

    # ── content writes (never touch enabled) ─────────────────────────────
    def create(self, tree_id: str, name: str, root: Node) -> RoutingTree:
        """Insert a CLOSED tree; a duplicate ``tree_id`` raises IntegrityError."""
        content = _canonical_tree(root)
        revision = tree_revision_of(root)
        with _tx(self.conn):                 # one txn: row + appended revision
            self.conn.execute(
                "INSERT INTO routing_trees "
                "(id, name, enabled, root, tree_revision, updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (tree_id, name, content, revision, _now_iso()))
            record_revision(self.conn, REVISION_TREE, content)
        return self._require(tree_id)

    def update(self, tree_id: str, name: str, root: Node) -> RoutingTree:
        """Replace name + content; ``enabled`` is deliberately not in the SQL."""
        content = _canonical_tree(root)
        revision = tree_revision_of(root)
        with _tx(self.conn):
            cur = self.conn.execute(
                "UPDATE routing_trees SET name = ?, root = ?, "
                "tree_revision = ?, updated_at = ? WHERE id = ?",
                (name, content, revision, _now_iso(), tree_id))
            if cur.rowcount == 0:
                raise KeyError(tree_id)
            record_revision(self.conn, REVISION_TREE, content)
        return self._require(tree_id)

    def copy(self, source_id: str, new_id: str, new_name: str) -> RoutingTree:
        """Copy one tree's content into a NEW closed tree (fresh updated_at)."""
        source = self.get(source_id)
        if source is None:
            raise KeyError(source_id)
        content = _canonical_tree(source.root)
        revision = tree_revision_of(source.root)   # re-derive: never trust the row
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO routing_trees "
                "(id, name, enabled, root, tree_revision, updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (new_id, new_name, content, revision, _now_iso()))
            record_revision(self.conn, REVISION_TREE, content)
        return self._require(new_id)

    # ── activation ───────────────────────────────────────────────────────
    def enable(self, tree_id: str) -> RoutingTree:
        """Close every other enabled tree and enable ``tree_id`` atomically."""
        with _write_tx(self.conn):
            exists = self.conn.execute(
                "SELECT 1 FROM routing_trees WHERE id = ?", (tree_id,)).fetchone()
            if exists is None:
                raise KeyError(tree_id)
            self.conn.execute(
                "UPDATE routing_trees SET enabled = 0 "
                "WHERE enabled = 1 AND id != ?", (tree_id,))
            self.conn.execute(
                "UPDATE routing_trees SET enabled = 1 WHERE id = ?", (tree_id,))
        return self._require(tree_id)

    def disable(self, tree_id: str) -> bool:
        """Close one tree; all-disabled is legal. False = unknown tree id."""
        with _tx(self.conn):
            exists = self.conn.execute(
                "SELECT 1 FROM routing_trees WHERE id = ?", (tree_id,)).fetchone()
            if exists is None:
                return False
            self.conn.execute(
                "UPDATE routing_trees SET enabled = 0 WHERE id = ?", (tree_id,))
        return True

    # ── helpers ──────────────────────────────────────────────────────────
    def _require(self, tree_id: str) -> RoutingTree:
        tree = self.get(tree_id)
        if tree is None:                     # pragma: no cover - row just written
            raise KeyError(tree_id)
        return tree
