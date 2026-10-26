"""Shared helpers for the unified-routing-tree /v1, /admin/try and reasoning tests.

Task 9/10 test migration (impl-tests).  The three migrated test modules --
``tests/test_v1.py``, ``tests/test_try.py`` and
``tests/test_routes_v1_reasoning.py`` -- used to build their fixtures by seeding
the legacy four-station pipeline and lowering its confidence floors through
``PUT /admin/pipeline``.  The unified routing tree replaces that configuration
channel (design doc ``2026-10-26-unified-routing-tree-design.md``): tests now
create a tree through the real ``/admin/trees`` API and enable it, exactly like
an operator would.  No helper in this module writes the database directly and
none depends on a global pytest fixture (every builder takes its inputs
explicitly).

Approved test seams consumed here (Lead ruling, 2026-10-26):

* ``create_app(settings, *, transport=None, decision_runner=None)`` -- the
  ``decision_runner`` keyword injects a deterministic fake for ``/admin/try`` and
  ``/v1`` natural-language nodes; the app exposes it as ``app.state.runner``.
* ``POST /admin/trees {id?,name,root}`` -> 201 ``{"tree", "snapshot_revision"}``;
  ``POST /admin/trees/{id}/enable`` -> 200 ``{"trees", "snapshot_revision"}``.
* ``POST /admin/try {tree_id,text,headers?,n_messages?}`` -> 200 with
  ``outcome/signals/trace/input/signal_ms/decision_ms/nl_calls/tree_id/
  tree_revision/snapshot_revision/questions_sha256/tagger_info``.
* ``RoutingRuntime(tagger, runner)`` is the shared runtime; swapping
  ``app.state.tagger`` in a test therefore also requires rebuilding
  ``app.state.runtime`` -- :func:`set_tagger` does both.

The builders return the canonical **JSON root** (``node_to_dict`` output) so a
test can post it to ``/admin/trees`` verbatim, or mutate one field (for example
``root["otherwise"]["reasoning"]``) and ``PUT`` it back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.config import Settings
from router.decision.runner import ChoiceResult
from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Route,
    When,
    node_to_dict,
)

#: Stable id/name of the tree :func:`activate_default_tree` installs, so a test
#: can assert ``snapshot.tree_id == DEFAULT_TREE_ID`` without bookkeeping.
DEFAULT_TREE_ID = "default-tree"
DEFAULT_TREE_NAME = "Default signal tree"


# --------------------------------------------------------------------------- #
# upstream + harness
# --------------------------------------------------------------------------- #
def ok_upstream(request: httpx.Request) -> httpx.Response:
    """The default mock upstream: one non-streaming OpenAI-style answer."""
    return httpx.Response(200, json={
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}]})


@dataclass
class Harness:
    """One ``create_app`` instance plus its ``TestClient`` and captured calls.

    ``requests`` records every upstream HTTP attempt made through the default
    ``httpx.MockTransport`` (``{url, body, headers}``); tests that pass their own
    ``transport`` capture the requests themselves.
    """

    app: object
    client: TestClient
    requests: list[dict] = field(default_factory=list)

    def last_upstream_body(self) -> dict:
        assert self.requests, "no upstream request was made"
        return self.requests[-1]["body"]


def _captured(request: httpx.Request) -> dict:
    try:
        body = json.loads(request.content or b"{}")
    except (TypeError, ValueError):        # pragma: no cover - defensive
        body = {}
    return {"url": str(request.url), "body": body,
            "headers": dict(request.headers)}


def make_app(tmp_path, handler=None, decision_runner=None, *,
             transport=None, settings=None, **settings_overrides) -> Harness:
    """Build one app + TestClient for a temp database.

    ``handler`` / ``transport`` are mutually exclusive test seams for the
    upstream: by default a capturing ``httpx.MockTransport`` around
    :func:`ok_upstream`; pass ``transport=`` for an async/streaming transport the
    test drives itself (then ``Harness.requests`` stays empty).  ``settings``
    passes a fully built :class:`Settings`; ``settings_overrides`` are applied to
    the default temp-database settings (e.g. ``signal_questions=...``).
    """
    if settings is None:
        settings = Settings(db_path=str(tmp_path / "t.db"), **settings_overrides)
    captured: list[dict] = []
    if transport is None:
        base = handler if handler is not None else ok_upstream

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(_captured(request))
            return base(request)

        transport = httpx.MockTransport(capture)
    app = create_app(settings, transport=transport,
                     decision_runner=decision_runner)
    return Harness(app=app, client=TestClient(app), requests=captured)


# --------------------------------------------------------------------------- #
# /admin session (open under the Settings() dataclass default; login when not)
# --------------------------------------------------------------------------- #
def login(client: TestClient, email: str | None = None,
          password: str | None = None) -> dict:
    """Log into the console and return ``{"Authorization": "Bearer ..."}``."""
    settings = client.app.state.settings
    response = client.post("/admin/login", json={
        "email": email or settings.admin_email,
        "password": password or settings.admin_password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def headers(client: TestClient) -> dict:
    """Auth headers every ``/admin`` helper uses.

    ``Settings.user_auth`` defaults to False (tests/legacy dev): no login is
    needed.  With ``user_auth=True`` the default seeded admin logs in.
    """
    if getattr(client.app.state.settings, "user_auth", False):
        return login(client)
    return {}


def _call(client: TestClient, method: str, path: str, payload=None):
    return client.request(method, path, json=payload, headers=headers(client))


# --------------------------------------------------------------------------- #
# tree management API (never writes the DB directly)
# --------------------------------------------------------------------------- #
def list_trees(client: TestClient) -> dict:
    """``GET /admin/trees`` -> ``{"trees": [...], "snapshot_revision": ...}``."""
    response = _call(client, "GET", "/admin/trees")
    assert response.status_code == 200, response.text
    return response.json()


def fetch_tree(client: TestClient, tree_id: str) -> dict:
    """``GET /admin/trees/{tree_id}`` -> the full tree envelope."""
    response = _call(client, "GET", f"/admin/trees/{tree_id}")
    assert response.status_code == 200, response.text
    return response.json()["tree"]


def create_tree(client: TestClient, name: str, root: dict | object,
                tree_id: str | None = None) -> dict:
    """``POST /admin/trees`` (always creates a CLOSED tree) -> envelope."""
    payload: dict = {"name": name, "root": _root_payload(root)}
    if tree_id is not None:
        payload["id"] = tree_id
    response = _call(client, "POST", "/admin/trees", payload)
    assert response.status_code == 201, response.text
    return response.json()["tree"]


def update_tree(client: TestClient, tree_id: str, name: str,
                root: dict | object) -> dict:
    """``PUT /admin/trees/{tree_id}`` -> ``{tree, snapshot_revision, warnings}``."""
    response = _call(client, "PUT", f"/admin/trees/{tree_id}",
                     {"name": name, "root": _root_payload(root)})
    assert response.status_code == 200, response.text
    return response.json()


def enable_tree(client: TestClient, tree_id: str) -> dict:
    """``POST /admin/trees/{tree_id}/enable`` -> ``{trees, snapshot_revision}``."""
    response = _call(client, "POST", f"/admin/trees/{tree_id}/enable")
    assert response.status_code == 200, response.text
    return response.json()


def disable_tree(client: TestClient, tree_id: str) -> dict:
    """``POST /admin/trees/{tree_id}/disable`` -> ``{trees, snapshot_revision}``."""
    response = _call(client, "POST", f"/admin/trees/{tree_id}/disable")
    assert response.status_code == 200, response.text
    return response.json()


def active_tree(client: TestClient) -> dict | None:
    """The single enabled tree row (``None`` when all trees are closed)."""
    trees = [row for row in list_trees(client)["trees"] if row["enabled"]]
    assert len(trees) <= 1, trees
    return trees[0] if trees else None


def activate_tree(client: TestClient, name: str, root: dict | object,
                  tree_id: str | None = None) -> dict:
    """Create a closed tree, enable it and return the enabled envelope."""
    tree = create_tree(client, name, root, tree_id=tree_id)
    body = enable_tree(client, tree["id"])
    assert [row["id"] for row in body["trees"] if row["enabled"]] == [tree["id"]]
    snapshot = client.app.state.snapshot
    assert snapshot.tree_id == tree["id"]
    assert body["snapshot_revision"] == snapshot.snapshot_revision
    return fetch_tree(client, tree["id"])


def activate_default_tree(client: TestClient, *, conf_min: float = 0.4,
                          fallback_reasoning: str | None = None) -> dict:
    """Install :func:`default_signal_tree` as the online tree."""
    return activate_tree(
        client, DEFAULT_TREE_NAME,
        default_signal_tree(conf_min=conf_min,
                            fallback_reasoning=fallback_reasoning),
        tree_id=DEFAULT_TREE_ID)


def set_active_fallback_reasoning(client: TestClient,
                                  level: str) -> dict:
    """Set the ``otherwise`` route's reasoning level on the online tree.

    Equivalent to the retired ``set_leaf_reasoning`` helper of
    ``tests/test_routes_v1_reasoning.py``: the old helper edited the last
    (fallback) station leaf; the unified tree keeps that route as the root
    branch's explicit ``otherwise`` edge.  Saving the ACTIVE tree republishes
    the snapshot (design §8.1).
    """
    tree = active_tree(client)
    if tree is None:
        return activate_default_tree(client, fallback_reasoning=level)
    root = tree["root"]
    fallback = root.get("otherwise")
    assert isinstance(fallback, dict), "default tree root has no otherwise edge"
    fallback["reasoning"] = level
    update_tree(client, tree["id"], tree["name"], root)
    return fetch_tree(client, tree["id"])


# --------------------------------------------------------------------------- #
# tagger seam: the hot path reads app.state.runtime, not app.state.tagger
# --------------------------------------------------------------------------- #
def set_tagger(app, tagger):
    """Swap the tagger used by the hot path and rebuild the shared runtime.

    ``create_app`` constructs ``app.state.runtime`` once with the tagger it
    created; tests that wrap/instrument the tagger must rebuild it.  Assumes the
    Task-8 constructor ``RoutingRuntime(tagger, runner)``.
    """
    app.state.tagger = tagger
    from router.pipeline.runtime import RoutingRuntime
    app.state.runtime = RoutingRuntime(tagger, getattr(app.state, "runner", None))
    return tagger


# --------------------------------------------------------------------------- #
# deterministic decision runner
# --------------------------------------------------------------------------- #
class FakeDecisionRunner:
    """Deterministic ``DecisionRunner`` for ``nl_choice`` nodes.

    Each queued answer is consumed by one ``choose`` call: a ``ChoiceResult``,
    a plain ``{"key": probability}`` mapping (wrapped into ``ChoiceResult``), or
    an ``Exception`` (raised to exercise ``nl_error``).
    """

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = []

    def info(self) -> dict:
        return {"engine": "fake-decision", "model_name": "fake-choice",
                "revision": "fake-v1"}

    async def choose(self, request):
        self.calls.append(request)
        if not self._answers:
            raise AssertionError("unexpected decision call")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            return ChoiceResult(probabilities=dict(answer))
        return answer

    @property
    def call_count(self) -> int:
        return len(self.calls)


# --------------------------------------------------------------------------- #
# canonical trees
# --------------------------------------------------------------------------- #
def default_signal_tree(conf_min: float = 0.4,
                        fallback_reasoning: str | None = None) -> dict:
    """Unified tree equivalent to the retired four-station seeded pipeline.

    ``① 安全闸``   -- two ordered ``block`` children (jailbreak, PII id card).
    ``② 关键词``   -- ``keyword`` contains ``医疗急症`` -> medical model.
    ``③ 学科``     -- ``domain`` health / computer_science branches: health+hard
                      and health+easy route to the medical models, cs+hard to
                      deepseek-v3 (direct terminals, exactly like the old
                      station leaves).
    ``④ 兜底``     -- the root branch's explicit ``otherwise`` route.

    ``conf_min`` parameterises every model-signal confidence floor (the old
    tests lowered the seeded 0.70 to 0.4 because the heuristic tagger caps
    confidence at 0.5).  Node ids/shape follow the shipped
    ``configs/default_tree.json`` (the canonical signal-tree default) so the
    fixture and the product default cannot drift; the extra ``time_window``
    route is gated on the *unconfigured* period name ``节假日``: the tree
    exercises the rule-signal path and signal preparation, while routing stays
    deterministic (the seeded periods are 白天/夜间/周末).  A test that wants a
    real time-window hit configures that period name first.
    """
    safety_jailbreak = Block(
        id="safety-jailbreak",
        when=When(signal="jailbreak", value="jailbreak", conf_min=conf_min))
    safety_pii = Block(
        id="safety-pii-cn-id",
        when=When(signal="pii", value="cn_id_card", op="contains",
                  conf_min=conf_min))
    keyword_medical = Route(
        id="keyword-emergency", model="qwen3-8b-medical",
        when=When(signal="keyword", value="医疗急症", op="contains"))
    health_hard = Route(
        id="health-hard", model="qwen3-8b-medical",
        when=When(signal="complexity", value="hard", conf_min=conf_min))
    health_easy = Route(
        id="health-easy", model="qwen3-0.6b-medical",
        when=When(signal="complexity", value="easy", conf_min=conf_min))
    domain_health = Branch(
        id="health",
        when=When(signal="domain", value="health", conf_min=conf_min),
        children=(health_hard, health_easy))
    cs_hard = Route(
        id="cs-hard", model="deepseek-v3",
        when=When(signal="complexity", value="hard", conf_min=conf_min))
    domain_cs = Branch(
        id="computer-science",
        when=When(signal="domain", value="computer_science",
                  conf_min=conf_min),
        children=(cs_hard,))
    daytime = Route(
        id="daytime-route", model="qwen3-8b",
        when=When(signal="time_window", value="节假日", op="contains"))
    # Ordinary children are ordered: safety blocks first, then the keyword
    # shortcut, then the subject branches, then the daytime rule route.  The
    # explicit ``otherwise`` edge is the guaranteed final fallback (design §7.2).
    root = Branch(
        id="root",
        children=(safety_jailbreak, safety_pii, keyword_medical, domain_health,
                  domain_cs, daytime),
        otherwise=Route(id="fallback", model="qwen3-8b",
                        reasoning=fallback_reasoning))
    return node_to_dict(root)


def nl_tree() -> dict:
    """Two-level pure natural-language tree: 2 candidates, child NL, otherwise.

    Level 1 (``nl-root``): ``fast`` -> route qwen3-8b / ``deep`` -> a child
    ``nl_choice``.  Level 2 (``deep-nl``): ``code`` -> qwen3-32b /
    ``reason`` -> deepseek-v3.  Both levels have an explicit ``otherwise``
    (root: qwen3-8b).  No ordinary signal is referenced -> the runtime must skip
    signal tagging entirely and call the runner once per reached NL node.
    """
    deep = NLChoice(
        id="deep-nl", instructions="choose the deep lane",
        options=(
            NLOption(key="code", text="写代码与重构任务",
                     target=Route(id="code-route", model="qwen3-32b")),
            NLOption(key="reason", text="多步推理与论证任务",
                     target=Route(id="reason-route", model="deepseek-v3")),
        ),
        otherwise=Route(id="deep-fallback", model="qwen3-8b"))
    root = NLChoice(
        id="nl-root", instructions="choose a lane",
        options=(
            NLOption(key="fast", text="快速问答与闲聊",
                     target=Route(id="fast-route", model="qwen3-8b")),
            NLOption(key="deep", text="需要多步推理与代码的任务", target=deep),
        ),
        otherwise=Route(id="nl-fallback", model="qwen3-8b"))
    return node_to_dict(root)


def gated_nl_tree(gate: When) -> dict:
    """Mixed tree: one entered ``when``-gated NL node + explicit fallbacks.

    The root branch walks its single NL child (gated by ``gate``) and always
    ends at the root ``otherwise`` route; request without the gate signal value
    therefore routes to the fallback model without calling the runner at all.
    """
    nl = NLChoice(
        id="gated-nl", instructions="choose the gated lane", when=gate,
        options=(
            NLOption(key="strong", text="难题：多步推理",
                     target=Route(id="gated-strong", model="qwen3-32b")),
            NLOption(key="light", text="简单问答",
                     target=Route(id="gated-light", model="qwen3-8b")),
        ),
        otherwise=Route(id="gated-nl-fallback", model="qwen3-8b"))
    root = Branch(
        id="gate-root", children=(nl,),
        otherwise=Route(id="gate-fallback", model="qwen3-8b"))
    return node_to_dict(root)


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def _root_payload(root: dict | object) -> dict:
    """Accept either the canonical dict or a frozen node dataclass."""
    if isinstance(root, dict):
        return root
    return node_to_dict(root)


__all__ = [
    "DEFAULT_TREE_ID",
    "DEFAULT_TREE_NAME",
    "FakeDecisionRunner",
    "Harness",
    "activate_default_tree",
    "activate_tree",
    "active_tree",
    "create_tree",
    "default_signal_tree",
    "disable_tree",
    "enable_tree",
    "fetch_tree",
    "gated_nl_tree",
    "headers",
    "list_trees",
    "login",
    "make_app",
    "nl_tree",
    "ok_upstream",
    "set_active_fallback_reasoning",
    "set_tagger",
    "update_tree",
]
