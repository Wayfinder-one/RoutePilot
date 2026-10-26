"""Store-layer smoke tests: idempotent seeding + the model-reference guard.

The old Station pipeline repository is gone (2026-10-26 unified routing tree);
trees live in ``router.store.routing_trees``.  ``seed_default`` only seeds the
catalog/provider/keyword/period data — a fresh database has **no** routing tree
until an operator creates and enables one through ``/admin/trees``.
"""

import pytest

from router.pipeline.tree import (Branch, NLChoice, NLOption, Route,
                                  RoutingTree, When)
from router.store.db import connect, migrate
from router.store.repositories import (KeywordRepo, ModelRepo,
                                       model_references, seed_default)
from router.store.routing_trees import RoutingTreeRepo


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    migrate(c)
    yield c
    c.close()


def test_seed_is_idempotent_and_creates_no_routing_tree(conn):
    seed_default(conn)
    seed_default(conn)
    assert len(ModelRepo(conn).list()) == 5
    assert "医疗急症" in KeywordRepo(conn).groups()
    # no implicit live routing: a fresh DB has zero trees (all disabled is legal)
    assert conn.execute(
        "SELECT COUNT(*) FROM routing_trees").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 0


def _route_tree() -> RoutingTree:
    return RoutingTree(
        id="t-route", name="Route tree", enabled=False,
        root=Branch(id="root", children=(
            Route("r1", "m1"),
            Route("r2", "m2"),
        ), otherwise=Route("route-fallback", "m0")))


def _nl_tree() -> RoutingTree:
    return RoutingTree(
        id="t-nl", name="NL tree", enabled=False,
        root=NLChoice(
            id="nl", instructions="choose",
            options=(
                NLOption("a", "", Route("nl/options/a", "m1"), "m1"),
                NLOption("b", "deep path", Route("b-route", "m2")),
            ),
            otherwise=Route("nl-fallback", "m0")))


def test_model_references_reports_route_nodes(conn):
    repo = RoutingTreeRepo(conn)
    saved = repo.create("t-route", "Route tree", _route_tree().root)

    refs = model_references(repo.list(), "m1")
    assert len(refs) == 1
    assert set(refs[0]) == {"tree_id", "tree_revision", "node_id", "via"}
    assert refs[0]["tree_id"] == "t-route"
    assert refs[0]["tree_revision"] == saved.tree_revision
    assert refs[0]["node_id"] == "r1"
    assert refs[0]["via"] == "route"
    assert model_references(repo.list(), "no-such-model") == []


def test_model_references_reports_direct_nl_model_candidates(conn):
    repo = RoutingTreeRepo(conn)
    saved = repo.create("t-nl", "NL tree", _nl_tree().root)
    trees = repo.list()

    refs = model_references(trees, "m1")
    option_refs = [ref for ref in refs if ref["via"] == "nl_option"]
    assert len(option_refs) == 1
    option = option_refs[0]
    assert option["tree_id"] == "t-nl"
    assert option["tree_revision"] == saved.tree_revision
    assert option["node_id"] == "nl/options/a"       # auto-built route target
    assert option["nl_node_id"] == "nl"
    assert option["option_key"] == "a"
    # the auto-built route is also a route reference (delete-guard parity)
    auto = [ref for ref in refs
            if ref["via"] == "route" and ref["node_id"] == "nl/options/a"]
    assert len(auto) == 1

    # a child candidate's target route is a plain route reference
    m2 = model_references(trees, "m2")
    assert [ref["node_id"] for ref in m2 if ref["via"] == "route"] == [
        "b-route"]


def test_model_references_does_not_treat_signal_strings_as_references(conn):
    """Only real model references count — a condition value naming the model
    (the retired ``capability_route`` matching) is not a reference."""
    tree = RoutingTree(
        id="t-signal", name="signal", enabled=False,
        root=Branch(id="root", children=(
            Route("signal-only", "other-model",
                  when=When("capability_route", "m1")),
        ), otherwise=Route("signal-fallback", "m0")))
    RoutingTreeRepo(conn).create("t-signal", "signal", tree.root)

    refs = model_references(RoutingTreeRepo(conn).list(), "m1")
    assert refs == []
    assert all("capability" not in ref["via"] for ref in
               model_references(RoutingTreeRepo(conn).list(), "other-model"))
