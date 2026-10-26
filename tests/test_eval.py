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

import pytest

from router.decision.runner import ChoiceResult
from router.pipeline.runtime import RoutingRuntime
from router.signals.jev import load_question_config, questions_sha256
from router.signals.registry import SIGNALS
from router.signals.tagger import TagResult
from router.store.db import connect
from tests.unified_tree_helpers import (
    FakeDecisionRunner,
    activate_default_tree,
    activate_tree,
    active_tree,
    make_app,
    nl_tree,
    set_tagger,
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


# --------------------------------------------------------------------------- #
# replay evidence: model identity only, not the hot-editable questions/paths
# --------------------------------------------------------------------------- #
#: Safe fake values: none of the shipped default tree's edges trigger, so a
#: replayed request routes to the same fallback both online and in replay.
_SAFE_SIGNAL_VALUES = {
    "jailbreak": "benign", "domain": "other", "complexity": "easy",
    "fact_check": "no_check", "modality": "text",
    "user_feedback": "satisfied"}


def _fake_signal_pack() -> dict:
    pack = {}
    for key, spec in SIGNALS.items():
        if spec.multi:
            value = []
        elif key in _SAFE_SIGNAL_VALUES:
            value = _SAFE_SIGNAL_VALUES[key]
        elif spec.values:
            value = spec.values[0][0]
        else:
            value = ""
        pack[key] = {"value": value, "confidence": 0.9,
                     "is_rule": spec.kind == "rule"}
    return pack


class SharedEvidence:
    """Mutable JEV-like evidence shared by tagger and decision runner.

    Production contract: ``JevDecisionRunner.info() == tagger.info()``; the
    tagger info embeds the hot-editable questions fingerprint and the absolute
    deployment path — both are NOT model identity and must not block replay.
    """

    def __init__(self):
        self.questions = load_question_config()
        self.model_name = "fake-jev"
        self.model_dir = "/opt/models/fake-jev-v1"
        self.temperature = 0.7
        self.weights_sha256 = "weights-v1"

    def info(self, questions=None) -> dict:
        return {"engine": "fake-jev",
                "model_name": self.model_name,
                "model_dir": self.model_dir,
                "temperature": self.temperature,
                "weights_sha256": self.weights_sha256,
                "questions_sha256": questions_sha256(
                    self.questions if questions is None else questions)}


class FakeJevTagger:
    name = "fake-jev"

    def __init__(self, evidence: SharedEvidence):
        self.evidence = evidence
        self.requests = []

    @property
    def questions(self):
        return self.evidence.questions

    def set_questions(self, cfg):
        self.evidence.questions = cfg

    def info(self):
        return self.evidence.info()

    async def tag(self, request):
        self.requests.append(request)
        # Mirror JevTagger: the result carries the FROZEN per-request evidence
        # (snapshot questions included), never a post-hoc tagger.info() read.
        questions = (request.questions if request.questions is not None
                     else self.questions)
        return TagResult(
            signals={key: dict(value)
                     for key, value in _fake_signal_pack().items()},
            questions_sha256=questions_sha256(questions),
            tagger_info=self.evidence.info(questions))

    async def aclose(self):
        pass


class EvidenceRunner:
    """Deterministic runner whose version evidence mirrors the tagger's."""

    def __init__(self, evidence: SharedEvidence, *answers):
        self.evidence = evidence
        self._answers = list(answers)
        self.calls = []

    def info(self):
        return self.evidence.info()

    async def choose(self, request):
        self.calls.append(request)
        if not self._answers:
            raise AssertionError("unexpected decision call")
        answer = self._answers.pop(0)
        if isinstance(answer, dict):
            return ChoiceResult(probabilities=dict(answer))
        return answer


def evidence_app(tmp_path, *runner_answers):
    """App with isolated questions file + JEV-like tagger/runner evidence."""
    evidence = SharedEvidence()
    runner = EvidenceRunner(evidence, *runner_answers)
    h = make_app(tmp_path, decision_runner=runner,
                 signal_questions=str(tmp_path / "signal_questions.json"))
    tagger = FakeJevTagger(evidence)
    set_tagger(h.app, tagger)
    return h, evidence, tagger


def _edited_questions_cfg() -> dict:
    cfg = json.loads(json.dumps(load_question_config()))
    cfg["signals"]["jailbreak"]["true"] += "（严格判定）"
    return cfg


def test_eval_replay_replays_pure_nl_records_with_model_evidence(tmp_path):
    """A pure-NL record stores no tagger_info by design: replay must not fail."""
    h, _evidence, _tagger = evidence_app(
        tmp_path, {"fast": 1.0, "deep": 0.0}, {"fast": 1.0, "deep": 0.0})
    activate_tree(h.client, "NL tree", nl_tree(), tree_id="nl-tree")
    chat(h, "帮我处理一个难题")
    flush(h.app)

    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 1 and body["matched"] == 1
    assert body["non_replayable"] == []
    (case,) = body["cases"]
    assert case["replayable"] is True
    assert case["got"] == case["expect"] == "qwen3-8b"


def test_eval_replay_survives_a_signal_question_edit(tmp_path):
    """Editing questions must not kill earlier records: snapshots replay history."""
    h, _evidence, _tagger = evidence_app(tmp_path)
    activate_default_tree(h.client)
    historical = h.app.state.snapshot
    chat(h, TEXT_WEATHER)
    chat(h, TEXT_MEDICAL)
    flush(h.app)

    response = h.client.put("/admin/signal-questions",
                            json={"config": _edited_questions_cfg()})
    assert response.status_code == 200, response.text
    assert h.app.state.snapshot.snapshot_revision != historical.snapshot_revision

    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 2 and body["matched"] == 2
    assert body["non_replayable"] == []
    for case in body["cases"]:
        assert case["replayable"] is True
        # questions are informational evidence only, never a replay blocker
        assert case["questions_revision"] == historical.questions_revision


def test_eval_replay_ignores_the_deployment_path(tmp_path):
    """Same artifact, new deploy path -> still the same model identity."""
    h, evidence, _tagger = evidence_app(tmp_path)
    activate_default_tree(h.client)
    chat(h, TEXT_WEATHER)
    flush(h.app)

    evidence.model_dir = "/srv/models/fake-jev-v2"
    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 1
    assert body["non_replayable"] == []


@pytest.mark.parametrize("attribute,value", [
    ("model_name", "fake-jev-v2"),
    ("weights_sha256", "weights-v2"),
])
def test_eval_replay_refuses_a_changed_tagger_model(tmp_path, attribute, value):
    h, evidence, _tagger = evidence_app(tmp_path)
    activate_default_tree(h.client)
    chat(h, TEXT_WEATHER)
    flush(h.app)

    setattr(evidence, attribute, value)
    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 0
    assert body["cases"][0]["reason"] == "tagger_model_mismatch"


def test_eval_replay_refuses_a_changed_decision_temperature(tmp_path):
    """Pure-NL replay has no tagger evidence: the decision evidence must decide."""
    h, evidence, _tagger = evidence_app(
        tmp_path, {"fast": 1.0, "deep": 0.0}, {"fast": 1.0, "deep": 0.0})
    activate_tree(h.client, "NL tree", nl_tree(), tree_id="nl-tree")
    chat(h, "帮我处理一个难题")
    flush(h.app)

    evidence.temperature = 0.9
    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 0
    assert body["cases"][0]["reason"] == "decision_runner_mismatch"


def test_eval_replay_requires_tagger_evidence_for_signal_trees(tmp_path):
    h, _evidence, _tagger = evidence_app(tmp_path)
    activate_default_tree(h.client)
    chat(h, TEXT_WEATHER)
    flush(h.app)
    records = list(h.app.state.requestlog.recent)
    records[-1]["tagger_info"] = None

    body = post_eval(h, mode="replay", days=7)
    assert body["total"] == 0
    assert body["cases"][0]["reason"] == "missing_tagger_evidence"


def test_eval_replay_denominator_matches_the_replayable_cases(tmp_path):
    h, _evidence, _tagger = evidence_app(tmp_path)
    activate_default_tree(h.client)
    chat(h, TEXT_WEATHER)
    chat(h, TEXT_MEDICAL)
    flush(h.app)
    records = list(h.app.state.requestlog.recent)
    records[0]["tagger_info"] = None          # one non-replayable record

    body = post_eval(h, mode="replay", days=7)
    replayable = [case for case in body["cases"] if case["replayable"]]
    assert body["total"] == len(replayable) == 1
    assert len(body["cases"]) == 2
    assert len(body["non_replayable"]) == 1
    assert body["matched"] == sum(1 for case in replayable
                                  if case["got"] == case["expect"])
