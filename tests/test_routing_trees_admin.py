"""HTTP contract for unified routing-tree management (Task 9 Step 1).

These tests are the spec for ``router/api/routes_trees.py``; the console store
(``router/console/routing-tree-store.js``) is built against the same contract.
Lead-ruled shape (2026-10-26):

* ``GET  /admin/trees``                -> 200 ``{"trees": [<full tree_to_dict>],
                                              "snapshot_revision": "<published>"}``
  - rows are server-ordered (``RoutingTreeRepo.list()``: updated_at, id) and
    embed the full ``root`` (no N+1 detail fetch);
  - tree object = ``{id, name, enabled(bool), root, tree_revision,
    updated_at}``.
* ``POST /admin/trees {id?,name,root}`` -> 201 ``{"tree", "snapshot_revision"}``;
  id optional (server generates one); id rule ``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``
  (invalid 422, duplicate 409); a new tree is ALWAYS disabled.
* ``GET  /admin/trees/{id}``           -> 200 ``{"tree": {...}}``; unknown 404.
* ``PUT  /admin/trees/{id} {name,root}`` -> 200 ``{"tree", "snapshot_revision",
  "warnings": []}``; a payload carrying ``enabled`` (any value) is 422 and the
  generic edit never changes activation.
* ``POST /admin/trees/{id}/copy {id?,name}`` -> 201 ``{"tree",
  "snapshot_revision"}``; the copy starts disabled and does not inherit
  enabled/updated_at.
* ``POST /admin/trees/{id}/enable|disable`` -> 200 ``{"trees", "snapshot_revision"}``
  (same shape as GET, so the console replaces the whole list). ``enable`` closes
  every other tree in the same write transaction; ``disable`` may leave no
  active tree.
* 422 body = ``{"detail": [Issue{code,zh,en,node_id,level}, ...]}``.
* ``POST /admin/try {tree_id,text,headers?,n_messages?}`` -> 200 with
  ``outcome/signals/trace/input/signal_ms/decision_ms/nl_calls/tree_id/
  tree_revision/snapshot_revision/questions_sha256/tagger_info``; trying a
  closed tree never republishes ``app.state.snapshot``.
* No ``DELETE /admin/trees/{id}`` in this version; ``enabled`` is always a JSON
  bool; ``snapshot_revision`` is the PUBLISHED online revision (never the
  target tree's own ``tree_revision``).

Everything below is expected to be RED until Tasks 7 (snapshot), 8 (runtime)
and 9 (routes) land; the failure mode is the point of this file.
"""

from __future__ import annotations

import re

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.pipeline.tree import NLChoice, NLOption, Route, node_to_dict
from router.store.db import connect

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

TREE_KEYS = {"id", "name", "enabled", "root", "tree_revision", "updated_at"}


# --------------------------------------------------------------------------- #
# helpers: app/client, canonical roots and the fake decision runner
# --------------------------------------------------------------------------- #
def ok_upstream(request):
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


class FakeChoice:
    """Duck-typed ``router.decision.runner.ChoiceResult`` (probabilities only)."""

    def __init__(self, probabilities):
        self.probabilities = dict(probabilities)


