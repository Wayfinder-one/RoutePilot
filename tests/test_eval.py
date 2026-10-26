"""Admin evaluation (labels + replay) through the shared RoutingRuntime.

Task 10 Step 4: both modes call ``app.state.runtime.route``; replay restores the
HISTORICAL effective snapshot from ``config_revisions(kind='snapshot')`` and
re-runs the original request text — it never reuses the recorded signal pack and
never silently substitutes today's tree.  Records that cannot be replayed are
listed in ``non_replayable`` with a concrete reason and do not enter the
accuracy denominator.
"""

import asyncio
import json

from router.pipeline.runtime import RoutingRuntime
from router.store.db import connect
from tests.unified_tree_helpers import (
    FakeDecisionRunner,
    activate_default_tree,
    activate_tree,
    active_tree,
    make_app,
    nl_tree,
    update_tree,
)

TEXT_WEATHER = "今天天气怎么样"
TEXT_MEDICAL = "我父亲病历上写着需要长期治疗，如何权衡方案？"


def chat(h, text):
    return h.client.post("/v1/chat/completions",
                         json={"messages": [{"role": "user", "content": text}]})


def flush(app):
    asyncio.run(app.state.requestlog.flush())


def post_eval(h, **body):
    response = h.client.post("/admin/eval", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def persisted_report(app):
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute(
            "SELECT report FROM evals ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return json.loads(row["report"]) if row else None


# --------------------------------------------------------------------------- #
# labels: every case goes through the runtime and is echoed in ``cases``
# --------------------------------------------------------------------------- #
def test_eval_labels_uses_the_runtime_and_keeps_every_case(tmp_path):
    h = make_app(tmp_path)
    activate_default_tree(h.client)

    body = post_eval(h, mode="labels", labels=[
        {"id": "fallback", "text": TEXT_WEATHER, "expect_model": "qwen3-8b"},
        {"id": "medical", "text": TEXT_MEDICAL,
         "expect_model": "qwen3-8b-medical"},
        {"id": "wrong", "text": TEXT_WEATHER, "expect_model": "qwen3-32b"},
    ])

    assert body["mode"] == "labels"
    assert body["total"] == 3 and body["matched"] == 2
    assert body["rate"] == 2 / 3
    assert body["matrix"]
    assert [m["id"] for m in body["mismatches"]] == ["wrong"]

    # cases: one row per label, not only the mismatches
    cases = body["cases"]
    assert [case["id"] for case in cases] == ["fallback", "medical", "wrong"]
    by_id = {case["id"]: case for case in cases}
    assert by_id["fallback"]["got"] == "qwen3-8b"
    assert by_id["medical"]["got"] == "qwen3-8b-medical"
    assert by_id["wrong"]["got"] == "qwen3-8b"
    snapshot = h.app.state.snapshot
    for case in cases:
        assert case["tree_id"] == snapshot.tree_id
        assert case["tree_revision"] == snapshot.tree_revision
        assert case["snapshot_revision"] == snapshot.snapshot_revision

    # the persisted report keeps the cases array (real-gold reviews read it)
    stored = persisted_report(h.app)
    assert stored["cases"] == cases


# --------------------------------------------------------------------------- #
# replay: historical snapshot + original text, never today's tree
# --------------------------------------------------------------------------- #
def test_eval_replay_restores_the_historical_snapshot(tmp_path):
    h = make_app(tmp_path)
    activate_default_tree(h.client)
    historical = h.app.state.snapshot
    chat(h, TEXT_WEATHER)
    chat(h, TEXT_MEDICAL)
    flush(h.app)

    # Today's tree now routes the fallback weather request to another model.
    tree = active_tree(h.client)
    root = tree["root"]
    root["otherwise"]["model"] = "qwen3-32b"
    update_tree(h.client, tree["id"], tree["name"], root)
    assert h.app.state.snapshot.snapshot_revision != historical.snapshot_revision

    body = post_eval(h, mode="replay", days=7)
    assert body["mode"] == "replay" and body["source"] == "memory"
    assert body["total"] == 2 and body["matched"] == 2     # historical tree won
    assert body["mismatches"] == []
    assert body["non_replayable"] == []
    cases = body["cases"]
    assert len(cases) == 2 and all(case["replayable"] is True for case in cases)
    for case in cases:
        # the recorded (historical) revisions, not the current republished ones
        assert case["tree_revision"] == historical.tree_revision
        assert case["snapshot_revision"] == historical.snapshot_revision
        assert case["got"] == case["expect"]
    assert persisted_report(h.app)["cases"] == cases


def test_eval_replay_classifies_unreplayable_records_with_reasons(tmp_path):
    h = make_app(tmp_path)
    activate_default_tree(h.client)
    for text in (TEXT_WEATHER, TEXT_MEDICAL, TEXT_WEATHER, TEXT_MEDICAL,
                 TEXT_WEATHER, TEXT_MEDICAL, TEXT_WEATHER):
        chat(h, text)
    flush(h.app)
    records = list(h.app.state.requestlog.recent)     # oldest -> newest
    assert len(records) == 7

    records[0]["text"] = ""                                    # missing text
    records[1].pop("headers")                                  # old-style record
    records[2].pop("n_messages")                               # old-style record
    records[3]["snapshot_revision"] = None                     # no pointer
    records[4]["snapshot_revision"] = "f" * 64                 # unrecorded sha
    records[5]["tagger_info"] = {"engine": "jev", "model": "other"}
    records[6].pop("tagger_info")                              # no evidence

    body = post_eval(h, mode="replay", days=7)
    # nothing was executable -> empty denominator, every row classified
    assert body["total"] == 0 and body["matched"] == 0
    assert body["rate"] == 0.0
    assert body["mismatches"] == []
    assert len(body["cases"]) == 7
    assert len(body["non_replayable"]) == 7
    reasons = [case["reason"] for case in body["cases"]]
    assert reasons == ["missing_text", "missing_request_headers",
                       "missing_request_n_messages", "missing_snapshot_revision",
                       "missing_snapshot_content", "tagger_model_mismatch",
                       "missing_tagger_evidence"]
    for case in body["non_replayable"]:
        assert case["replayable"] is False and case["got"] is None


def test_eval_replay_refuses_a_changed_decision_runner(tmp_path):
    runner = FakeDecisionRunner({"fast": 1.0, "deep": 0.0})
    h = make_app(tmp_path, decision_runner=runner)
    activate_tree(h.client, "NL tree", nl_tree(), tree_id="nl-tree")
    chat(h, "帮我处理一个难题")
    flush(h.app)

    class OtherRunner:
        def info(self):
            return {"engine": "fake-decision", "model_name": "fake-choice",
                    "revision": "different-version"}

        async def choose(self, request):        # pragma: no cover - not reached
            raise AssertionError("model version changed; replay must not run")

    h.app.state.runner = OtherRunner()
    h.app.state.runtime = RoutingRuntime(h.app.state.tagger, h.app.state.runner)

    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 0
    assert len(body["cases"]) == 1
    assert body["cases"][0]["replayable"] is False
    assert body["cases"][0]["reason"] == "decision_runner_mismatch"


# --------------------------------------------------------------------------- #
# privacy / bad input
# --------------------------------------------------------------------------- #
def test_eval_replay_report_keeps_no_trace_signals_or_text(tmp_path):
    h = make_app(tmp_path)
    activate_default_tree(h.client)
    chat(h, TEXT_MEDICAL)
    flush(h.app)
    post_eval(h, mode="replay", days=7)

    stored = persisted_report(h.app)
    dumped = json.dumps(stored, ensure_ascii=False)
    # replay cases are record metadata only: no request text, trace or signals
    assert TEXT_MEDICAL not in dumped
    assert "trace" not in dumped and "signals" not in dumped
    for case in stored["cases"]:
        assert "text" not in case


def test_eval_bad_mode_is_422(tmp_path):
    h = make_app(tmp_path)
    assert h.client.post("/admin/eval", json={"mode": "nope"}).status_code == 422
