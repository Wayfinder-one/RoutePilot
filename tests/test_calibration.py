"""Tests for the offline JEV calibration tooling (scripts/jev_*.py).

The scripts are not a package, so they are loaded by path. No torch is touched:
dump's torch imports are lazy and calibration is pure Python.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"jev_scripts_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load("jev_eval_common")
calibrate = _load("jev_calibrate")
dump = _load("jev_dump_scores")


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def test_normalise_rows_and_gold(tmp_path):
    rows = [
        {"id": "a", "state": "hello", "question": "q", "kind": "noul",
         "options": ["no", "yes"], "target": [0.0, 1.0],
         "metadata": {"signal": "jailbreak", "sub": None, "lang": "en",
                      "text_sha256": "h1"}},
        {"id": "b", "state": "world", "question": "q", "kind": "choice",
         "options": ["x: X", "y: Y"], "target": [1.0, 0.0],
         "metadata": {"signal": "modality", "sub": None, "lang": "en",
                      "answer_keys": ["x", "y"], "text_sha256": "h2"}},
        {"id": "c", "state": "id 110101199003071234", "question": "q",
         "kind": "noul", "options": ["no", "yes"], "target": [1.0, 0.0],
         "metadata": {"signal": "pii", "sub": "cn_id_card", "lang": "zh",
                      "text_sha256": "h3"}},
    ]
    path = _write_jsonl(tmp_path / "eval.jsonl", rows)
    out = common.normalise_rows(path)
    assert [r["signal"] for r in out] == ["jailbreak", "modality", "pii"]
    assert out[0]["kind"] == "noul" and out[0]["gold"] == 1
    assert out[1]["kind"] == "choice" and out[1]["gold_value"] == "x"
    assert out[2]["sub"] == "cn_id_card" and out[2]["gold"] == 0
    # filters + deterministic stride
    only_noul = common.normalise_rows(path, signals=["jailbreak", "pii"],
                                      limit_per_signal=1)
    assert len(only_noul) == 2
    # unique texts keeps per-type gold
    texts = common.unique_texts(out)
    assert len(texts) == 3
    assert texts[2]["gold_by_type"] == {"cn_id_card": 0}


def test_probs_helpers():
    assert common.probs_by_value({"answer_keys": ["a", "b"]}, [0.25, 0.75]) == {
        "a": 0.25, "b": 0.75}
    assert common.top_margin([0.7, 0.2, 0.1]) == pytest.approx(0.5)
    assert common.top_margin([1.0]) == 0.0
    assert common.argmax_index([0.1, 0.9, 0.2]) == 1


def test_risk_coverage_aurc_and_ece():
    # Three samples: the most confident is wrong, then two correct.
    conf = [0.9, 0.8, 0.7]
    error = [True, False, False]
    curve = calibrate.risk_coverage(conf, error)
    assert curve[-1]["coverage"] == 1.0
    assert curve[-1]["risk"] == 1 / 3
    assert curve[0]["risk"] == 1.0            # first admitted sample is wrong
    assert calibrate.aurc(conf, error) == (1.0 + 0.5 + 1 / 3) / 3
    # Perfect ranking = monotonically decreasing risk, worse than the reverse.
    good = calibrate.aurc([0.9, 0.8, 0.7], [False, True, True])
    assert good > calibrate.aurc([0.9, 0.8, 0.7], [False, False, True])
    # ECE: a bucket whose mean confidence equals its accuracy is 0.
    assert calibrate.ece([0.8] * 10, [True] * 8 + [False] * 2) == pytest.approx(0.0)
    assert calibrate.ece([0.8] * 10, [True] * 10) == pytest.approx(0.2)
    # All-confident-and-wrong gives a large ECE.
    assert calibrate.ece([0.95] * 4, [False] * 4) > 0.9
    assert calibrate.risk_at_coverages(conf, error, coverages=(0.5, 1.0)) == [
        {"coverage": 2 / 3, "risk": 0.5, "n": 2},
        {"coverage": 1.0, "risk": 1 / 3, "n": 3},
    ]


def _binary_samples(n_pos=10, n_neg=10, conf_pos=0.8, conf_neg=0.3):
    samples = [{"confidence": conf_pos, "pred_positive": True, "gold": 1}
               for _ in range(n_pos)]
    samples += [{"confidence": conf_neg, "pred_positive": True, "gold": 0}
                for _ in range(n_neg)]
    return samples


def test_binary_gate_table():
    table = calibrate.binary_gate_table(_binary_samples(), floors=(0.3, 0.7))
    at_03, at_07 = table
    assert at_03["coverage"] == 1.0
    assert at_03["block_recall"] == 1.0
    assert at_03["false_block"] == 1.0
    assert at_07["coverage"] == 0.5
    assert at_07["tp"] == 10 and at_07["fp"] == 0
    assert at_07["block_recall"] == 1.0 and at_07["false_block"] == 0.0
    assert at_07["risk"] == 0.0


def test_choice_metrics():
    samples = [
        {"confidence": 0.9, "correct": True, "gold_value": "math"},
        {"confidence": 0.7, "correct": True, "gold_value": "math"},
        {"confidence": 0.4, "correct": False, "gold_value": "law"},
    ]
    metrics = calibrate.choice_metrics(samples, floors=(0.7,))
    assert metrics["n"] == 3
    assert metrics["accuracy"] == 2 / 3
    assert metrics["per_class"]["math"]["recall"] == 1.0
    assert metrics["per_class"]["law"]["recall"] == 0.0
    assert metrics["gates"][0]["coverage"] == 2 / 3
    assert metrics["gates"][0]["accuracy"] == 1.0


def _fullpack_row(gold_by_type, p_by_type, rule_hits, floor_conf=0.9):
    merged = sorted(set(p_by_type) | set(rule_hits))
    conf_by_value = {t: (1.0 if t in rule_hits else floor_conf) for t in merged}
    return {
        "text_sha256": "x",
        "signals": {"pii": {"value": merged, "conf_by_value": conf_by_value,
                            "p_by_type": p_by_type}},
        "gold_by_type": gold_by_type,
        "pii_rule_hits": rule_hits,
        "pii_model_hits": sorted(p_by_type),
        "pii_hits_merged": merged,
        "pii_conf_by_value": conf_by_value,
        "action": {"kind": "block", "model": None, "station_id": "s",
                   "node_id": "n", "score": None, "reasoning": None},
    }


def test_pii_layers():
    rows = [
        # T1: cn_id_card present, both JEV and the regex hit it.
        _fullpack_row({"cn_id_card": 1, "email_address": 0},
                      {"cn_id_card": 0.99}, ["cn_id_card"]),
        # T2: email present, only the regex hits it (JEV missed).
        _fullpack_row({"cn_id_card": 0, "email_address": 1},
                      {}, ["email_address"]),
        # T3: cn present via JEV only; email present but missed by both layers.
        _fullpack_row({"cn_id_card": 1, "email_address": 1},
                      {"cn_id_card": 0.99}, []),
    ]
    layers = calibrate.pii_layers(rows, floors=(0.30,))
    entry = layers["floors"]["0.3"]
    by_type = {r["type"]: r for r in entry["types"]}
    assert by_type["cn_id_card"]["union_recall"] == 1.0
    assert by_type["cn_id_card"]["jev_recall"] == 1.0
    assert by_type["cn_id_card"]["rule_recall"] == 0.5
    assert by_type["email_address"]["jev_recall"] == 0.0
    assert by_type["email_address"]["rule_recall"] == 0.5
    assert entry["micro"]["tp"] == 3          # both cn + the email rule hit
    assert entry["micro"]["recall"] == 0.75


def test_action_distribution_uses_default_tree():
    signals = {
        "jailbreak": {"value": "benign", "confidence": 0.9, "is_rule": False},
        "pii": {"value": [], "confidence": None, "is_rule": False},
        "domain": {"value": "math", "confidence": 0.9, "is_rule": False},
        "complexity": {"value": "easy", "confidence": 0.9, "is_rule": False},
        "modality": {"value": "text", "confidence": None, "is_rule": False},
        "fact_check": {"value": "no_check", "confidence": None, "is_rule": False},
        "user_feedback": {"value": None, "confidence": None, "is_rule": False},
    }
    rows = [{"signals": signals}]
    dist = calibrate.action_distribution(rows)
    assert dist["n"] == 1
    assert dist["actions"] == {"route:qwen3-8b": 1}   # fallback station ④


def test_analyse_and_render_end_to_end(tmp_path):
    rowwise = [
        {"id": "1", "signal": "jailbreak", "sub": None, "lang": "en",
         "kind": "noul", "gold": 1, "gold_value": None, "positive": "jailbreak",
         "probs": {"false": 0.2, "true": 0.8}, "p_yes": 0.8, "value": "yes",
         "pred_positive": True, "confidence": 0.73, "margin": 0.6,
         "correct": True, "input": {"input_tokens": 5}},
        {"id": "2", "signal": "jailbreak", "sub": None, "lang": "en",
         "kind": "noul", "gold": 0, "gold_value": None, "positive": "jailbreak",
         "probs": {"false": 0.7, "true": 0.3}, "p_yes": 0.3, "value": "no",
         "pred_positive": False, "confidence": 0.12, "margin": 0.4,
         "correct": True, "input": {"input_tokens": 5}},
    ]
    path = _write_jsonl(tmp_path / "dump.jsonl", rowwise)
    (tmp_path / "dump.jsonl.meta.json").write_text(
        json.dumps({"eval_path": "signals_test.jsonl",
                    "tagger": {"model_name": "r4b3", "temperature": 3.45,
                               "window": "1024:0", "max_length": 1024,
                               "questions_sha256": "deadbeef"}}), encoding="utf-8")
    report = calibrate.analyse([str(path)])
    assert report["signals"]["jailbreak"]["n"] == 2
    assert report["signals"]["jailbreak"]["gates"]
    markdown = calibrate.render_markdown(report)
    assert "# JEV 概率校准" in markdown
    assert "jailbreak" in markdown and "argmax acc" in markdown


def test_temperature_softmax_matches_production_formula():
    """The fixed-shape scorer must divide the exponentiated logits (not the
    raw ones) — the 2026-10-24 bug that made dump confidences all 1.0."""
    out = common.temperature_softmax([0.0, 8.3581], 3.4500204)
    assert sum(out) == pytest.approx(1.0)
    assert out[0] < out[1]
    assert out[1] == pytest.approx(0.9182, abs=1e-3)
    # A higher temperature flattens the distribution.
    flat = common.temperature_softmax([0.0, 8.3581], 1e6)
    assert flat[0] == pytest.approx(0.5, abs=1e-4)
    # Choice: the softmax-normalised list has K entries and is a distribution.
    choice = common.temperature_softmax([12.0, 3.0, -4.0], 3.45)
    assert len(choice) == 3 and sum(choice) == pytest.approx(1.0)
    assert choice[0] > choice[1] > choice[2]
    # Degenerate single candidate: the binary noul convention is [0, logit].
    assert common.temperature_softmax([0.0, 0.0], 3.45) == [0.5, 0.5]


def test_dump_rowwise_with_fake_tagger(tmp_path, monkeypatch):
    """dump_rowwise works against the tagger's pure helper surface (no torch)."""
    from router.signals.jev import JevTagger

    class _Tok:
        pass

    tagger = object.__new__(JevTagger)
    tagger.max_length = 1024
    tagger.prompt_max_length = 1184
    tagger.tokenizer = _Tok()
    tagger.temperature = 1.0

    def _context_window(text):
        return text, {"input_tokens": len(text), "input_visible_tokens": len(text),
                      "input_truncated": False, "input_window": "1024:0"}

    def _fixed_shape_score(_tagger, records, *, batch=48, length=None,
                          progress=None):
        return [[0.15, 0.85] if r["kind"] == "noul" else [0.9, 0.1]
                for r in records]

    tagger._context_window = _context_window
    monkeypatch.setattr(dump.common, "fixed_shape_score", _fixed_shape_score)
    rows = [
        {"id": "a", "signal": "jailbreak", "sub": None, "lang": "en",
         "kind": "noul", "state": "hello", "question": "q", "options": ["no", "yes"],
         "answer_keys": ["false", "true"], "positive": "jailbreak", "gold": 1,
         "gold_value": None, "text_sha256": "h1"},
        {"id": "b", "signal": "modality", "sub": None, "lang": "en",
         "kind": "choice", "state": "hello", "question": "q",
         "options": ["text: T", "image: I"], "answer_keys": ["text", "image"],
         "positive": None, "gold": 1, "gold_value": "text", "text_sha256": "h2"},
    ]
    out = dump.dump_rowwise(tagger, rows, batch=8)
    assert out[0]["p_yes"] == 0.85 and out[0]["pred_positive"] is True
    assert out[0]["probs"] == {"false": 0.15, "true": 0.85}
    assert out[0]["input"]["input_window"] == "1024:0"
    assert out[1]["value"] == "text" and out[1]["correct"] is True
    # confidence is the shared entropy formula
    from router.signals._math import entropy_confidence
    assert out[1]["confidence"] == entropy_confidence([0.9, 0.1])
