"""Immutable effective routing snapshots and content-addressed revision.

A request is judged on exactly one frozen snapshot; admin writes build a new
snapshot and publish it with one assignment.  ``snapshot_revision`` is the
``sha256`` of the canonical append-only *effective* document (compiled tree +
questions + decision runner evidence + backend routing config), so a ledger row
can be replayed by loading that document back with ``snapshot_from_content``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from router.api.upstream import Backend
from router.pipeline.compiler import compile_tree
from router.pipeline.scope import compute_tree_scope, required_signal_keys
from router.pipeline.snapshot import (
    REVISION_SNAPSHOT,
    RoutingSnapshot,
    backends_revision_of,
    build_snapshot,
    canonical_json,
    ensure_snapshot_revision,
    freeze_data,
    install_snapshot,
    load_capabilities,
    load_questions,
    load_snapshot,
    sha256_text,
    snapshot_content,
    snapshot_from_content,
    thaw_data,
)
from router.pipeline.tree import (
    NLChoice,
    NLOption,
    Route,
    RoutingTree,
    tree_revision_of,
)
from router.store.db import connect, migrate
from router.store.repositories import (
    ModelRepo,
    ProviderRepo,
    ensure_questions_revision,
)
from router.store.routing_trees import RoutingTreeRepo

CAPABILITIES = {"m1": "fast tasks", "m2": "deep tasks"}
DECISION = {"engine": "fake-jev", "revision": "tagger-v1", "model_sha256": "abc"}
_MISSING = object()


def backends():
    return {
        "m1": Backend(url="http://a.example/v1/chat/completions",
                      headers={"Authorization": "Bearer secret-token"},
                      upstream_name="m1-up", provider="local"),
        "m2": Backend(url="http://b.example/v1/chat/completions",
                      headers={}, upstream_name="m2-up", provider="openai"),
    }


def direct_options(order=("m1", "m2")):
    return tuple(
        NLOption(key=key, text="", target=Route(f"root/options/{key}", key),
                 capability_model=key)
        for key in order)


def tree_with_options(options=None, *, instructions="choose", tree_id="tree-a",
                      **nl_kwargs) -> RoutingTree:
    return RoutingTree(
        id=tree_id, name="Tree A", enabled=True,
        root=NLChoice(id="root", instructions=instructions,
                      options=direct_options() if options is None else options,
                      otherwise=Route("fallback", "m1"), **nl_kwargs))


def snapshot_of(tree, *, capabilities=None, questions=None,
                decision_info=_MISSING, jev_trim=True, backends_map=None,
                ) -> RoutingSnapshot:
    compiled = compile_tree(tree, CAPABILITIES if capabilities is None
                            else capabilities)
    return build_snapshot(
        tree=compiled,
        backends=backends() if backends_map is None else backends_map,
        questions=questions,
        decision_info=DECISION if decision_info is _MISSING else decision_info,
        jev_trim=jev_trim)


def revision_of(tree=None, **kwargs) -> str:
    return snapshot_of(tree or tree_with_options(), **kwargs).snapshot_revision


@pytest.fixture()
def conn(tmp_path):
    connection = connect(str(tmp_path / "router.db"))
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# fields and derivations
# --------------------------------------------------------------------------- #
def test_build_snapshot_derives_content_addressed_fields():
    tree = tree_with_options()
    snapshot = snapshot_of(tree, questions={"q": {"list": [1, 2]}})

    assert isinstance(snapshot, RoutingSnapshot)
    assert snapshot.tree_id == "tree-a"
    assert snapshot.tree_revision == tree_revision_of(tree.root)
    assert snapshot.required_signals == required_signal_keys(snapshot.tree.root)
    assert snapshot.scope.signals == compute_tree_scope(
        snapshot.tree.root).signals
    assert snapshot.questions_revision == sha256_text(
        canonical_json({"q": {"list": [1, 2]}}))
    assert snapshot.decision_revision == sha256_text(canonical_json(DECISION))
    assert snapshot.backends_revision == backends_revision_of(backends())


def test_snapshot_content_is_canonical_and_content_addressed():
    snapshot = snapshot_of(tree_with_options(), questions={"q": [1, 2]})
    content = snapshot_content(snapshot)
    assert content == canonical_json(json.loads(content))
    assert sha256_text(content) == snapshot.snapshot_revision
    again = snapshot_of(tree_with_options(), questions={"q": [1, 2]})
    assert again.snapshot_revision == snapshot.snapshot_revision


# --------------------------------------------------------------------------- #
# revision sensitivity (what the effective configuration covers)
# --------------------------------------------------------------------------- #
def test_revision_tracks_candidate_order():
    assert revision_of(tree_with_options(direct_options(("m1", "m2")))) \
        != revision_of(tree_with_options(direct_options(("m2", "m1"))))


def test_revision_tracks_instructions_and_thresholds():
    base = revision_of()
    assert revision_of(tree_with_options(instructions="choose differently")) != base
    assert revision_of(tree_with_options(min_prob=0.5)) != base
    assert revision_of(tree_with_options(min_margin=0.2)) != base


def test_revision_tracks_referenced_capability_only():
    base = revision_of()
    assert revision_of(capabilities={
        "m1": "fast tasks v2", "m2": "deep tasks"}) != base
    assert revision_of(capabilities={
        "m1": "fast tasks", "m2": "deep tasks", "unused": "never referenced"}) == base


def test_revision_tracks_questions_decision_backends_and_trim():
    base = revision_of(questions={"q": 1})
    assert revision_of(questions={"q": 2}) != base
    assert revision_of(questions=None) != base
    assert revision_of(questions={"q": 1},
                       decision_info={"engine": "fake-jev",
                                      "revision": "tagger-v2"}) != base
    changed = backends()
    changed["m1"] = Backend(url="http://new.example/v1/chat/completions",
                            headers={}, upstream_name="m1-up", provider="local")
    assert revision_of(questions={"q": 1}, backends_map=changed) != base
    assert revision_of(questions={"q": 1}, jev_trim=False) != base


def test_tree_envelope_fields_do_not_change_revisions():
    root = tree_with_options().root
    one = snapshot_of(RoutingTree(id="tree-a", name="A", enabled=True,
                                  root=root, tree_revision="ignored-1",
                                  updated_at="2026-10-26T00:00:00Z"))
    two = snapshot_of(RoutingTree(id="tree-a", name="B", enabled=False,
                                  root=root, tree_revision="ignored-2",
                                  updated_at="2026-11-01T00:00:00Z"))
    assert one.snapshot_revision == two.snapshot_revision
    assert one.tree_revision == two.tree_revision == tree_revision_of(root)


def test_authorization_material_never_enters_the_content_or_revision():
    content = snapshot_content(snapshot_of(tree_with_options(),
                                          questions={"q": 1}))
    assert "Authorization" not in content
    assert "secret-token" not in content

    changed = backends()
    changed["m1"] = Backend(url="http://a.example/v1/chat/completions",
                            headers={"Authorization": "Bearer other-token"},
                            upstream_name="m1-up", provider="local")
    assert revision_of(questions={"q": 1}, backends_map=changed) \
        == revision_of(questions={"q": 1})


# --------------------------------------------------------------------------- #
# immutability and freeze/thaw
# --------------------------------------------------------------------------- #
def test_snapshot_is_deeply_read_only():
    snapshot = snapshot_of(tree_with_options(), questions={"q": {"list": [1]}})
    with pytest.raises(TypeError):
        snapshot.backends["m9"] = snapshot.backends["m1"]
    with pytest.raises(TypeError):
        snapshot.questions["q"] = {}
    with pytest.raises((AttributeError, TypeError)):
        snapshot.backends.clear()
    with pytest.raises((AttributeError, TypeError)):
        snapshot.tree.root.options = ()
    with pytest.raises((AttributeError, TypeError)):
        snapshot.questions["q"]["list"].append(2)


def test_freeze_and_thaw_data_round_trip_as_independent_copies():
    source = {"a": {"b": [1, 2]}, "c": ("x",), "d": None}
    frozen = freeze_data(source)
    assert frozen["a"]["b"] == (1, 2)
    with pytest.raises(TypeError):
        frozen["a"]["new"] = 1
    with pytest.raises((AttributeError, TypeError)):
        frozen["a"]["b"].append(3)

    expected = {"a": {"b": [1, 2]}, "c": ["x"], "d": None}
    thawed = thaw_data(frozen)
    assert thawed == expected
    thawed["a"]["b"].append(3)
    assert frozen["a"]["b"] == (1, 2)
    assert thaw_data(frozen) == expected


# --------------------------------------------------------------------------- #
# append-only content round trip (replay path)
# --------------------------------------------------------------------------- #
def test_content_round_trip_restores_compiled_tree_and_questions():
    questions = {"pii": {"instructions": "找出 PII", "list": [1, 2]}}
    snapshot = snapshot_of(tree_with_options(), questions=questions)

    restored = snapshot_from_content(snapshot_content(snapshot))
    assert isinstance(restored, RoutingSnapshot)
    assert restored.snapshot_revision == snapshot.snapshot_revision
    assert restored.tree_id == snapshot.tree_id
    assert restored.tree.root == snapshot.tree.root
    assert [option.text for option in restored.tree.root.options] == [
        "m1: fast tasks", "m2: deep tasks"]
    assert thaw_data(restored.questions) == questions
    assert restored.questions_revision == snapshot.questions_revision
    assert restored.decision_revision == snapshot.decision_revision
    assert restored.backends_revision == snapshot.backends_revision
    assert restored.tree_revision == snapshot.tree_revision
    assert restored.jev_trim is True
    assert restored.required_signals == snapshot.required_signals
    assert restored.scope.signals == snapshot.scope.signals
    assert snapshot_content(restored) == snapshot_content(snapshot)


def test_no_active_tree_snapshot_still_has_a_content_revision():
    base = build_snapshot(tree=None, backends=backends(),
                          questions={"q": 1}, decision_info=DECISION,
                          jev_trim=True)
    assert base.tree is None
    assert base.tree_id is None
    assert base.tree_revision == ""
    assert base.required_signals == frozenset()
    assert base.snapshot_revision

    changed = backends()
    changed["m1"] = Backend(url="http://new.example/v1/chat/completions",
                            headers={}, upstream_name="m1-up", provider="local")
    variants = [
        build_snapshot(tree=None, backends=backends(), questions={"q": 2},
                       decision_info=DECISION, jev_trim=True),
        build_snapshot(tree=None, backends=backends(), questions={"q": 1},
                       decision_info={"engine": "fake-jev",
                                      "revision": "tagger-v2"},
                       jev_trim=True),
        build_snapshot(tree=None, backends=changed, questions={"q": 1},
                       decision_info=DECISION, jev_trim=True),
        build_snapshot(tree=None, backends=backends(), questions={"q": 1},
                       decision_info=DECISION, jev_trim=False),
    ]
    assert all(variant.snapshot_revision != base.snapshot_revision
               for variant in variants)
    assert snapshot_from_content(
        snapshot_content(base)).snapshot_revision == base.snapshot_revision


# --------------------------------------------------------------------------- #
# publish / record / DB assembly
# --------------------------------------------------------------------------- #
def test_install_snapshot_is_one_assignment_without_station_aliases():
    app = SimpleNamespace(state=SimpleNamespace())
    snapshot = snapshot_of(tree_with_options())
    install_snapshot(app, snapshot)
    assert app.state.snapshot is snapshot
    assert not hasattr(app.state, "pipeline")
    assert not hasattr(app.state, "backends")


def test_ensure_snapshot_revision_records_append_only_content(conn):
    snapshot = snapshot_of(tree_with_options(), questions={"q": 1})
    revision = ensure_snapshot_revision(conn, snapshot)
    assert revision == snapshot.snapshot_revision
    row = conn.execute(
        "SELECT content FROM config_revisions WHERE kind = ? AND sha256 = ?",
        (REVISION_SNAPSHOT, revision)).fetchone()
    assert row is not None
    assert row["content"] == snapshot_content(snapshot)
    assert ensure_snapshot_revision(conn, snapshot) == revision
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM config_revisions WHERE kind = ?",
        (REVISION_SNAPSHOT,)).fetchone()["count"]
    assert count == 1


def test_load_snapshot_assembles_active_tree_models_backends_and_questions(conn):
    ProviderRepo(conn).upsert("local", "http://127.0.0.1:9000/v1",
                              api_key="secret-key")
    ProviderRepo(conn).upsert("openai", "http://127.0.0.1:9100/v1")
    ModelRepo(conn).create("m1", "m1-up", "local",
                           profile=json.dumps({"capability": "fast tasks"}))
    ModelRepo(conn).create("m2", "m2-up", "openai",
                           profile=json.dumps({"capability": "deep tasks"}))
    source = tree_with_options()
    RoutingTreeRepo(conn).create("tree-a", "Tree A", source.root)
    RoutingTreeRepo(conn).enable("tree-a")
    questions = {"pii": {"instructions": "找出 PII"}}
    ensure_questions_revision(conn, questions)

    snapshot = load_snapshot(conn, jev_trim=True, decision_info=DECISION)
    assert snapshot.tree_id == "tree-a"
    assert snapshot.tree_revision == tree_revision_of(source.root)
    assert isinstance(snapshot.tree.root, NLChoice)
    assert [option.text for option in snapshot.tree.root.options] == [
        "m1: fast tasks", "m2: deep tasks"]
    assert snapshot.backends["m1"].url == (
        "http://127.0.0.1:9000/v1/chat/completions")
    assert snapshot.backends["m1"].headers["Authorization"] == "Bearer secret-key"
    assert thaw_data(snapshot.questions) == questions
    assert snapshot.questions_revision == sha256_text(canonical_json(questions))
    assert snapshot.decision_revision == sha256_text(canonical_json(DECISION))
    assert "secret-key" not in snapshot_content(snapshot)
    assert snapshot.required_signals == frozenset()
    assert not snapshot.scope.full
    assert load_capabilities(conn) == {"m1": "fast tasks", "m2": "deep tasks"}
    assert load_questions(conn) == questions

    RoutingTreeRepo(conn).disable("tree-a")
    empty = load_snapshot(conn, jev_trim=True)
    assert empty.tree is None
    assert empty.tree_id is None
    assert empty.tree_revision == ""
    assert empty.snapshot_revision


def test_ensure_snapshot_revision_joins_an_outer_transaction(conn):
    snapshot = snapshot_of(tree_with_options())
    conn.execute("BEGIN IMMEDIATE")
    revision = ensure_snapshot_revision(conn, snapshot)
    assert revision == snapshot.snapshot_revision
    assert conn.in_transaction             # the outer transaction was not committed
    conn.execute("ROLLBACK")
    row = conn.execute(
        "SELECT 1 FROM config_revisions WHERE kind = ? AND sha256 = ?",
        (REVISION_SNAPSHOT, revision)).fetchone()
    assert row is None                     # rolled back together with the caller
