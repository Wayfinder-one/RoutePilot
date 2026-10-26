"""RoutingTreeRepo: unified routing tree rows + single-active invariant (v19).

The repository is the store-layer trust boundary for design §7/§8:

* create/copy produce a CLOSED tree; the generic ``update`` never touches
  ``enabled`` (enable/disable are separate operations);
* content writes append ``config_revisions(kind='tree')`` keyed by the
  canonical ``tree_revision`` (re-saving identical content is idempotent);
* ``enable`` closes every other enabled row and enables the target in the SAME
  ``BEGIN IMMEDIATE`` transaction, so two connections racing to enable
  different trees end with exactly one active tree;
* ``tree_revision`` covers tree content only — never name, enabled or
  updated_at — while candidate order is content and therefore hash-relevant.
"""

import hashlib
import json
import sqlite3
import threading

import pytest

from router.pipeline.tree import (Branch, NLChoice, NLOption, Route, When,
                                  node_to_dict, tree_revision_of)
from router.store.db import connect, migrate
from router.store.routing_trees import REVISION_TREE, RoutingTreeRepo


def simple_root(model: str = "qwen3-8b") -> Branch:
    """A small valid tree: one when-guarded route plus an explicit fallback."""
    return Branch(
        id="root",
        children=(
            Route(id="r1", model=model,
                  when=When(signal="domain", value="health")),
        ),
        otherwise=Route(id="fallback", model="qwen3-8b"))


def nl_root(first: str, second: str) -> NLChoice:
    """An nl_choice whose candidate ORDER is the only argument difference."""
    def option(key: str, model: str) -> NLOption:
        return NLOption(key=key, text="", capability_model=model,
                        target=Route(id=f"nl/options/{key}", model=model))

    return NLChoice(id="nl", instructions="pick a lane",
                    options=(option(first, "qwen3-8b"),
                             option(second, "deepseek-v3")),
                    otherwise=Route(id="nl/fallback", model="qwen3-8b"))


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    migrate(c)
    yield c
    c.close()


def active_ids(path: str) -> list[str]:
    conn = connect(path)
    try:
        return [row["id"] for row in conn.execute(
            "SELECT id FROM routing_trees WHERE enabled = 1")]
    finally:
        conn.close()


@pytest.fixture
def repo(conn):
    return RoutingTreeRepo(conn)


@pytest.fixture
def enabled_tree(repo):
    tree = repo.create("tree-a", "A", simple_root())
    repo.enable(tree.id)
    return tree.id


@pytest.fixture
def two_trees(repo):
    first = repo.create("tree-a", "A", simple_root("qwen3-8b"))
    second = repo.create("tree-b", "B", simple_root("deepseek-v3"))
    return first.id, second.id


# --------------------------------------------------------------------------- #
# create / get / list
# --------------------------------------------------------------------------- #
def test_create_defaults_closed_and_roundtrips(repo, conn):
    root = simple_root()
    tree = repo.create("t1", "First", root)

    assert (tree.id, tree.name, tree.enabled) == ("t1", "First", False)
    assert tree.tree_revision == tree_revision_of(root)
    assert tree.updated_at
    assert repo.get("t1") == tree
    assert repo.get("missing") is None
    assert [stored.id for stored in repo.list()] == ["t1"]
    raw = conn.execute("SELECT enabled, root FROM routing_trees WHERE id='t1'"
                       ).fetchone()
    assert raw["enabled"] == 0
    assert json.loads(raw["root"]) == node_to_dict(root)

    # tree_id is caller-supplied (the brief's contract): a duplicate is a PK
    # conflict, which the API layer maps to 409.
    with pytest.raises(sqlite3.IntegrityError):
        repo.create("t1", "dupe", root)


def test_list_is_ordered_by_updated_at_then_id(repo, conn):
    repo.create("t-b", "B", simple_root())
    repo.create("t-a", "A", simple_root())
    conn.execute("UPDATE routing_trees SET updated_at = "
                 "'2026-10-26T10:00:00+08:00'")
    conn.execute("UPDATE routing_trees SET updated_at = "
                 "'2026-10-26T11:00:00+08:00' WHERE id = 't-a'")
    conn.commit()
    assert [tree.id for tree in repo.list()] == ["t-b", "t-a"]
    conn.execute("UPDATE routing_trees SET updated_at = "
                 "'2026-10-26T11:00:00+08:00'")
    conn.commit()
    assert [tree.id for tree in repo.list()] == ["t-a", "t-b"]   # tie: id


