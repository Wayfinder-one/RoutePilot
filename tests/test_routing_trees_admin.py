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

import json
import re
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api import routes_trees
from router.api.app import create_app
from router.config import Settings
from router.pipeline.tree import (Block, Branch, NLChoice, NLOption, Route,
                                  When, node_to_dict)
from router.signals.jev import load_question_config
from router.store.db import connect
from router.store.routing_trees import RoutingTreeRepo

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
            "questions_sha256", "tagger_info", "warnings"}


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
    assert body["warnings"] == []          # healthy tree: nothing to warn about

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
    assert body["warnings"] == []
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


# --------------------------------------------------------------------------- #
# fix-wave 2: payload strictness (B-M-2)
# --------------------------------------------------------------------------- #
def test_create_payload_with_enabled_is_422(tmp_path):
    """Activation is enable/disable-only: a create payload carrying `enabled`
    (any value) is rejected instead of being silently ignored."""
    client = make_client(tmp_path)
    for value in (True, False, None):
        response = client.post("/admin/trees", json={
            "id": "tree-x", "name": "X", "root": route_root(),
            "enabled": value})
        assert response.status_code == 422, (value, response.text)
        assert response.json()["detail"][0]["code"] == "enabled_not_editable"
    assert tree_list(client)["trees"] == []


def test_update_payload_id_must_match_the_path(tmp_path):
    client = make_client(tmp_path)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    response = client.put("/admin/trees/tree-a", json={
        "id": "tree-b", "name": "A", "root": route_root()})
    assert response.status_code == 422
    assert response.json()["detail"][0]["code"] == "tree_id_mismatch"
    # the same id is accepted, and the id stays optional
    assert client.put("/admin/trees/tree-a", json={
        "id": "tree-a", "name": "A2", "root": route_root()}).status_code == 200
    assert client.put("/admin/trees/tree-a", json={
        "name": "A3", "root": route_root()}).status_code == 200
    assert fetch_tree(client, "tree-a")["name"] == "A3"


# --------------------------------------------------------------------------- #
# fix-wave 2: /admin/try is best-effort for saved drafts (B-M-1)
# --------------------------------------------------------------------------- #
def incomplete_draft_root():
    """Structure-valid but NO guaranteed fallback (a legal saved draft)."""
    return node_to_dict(Branch(id="root", children=(
        Route(id="law-only", model="qwen3-8b",
              when=When(signal="domain", value="law", conf_min=0.4)),)))


