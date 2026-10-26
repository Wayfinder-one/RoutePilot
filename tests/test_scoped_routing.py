"""Hot-path signal trimming: the snapshot's tree scope drives /v1 and /admin/try.

Migrated to the unified routing tree (Task 11).  ``ROUTER_JEV_TRIM=1`` (the
``Settings.jev_trim`` default) means the runtime passes the immutable snapshot's
compiled ``SignalScope`` to the tagger; ``/v1`` (online snapshot) and
``/admin/try`` (the tried tree's own snapshot) both go through the SAME
``RoutingRuntime``, so neither has a separate "full pack" special path:

* the scope is ``compute_tree_scope`` over the compiled tree: model ``when`` /
  ``weights`` references only, rule signals excluded, PII trimmed per subtype;
* a pure natural-language tree references no ordinary signal -> the tagger is
  never called;
* a rule-only tree still tags once, with an empty MODEL scope (rules are local);
* ``/admin/try`` uses the scope of the tree it was asked to try;
* with trimming disabled the runtime passes ``scope=None`` (full pack);
* rewriting the active tree republishes the snapshot, so the NEXT request uses
  the new scope.
"""

from __future__ import annotations

from router.pipeline.scope import compute_tree_scope
from router.pipeline.tree import Branch, Route, When, node_to_dict
from router.signals.registry import SIGNALS
from router.signals.tagger import TagResult

from tests.unified_tree_helpers import (
    FakeDecisionRunner,
    activate_default_tree,
    activate_tree,
    create_tree,
    make_app,
    nl_tree,
    set_tagger,
    update_tree,
)


#: Values that do not trigger the shipped default tree's safety/route edges, so
#: the scope assertions are the only thing under test.
_SAFE_VALUES = {"jailbreak": "benign", "domain": "other",
                "complexity": "easy", "fact_check": "no_check",
                "modality": "text", "user_feedback": "satisfied"}


def fake_signal_pack() -> dict:
    """A healthy, non-triggering ordinary pack covering every registered signal."""
    pack = {}
    for key, spec in SIGNALS.items():
        if spec.multi:
            value = []
        elif key in _SAFE_VALUES:
            value = _SAFE_VALUES[key]
        elif spec.values:
            value = spec.values[0][0]
        else:                                  # free-form rule signals
            value = ""
        pack[key] = {"value": value,
                     "confidence": 1.0 if spec.kind == "rule" else 0.9,
                     "is_rule": spec.kind == "rule"}
    return pack


class CapturingTagger:
    """The tagger seam: records every TagRequest the shared runtime builds."""

    name = "fake-jev"

    def __init__(self):
        self.requests = []

    async def tag(self, request):
        self.requests.append(request)
        return TagResult(signals={
            key: dict(value) for key, value in fake_signal_pack().items()})

    async def aclose(self):
        pass


def scoped_harness(tmp_path, *, decision_runner=None, jev_trim=True):
    harness = make_app(tmp_path, decision_runner=decision_runner,
                       jev_trim=jev_trim)
    tagger = CapturingTagger()
    set_tagger(harness.app, tagger)
    return harness, tagger


def chat(client, text="hello"):
    return client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": text}]})


def rule_only_root():
    return node_to_dict(Branch(
        id="rule-root",
        children=(Route(id="keyword-emergency", model="qwen3-8b",
                        when=When(signal="keyword", value="医疗急症",
                                  op="contains")),),
        otherwise=Route(id="rule-fallback", model="qwen3-8b")))


def domain_only_root():
    return node_to_dict(Branch(
        id="domain-root",
        children=(Route(id="domain-health", model="qwen3-8b",
                        when=When(signal="domain", value="health",
                                  conf_min=0.4)),),
        otherwise=Route(id="domain-fallback", model="qwen3-8b")))


def fact_check_root():
    return node_to_dict(Branch(
        id="fc-root",
        children=(Route(id="fc-route", model="qwen3-8b",
                        when=When(signal="fact_check", value="needs_check",
                                  conf_min=0.4)),),
        otherwise=Route(id="fc-fallback", model="qwen3-8b")))


# ① trimming on: the hot path passes the snapshot's compiled tree scope
def test_hot_path_passes_the_snapshot_tree_scope(tmp_path):
    harness, tagger = scoped_harness(tmp_path)
    activate_default_tree(harness.client)
    snapshot = harness.app.state.snapshot

    assert harness.app.state.settings.jev_trim is True
    assert snapshot.jev_trim is True
    assert snapshot.scope.trimmed
    response = chat(harness.client)
    assert response.status_code == 200, response.text

    request = tagger.requests[-1]
    assert request.scope == snapshot.scope
    assert request.scope == compute_tree_scope(snapshot.tree.root)
    assert request.scope.signals == frozenset(
        {"jailbreak", "pii", "domain", "complexity"})
    assert request.scope.pii_types == frozenset({"cn_id_card"})


