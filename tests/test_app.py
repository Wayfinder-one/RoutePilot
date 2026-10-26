"""Startup assembly under the unified routing tree (Task 9 Step 6).

The four-station pipeline is retired: ``create_app`` boots on an EMPTY tree
table (no active tree is a legal state; ``/v1`` answers 503 ``no_active_tree``)
and refuses to start only when the *active* tree is unhealthy.  These tests pin
that contract, including configurations written straight into the database
(bypassing the save-time validation) and the DecisionRunner requirement for
natural-language trees.
"""

import json

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.pipeline.tree import Branch, NLChoice, NLOption, Route, When
from router.reasoning import validate_model_profile
from router.store.db import connect, migrate
from router.store.repositories import seed_default
from router.store.routing_trees import RoutingTreeRepo


def client(tmp_path):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "t.db"))))


def prepared_db(tmp_path):
    """A migrated + seeded database (models/providers/keywords/time periods)."""
    path = str(tmp_path / "t.db")
    conn = connect(path)
    migrate(conn)
    seed_default(conn)
    conn.close()
    return path


def install_active_tree(path, tree_id, root):
    """Write one ENABLED tree straight into the DB (bypasses API validation)."""
    conn = connect(path)
    try:
        repo = RoutingTreeRepo(conn)
        repo.create(tree_id, tree_id, root)
        repo.enable(tree_id)
    finally:
        conn.close()


def declare_model_profile(path, model_id, reasoning):
    profile = validate_model_profile({"reasoning": reasoning}, "openai")
    conn = connect(path)
    try:
        conn.execute("UPDATE models SET profile = ? WHERE id = ?",
                     (json.dumps(profile), model_id))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# public endpoints
# --------------------------------------------------------------------------- #
def test_healthz(tmp_path):
    r = client(tmp_path).get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_console_mounted(tmp_path):
    r = client(tmp_path).get("/console/index.html")
    assert r.status_code in (200, 404)   # 404 until Task 18 adds the pages


# --------------------------------------------------------------------------- #
# boot with no active tree (legal state) / active-tree health gate
# --------------------------------------------------------------------------- #
def test_fresh_install_boots_with_no_active_tree(tmp_path):
    """No configured tree is legal: the app boots and ``/v1`` answers 503
    before tagging rather than inventing a default route."""
    app = create_app(Settings(db_path=str(tmp_path / "t.db")))
    c = TestClient(app)
    assert app.state.snapshot.tree is None
    assert app.state.snapshot.tree_id is None

    listing = c.get("/admin/trees").json()
    assert listing["trees"] == []
    assert listing["snapshot_revision"] == app.state.snapshot.snapshot_revision

    r = c.post("/v1/chat/completions",
               json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 503
    assert r.json()["error"]["message"] == "no_active_tree"


def test_startup_refuses_an_unhealthy_stored_tree(tmp_path):
    """An ACTIVE tree the engine cannot execute must not reach the hot path:
    the service refuses to start instead of 502-ing every request."""
    path = prepared_db(tmp_path)
    install_active_tree(path, "bad-tree", Branch(
        id="root",
        children=(Route(id="only", model="qwen3-8b",
                        when=When(signal="domain", value="health")),)))
    with pytest.raises(RuntimeError) as exc:
        create_app(Settings(db_path=path))
    assert "missing_fallback" in str(exc.value)


def test_startup_refuses_an_active_nl_tree_without_a_decision_runner(tmp_path):
    """A natural-language tree needs a DecisionRunner; heuristic mode has none,
    so booting such an active tree is a hard configuration error."""
    path = prepared_db(tmp_path)
    install_active_tree(path, "nl-tree", NLChoice(
        id="root", instructions="choose",
        options=(NLOption("a", "first lane", Route("a-route", "qwen3-8b")),
                 NLOption("b", "second lane", Route("b-route", "qwen3-32b"))),
        otherwise=Route("fallback", "qwen3-8b")))
    with pytest.raises(RuntimeError) as exc:
        create_app(Settings(db_path=path))       # heuristic => runner None
    assert "decision_runner_unavailable" in str(exc.value)


def test_startup_judges_reasoning_written_straight_into_the_db(tmp_path):
    """The boot check reads the same model catalog as the enable health check:
    a route selecting an undeclared reasoning level must be caught at boot when
    it bypassed the save-time validation (direct DB edit / older version)."""
    path = prepared_db(tmp_path)
    declare_model_profile(path, "qwen3-8b",
                          {"efforts": {"low": "low"}, "default": "low"})
    install_active_tree(path, "reasoning-bad", Route(
        id="root", model="qwen3-8b", reasoning="high"))
    with pytest.raises(RuntimeError) as exc:
        create_app(Settings(db_path=path))
    assert "reasoning_unsupported" in str(exc.value)


def test_startup_accepts_a_declared_reasoning_level(tmp_path):
    """The boot-time check must not over-block: a declared level is legal and
    the compiled snapshot keeps it."""
    path = prepared_db(tmp_path)
    declare_model_profile(path, "qwen3-8b",
                          {"efforts": {"low": "low", "high": "high"},
                           "default": "low"})
    install_active_tree(path, "reasoning-ok", Route(
        id="root", model="qwen3-8b", reasoning="high"))
    app = create_app(Settings(db_path=path))     # must not raise
    assert app.state.snapshot.tree_id == "reasoning-ok"
    assert app.state.snapshot.tree.root.reasoning == "high"