def test_try_incomplete_draft_runs_and_reports_enable_warnings(tmp_path):
    """spec §9: any SAVED tree is tryable.  A draft missing its fallback is a
    legal save (§7.2), so try must not reject it; the enable findings are
    returned as non-blocking warnings."""
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "Draft", incomplete_draft_root(), tree_id="draft")
    assert app.state.snapshot.tree_id is None        # nothing enabled

    response = client.post("/admin/try", json={
        "tree_id": "draft", "text": "今天天气怎么样"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"]["type"] == "miss"
    assert body["trace"], "the evaluated path must be reported"
    assert any(event.get("event") == "condition"
               for event in body["trace"])
    codes = {issue["code"] for issue in body["warnings"]}
    assert "missing_fallback" in codes
    assert body["tree_id"] == "draft"
    # trying never enables or publishes
    assert app.state.snapshot.tree_id is None
    assert fetch_tree(client, "draft")["enabled"] is False


def test_try_nl_draft_without_a_runner_is_a_local_nl_error(tmp_path):
    """A draft may contain nl_choice while the deployment has no decision
    model: try uses an always-failing runner so the NL node becomes a local
    nl_error (otherwise/Miss) instead of a 500."""
    client = make_client(tmp_path)                   # heuristic => runner None
    create_tree(client, "NL draft", nl_root(), tree_id="nl-draft")

    response = client.post("/admin/try", json={
        "tree_id": "nl-draft", "text": "write some code"})
    assert response.status_code == 200, response.text
    body = response.json()
    codes = {issue["code"] for issue in body["warnings"]}
    assert "decision_runner_unavailable" in codes
    nl_events = [event for event in body["trace"]
                 if event.get("event") == "nl_choice"]
    assert nl_events and nl_events[0]["reason"] == "nl_error"
    # the NL node fell through to its explicit otherwise route
    assert body["outcome"]["type"] == "hit"
    assert body["outcome"]["model"] == "qwen3-8b"
    assert body["nl_calls"] == 1


# --------------------------------------------------------------------------- #
# fix-wave 2: TOCTOU — the validation level/publish decision come from a
# lock-internal re-read (B-I-1)
# --------------------------------------------------------------------------- #
def _active_tree(conn):
    trees = [tree for tree in RoutingTreeRepo(conn).list() if tree.enabled]
    assert len(trees) <= 1
    return trees[0] if trees else None


def test_concurrent_enable_cannot_defeat_an_in_flight_put_health_check(
        tmp_path, monkeypatch):
    """Deterministic interleaving: a PUT starts while tree-b is CLOSED (so it
    would only need the structure check), a concurrent enable makes tree-b
    ACTIVE before the PUT reaches the lock.  The PUT must then run the FULL
    health check (422 for the unhealthy body), not persist an unhealthchecked
    enabled tree with a stale snapshot."""
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    enable(client, "tree-a")
    create_tree(client, "B", route_root(), tree_id="tree-b")
    unhealthy = incomplete_draft_root()

    real_clean_name = routes_trees._clean_name
    paused = threading.Event()
    release = threading.Event()

    def gated_clean_name(raw):
        value = real_clean_name(raw)
        if raw == "B renamed" and not paused.is_set():
            paused.set()
            assert release.wait(10)
        return value

    monkeypatch.setattr(routes_trees, "_clean_name", gated_clean_name)
    response_box = {}

    def put_worker():
        own = TestClient(app)
        response_box["put"] = own.put(
            "/admin/trees/tree-b",
            json={"name": "B renamed", "root": unhealthy})

    thread = threading.Thread(target=put_worker)
    thread.start()
    assert paused.wait(10), "the PUT never reached the race window"

    # The concurrent enable lands while the PUT is between its pure parse and
    # the lock: the lock-internal re-read now sees enabled=True.
    assert client.post("/admin/trees/tree-b/enable").status_code == 200
    release.set()
    thread.join(10)
    assert not thread.is_alive()

    rejected = response_box["put"]
    assert rejected.status_code == 422, rejected.text
    assert "missing_fallback" in rejected.text

    conn = connect(app.state.settings.db_path)
    try:
        active = _active_tree(conn)
        assert active is not None and active.id == "tree-b"
        # the stored tree still has the healthy content the enable validated
        assert active.tree_revision == app.state.snapshot.tree_revision
        issues = routes_trees.enable_issues(
            conn, active, runner_available=False)
        assert [issue for issue in issues if issue.level == "hard"] == []
    finally:
        conn.close()
    # published snapshot == the database's active tree
    assert app.state.snapshot.tree_id == active.id


def test_concurrent_put_and_enable_end_consistent(tmp_path):
    """Hammer PUT(alternating healthy/unhealthy draft) and enable on one tree;
    every published snapshot must describe the DB's active tree and that tree
    must have passed the full health check."""
    app = make_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "A", route_root(), tree_id="tree-a")
    enable(client, "tree-a")
    create_tree(client, "B", route_root(), tree_id="tree-b")
    unhealthy = incomplete_draft_root()
    healthy = route_root()
    errors = []

    def put_worker():
        own = TestClient(app)
        for index in range(12):
            body = unhealthy if index % 2 == 0 else healthy
            response = own.put("/admin/trees/tree-b",
                               json={"name": "B", "root": body})
            if response.status_code not in (200, 422):
                errors.append(("put", response.status_code, response.text))

    def enable_worker():
        own = TestClient(app)
        for _ in range(12):
            response = own.post("/admin/trees/tree-b/enable")
            # 422 is legitimate: a concurrent PUT may have stored an
            # unhealthy draft while tree-b was still closed.
            if response.status_code not in (200, 422):
                errors.append(("enable", response.status_code, response.text))

    threads = [threading.Thread(target=put_worker),
               threading.Thread(target=enable_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    conn = connect(app.state.settings.db_path)
    try:
        active = _active_tree(conn)
        # e.g. the last successful enable may have been followed by no state
        # change at all; tree-b may also still be closed with tree-a active.
        assert active is not None and active.id in ("tree-a", "tree-b")
        # the DB's enabled tree ALWAYS passed the full health check ...
        issues = routes_trees.enable_issues(
            conn, active, runner_available=False)
        assert [issue for issue in issues if issue.level == "hard"] == []
        # ... and the published snapshot describes exactly that tree.
        assert app.state.snapshot.tree_id == active.id
        assert app.state.snapshot.tree_revision == active.tree_revision
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# fix-wave 2: provider signal-value coverage wiring (B-I-2)
# --------------------------------------------------------------------------- #
def test_enable_context_carries_provider_signal_values(tmp_path):
    """The tagger's questions config becomes the validation context's
    ``signal_values`` (provider coverage); no questions -> unconstrained."""
    from router.pipeline.validation import provider_signal_values

    app = make_app(tmp_path)
    conn = connect(app.state.settings.db_path)
    try:
        cfg = {"signals": {"domain": {"instructions": ["选择领域"]}}}
        context = routes_trees.build_enable_context(
            conn, runner_available=False, questions=cfg)
        assert context.signal_values == provider_signal_values(cfg)
        plain = routes_trees.build_enable_context(
            conn, runner_available=False)
        assert plain.signal_values is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# signal-question writes judge the ACTIVE tree against the PROSPECTIVE config
# --------------------------------------------------------------------------- #
def pii_signal_root():
    return node_to_dict(Branch(
        id="pii-root",
        children=(Block(id="pii-block",
                        when=When("pii", "cn_id_card", op="contains")),),
        otherwise=Route(id="pii-fallback", model="qwen3-8b")))


def questions_app(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"),
                        signal_questions=str(tmp_path / "q.json"))
    return create_app(settings, transport=httpx.MockTransport(ok_upstream))


def edited_questions(*, drop_pii_type=None):
    cfg = json.loads(json.dumps(load_question_config()))
    if drop_pii_type is not None:
        del cfg["signals"]["pii"]["types"][drop_pii_type]
    return cfg


def _active_pii_tree(tmp_path):
    app = questions_app(tmp_path)
    client = TestClient(app)
    create_tree(client, "PII tree", pii_signal_root(), tree_id="pii-tree")
    enable(client, "pii-tree")
    return app, client


def test_signal_questions_rejects_dropping_a_value_the_active_tree_needs(
        tmp_path):
    """A prospective config must not silently break the running tree."""
    app, client = _active_pii_tree(tmp_path)
    before = online_revision(app)

    response = client.put("/admin/signal-questions",
                          json={"config": edited_questions(
                              drop_pii_type="cn_id_card")})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "signal_value_unavailable" in {issue["code"] for issue in detail}
    issue = next(i for i in detail if i["code"] == "signal_value_unavailable")
    assert set(issue) == {"code", "zh", "en", "node_id", "level"}
    assert issue["level"] == "hard" and issue["node_id"] == "pii-block"
    # rejected BEFORE applying/persisting/publishing: nothing moved
    assert online_revision(app) == before
    assert not (tmp_path / "q.json").exists()
    conn = connect(app.state.settings.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM config_revisions WHERE kind = 'questions'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM changes WHERE kind = 'signal_questions'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_signal_questions_accepts_dropping_an_unreferenced_value(tmp_path):
    app, client = _active_pii_tree(tmp_path)
    before = online_revision(app)

    response = client.put("/admin/signal-questions",
                          json={"config": edited_questions(
                              drop_pii_type="email_address")})

    assert response.status_code == 200, response.text
    assert (tmp_path / "q.json").exists()
    # the write republished the snapshot with the new question revision
    assert online_revision(app) != before


def test_signal_questions_without_an_active_tree_is_accepted(tmp_path):
    app = questions_app(tmp_path)
    client = TestClient(app)

    response = client.put("/admin/signal-questions",
                          json={"config": edited_questions(
                              drop_pii_type="cn_id_card")})

    assert response.status_code == 200, response.text
    assert (tmp_path / "q.json").exists()


def test_saved_question_config_boots_with_the_active_tree(tmp_path,
                                                          monkeypatch):
    """No "write accepted / restart rejected": boot judges the same config."""
    app, client = _active_pii_tree(tmp_path)
    response = client.put("/admin/signal-questions",
                          json={"config": edited_questions(
                              drop_pii_type="email_address")})
    assert response.status_code == 200, response.text
    saved = json.loads((tmp_path / "q.json").read_text(encoding="utf-8"))

    import router.api.app as app_module

    class LoadedQuestionsTagger:
        name = "fake-jev"

        def __init__(self):
            self.questions = saved

        def info(self):
            return {"engine": "fake-jev"}

        async def tag(self, request):        # pragma: no cover - boot only
            raise AssertionError("not called at boot")

        async def aclose(self):
            pass

    monkeypatch.setattr(app_module, "_make_tagger",
                        lambda settings, kw: LoadedQuestionsTagger())
    restarted = create_app(
        Settings(db_path=str(tmp_path / "t.db"),
                 signal_questions=str(tmp_path / "q.json")),
        transport=httpx.MockTransport(ok_upstream))
    assert restarted.state.snapshot.tree_id == "pii-tree"