# ② a pure NL tree references no ordinary signal -> no tagging at all
def test_pure_nl_tree_never_calls_the_signal_tagger(tmp_path):
    runner = FakeDecisionRunner({"fast": 1.0, "deep": 0.0})
    harness, tagger = scoped_harness(tmp_path, decision_runner=runner)
    activate_tree(harness.client, "Pure NL", nl_tree(), tree_id="nl-tree")

    snapshot = harness.app.state.snapshot
    assert snapshot.tree_id == "nl-tree"
    assert snapshot.required_signals == frozenset()
    response = chat(harness.client)
    assert response.status_code == 200, response.text

    assert tagger.requests == []
    assert runner.call_count == 1


# ③ a rule-only tree tags once, but asks the model zero questions
def test_rule_only_tree_tags_once_with_an_empty_model_scope(tmp_path):
    harness, tagger = scoped_harness(tmp_path)
    activate_tree(harness.client, "Rule only", rule_only_root(),
                  tree_id="rule-only")
    snapshot = harness.app.state.snapshot
    assert snapshot.required_signals == frozenset({"keyword"})

    response = chat(harness.client)
    assert response.status_code == 200, response.text

    assert len(tagger.requests) == 1
    request = tagger.requests[-1]
    assert request.scope == snapshot.scope
    assert request.scope.trimmed
    assert request.scope.signals == frozenset()
    assert request.scope.pii_types == frozenset()


# ④ /admin/try uses the tried tree's scope through the shared runtime
def test_admin_try_uses_the_tried_trees_own_scope(tmp_path):
    harness, tagger = scoped_harness(tmp_path)
    activate_default_tree(harness.client)
    online = harness.app.state.snapshot
    create_tree(harness.client, "Closed fc", fact_check_root(),
                tree_id="closed-fc")
    tagger.requests.clear()

    response = harness.client.post(
        "/admin/try", json={"tree_id": "closed-fc", "text": "hello"})
    assert response.status_code == 200, response.text

    assert len(tagger.requests) == 1
    request = tagger.requests[-1]
    assert request.scope.trimmed
    assert request.scope.signals == frozenset({"fact_check"})
    assert request.scope.pii_types == frozenset()
    # trying never reads or replaces the online snapshot
    assert harness.app.state.snapshot is online
    assert harness.app.state.snapshot.snapshot_revision == online.snapshot_revision


# ⑤ trimming off: the runtime passes the full-pack semantics (scope=None)
def test_trimming_off_passes_the_full_pack(tmp_path):
    harness, tagger = scoped_harness(tmp_path, jev_trim=False)
    activate_default_tree(harness.client)
    # the snapshot still carries the compiled scope; only its USE is disabled
    assert harness.app.state.snapshot.scope.trimmed

    response = chat(harness.client)
    assert response.status_code == 200, response.text
    assert tagger.requests[-1].scope is None


# ⑥ a hot tree edit republishes the snapshot (and therefore the scope)
def test_hot_tree_edit_republishes_the_scope(tmp_path):
    harness, tagger = scoped_harness(tmp_path)
    tree = activate_tree(harness.client, "Domain only", domain_only_root(),
                         tree_id="domain-only")
    before = harness.app.state.snapshot
    assert before.scope.signals == frozenset({"domain"})
    assert chat(harness.client).status_code == 200
    assert tagger.requests[-1].scope == before.scope

    root = tree["root"]
    root["children"].append(node_to_dict(Route(
        id="fc-route", model="qwen3-8b",
        when=When(signal="fact_check", value="needs_check", conf_min=0.4))))
    update_tree(harness.client, "domain-only", "Domain only", root)

    after = harness.app.state.snapshot
    assert after.snapshot_revision != before.snapshot_revision
    assert after.scope.signals == frozenset({"domain", "fact_check"})
    assert chat(harness.client).status_code == 200
    assert tagger.requests[-1].scope == after.scope

    root["children"].pop()
    update_tree(harness.client, "domain-only", "Domain only", root)
    assert chat(harness.client).status_code == 200
    assert "fact_check" not in tagger.requests[-1].scope.signals
