"""Task 11 Step 1 regression guards: ordinary signals + retired symbols.

Half of this file is a **characterization** of the ordinary JEV signal pack
(Task 11 refactor gate: ``_model_signals`` used to append a decision-model
candidate record; every ordinary signal's ``value`` / ``confidence`` /
``evidence`` must stay byte-identical after that path was removed).  The scorer
is a fixed fake keyed by record *content*, so the same probabilities apply no
matter how many records are batched; a real torch/model is never needed.

The other half pins the retired symbols: the decision-only signal (spelled here
at runtime on purpose, so a grep for the retired name only finds the explicit
guards), the per-request capability pack field, the capability record helpers on
``JevTagger`` and the deleted legacy modules must all be gone.

Self-contained on purpose: no import from ``tests/test_jev.py``.
"""

import dataclasses
from pathlib import Path

import pytest

import router
import router.decision
import router.pipeline
from router.pipeline.scope import SignalScope
from router.signals.jev import JevTagger, load_question_config
from router.signals.registry import SIGNALS
from router.signals.tagger import TagRequest

#: The decision-model candidate signal retired with the unified tree.  Spelled
#: at runtime so the retired identifier leaves no grep residue in the tree.
RETIRED_SIGNAL = "capability" + "_route"

#: Ordinary model signals scored by the JEV tagger.
ORDINARY_SIGNALS = ("domain", "jailbreak", "fact_check", "modality",
                    "complexity", "user_feedback", "pii")


def stub_tagger() -> JevTagger:
    """A JevTagger with only the config the pure decode path reads (no torch)."""
    tagger = object.__new__(JevTagger)
    tagger.questions = load_question_config()
    return tagger


def deterministic_score(records: list[dict]) -> list[list[float]]:
    """Fixed per-record distribution identified by record *content*, not index."""
    out: list[list[float]] = []
    for record in records:
        count = len(record["answer_keys"])
        question = record["question"]
        if record["kind"] == "noul":
            if "越狱" in question or "绕过" in question:
                probs = [0.2, 0.8]
            elif "困难" in question:
                probs = [0.1, 0.9]
            elif "核实" in question:
                probs = [0.9, 0.1]
            else:                          # PII: cn_id_card is the only hit
                probs = [0.1, 0.9] if "身份证号" in question else [0.9, 0.1]
        elif count == 14:                  # domain: computer_science is index 3
            probs = [0.0] * 14
            probs[3] = 1.0
        elif count == 3:                   # modality: text
            probs = [1.0, 0.0, 0.0]
        elif count == 4:                   # user_feedback
            probs = [0.1, 0.2, 0.3, 0.4]
        else:                              # pragma: no cover - guard
            raise AssertionError(f"unexpected choice record: {record!r}")
        out.append(probs)
    return out


def run_model_signals(*, scope=None):
    """Score one request pack; returns ``(scored_records, signals)``."""
    captured: list[list[dict]] = []

    def score(records):
        captured.append(list(records))
        return deterministic_score(records)

    tagger = stub_tagger()
    tagger._score = score
    signals = tagger._model_signals(
        TagRequest(text="x", n_messages=2, scope=scope))
    return captured[0], signals


# ── ordinary signal characterization (the Task 11 refactor gate) ───────────
def test_ordinary_signal_pack_golden_values_are_stable():
    """The full pack under the fixed fake scorer, pinned value + evidence."""
    _records, signals = run_model_signals()
    assert set(signals) == set(ORDINARY_SIGNALS)
    assert all(signal["is_rule"] is False for signal in signals.values())

    domain_keys = list(
        load_question_config()["signals"]["domain"]["candidates"])
    assert signals["domain"]["value"] == "computer_science"
    assert signals["domain"]["confidence"] == pytest.approx(1.0)
    assert signals["domain"]["p_top"] == pytest.approx(1.0)
    assert signals["domain"]["margin"] == pytest.approx(1.0)
    assert signals["domain"]["prob_by_value"] == {
        key: (1.0 if key == "computer_science" else 0.0)
        for key in domain_keys}

    assert signals["modality"]["value"] == "text"
    assert signals["modality"]["confidence"] == pytest.approx(1.0)

    feedback = signals["user_feedback"]
    assert feedback["value"] == "want_different"
    assert feedback["p_top"] == pytest.approx(0.4)
    assert feedback["margin"] == pytest.approx(0.1)
    assert feedback["confidence"] == pytest.approx(0.07678032766449217)
    assert feedback["prob_by_value"] == {"satisfied": 0.1,
                                         "need_clarification": 0.2,
                                         "wrong_answer": 0.3,
                                         "want_different": 0.4}

    jailbreak = signals["jailbreak"]
    assert jailbreak["value"] == "jailbreak"
    assert jailbreak["p_yes"] == pytest.approx(0.8)
    assert jailbreak["margin"] == pytest.approx(0.6)
    assert jailbreak["confidence"] == pytest.approx(0.2780719051126377)
    assert jailbreak["prob_by_value"] == {"false": 0.2, "true": 0.8}

    assert signals["fact_check"]["value"] == "no_check"
    assert signals["fact_check"]["p_yes"] == pytest.approx(0.1)
    assert signals["complexity"]["value"] == "hard"
    assert signals["complexity"]["p_yes"] == pytest.approx(0.9)

    pii = signals["pii"]
    pii_types = list(load_question_config()["signals"]["pii"]["types"])
    assert pii["value"] == ["cn_id_card"]
    assert pii["model_hits"] == ["cn_id_card"]
    assert pii["confidence"] == pytest.approx(0.5310044064107189)
    assert pii["conf_by_value"] == {
        "cn_id_card": pytest.approx(0.5310044064107189)}
    assert pii["model_p_by_type"] == {
        typ: (0.9 if typ == "cn_id_card" else 0.1) for typ in pii_types}