class FakeDecisionRunner:
    """Deterministic runner: pops one queued answer per ``nl_choice`` call."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = []

    def info(self):
        return {"engine": "fake-decision", "revision": "fake-v1"}

    async def choose(self, request):
        self.calls.append(request)
        return self._answers.pop(0)


def route_root(model="qwen3-8b"):
    """A weightless route root: healthy with any model catalog, no signals."""
    return node_to_dict(Route(id="root", model=model))


def nl_root():
    """A closed-draft natural-language tree with two ``child`` candidates."""
    return node_to_dict(NLChoice(
        id="nl", instructions="choose a lane",
        options=(
            NLOption(key="fast", text="quick questions",
                     target=Route(id="fast-route", model="qwen3-8b")),
            NLOption(key="deep", text="multi-step reasoning tasks",
                     target=Route(id="deep-route", model="deepseek-v3")),
        ),
        otherwise=Route(id="fallback", model="qwen3-8b")))


def make_app(tmp_path, *, decision_runner=None):
    settings = Settings(db_path=str(tmp_path / "t.db"))
    kwargs = {"transport": httpx.MockTransport(ok_upstream)}
    if decision_runner is not None:
        kwargs["decision_runner"] = decision_runner
    return create_app(settings, **kwargs)


def make_client(tmp_path, *, decision_runner=None):
    return TestClient(make_app(tmp_path, decision_runner=decision_runner))


def create_tree(client, name, root, tree_id=None):
    payload = {"name": name, "root": root}
    if tree_id is not None:
        payload["id"] = tree_id
    response = client.post("/admin/trees", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def tree_list(client):
    response = client.get("/admin/trees")
    assert response.status_code == 200, response.text
    return response.json()


def fetch_tree(client, tree_id):
    response = client.get(f"/admin/trees/{tree_id}")
    assert response.status_code == 200, response.text
    return response.json()["tree"]


def enable(client, tree_id):
    response = client.post(f"/admin/trees/{tree_id}/enable")
    assert response.status_code == 200, response.text
    return response.json()


def online_revision(app):
    """The published snapshot revision the hot path currently uses."""
    return app.state.snapshot.snapshot_revision


def enabled_ids(body):
    return [row["id"] for row in body["trees"] if row["enabled"]]


# --------------------------------------------------------------------------- #
# create: closed by default, full envelope, id rules
# --------------------------------------------------------------------------- #
def test_create_tree_is_closed_and_returns_full_envelope(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    body = create_tree(client, "First", route_root(), tree_id="tree-a")

    tree = body["tree"]
    assert set(tree) == TREE_KEYS
    assert (tree["id"], tree["name"]) == ("tree-a", "First")
    assert tree["enabled"] is False            # JSON bool, never 0/1
    assert tree["root"] == route_root()        # canonical full-out-edge form
    assert isinstance(tree["tree_revision"], str) and tree["tree_revision"]
    assert isinstance(tree["updated_at"], str) and tree["updated_at"]
    # creating a closed tree must not republish the online snapshot
    assert body["snapshot_revision"] == online_revision(app)

    listing = tree_list(client)
    assert [row["id"] for row in listing["trees"]] == ["tree-a"]
    assert listing["trees"][0] == tree         # list rows embed the full root
    assert listing["snapshot_revision"] == online_revision(app)
    assert fetch_tree(client, "tree-a") == tree
    assert client.get("/admin/trees/nope").status_code == 404


def test_create_tree_id_rules_and_bad_payloads(tmp_path):
    client = make_client(tmp_path)

    generated = create_tree(client, "Generated", route_root())["tree"]
    assert ID_RE.match(generated["id"]), generated["id"]
    assert generated["enabled"] is False

    create_tree(client, "Explicit", route_root(), tree_id="tree-a")
    duplicate = client.post("/admin/trees", json={
        "id": "tree-a", "name": "Again", "root": route_root()})
    assert duplicate.status_code == 409

    invalid_id = client.post("/admin/trees", json={
        "id": "bad id!", "name": "Bad", "root": route_root()})
    assert invalid_id.status_code == 422

    invalid_root = client.post("/admin/trees", json={
        "name": "Bad", "root": {"id": "r", "type": "telepathy"}})
    assert invalid_root.status_code == 422


def test_list_is_stable_and_server_ordered(tmp_path):
    client = make_client(tmp_path)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    create_tree(client, "B", route_root(), tree_id="tree-b")

    first = tree_list(client)
    second = tree_list(client)
    ids = [row["id"] for row in first["trees"]]
    assert ids == ["tree-a", "tree-b"]         # repo order: updated_at, id
    assert [row["id"] for row in second["trees"]] == ids
    assert all(isinstance(row["enabled"], bool) for row in first["trees"])
    assert all("root" in row for row in first["trees"])


# --------------------------------------------------------------------------- #
# update: content only, never enabled
# --------------------------------------------------------------------------- #
def test_update_saves_content_without_touching_enabled(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    assert enabled_ids(enable(client, "tree-a")) == ["tree-a"]
    before = fetch_tree(client, "tree-a")
    assert before["enabled"] is True

    response = client.put("/admin/trees/tree-a", json={
        "name": "Renamed", "root": route_root(model="qwen3-32b")})
    assert response.status_code == 200
    body = response.json()
    tree = body["tree"]
    assert set(tree) == TREE_KEYS
    assert tree["name"] == "Renamed"
    assert tree["enabled"] is True             # generic edit never writes it
    assert tree["root"]["model"] == "qwen3-32b"
    assert tree["tree_revision"] != before["tree_revision"]
    assert body["warnings"] == []
    # the active tree's save republishes the snapshot: the online revision is
    # the just-published one, not the tree content hash.
    assert body["snapshot_revision"] == online_revision(app)
    assert body["snapshot_revision"] != tree["tree_revision"]

    # `enabled` in the payload is rejected for ANY value (never silently ignored)
    for value in (True, False, None):
        rejected = client.put("/admin/trees/tree-a", json={
            "name": "Nope", "root": route_root(), "enabled": value})
        assert rejected.status_code == 422, (value, rejected.text)
    current = fetch_tree(client, "tree-a")
    assert current["name"] == "Renamed" and current["enabled"] is True

    assert client.put("/admin/trees/nope", json={
        "name": "x", "root": route_root()}).status_code == 404


# --------------------------------------------------------------------------- #
# copy: always starts closed, keeps the source's content revision
# --------------------------------------------------------------------------- #
def test_copy_starts_closed_and_does_not_inherit_metadata(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    assert enabled_ids(enable(client, "tree-a")) == ["tree-a"]
    # make "not inherited" observable even when the whole test runs in one second
    conn = connect(app.state.settings.db_path)
    conn.execute("UPDATE routing_trees SET updated_at = "
                 "'2020-01-01T00:00:00+08:00' WHERE id = 'tree-a'")
    conn.commit()
    conn.close()
    source = fetch_tree(client, "tree-a")

    response = client.post("/admin/trees/tree-a/copy",
                           json={"id": "tree-copy", "name": "Copy"})
    assert response.status_code == 201
    copied = response.json()["tree"]
    assert (copied["id"], copied["name"]) == ("tree-copy", "Copy")
    assert copied["enabled"] is False          # a copy never starts enabled
    assert copied["root"] == source["root"]
    assert copied["tree_revision"] == source["tree_revision"]
    assert copied["updated_at"] != "2020-01-01T00:00:00+08:00"
    assert response.json()["snapshot_revision"] == online_revision(app)
    assert enabled_ids(tree_list(client)) == ["tree-a"]   # source untouched

    # `id` is optional on copy too: the server generates a valid one
    generated = client.post("/admin/trees/tree-a/copy", json={"name": "Copy 2"})
    assert generated.status_code == 201
    assert ID_RE.match(generated.json()["tree"]["id"])
    assert generated.json()["tree"]["enabled"] is False

    assert client.post("/admin/trees/nope/copy",
                       json={"id": "x", "name": "X"}).status_code == 404


# --------------------------------------------------------------------------- #
# enable / disable: single active tree, all-closed is legal
# --------------------------------------------------------------------------- #
def test_enable_switches_and_disable_leaves_no_active(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    create_tree(client, "B", route_root(model="qwen3-32b"), tree_id="tree-b")

    first = enable(client, "tree-a")
    assert set(first) == {"trees", "snapshot_revision"}
    assert enabled_ids(first) == ["tree-a"]
    assert first["snapshot_revision"] == online_revision(app)
    assert app.state.snapshot.tree_id == "tree-a"
    revision_a = first["snapshot_revision"]
    tree_a_revision = next(row["tree_revision"] for row in first["trees"]
                           if row["id"] == "tree-a")
    # snapshot_revision is the published configuration hash, never the tree hash
    assert revision_a != tree_a_revision

    second = enable(client, "tree-b")
    assert enabled_ids(second) == ["tree-b"]   # B enabled A closed in one txn
    assert second["snapshot_revision"] == online_revision(app)
    assert second["snapshot_revision"] != revision_a
    assert app.state.snapshot.tree_id == "tree-b"
    # the enable response must equal a plain GET list (console replaces the list)
    assert second == tree_list(client)

    closed = client.post("/admin/trees/tree-b/disable")
    assert closed.status_code == 200
    body = closed.json()
    assert [row for row in body["trees"] if row["enabled"]] == []
    assert body["snapshot_revision"] == online_revision(app)
    assert app.state.snapshot.tree_id is None
    # disabling an already-closed tree is idempotent; unknown ids are 404
    assert client.post("/admin/trees/tree-b/disable").status_code == 200
    assert client.post("/admin/trees/nope/enable").status_code == 404
    assert client.post("/admin/trees/nope/disable").status_code == 404


# --------------------------------------------------------------------------- #
# two-level validation: save a draft, full health check on enable
# --------------------------------------------------------------------------- #
def test_incomplete_draft_can_be_saved_but_not_enabled(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    # A branch with no out-edges is a legal *draft* but has no guaranteed
    # fallback, so enabling it must be rejected with typed findings.
    draft = {"id": "draft-root", "type": "branch"}
    saved = create_tree(client, "Draft", draft, tree_id="draft")
    assert saved["tree"]["enabled"] is False
    assert saved["tree"]["root"] == {"id": "draft-root", "type": "branch",
                                     "children": []}

    rejected = client.post("/admin/trees/draft/enable")
    assert rejected.status_code == 422
    issues = rejected.json()["detail"]
    assert isinstance(issues, list) and issues
    for issue in issues:
        assert set(issue) == {"code", "zh", "en", "node_id", "level"}
    assert "missing_fallback" in {issue["code"] for issue in issues}
    assert all(issue["level"] == "hard" for issue in issues)

    # still closed, and the online snapshot was not republished
    assert fetch_tree(client, "draft")["enabled"] is False
    assert app.state.snapshot.tree_id is None


# --------------------------------------------------------------------------- #
# try: closed trees are tryable and never touch the online snapshot
# --------------------------------------------------------------------------- #
TRY_KEYS = {"outcome", "signals", "trace", "input", "signal_ms", "decision_ms",
            "nl_calls", "tree_id", "tree_revision", "snapshot_revision",
            "questions_sha256", "tagger_info"}


def test_try_closed_signal_tree_keeps_online_snapshot(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    create_tree(client, "B", route_root(model="qwen3-32b"), tree_id="tree-b")
    enable(client, "tree-a")
    online = app.state.snapshot
    revision = online.snapshot_revision
    target = fetch_tree(client, "tree-b")

    response = client.post("/admin/try", json={
        "tree_id": "tree-b", "text": "hello", "headers": {}, "n_messages": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TRY_KEYS
    assert body["outcome"]["type"] == "hit"
    assert body["outcome"]["kind"] == "route"
    assert body["outcome"]["model"] == "qwen3-32b"
    assert body["outcome"]["node_id"] == "root"
    assert body["tree_id"] == "tree-b"
    assert body["tree_revision"] == target["tree_revision"]
    assert body["snapshot_revision"] != revision      # the closed tree's snapshot
    assert body["nl_calls"] == 0

    # the online snapshot object/revision/active tree are all untouched
    assert app.state.snapshot is online
    assert app.state.snapshot.snapshot_revision == revision
    assert app.state.snapshot.tree_id == "tree-a"


def test_try_closed_nl_tree_uses_the_decision_runner(tmp_path):
    runner = FakeDecisionRunner(FakeChoice({"fast": 1.0, "deep": 0.0}))
    app = make_app(tmp_path, decision_runner=runner)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    enable(client, "tree-a")
    online = app.state.snapshot
    revision = online.snapshot_revision
    target = create_tree(client, "NL", nl_root(), tree_id="tree-nl")["tree"]
    assert target["enabled"] is False

    response = client.post("/admin/try", json={
        "tree_id": "tree-nl", "text": "write some code for me"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TRY_KEYS
    assert body["outcome"]["type"] == "hit"
    assert body["outcome"]["model"] == "qwen3-8b"
    assert body["outcome"]["node_id"] == "fast-route"
    assert body["nl_calls"] == 1
    assert len(runner.calls) == 1
    # candidate order is behaviour: it reaches the runner exactly in config order
    assert [option.key for option in runner.calls[0].options] == ["fast", "deep"]
    nl_events = [event for event in body["trace"]
                 if event.get("event") == "nl_choice"]
    assert len(nl_events) == 1 and nl_events[0]["selected_key"] == "fast"
    assert body["tree_id"] == "tree-nl"
    assert body["tree_revision"] == target["tree_revision"]
    assert body["snapshot_revision"] not in ("", revision)
    assert app.state.snapshot is online
    assert app.state.snapshot.snapshot_revision == revision
    assert app.state.snapshot.tree_id == "tree-a"


# --------------------------------------------------------------------------- #
# no delete operation in this version
# --------------------------------------------------------------------------- #
def test_delete_is_not_exposed(tmp_path):
    client = make_client(tmp_path)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    assert client.delete("/admin/trees/tree-a").status_code == 405