# --------------------------------------------------------------------------- #
# canonical revision + append-only history
# --------------------------------------------------------------------------- #
def test_tree_revision_is_utf8_canonical_sha256(repo):
    """Pinned contract: sha256(UTF-8 canonical JSON of node_to_dict(root)),
    ensure_ascii=False + sort_keys=True + compact separators, arrays kept in
    configuration order (§8.2). The hashes below were computed independently."""
    assert tree_revision_of(simple_root()) == (
        "ac2ab0540be16a6bbf8a7367de42f9b01a4e9d5cacda0d71149f99ff5950cf6d")
    chinese = NLChoice(id="nl", instructions="选择：医疗还是编程",
                       otherwise=Route(id="nl/fallback", model="qwen3-8b"))
    assert tree_revision_of(chinese) == (
        "676a61501be1193b7eaf38d86858484957cc2edd8152e06190fe6c1aebd1ae6c")
    assert repo.create("t-cn", "中文", chinese).tree_revision == (
        "676a61501be1193b7eaf38d86858484957cc2edd8152e06190fe6c1aebd1ae6c")


def test_content_write_appends_tree_revision_once(repo, conn):
    root = simple_root()
    tree = repo.create("t1", "First", root)

    rows = conn.execute("SELECT kind, sha256, content FROM config_revisions "
                        "WHERE kind = ?", (REVISION_TREE,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["sha256"] == tree.tree_revision
    content = rows[0]["content"]
    # the stored content IS the canonical text: offline re-hash reproduces the
    # revision, and the routing_trees.root column holds the same bytes.
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == tree.tree_revision
    assert content == conn.execute(
        "SELECT root FROM routing_trees WHERE id = 't1'").fetchone()[0]
    assert json.loads(content) == node_to_dict(root)

    # Identical content in another tree is idempotent: UNIQUE(kind, sha256)
    # keeps the original revision row and both trees share the revision.
    other = repo.create("t2", "Second", root)
    assert other.tree_revision == tree.tree_revision
    assert conn.execute("SELECT COUNT(*) FROM config_revisions WHERE kind = ?",
                        (REVISION_TREE,)).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# update: content only, never enabled (brief Step 3)
# --------------------------------------------------------------------------- #
def test_update_content_does_not_change_enabled(repo, enabled_tree):
    before = repo.get(enabled_tree)
    repo.update(enabled_tree, "renamed", before.root)
    after = repo.get(enabled_tree)
    assert after.enabled is True
    assert after.name == "renamed"
    assert after.tree_revision == before.tree_revision

    with pytest.raises(KeyError):
        repo.update("missing", "x", before.root)


def test_update_new_content_bumps_revision_and_records_history(repo, conn):
    tree = repo.create("t1", "First", simple_root())
    repo.update("t1", "First", nl_root("a", "b"))
    updated = repo.get("t1")
    assert updated.tree_revision == tree_revision_of(nl_root("a", "b"))
    assert updated.tree_revision != tree.tree_revision
    shas = {row[0] for row in conn.execute(
        "SELECT sha256 FROM config_revisions WHERE kind = ?", (REVISION_TREE,))}
    assert shas == {tree.tree_revision, updated.tree_revision}


def test_tree_revision_ignores_name_enabled_and_updated_at(repo):
    root = simple_root()
    tree = repo.create("t1", "name-a", root)
    revision = tree.tree_revision
    repo.update("t1", "name-b", root)          # name-only change
    assert repo.get("t1").tree_revision == revision
    repo.enable("t1")
    assert repo.get("t1").tree_revision == revision
    repo.disable("t1")
    assert repo.get("t1").tree_revision == revision
    assert revision == tree_revision_of(root)


def test_candidate_order_changes_revision_and_is_preserved(repo):
    assert tree_revision_of(nl_root("a", "b")) != tree_revision_of(nl_root("b", "a"))
    tree = repo.create("t1", "A", nl_root("a", "b"))
    repo.update("t1", "A", nl_root("b", "a"))
    updated = repo.get("t1")
    assert updated.tree_revision == tree_revision_of(nl_root("b", "a"))
    assert updated.tree_revision != tree.tree_revision
    assert [option.key for option in updated.root.options] == ["b", "a"]


# --------------------------------------------------------------------------- #
# enable / disable: single active tree (brief Step 3 + spec §12.6)
# --------------------------------------------------------------------------- #
def test_enable_switches_and_disable_can_leave_no_active(repo, two_trees):
    first, second = two_trees
    repo.enable(first)
    assert [tree.id for tree in repo.list() if tree.enabled] == [first]
    repo.enable(second)
    assert [tree.id for tree in repo.list() if tree.enabled] == [second]
    repo.disable(second)
    assert [tree for tree in repo.list() if tree.enabled] == []


def test_enable_is_idempotent_and_unknown_ids_fail_cleanly(repo, two_trees):
    first, second = two_trees
    repo.enable(first)
    repo.enable(first)                         # repeated enable: still one active
    assert [tree.id for tree in repo.list() if tree.enabled] == [first]
    with pytest.raises(KeyError):
        repo.enable("missing")
    assert [tree.id for tree in repo.list() if tree.enabled] == [first]
    assert repo.disable(first) is True
    assert repo.disable(first) is True         # already closed, but it exists
    assert repo.disable("missing") is False


def test_concurrent_enable_from_two_connections_leaves_one_active(two_trees, tmp_path):
    """Two independent connections enable different trees at the same time.

    ``enable`` takes BEGIN IMMEDIATE, so the second writer waits then closes the
    first; the v19 partial unique index never fires and the final state has
    exactly one active tree (spec §12.6)."""
    first, second = two_trees
    path = str(tmp_path / "t.db")
    connections = [connect(path), connect(path)]
    barrier = threading.Barrier(2)
    errors = []

    def worker(conn, tree_id):
        try:
            barrier.wait(timeout=5)
            RoutingTreeRepo(conn).enable(tree_id)
        except BaseException as exc:          # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(conn, tree_id))
               for conn, tree_id in zip(connections, (first, second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    for conn in connections:
        conn.close()

    active = active_ids(path)
    assert len(active) == 1
    assert active[0] in (first, second)


# --------------------------------------------------------------------------- #
# white-box contracts the brief states literally
# --------------------------------------------------------------------------- #
def test_enable_takes_begin_immediate(repo, two_trees):
    """The single-active guard must hold the write lock BEFORE reading state."""
    first, _ = two_trees
    statements = []
    repo.conn.set_trace_callback(statements.append)
    try:
        repo.enable(first)
    finally:
        repo.conn.set_trace_callback(None)
    assert any(stmt.strip().upper() == "BEGIN IMMEDIATE" for stmt in statements)


def test_update_sql_never_writes_enabled(repo, enabled_tree):
    """The generic edit path cannot flip activation behind the enable/disable
    operations' back (§7.1: the edit endpoint must not carry enabled)."""
    statements = []
    repo.conn.set_trace_callback(statements.append)
    try:
        repo.update(enabled_tree, "renamed", simple_root())
    finally:
        repo.conn.set_trace_callback(None)
    updates = [stmt for stmt in statements
               if stmt.lstrip().upper().startswith("UPDATE")]
    assert updates
    assert all("ENABLED" not in stmt.upper() for stmt in updates)
    assert repo.get(enabled_tree).enabled is True


# --------------------------------------------------------------------------- #
# copy: closed copy, same content revision, fresh updated_at (brief Step 5)
# --------------------------------------------------------------------------- #
def test_copy_creates_closed_tree_with_same_revision(repo, conn, enabled_tree):
    conn.execute("UPDATE routing_trees SET updated_at = "
                 "'2020-01-01T00:00:00+08:00' WHERE id = ?", (enabled_tree,))
    conn.commit()
    source = repo.get(enabled_tree)

    copied = repo.copy(enabled_tree, "tree-copy", "Copy")
    assert (copied.id, copied.name, copied.enabled) == ("tree-copy", "Copy", False)
    assert copied.tree_revision == source.tree_revision
    assert copied.tree_revision == tree_revision_of(source.root)
    assert copied.updated_at != source.updated_at      # not inherited
    assert source.enabled is True                      # source untouched
    assert repo.get("tree-copy").enabled is False

    with pytest.raises(KeyError):
        repo.copy("missing", "x", "X")
    with pytest.raises(sqlite3.IntegrityError):
        repo.copy(enabled_tree, "tree-copy", "again")