def test_trimmed_scope_keeps_the_referenced_ordinary_signals_identical():
    """The pipeline scope trims only unreferenced questions, never values."""
    full_records, full = run_model_signals()
    trimmed_records, trimmed = run_model_signals(scope=SignalScope(
        signals=frozenset({"domain", "pii"}),
        pii_types=frozenset({"cn_id_card"}), full=False, reason="test"))
    assert set(trimmed) == {"domain", "pii"}
    assert len(trimmed_records) < len(full_records)
    # a referenced model signal keeps the exact full-pack value and evidence
    assert trimmed["domain"] == full["domain"]
    assert trimmed["pii"]["model_p_by_type"] == {
        "cn_id_card": full["pii"]["model_p_by_type"]["cn_id_card"]}
    assert trimmed["pii"]["conf_by_value"] == {
        "cn_id_card": full["pii"]["conf_by_value"]["cn_id_card"]}
    assert trimmed["pii"]["value"] == full["pii"]["value"]


def test_full_pack_scores_exactly_the_ordinary_question_records():
    """No retired decision record is appended to the forward batch."""
    from router.signals.jev import build_signal_records

    records, signals = run_model_signals()
    assert set(signals) == set(ORDINARY_SIGNALS)
    expected = build_signal_records(load_question_config(), "x", None)
    # the ranked scorer receives exactly the rendered ordinary records, in order
    assert [record["question"] for record in records] == [
        item["record"]["question"] for item in expected]
    assert [record["answer_keys"] for record in records] == [
        item["record"]["answer_keys"] for item in expected]
    # 3 choice + 3 noul + 30 PII subtype records at n_messages=2
    assert len(records) == 36


# ── retired symbols: must stay gone ───────────────────────────────────────
#: Files deleted by Task 11 (``router/``-relative paths).
RETIRED_MODULES = ("decision/capability.py", "decision/pack.py")


@pytest.mark.parametrize("relative", RETIRED_MODULES)
def test_retired_module_file_is_deleted(relative):
    assert not (Path(router.__file__).parent / relative).exists()


def test_decision_only_signal_is_not_registered():
    assert RETIRED_SIGNAL not in SIGNALS


def test_no_signal_is_left_in_the_retired_decision_group():
    assert [spec.key for spec in SIGNALS.values()
            if spec.group == "decision"] == []


def test_tag_request_no_longer_carries_a_decision_pack():
    fields = {field.name for field in dataclasses.fields(TagRequest)}
    assert "capability" not in fields
    assert "scope" in fields


def test_jev_no_longer_exposes_the_decision_signal_path():
    from router.signals import jev

    assert not hasattr(jev, "CAPABILITY_SIGNAL")
    for name in ("_capability_in_scope", "_build_capability_record",
                 "_decode_capability", "_capability_signal"):
        assert not hasattr(jev.JevTagger, name)
    # the pure choice boundary used by the new decision runner survives
    assert hasattr(jev.JevTagger, "score_choice")


def test_decision_package_exports_only_the_new_boundaries():
    assert not hasattr(router.decision, "CAPABILITY_SIGNAL")
    assert not hasattr(router.decision, "build_pack")
    assert not hasattr(router.decision, "empty_pack")
    # the model-catalog + choice/runner boundaries survive
    for name in ("capability_of_model", "normalize_capability",
                 "build_choice_record", "decode_choice",
                 "ChoiceRequest", "ChoiceResult", "DecisionRunner"):
        assert hasattr(router.decision, name)


def test_repositories_no_longer_offer_station_apis():
    from router.store import repositories

    for name in ("StationRepo", "default_pipeline", "ensure_pipeline_revision"):
        assert not hasattr(repositories, name)
    # the delete-model 409 guard survives, rebuilt on saved trees
    assert hasattr(repositories, "model_references")


def test_scope_no_longer_offers_the_station_scope_api():
    from router.pipeline import scope

    assert not hasattr(scope, "compute_signal_scope")
    assert hasattr(scope, "compute_tree_scope")
    assert hasattr(scope, "required_signal_keys")
