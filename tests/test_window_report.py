"""Tests for the P1-D window tooling (report / matrix / A-B runner).

Everything here is CPU-only and synthetic: no GPU, no network, no real eval
dump. The scripts are not a package, so they are loaded by path (same pattern
as ``tests/test_calibration.py``).

Covered contracts:
* every metric comes from the real ``router.eval_metrics`` module (FPR is
  ``fp/(fp+tn)``, FDR is ``fp/(fp+tp)`` — the 2026-10-24 review found these
  swapped in the old calibrate report);
* the corrected PII micro-F1 (``p_yes > 0.5``) is what the report publishes,
  with the zero-division case shown as 0 (the errata explicitly says the old
  column was recall@0.3);
* PII model / rule / union layers are three independent confusion matrices;
* floor gates are parameterised (never a hardcoded 0.6) and produce block
  recall / false-block (FPR) numbers;
* p50/p95 + bootstrap CI use the metrics library and are deterministic;
* the matrix scenario builder puts content at head/middle/tail, floor override
  changes pipeline actions, dedupe/holdout/sampling behave as specified, and
  matrix rows are consumable by the report (final route/block distribution).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

from router.eval_metrics import Confusion, bootstrap_ci, percentile  # noqa: E402
from router.signals._math import binary_entropy_confidence  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"window_scripts_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = _load("jev_window_report")
matrix = _load("jev_window_matrix")
ab = _load("eval_jev_window_ab")


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")
    return path


def _make_row(*, signal="jailbreak", condition="padded4k", arm="256:768",
              gold=1, p_yes=0.9, confidence=None, sub=None,
              rule_hits=None, kind="noul", row_id=None, **extra):
    """A window_ab-schema row (the format both runners write)."""
    confidence = (binary_entropy_confidence(p_yes)
                  if confidence is None and p_yes is not None else confidence)
    pred = None if p_yes is None else p_yes > 0.5
    row = {
        "condition": condition,
        "id": row_id or f"{condition}:{signal}:{sub or ''}:{len(rule_hits or [])}:"
                        f"{gold}:{p_yes}",
        "signal": signal, "sub": sub, "lang": "en", "kind": kind,
        "gold": gold, "gold_value": None, "arm": arm,
        "window": {"input_tokens": 10, "input_visible_tokens": 10,
                   "input_truncated": False, "input_window": arm},
        "input_tokens": 10, "visible_tokens": 10,
        "rule_pii_hits": list(rule_hits or []),
        "probs": {"no": 1.0 - (p_yes or 0.0), "yes": p_yes or 0.0},
        "p_yes": p_yes, "confidence": confidence,
        "value": "yes" if pred else "no", "pred_positive": pred,
        "correct": (pred == (gold == 1)) if pred is not None else None,
    }
    row.update(extra)
    return row


def _pii_rows(specs, *, condition="padded4k", arm="256:768"):
    """Build PII rows from ``(gold, model_positive, rule_hit, sub)`` tuples."""
    rows = []
    for index, (gold, model_positive, rule_hit, sub) in enumerate(specs):
        p_yes = 0.9 if model_positive else 0.1
        row = _make_row(signal="pii", condition=condition, arm=arm,
                        gold=gold, p_yes=p_yes, sub=sub,
                        rule_hits=[sub] if rule_hit else [],
                        row_id=f"{condition}:pii:{sub}:{index}")
        rows.append(row)
    return rows


def _report(rows, **kwargs):
    return report.compute_report(rows, **kwargs)


def _signal(payload, condition, arm, signal):
    return payload["conditions"][condition]["arms"][arm]["signals"][signal]


# --------------------------------------------------------------------------- #
# metrics module + corrected PII 口径
# --------------------------------------------------------------------------- #
def test_uses_real_metrics_module():
    import router.eval_metrics as real
    assert report.metrics is real
    confusion = Confusion(tp=1, fp=5, tn=5, fn=0)
    # FPR = fp/(fp+tn); FDR = fp/(fp+tp) — different on purpose.
    assert confusion.fpr == pytest.approx(5 / 10)
    assert confusion.fdr == pytest.approx(5 / 6)


def test_corrected_pii_micro_f1_is_p_above_half():
    """Two TP, one FN, no FP -> 2*2/(2*2+0+1) = 0.8 (old report had recall)."""
    rows = _pii_rows([
        (1, True, False, "org"),      # TP
        (1, True, False, "person"),   # TP
        (1, False, False, "location"),  # FN (model miss, no rule)
        (0, False, False, "email_address"),  # TN
    ])
    payload = _report(rows, floors=[0.3, 0.6])
    pii = _signal(payload, "padded4k", "256:768", "pii")
    binary = pii["binary"]
    assert (binary["tp"], binary["fp"], binary["tn"], binary["fn"]) == (2, 0, 1, 1)
    assert binary["precision"] == pytest.approx(1.0)
    assert binary["recall"] == pytest.approx(2 / 3)
    assert binary["f1"] == pytest.approx(0.8)
    # The errata block names the old wrong column, and the JSON keeps it clear.
    assert "recall@0.3" in payload["errata"]["summary"]
    markdown = report.render_markdown(payload)
    assert "0.8000" in markdown
    assert "recall@0.3" in markdown


def test_zero_positive_predictions_f1_is_undefined_but_displayed_zero():
    rows = _pii_rows([(1, False, False, "org"), (1, False, False, "person")])
    payload = _report(rows)
    binary = _signal(payload, "padded4k", "256:768", "pii")["binary"]
    # router.eval_metrics keeps the honest None (0/0), markdown shows "0.0000*".
    assert binary["f1"] is None
    markdown = report.render_markdown(payload)
    assert "0.0000*" in markdown


# --------------------------------------------------------------------------- #
# FPR vs FDR (the exact bug of the old calibrate report)
# --------------------------------------------------------------------------- #
def test_report_fpr_and_fdr_are_different():
    # tp=2, fp=4, tn=6, fn=1 -> FPR=4/10, FDR=4/6
    specs = []
    specs += [(1, True, False, "org")] * 2
    specs += [(0, True, False, "person")] * 4
    specs += [(0, False, False, "email_address")] * 6
    specs += [(1, False, False, "location")]
    payload = _report(_pii_rows(specs))
    binary = _signal(payload, "padded4k", "256:768", "pii")["binary"]
    assert (binary["tp"], binary["fp"], binary["tn"], binary["fn"]) == (2, 4, 6, 1)
    assert binary["fpr"] == pytest.approx(4 / 10)
    assert binary["fdr"] == pytest.approx(4 / 6)
    assert binary["fpr"] != binary["fdr"]


# --------------------------------------------------------------------------- #
# PII three-layer evidence (model / rule / union)
# --------------------------------------------------------------------------- #
def test_pii_three_layers_are_independent():
    rows = _pii_rows([
        (1, True, False, "person"),        # model TP, no rule -> all layers TP
        (1, False, True, "email_address"),  # model miss + rule hit -> union TP
        (0, False, True, "url"),           # rule false positive -> union FP
        (0, False, False, "location"),     # TN everywhere
    ])
    payload = _report(rows)
    layers = _signal(payload, "padded4k", "256:768", "pii")["layers"]
    model = layers["layers"]["model"]
    rule = layers["layers"]["rule"]
    union = layers["layers"]["union"]
    assert (model["tp"], model["fp"]) == (1, 0)
    assert (rule["tp"], rule["fp"]) == (1, 1)
    assert (union["tp"], union["fp"]) == (2, 1)
    # Rule recall gain on positives: union recall 1.0 - model recall 0.5.
    assert layers["deltas"]["rule_recall_gain"] == pytest.approx(0.5)
    assert layers["deltas"]["rule_true_positive_gain"] == 1
    assert layers["deltas"]["rule_false_positive_gain"] == 1
    # Per-type recall table sees both the model-only and union hit.
    per_type = layers["per_type"]
    assert per_type["email_address"]["floors"]["0.6"]["model_block_recall"] == 0.0
    assert per_type["email_address"]["floors"]["0.6"]["union_block_recall"] == 1.0


def test_old_pii_p_by_type_is_not_used_to_fake_model_probability():
    """Legacy p_by_type had rule hits overwritten to 1.0; refuse to use it."""
    signals = {"pii": {"value": ["email_address"],
                       "confidence": 1.0,
                       "conf_by_value": {"email_address": 1.0},
                       "is_rule": True,
                       "p_by_type": {"email_address": 1.0}}}
    p_yes, hits = matrix._pii_model_p(signals, "email_address",
                                      ["email_address"])
    assert p_yes is None and hits is None
    # Non-rule types still fall back to the legacy model probability.
    signals["pii"]["p_by_type"] = {"person": 0.83}
    p_yes, _ = matrix._pii_model_p(signals, "person", [])
    assert p_yes == pytest.approx(0.83)
    # New P1-A evidence always wins.
    signals["pii"]["model_p_by_type"] = {"email_address": 0.61}
    signals["pii"]["model_hits"] = []
    p_yes, hits = matrix._pii_model_p(signals, "email_address",
                                      ["email_address"])
    assert p_yes == pytest.approx(0.61) and hits == []


# --------------------------------------------------------------------------- #
# floor parameterisation: gate actions (block recall / false block)
# --------------------------------------------------------------------------- #
def test_floor_gate_is_parameterised():
    # p_yes=0.9 -> confidence ~0.53: passes floors .45/.3 but not .7/.6.
    rows = [
        _make_row(signal="jailbreak", gold=1, p_yes=0.9),    # conf ~0.53
        _make_row(signal="jailbreak", gold=0, p_yes=0.9),    # conf ~0.53
        _make_row(signal="jailbreak", gold=0, p_yes=0.1),    # conf ~0.53, no hit
    ]
    payload = _report(rows, floors=[0.3, 0.6, 0.7])
    floors = {entry["floor"]: entry
              for entry in _signal(payload, "padded4k", "256:768",
                                   "jailbreak")["floors"]}
    low = floors[0.3]["model"]
    high = floors[0.6]["model"]
    assert (low["tp"], low["fp"], low["fn"], low["tn"]) == (1, 1, 0, 1)
    assert low["recall"] == pytest.approx(1.0)
    assert low["fpr"] == pytest.approx(0.5)          # false block rate
    assert (high["tp"], high["fp"]) == (0, 0)        # confidence below the floor
    assert high["recall"] == pytest.approx(0.0)
    # The floor grid is echoed in the JSON so numbers are reproducible.
    assert payload["config"]["floors"] == [0.3, 0.6, 0.7]
    # No hardcoded .6: a two-floor grid only reports those two floors.
    two = _report(rows, floors=[0.15, 0.30])
    assert [entry["floor"] for entry in
            _signal(two, "padded4k", "256:768", "jailbreak")["floors"]] == \
        [0.15, 0.30]


def test_pii_union_gate_includes_deterministic_rules():
    rows = _pii_rows([
        (1, False, True, "email_address"),   # rule saves the recall
        (0, False, False, "location"),
    ])
    payload = _report(rows, floors=[0.15, 0.9])
    floors = {entry["floor"]: entry for entry in
              _signal(payload, "padded4k", "256:768", "pii")["floors"]}
    assert floors[0.15]["model"]["recall"] == 0.0
    assert floors[0.15]["union"]["recall"] == pytest.approx(1.0)
    assert floors[0.9]["union"]["recall"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# latency + bootstrap CI
# --------------------------------------------------------------------------- #
def test_latency_percentiles_and_ci_are_deterministic():
    rows = [_make_row(p_yes=0.9, row_id=f"r{i}",
                      score_ms=float(i + 1)) for i in range(20)]
    payload = _report(rows, n_boot=200, seed=7)
    latency = payload["conditions"]["padded4k"]["arms"]["256:768"]["latency"]
    assert latency["n"] == 20
    assert latency["p50"] == pytest.approx(10.5)
    assert latency["p95"] == pytest.approx(19.05)
    values = [float(i + 1) for i in range(20)]
    low, high = bootstrap_ci(values, statistic="median", n_boot=200,
                             confidence=0.95, seed=7)
    assert latency["p50_ci"] == pytest.approx([low, high])
    low95, high95 = bootstrap_ci(
        values, statistic=lambda sample: percentile(sample, 0.95),
        n_boot=200, confidence=0.95, seed=7)
    assert latency["p95_ci"] == pytest.approx([low95, high95])
    for key in ("p50_ci", "p95_ci", "mean_ci"):
        assert isinstance(latency[key], list) and len(latency[key]) == 2
        assert latency[key][0] <= latency[key][1]
    again = _report(rows, n_boot=200, seed=7)
    assert (again["conditions"]["padded4k"]["arms"]["256:768"]["latency"]["p50_ci"]
            == latency["p50_ci"])


def test_constant_latency_ci_is_degenerate():
    rows = [_make_row(p_yes=0.9, row_id=f"c{i}", score_ms=12.5)
            for i in range(8)]
    latency = _report(rows, n_boot=100)["conditions"]["padded4k"]["arms"][
        "256:768"]["latency"]
    assert latency["p50_ci"] == pytest.approx([12.5, 12.5])
    assert latency["p95_ci"] == pytest.approx([12.5, 12.5])


def test_missing_latency_is_reported_not_invented():
    payload = _report([_make_row(p_yes=0.9)])
    assert payload["conditions"]["padded4k"]["arms"]["256:768"]["latency"] is None
    assert any("latency" in warning for warning in payload["warnings"])


# --------------------------------------------------------------------------- #
# report rendering / CLI
# --------------------------------------------------------------------------- #
def test_report_writes_markdown_and_json(tmp_path):
    rows = [_make_row(p_yes=0.9, sub=None),
            _make_row(signal="pii", gold=1, p_yes=0.9, sub="org"),
            _make_row(signal="pii", gold=0, p_yes=0.1, sub="person")]
    input_path = _write_jsonl(tmp_path / "rows.jsonl", rows)
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    code = report.main(["--input", str(input_path),
                        "--out-md", str(out_md),
                        "--out-json", str(out_json),
                        "--floors", "0.3,0.6,0.7",
                        "--bootstrap", "50"])
    assert code == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["schema"] == report.SCHEMA
    assert payload["n_rows"] == 3
    markdown = out_md.read_text(encoding="utf-8")
    assert "Errata" in markdown
    assert "PII 三层证据" in markdown
    assert "0.3000" in markdown


def test_report_is_driven_by_rows_not_hardcoded(tmp_path):
    """Changing the rows must change the published numbers."""
    base = _pii_rows([(1, True, False, "org"), (1, False, False, "person")])
    changed = _pii_rows([(1, True, False, "org"), (1, True, False, "person")])
    first = _report(base)["conditions"]["padded4k"]["arms"]["256:768"][
        "signals"]["pii"]["binary"]
    second = _report(changed)["conditions"]["padded4k"]["arms"]["256:768"][
        "signals"]["pii"]["binary"]
    assert first["tp"] == 1 and second["tp"] == 2
    assert first["f1"] != second["f1"]


# --------------------------------------------------------------------------- #
# matrix: scenarios
# --------------------------------------------------------------------------- #
class _FakeTokenizer:
    """Whitespace tokenizer good enough for the position semantics."""

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": text.split()}

    def decode(self, ids):
        return " ".join(ids)


def test_padded_state_puts_content_at_head_middle_tail():
    tokenizer = _FakeTokenizer()
    filler = [f"f{i}" for i in range(10)]
    state = "A B"
    tail = matrix.build_scenario_state(state, filler, tokenizer, 6, "tail")
    head = matrix.build_scenario_state(state, filler, tokenizer, 6, "head")
    middle = matrix.build_scenario_state(state, filler, tokenizer, 6, "middle")
    assert tail.endswith("A B") and tail.startswith("f0 f1")
    assert head.startswith("A B") and head.endswith("f3")
    assert middle.index("A B") > middle.index("f0")
    assert middle.index("A B") < middle.index("f2")
    # Content already over budget: returned verbatim for every position.
    long_state = " ".join(["x"] * 20)
    for position in ("head", "middle", "tail"):
        assert matrix.build_scenario_state(long_state, filler, tokenizer, 5,
                                           position) == long_state


def test_condition_labels_keep_legacy_name_for_b0_only():
    assert matrix.condition_label("tail", "clean_b0", False) == "padded4k"
    assert matrix.condition_label("tail", "clean_b1", False) == "padded4k@clean_b1"
    assert matrix.condition_label("tail", "clean_b0", True) == "padded4k@clean_b0"
    assert matrix.condition_label("head", "clean_b0", False) == "padded4k_head"


# --------------------------------------------------------------------------- #
# matrix: pipeline floor override + final actions
# --------------------------------------------------------------------------- #
def _demo_stations():
    from router.pipeline.model import station_from_dict
    return [station_from_dict({
        "id": "safety", "name": "safety", "root": {
            "id": "fork", "kind": "fork", "action": "route",
            "children": [
                {"id": "jb", "signal": "jailbreak", "value": "jailbreak",
                 "conf_min": 0.6, "kind": "leaf", "action": "block"},
                {"id": "cmp", "signal": "complexity", "value": "hard",
                 "conf_min": 0.7, "kind": "leaf", "action": "route",
                 "model": "strong"},
            ]}}),
        station_from_dict({"id": "fallback", "name": "fallback", "root": {
            "id": "fb", "kind": "leaf", "action": "route", "model": "cheap"}})]


def _collect_conditions(station):
    out = []

    def walk(node):
        if node.signal is not None:
            out.append((node.signal, node.conf_min))
        for child in node.children:
            walk(child)

    walk(station.root)
    return out


def test_override_floors_reparameterises_every_model_condition():
    stations = _demo_stations()
    low = matrix.override_floors(stations, 0.2)
    high = matrix.override_floors(stations, 0.8)
    assert _collect_conditions(low[0]) == [("jailbreak", 0.2),
                                          ("complexity", 0.2)]
    assert _collect_conditions(high[0]) == [("jailbreak", 0.8),
                                           ("complexity", 0.8)]
    # Rule signals keep their deterministic semantics (null floor).
    from router.pipeline.model import station_from_dict
    keyword = [station_from_dict({
        "id": "kw", "name": "kw", "root": {
            "id": "k", "signal": "keyword", "value": "医疗急症",
            "conf_min": None, "kind": "leaf", "action": "route",
            "model": "medical"}})]
    assert _collect_conditions(matrix.override_floors(keyword, 0.9)[0]) == \
        [("keyword", None)]


def test_floor_changes_final_action_and_is_recorded_per_floor():
    stations = _demo_stations()
    floors = [0.3, 0.9]
    stations_by_floor = {floor: matrix.override_floors(stations, floor)
                         for floor in floors}
    signals = {"jailbreak": {"value": "jailbreak", "confidence": 0.8,
                             "is_rule": False},
               "complexity": {"value": "easy", "confidence": 0.1,
                              "is_rule": False}}
    actions = matrix.instance_actions(signals, stations_by_floor, floors)
    assert actions["0.3"]["kind"] == "block"
    # At floor 0.9 the jailbreak condition is below its floor -> fallback route.
    assert actions["0.9"]["kind"] == "route"
    assert actions["0.9"]["model"] == "cheap"


def test_matrix_row_is_report_compatible():
    stations = _demo_stations()
    floors = [0.3, 0.9]
    stations_by_floor = {floor: matrix.override_floors(stations, floor)
                         for floor in floors}
    instance = {
        "sha256": "abc", "rows": [{"id": "r1", "signal": "jailbreak",
                                   "kind": "noul", "gold": 1, "lang": "en",
                                   "sub": None, "gold_value": None}],
        "signals": {"jailbreak": {"value": "jailbreak", "confidence": 0.8,
                                  "is_rule": False}},
        "tag_window": {"input_tokens": 5, "input_visible_tokens": 5,
                       "input_truncated": False, "input_window": "256:768"},
        "rule_hits": [], "score_ms": 12.0, "prep_ms": 2.0, "forward_ms": 10.0,
        "actions": matrix.instance_actions(instance_signals := {
            "jailbreak": {"value": "jailbreak", "confidence": 0.8,
                          "is_rule": False}}, stations_by_floor, floors),
    }
    row = matrix._matrix_row(instance["rows"][0], instance,
                             condition="padded4k", position="tail",
                             filler_stem="clean_b0", arm="256:768",
                             scoring="fixed")
    assert row["p_yes"] is not None and row["pred_positive"] is True
    assert row["actions"]["0.3"]["kind"] == "block"
    assert row["score_ms"] == 12.0
    payload = _report([row], floors=floors)
    final = payload["conditions"]["padded4k"]["arms"]["256:768"]["final_actions"]
    assert final["0.3"]["counts"]["block"] == 1
    assert final["0.9"]["counts"]["route"] == 1
    markdown = report.render_markdown(payload)
    assert "最终 route / block 分布" in markdown
    assert "block recall" in markdown


# --------------------------------------------------------------------------- #
# matrix: sampling / holdout / dedupe
# --------------------------------------------------------------------------- #
def test_paired_sample_rows_are_deterministic_and_quota_based():
    rows = []
    for signal in ("jailbreak", "fact_check", "complexity", "pii"):
        for index in range(10):
            rows.append({"signal": signal, "gold": index % 2,
                         "state": f"{signal}-{index}", "id": f"{signal}-{index}"})
    sampled = matrix.sample_rows(rows, quota=4, pii_target=6)
    assert len(sampled) == 3 * 4 + 6
    # Deterministic: the same input always yields the same paired sample.
    assert [row["id"] for row in sampled] == [
        row["id"] for row in matrix.sample_rows(rows, quota=4, pii_target=6)]
    # PII keeps every positive.
    positives = [row for row in rows if row["signal"] == "pii"
                 and row["gold"] == 1]
    assert all(row in sampled for row in positives)


def test_holdout_ids_loaded_and_filtered(tmp_path):
    json_path = tmp_path / "ids.json"
    json_path.write_text(json.dumps({"ids": ["a", "b"]}), encoding="utf-8")
    jsonl_path = tmp_path / "ids.jsonl"
    jsonl_path.write_text('{"id": "c"}\n{"doc_ids": ["d", "e"]}\n',
                          encoding="utf-8")
    text_path = tmp_path / "ids.txt"
    text_path.write_text("f\n# comment\n", encoding="utf-8")
    excluded = matrix.load_ids([str(json_path), str(jsonl_path),
                               str(text_path)])
    assert excluded == {"a", "b", "c", "d", "e", "f"}
    rows = [{"id": "a"}, {"id": "x", "text_sha256": "b"},
            {"id": "y", "group_id": "c"}, {"id": "keep"}]
    kept, removed = matrix.filter_holdout(rows, excluded)
    assert removed == 3
    assert [row["id"] for row in kept] == ["keep"]


def test_group_instances_dedupes_full_text_only():
    rows = [
        {"id": "q1", "state": "same text", "signal": "pii", "sub": "org"},
        {"id": "q2", "state": "same text", "signal": "pii", "sub": "person"},
        {"id": "q3", "state": "other text", "signal": "jailbreak"},
    ]
    instances = matrix.group_instances(rows)
    assert len(instances) == 2
    assert [len(instance["rows"]) for instance in instances] == [2, 1]


def test_row_scores_from_full_pack_new_and_legacy_fields():
    new_signals = {
        "jailbreak": {"value": "jailbreak", "confidence": 0.77,
                      "p_yes": 0.91, "is_rule": False},
        "complexity": {"value": "hard", "confidence": 0.4,
                       "is_rule": False},
        "pii": {"value": ["person"], "model_p_by_type": {"person": 0.88},
                "model_hits": ["person"], "rule_hits": []},
    }
    scores = matrix.row_scores({"signal": "jailbreak", "kind": "noul",
                                "gold": 1}, new_signals, [])
    assert scores["p_yes"] == pytest.approx(0.91)
    assert scores["pred_positive"] is True
    pii_scores = matrix.row_scores({"signal": "pii", "sub": "person",
                                    "kind": "noul", "gold": 1},
                                   new_signals, [])
    assert pii_scores["p_yes"] == pytest.approx(0.88)
    # Legacy noul pack without p_yes: recover it from the entropy confidence.
    legacy = {"complexity": {"value": "hard", "confidence": 0.65,
                             "is_rule": False}}
    legacy_scores = matrix.row_scores({"signal": "complexity", "kind": "noul",
                                       "gold": 1}, legacy, [])
    assert legacy_scores["pred_positive"] is True
    assert legacy_scores["p_yes"] > 0.5
    assert binary_entropy_confidence(legacy_scores["p_yes"]) == \
        pytest.approx(0.65, abs=1e-6)


def test_probs_from_logits_matches_production_softmax():
    class _Tagger:
        temperature = 3.45

    # noul record: one prompt -> logits [0.0, score]
    probs = matrix.probs_from_logits(_Tagger(), [2.5], [1])
    expected = matrix.common.temperature_softmax([0.0, 2.5], 3.45)
    assert probs[0] == pytest.approx(expected)
    # choice record: two prompts -> softmax over both logits
    probs = matrix.probs_from_logits(_Tagger(), [1.0, 2.0], [2])
    expected = matrix.common.temperature_softmax([1.0, 2.0], 3.45)
    assert sum(probs[0]) == pytest.approx(1.0)
    assert probs[0] == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# matrix: production check aggregation + extra markdown
# --------------------------------------------------------------------------- #
def test_production_check_summary_counts_flips_and_deltas():
    per_group = {
        "padded4k/256:768": {
            "condition": "padded4k", "arm": "256:768", "n_instances": 2,
            "floors": {
                "0.3": {"n": 2, "flips": ["a"], "fixed_actions": {},
                        "production_actions": {}},
                "0.6": {"n": 2, "flips": [], "fixed_actions": {},
                        "production_actions": {}},
            },
            "score_delta": {"n": 4, "max_abs_delta": 0.02,
                            "mean_abs_delta": None, "_sum_abs": 0.04},
            "examples": [],
        },
    }
    summary = matrix.summarise_production_check(per_group)
    assert summary["n_instances"] == 2
    assert summary["total_action_flips"] == 1
    assert summary["groups"]["padded4k/256:768"]["score_delta"][
        "mean_abs_delta"] == pytest.approx(0.01)
    section = "\n".join(matrix._production_section(summary))
    assert "action flips" in section and "0.3" in section
    # Missing check -> clear "must run production path" statement.
    assert "生产" in "\n".join(matrix._production_section(None))


def test_matrix_section_renders_from_report_payload():
    stations = _demo_stations()
    floors = [0.3, 0.9]
    stations_by_floor = {floor: matrix.override_floors(stations, floor)
                         for floor in floors}
    signals = {"jailbreak": {"value": "jailbreak", "confidence": 0.8,
                             "is_rule": False}}
    instance = {
        "sha256": "s1", "rows": [{"id": "r1", "signal": "jailbreak",
                                  "kind": "noul", "gold": 1}],
        "signals": signals, "tag_window": {}, "rule_hits": [], "score_ms": 1.0,
        "actions": matrix.instance_actions(signals, stations_by_floor, floors),
    }
    row = matrix._matrix_row(instance["rows"][0], instance,
                             condition="padded4k", position="tail",
                             filler_stem="clean_b0", arm="256:768",
                             scoring="fixed")
    payload = _report([row], floors=floors)
    section = "\n".join(matrix._matrix_section(payload))
    assert "Window × floor" in section
    assert "block recall" in section
    assert "1/0/0" in section          # floor 0.3: 1 block / 0 route / 0 none


# --------------------------------------------------------------------------- #
# A/B runner: old hardcoded-.6 single-signal replay is gone, CLI stays
# --------------------------------------------------------------------------- #
def test_ab_runner_removed_single_signal_action_replay():
    for name in ("build_pack", "evaluate_action"):
        assert not hasattr(ab, name)
    assert callable(ab.sample_rows) and callable(ab.pad_state)
    rows = [{"signal": "jailbreak", "gold": index % 2}
            for index in range(10)]
    assert ab.sample_rows(rows, "natural") == rows
    source = Path(ab.__file__).read_text(encoding="utf-8")
    # The old hardcoded safe-action floor and its fabricated pack are gone.
    assert "gate = positive and conf >= 0.6" not in source
    assert "def build_pack" not in source


def test_ab_sample_matches_matrix_sampling():
    rows = [{"signal": signal, "gold": index % 2,
             "id": f"{signal}-{index}"}
            for signal in ("jailbreak", "fact_check", "complexity", "pii")
            for index in range(8)]
    ab_sample = ab.sample_rows(rows, "padded4k")
    matrix_sample = matrix.sample_rows(rows, quota=120, pii_target=300,
                                       signals=ab.MIXED_SIGNALS)
    assert [row["id"] for row in ab_sample] == [row["id"] for row in matrix_sample]


# --------------------------------------------------------------------------- #
# real dump regression (skipped when the raw docs are absent)
# --------------------------------------------------------------------------- #
_REAL_DUMP = (ROOT / "docs" / "jev-pii-200k" / "raw" / "jev-dump"
              / "window_ab.jsonl")


@pytest.mark.skipif(not _REAL_DUMP.exists(),
                    reason="raw window_ab.jsonl not present")
def test_real_window_dump_reproduces_corrected_pii_f1():
    """The reviewed numbers (padded4k, p_yes>0.5) must be computed, not typed."""
    rows = report.read_rows([_REAL_DUMP])
    payload = report.compute_report(rows, floors=[0.3])
    expected = {"0:1024": (98, 0, 86, 0.6950),
                "256:768": (123, 0, 61, 0.8013),
                "512:512": (122, 0, 62, 0.7974),
                "1024:0": (0, 0, 184, 0.0)}
    for arm, (tp, fp, fn, f1) in expected.items():
        binary = _signal(payload, "padded4k", arm, "pii")["binary"]
        assert (binary["tp"], binary["fp"], binary["fn"]) == (tp, fp, fn)
        if f1:
            assert binary["f1"] == pytest.approx(f1, abs=1e-4)
        else:
            assert binary["f1"] is None       # 0/0 is undefined, not a fake 0
    # The old wrong column is exactly recall@0.3 on the same rows.
    errata_marks = report.render_markdown(payload)
    assert "旧列实际=recall@0.3" in errata_marks
    assert "0.6950" in errata_marks and "0.8013" in errata_marks

# --------------------------------------------------------------------------- #
# matrix: production tag assembly with a fake backbone (CPU-only)
# --------------------------------------------------------------------------- #
class _FakePackTokenizer:
    """Word-count tokenizer; all test texts fit the window so it is verbatim."""

    pad_token_id = 0

    def __call__(self, text, add_special_tokens=False, **kwargs):
        return {"input_ids": [len(part) for part in text.split()]}

    def decode(self, ids):
        return " ".join(str(int(value)) for value in ids)

    def apply_chat_template(self, messages, tokenize=False,
                            add_generation_prompt=True, enable_thinking=False):
        return "|".join(message["content"] for message in messages)


def _fake_jev_tagger(monkeypatch):
    """A JevTagger object whose backbone ``_score`` is a deterministic stub.

    The REAL production ``tag()`` assembly runs (question records, decode,
    rule merge, P1-A evidence attachment); only the model forward is faked, so
    the fixed-shape lookup / fallback machinery is exercised end-to-end.
    """
    from router.signals.jev import JevTagger, load_question_config

    tagger = object.__new__(JevTagger)
    tagger.questions = load_question_config()
    tagger.question_path = "configs/signal_questions.json"
    tagger.temperature = 3.45
    tagger.max_length = 1024
    tagger.window = (1024, 0)
    tagger.window_spec = "1024:0"
    tagger.tokenizer = _FakePackTokenizer()
    tagger.kw_groups = {}
    tagger.time_periods = {}
    tagger.device = "cpu"
    # Long-text PII chunking is off by default (P2-A contract); the fake tagger
    # bypasses __init__, so mirror the disabled attributes explicitly.
    tagger.chunk_segments = 0
    tagger.chunk_size = 512
    tagger.chunk_agg = "noisy_or"
    tagger.chunk_enabled = False
    tagger.chunk_calibration = {}
    tagger._chunk_calib_desc = "none"
    tagger._original_score_calls = 0

    from router.signals.jev import candidate_prompts

    def original_score(records):
        tagger._original_score_calls += 1
        out = []
        for record in records:
            count = len(candidate_prompts(record))
            out.append([0.2, 0.8] if count == 1 else [0.1] * count)
        return out

    tagger._score = original_score
    tagger._info_for = lambda questions: {"questions_sha256": "sha-test"}
    return tagger


def _instance(state="hello world", row_id="r1", signal="jailbreak"):
    return matrix.group_instances([{
        "id": row_id, "state": state, "signal": signal, "kind": "noul",
        "gold": 1, "sub": None, "lang": "en", "gold_value": None,
    }])


def test_fixed_scoring_uses_production_tag_assembly(monkeypatch):
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(
        matrix, "score_prompts_fixed",
        lambda tagger, prompts, batch=48: ([0.0] * len(prompts),
                                           [1.5] * len(prompts)))
    instances = _instance()
    info = matrix.score_instances_fixed(tagger, instances, batch=4,
                                        n_messages=2)
    assert info["fallback_count"] == 0
    assert info["prompt_count"] > 30          # full 7-signal pack, no trim
    signals = instances[0]["signals"]
    # scope=None keeps the complete pack even though the live tree trims it.
    assert set(signals) >= {"jailbreak", "fact_check", "complexity", "domain",
                            "modality", "user_feedback", "pii"}
    # Lookup probs: logit 0.0 for both classes -> p_yes = 0.5 exactly.
    assert signals["jailbreak"]["p_yes"] == pytest.approx(0.5)
    assert instances[0]["score_ms"] > 0
    assert tagger._original_score_calls == 0   # the stub backbone was never used


def test_fixed_scoring_falls_back_to_production_score_on_render_drift(
        monkeypatch):
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(
        matrix, "score_prompts_fixed",
        lambda tagger, prompts, batch=48: ([0.0] * len(prompts),
                                           [1.0] * len(prompts)))
    real_render = tagger._render
    calls = {"n": 0}

    def flaky_render(records):
        calls["n"] += 1
        prompts, counts = real_render(records)
        if calls["n"] > 1:                    # tag() re-render drifts
            prompts = [prompt + "|drift" for prompt in prompts]
        return prompts, counts

    tagger._render = flaky_render
    instances = _instance()
    info = matrix.score_instances_fixed(tagger, instances, batch=4,
                                        n_messages=2)
    assert info["fallback_count"] == len(instances)
    assert tagger._original_score_calls >= 1  # real _score keeps it correct
    assert instances[0]["signals"]["jailbreak"]["p_yes"] == pytest.approx(0.8)


def test_production_scoring_helper_works_with_fake_backbone(monkeypatch):
    tagger = _fake_jev_tagger(monkeypatch)
    instances = _instance()
    info = matrix.score_instances_production(tagger, instances, n_messages=2)
    assert info == {"fallback_count": 0, "prompt_count": 0}
    assert instances[0]["score_ms"] is not None
    assert instances[0]["signals"]["jailbreak"]["p_yes"] == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# A/B runner: scoring path without GPU
# --------------------------------------------------------------------------- #
def test_ab_score_condition_end_to_end(monkeypatch):
    tagger = _fake_jev_tagger(monkeypatch)
    tagger.window = (1024, 0)

    def fake_fixed_score(tagger, records, *, batch=48, length=None,
                         progress=None):
        return [[0.2, 0.8] if len(record["answer_keys"]) == 2
                else [0.1] * len(record["answer_keys"])
                for record in records]

    monkeypatch.setattr(ab.common, "fixed_shape_score", fake_fixed_score)
    rows = [{"id": "r1", "state": "a b c", "signal": "jailbreak",
             "sub": None, "lang": "en", "kind": "noul", "gold": 1,
             "gold_value": None},
            {"id": "r2", "state": "d e", "signal": "jailbreak",
             "sub": None, "lang": "en", "kind": "noul", "gold": 0,
             "gold_value": None}]
    for row in rows:
        row.update({"question": "q", "options": ["no", "yes"],
                    "answer_keys": ["false", "true"]})
    out = ab.score_condition(tagger, rows, "padded4k",
                             ["1024:0", "0:1024"], filler_ids=[1] * 20,
                             target=10, batch=2)
    assert len(out) == 4
    assert {row["arm"] for row in out} == {"1024:0", "0:1024"}
    assert all(row["score_ms"] > 0 for row in out)
    assert all("rule_pii_hits" in row for row in out)
    payload = _report(out)
    binary = _signal(payload, "padded4k", "1024:0", "jailbreak")["binary"]
    # Both rows are predicted positive (fake probs [0.2, 0.8]).
    assert (binary["tp"], binary["fp"], binary["tn"], binary["fn"]) == (1, 1, 0, 0)


# --------------------------------------------------------------------------- #
# matrix: full main() with a fake backbone (sampling / floors / outputs)
# --------------------------------------------------------------------------- #
def _official_eval_rows():
    rows = []
    for index, (signal, sub, positive) in enumerate([
            ("jailbreak", None, 1), ("jailbreak", None, 0),
            ("pii", "org", 1), ("pii", "person", 1), ("pii", "location", 0)]):
        rows.append({
            "id": f"eval:{signal}:{sub or 'x'}:{index}",
            "state": f"state words {index}",
            "question": "q", "kind": "noul", "options": ["no", "yes"],
            "target": [0.0, 1.0] if positive else [1.0, 0.0],
            "metadata": {"signal": signal, "sub": sub, "lang": "en",
                         "text_sha256": f"sha-{index}"},
        })
    return rows


def test_matrix_main_end_to_end_with_fake_backbone(tmp_path, monkeypatch):
    eval_path = _write_jsonl(tmp_path / "eval.jsonl", _official_eval_rows())
    filler_path = tmp_path / "clean_b0.txt"
    filler_path.write_text(" ".join(["filler"] * 40), encoding="utf-8")
    pipeline_path = tmp_path / "pipeline.json"
    from router.pipeline.model import station_to_dict
    pipeline_path.write_text(json.dumps(
        [station_to_dict(station) for station in _demo_stations()]),
        encoding="utf-8")
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(matrix, "_load_tagger", lambda args: tagger)
    monkeypatch.setattr(
        matrix, "score_prompts_fixed",
        lambda tagger, prompts, batch=48: ([0.0] * len(prompts),
                                           [0.5] * len(prompts)))
    out = tmp_path / "matrix.jsonl"
    code = matrix.main([
        "--eval", str(eval_path), "--out", str(out),
        "--filler", str(filler_path),
        "--pipeline-json", str(pipeline_path),
        "--windows", "1024:0,0:1024",
        "--positions", "tail",
        "--floors", "0.15,0.30",
        "--quota", "10", "--pii-target", "10",
        "--production-sample", "2",
    ])
    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert rows, "matrix wrote no rows"
    assert {row["condition"] for row in rows} == {"natural", "padded4k"}
    assert {row["arm"] for row in rows} == {"1024:0", "0:1024"}
    for row in rows:
        assert row["scoring"] == "fixed"
        assert set(row["actions"]) == {"0.15", "0.3"}
        assert row["instance_sha256"]
        # full 7-signal pack recorded per row for audit
        assert "pii" in row["signals"]
    meta = json.loads((tmp_path / "matrix.jsonl.meta.json").read_text())
    assert meta["holdout"]["excluded_ids"] == 0
    assert meta["scoring"] == "fixed"
    assert meta["production_check"]["n_instances"] > 0
    assert meta["written"] == len(rows)
    # report + matrix + production sections are generated automatically
    md = (tmp_path / "matrix.jsonl.report.md").read_text(encoding="utf-8")
    assert "Window × floor" in md
    assert "固定 shape vs 生产评分路径" in md
    json_payload = json.loads(
        (tmp_path / "matrix.jsonl.report.json").read_text(encoding="utf-8"))
    assert json_payload["conditions"]["padded4k"]["arms"]
    assert json_payload["conditions"]["padded4k"]["arms"]["1024:0"][
        "final_actions"]
    assert (tmp_path / "matrix.jsonl.production.jsonl").exists()


# --------------------------------------------------------------------------- #
# A/B runner main() without GPU, and matrix production scoring mode
# --------------------------------------------------------------------------- #
def test_ab_main_end_to_end_with_fake_backbone(tmp_path, monkeypatch):
    eval_path = _write_jsonl(tmp_path / "eval.jsonl", _official_eval_rows())
    filler_path = tmp_path / "clean_b0.txt"
    filler_path.write_text(" ".join(["filler"] * 20), encoding="utf-8")
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(ab, "_load",
                        lambda model, questions, device: tagger)
    monkeypatch.setattr(
        ab.common, "fixed_shape_score",
        lambda tagger, records, *, batch=48, length=None, progress=None:
        [[0.2, 0.8] if len(record["answer_keys"]) == 2
         else [0.1] * len(record["answer_keys"]) for record in records])
    out = tmp_path / "window_ab.jsonl"
    code = ab.main([
        "--eval", str(eval_path), "--filler", str(filler_path),
        "--windows", "1024:0,0:1024", "--conditions", "natural,padded4k",
        "--pipeline-json", "ignored.json",   # accepted for legacy CLI compat
        "--out", str(out),
    ])
    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert len(rows) == 5 * 2 * 2
    assert {row["condition"] for row in rows} == {"natural", "padded4k"}
    assert all(row["score_ms"] > 0 for row in rows)
    meta = json.loads((tmp_path / "window_ab.jsonl.summary.json").read_text())
    assert meta["written"] == len(rows)
    assert "single-signal safety-pack replay was removed" in meta["note"]
    markdown = (tmp_path / "window_ab.jsonl.tables.md").read_text()
    assert "Errata" in markdown and "旧列实际=recall@0.3" in markdown
    assert (tmp_path / "window_ab.jsonl.report.json").exists()


def test_matrix_main_production_scoring_mode(tmp_path, monkeypatch):
    eval_path = _write_jsonl(tmp_path / "eval.jsonl", _official_eval_rows())
    filler_path = tmp_path / "clean_b0.txt"
    filler_path.write_text(" ".join(["filler"] * 20), encoding="utf-8")
    from router.pipeline.model import station_to_dict
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps(
        [station_to_dict(station) for station in _demo_stations()]))
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(matrix, "_load_tagger", lambda args: tagger)
    out = tmp_path / "matrix_prod.jsonl"
    code = matrix.main([
        "--eval", str(eval_path), "--out", str(out),
        "--filler", str(filler_path),
        "--pipeline-json", str(pipeline_path),
        "--windows", "1024:0", "--skip-natural",
        "--positions", "head", "--floors", "0.3",
        "--signals", "jailbreak,pii",
        "--quota", "10", "--pii-target", "10",
        "--scoring", "production", "--no-report",
    ])
    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert rows and all(row["scoring"] == "production" for row in rows)
    assert all(row["score_ms"] is not None for row in rows)
    assert {row["position"] for row in rows} == {"head"}
    meta = json.loads((tmp_path / "matrix_prod.jsonl.meta.json").read_text())
    assert meta["scoring"] == "production"
    assert meta["production_check"] is None
    # Real tag() path: warm-up was paid once for the padded scenario family and
    # the latency definition is recorded so the report's p50/p95 is interpretable.
    warmup = meta["production"]["warmup"]
    assert warmup["count"] == 1
    assert warmup["events"][0]["scenario"] == "padded"
    assert warmup["events"][0]["instances"] >= 1
    assert "tag() wall time" in meta["production"]["latency_definition"]
    assert meta["production"]["compile_note"]


# --------------------------------------------------------------------------- #
# P2-B: fixed-vs-production compare mode
# --------------------------------------------------------------------------- #
def test_parse_compare_scoring_validates_the_pair():
    assert matrix.parse_compare_scoring(None) is None
    assert matrix.parse_compare_scoring("") is None
    assert matrix.parse_compare_scoring("fixed,production") == (
        "fixed", "production")
    assert matrix.parse_compare_scoring("production,fixed") == (
        "fixed", "production")
    for bad in ("fixed", "fixed,production,extra", "fixed,max"):
        with pytest.raises(SystemExit):
            matrix.parse_compare_scoring(bad)


def test_select_conditions_filters_by_label_and_rejects_unknown():
    plan = matrix.condition_plan(True, ["tail"], ["/tmp/clean_b0.txt"])
    assert [entry["label"] for entry in plan] == ["natural", "padded4k"]
    assert [entry["label"] for entry in matrix.select_conditions(plan, None)] == [
        "natural", "padded4k"]
    assert [entry["label"] for entry in matrix.select_conditions(
        plan, "padded4k")] == ["padded4k"]
    with pytest.raises(SystemExit):
        matrix.select_conditions(plan, "padded4k_middle")
    # An empty filter string means "no filter", not "select nothing".
    assert len(matrix.select_conditions(plan, "")) == 2


def _compare_instance(*, sha, row_id, fixed_actions, production_actions,
                      fixed_p=0.9, production_p=0.8, fixed_ms=10.0,
                      production_ms=50.0, signals_extra=None):
    fixed_signals = {"jailbreak": {"p_yes": fixed_p, "confidence": 0.5,
                                   "value": "jailbreak", "is_rule": False}}
    production_signals = {"jailbreak": {"p_yes": production_p,
                                        "confidence": 0.4,
                                        "value": "jailbreak",
                                        "is_rule": False}}
    if signals_extra:
        fixed_signals.update(signals_extra)
        production_signals.update(signals_extra)
    rows = [{"id": row_id, "signal": "jailbreak", "sub": None, "kind": "noul",
             "gold": 1, "gold_value": None, "state": "x"}]
    fixed = {"sha256": sha, "rows": rows, "signals": fixed_signals,
             "rule_hits": [], "actions": fixed_actions, "score_ms": fixed_ms}
    production = {"sha256": sha, "rows": rows,
                  "signals": production_signals, "rule_hits": [],
                  "actions": production_actions, "score_ms": production_ms}
    return fixed, production


def test_build_compare_rows_pairs_by_id_and_reports_delta():
    fixed, production = _compare_instance(
        sha="s1", row_id="r1",
        fixed_actions={"0.3": {"kind": "route", "model": "cheap"}},
        production_actions={"0.3": {"kind": "block", "model": None}},
        fixed_p=0.9, production_p=0.75, fixed_ms=10.0, production_ms=42.0)
    rows = matrix.build_compare_rows([fixed], [production],
                                     condition="padded4k", arm="1024:0",
                                     floors=[0.3])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "r1" and row["instance_sha256"] == "s1"
    assert row["fixed"]["actions"]["0.3"]["kind"] == "route"
    assert row["production"]["actions"]["0.3"]["kind"] == "block"
    assert row["delta"]["own_signal_abs"] == pytest.approx(0.15)
    assert row["delta"]["by_signal"]["jailbreak"] == pytest.approx(0.15)
    # Pairing must be exactly one-to-one: any drift is a hard error.
    with pytest.raises(RuntimeError):
        matrix.build_compare_rows([fixed], [production], condition="c",
                                  arm="a", floors=[0.3])[0:0] or \
            matrix.build_compare_rows(
                [fixed], [dict(production, sha256="other")],
                condition="c", arm="a", floors=[0.3])
    with pytest.raises(RuntimeError):
        matrix.build_compare_rows([fixed], [], condition="c", arm="a",
                                  floors=[0.3])


def _compare_row(*, row_id, flip, fixed_kind="route",
                 fixed_model="cheap", production_kind="block",
                 production_model=None, production_ms=50.0,
                 instance="s1", own_delta=0.1):
    fixed_action = {"kind": fixed_kind, "model": fixed_model}
    production_action = {"kind": production_kind, "model": production_model}
    if not flip:
        fixed_action = dict(production_action)
    return {
        "schema": 1, "condition": "padded4k", "arm": "1024:0",
        "id": row_id, "signal": "jailbreak", "sub": None, "lang": "en",
        "kind": "noul", "gold": 1, "gold_value": None,
        "instance_sha256": instance,
        "fixed": {"p_yes": 0.9, "score_ms": 10.0,
                  "actions": {"0.3": fixed_action, "0.6": fixed_action}},
        "production": {"p_yes": 0.8, "score_ms": production_ms,
                       "actions": {"0.3": production_action,
                                   "0.6": production_action}},
        "delta": {"own_signal_abs": own_delta,
                  "by_signal": {"jailbreak": own_delta}},
    }


def test_report_compare_section_aggregates_flips_and_latency():
    compare_rows = [
        _compare_row(row_id="r1", flip=True, production_ms=40.0,
                     instance="s1", own_delta=0.2),
        _compare_row(row_id="r2", flip=False, production_ms=60.0,
                     instance="s2", own_delta=0.0),
        _compare_row(row_id="r3", flip=True, production_ms=80.0,
                     instance="s3", own_delta=0.4),
    ]
    payload = _report([], floors=[0.3, 0.6], compare_rows=compare_rows,
                      compare_inputs=["/tmp/x.compare.jsonl"])
    compare = payload["compare"]
    assert compare["n_rows"] == 3 and compare["n_groups"] == 1
    entry = compare["groups"]["padded4k"]["1024:0"]
    floor = entry["floors"]["0.3"]
    assert floor["n"] == 3 and floor["flips"] == 2
    assert floor["flip_rate"] == pytest.approx(2 / 3)
    assert floor["flip_rate_ci"][0] <= floor["flip_rate"] <= floor["flip_rate_ci"][1]
    assert floor["transitions"]["route:cheap -> block"] == 2
    # floor .6 flips on the same rows (the helper flips both floors for them)
    assert entry["floors"]["0.6"]["flips"] == 2
    # production latency is a real p50/p95 with CIs from the metrics library
    latency = entry["production_latency"]
    assert latency["n"] == 3
    assert latency["p50"] == 60.0
    # linear-interpolated p95 of [40, 60, 80] = 60 + 0.9 * 20 = 78
    assert latency["p95"] == pytest.approx(78.0)
    assert latency["p50_ci"][0] <= latency["p50_ci"][1]
    # |Δp_yes| over unique texts
    delta = entry["p_yes_delta"]
    assert delta["n"] == 3
    assert delta["max_abs"] == pytest.approx(0.4)
    assert delta["mean_abs"] == pytest.approx(0.2)
    markdown = report.render_markdown(payload)
    assert "## 10. 生产路径复核（全量对比）" in markdown
    assert "flip rate" in markdown
    assert "p50" in markdown
    # No hardcoded numbers: a different data set yields a different rate.
    changed = _report([], floors=[0.3], compare_rows=[
        _compare_row(row_id="r1", flip=False, instance="s1")])
    assert changed["compare"]["groups"]["padded4k"]["1024:0"][
        "floors"]["0.3"]["flip_rate"] == 0.0


def test_report_cli_auto_detects_compare_sidecar(tmp_path):
    rows = [_make_row(p_yes=0.9)]
    input_path = _write_jsonl(tmp_path / "matrix.jsonl", rows)
    _write_jsonl(tmp_path / "matrix.jsonl.compare.jsonl",
                 [_compare_row(row_id="r1", flip=True)])
    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    code = report.main(["--input", str(input_path),
                        "--out-md", str(out_md),
                        "--out-json", str(out_json),
                        "--bootstrap", "50"])
    assert code == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["compare"] is not None
    assert payload["compare"]["n_rows"] == 1
    assert "## 10. 生产路径复核（全量对比）" in out_md.read_text(encoding="utf-8")
    # Explicit --compare-input wins over the sibling auto-detection.
    other = _write_jsonl(tmp_path / "other.compare.jsonl",
                         [_compare_row(row_id="r1", flip=False),
                          _compare_row(row_id="r2", flip=True)])
    code = report.main(["--input", str(input_path),
                        "--compare-input", str(other),
                        "--out-md", str(out_md),
                        "--out-json", str(out_json),
                        "--bootstrap", "50"])
    assert code == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["compare"]["n_rows"] == 2


def test_matrix_main_compare_mode_pairs_rows_end_to_end(tmp_path, monkeypatch):
    eval_path = _write_jsonl(tmp_path / "eval.jsonl", _official_eval_rows())
    filler_path = tmp_path / "clean_b0.txt"
    filler_path.write_text(" ".join(["filler"] * 20), encoding="utf-8")
    from router.pipeline.model import station_to_dict
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps(
        [station_to_dict(station) for station in _demo_stations()]))
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(matrix, "_load_tagger", lambda args: tagger)
    # Fixed pass: p_yes = 0.5 everywhere -> no block.  Production pass: the fake
    # backbone returns p_yes = 0.8 (conf ~0.278) -> block only at floor .15.
    monkeypatch.setattr(
        matrix, "score_prompts_fixed",
        lambda tagger, prompts, batch=48: ([0.0] * len(prompts),
                                           [1.0] * len(prompts)))
    out = tmp_path / "compare_matrix.jsonl"
    code = matrix.main([
        "--eval", str(eval_path), "--out", str(out),
        "--filler", str(filler_path),
        "--pipeline-json", str(pipeline_path),
        "--windows", "1024:0",
        "--positions", "tail",
        "--conditions", "natural,padded4k",
        "--floors", "0.15,0.30",
        "--quota", "10", "--pii-target", "10",
        "--compare-scoring", "fixed,production",
    ])
    assert code == 0
    compare_path = tmp_path / "compare_matrix.jsonl.compare.jsonl"
    assert compare_path.exists()
    compare_rows = [json.loads(line) for line in
                    compare_path.read_text().splitlines() if line]
    main_rows = [json.loads(line) for line in out.read_text().splitlines()
                 if line]
    assert {row["condition"] for row in compare_rows} == {"natural", "padded4k"}
    # Pairing: every fixed output row has exactly one compare row, same id.
    fixed_ids = {(row["condition"], row["arm"], row["id"]) for row in main_rows}
    compare_ids = {(row["condition"], row["arm"], row["id"])
                   for row in compare_rows}
    assert fixed_ids == compare_ids
    for row in compare_rows:
        assert set(row["fixed"]["actions"]) == {"0.15", "0.3"}
        assert set(row["production"]["actions"]) == {"0.15", "0.3"}
    meta = json.loads((tmp_path / "compare_matrix.jsonl.meta.json").read_text())
    assert meta["scoring"] == "compare"
    assert meta["compare"]["rows"] == len(compare_rows)
    # One warm-up per scenario family: natural shapes and padded equal-length
    # shapes are warmed before their measured rows.
    warmup = meta["production"]["warmup"]
    assert warmup["count"] == 2
    assert [event["scenario"] for event in warmup["events"]] == [
        "natural", "padded"]
    assert all(event["instances"] >= 1 for event in warmup["events"])
    assert meta["conditions"] == ["natural", "padded4k"]
    # Report §10 is generated automatically with the flip table.
    md = (tmp_path / "compare_matrix.jsonl.report.md").read_text()
    assert "## 10. 生产路径复核（全量对比）" in md
    payload = json.loads(
        (tmp_path / "compare_matrix.jsonl.report.json").read_text())
    floor = payload["compare"]["groups"]["padded4k"]["1024:0"]["floors"]
    # Production p_yes=0.8 (conf ~0.278) makes the jailbreak condition match on
    # every row (full signal pack) at floor .15; at .30 it misses again.
    assert floor["0.15"]["flips"] == len(compare_rows) // 2
    assert floor["0.3"]["flips"] == 0


def test_matrix_main_conditions_filter_end_to_end(tmp_path, monkeypatch):
    eval_path = _write_jsonl(tmp_path / "eval.jsonl", _official_eval_rows())
    filler_path = tmp_path / "clean_b0.txt"
    filler_path.write_text(" ".join(["filler"] * 20), encoding="utf-8")
    from router.pipeline.model import station_to_dict
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps(
        [station_to_dict(station) for station in _demo_stations()]))
    tagger = _fake_jev_tagger(monkeypatch)
    monkeypatch.setattr(matrix, "_load_tagger", lambda args: tagger)
    monkeypatch.setattr(
        matrix, "score_prompts_fixed",
        lambda tagger, prompts, batch=48: ([0.0] * len(prompts),
                                           [1.0] * len(prompts)))
    out = tmp_path / "filtered.jsonl"
    code = matrix.main([
        "--eval", str(eval_path), "--out", str(out),
        "--filler", str(filler_path),
        "--pipeline-json", str(pipeline_path),
        "--windows", "1024:0",
        "--conditions", "padded4k",
        "--floors", "0.15",
        "--quota", "10", "--pii-target", "10",
    ])
    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert rows and {row["condition"] for row in rows} == {"padded4k"}
    meta = json.loads((tmp_path / "filtered.jsonl.meta.json").read_text())
    assert meta["conditions"] == ["padded4k"]


def test_matrix_rejects_conflicting_verification_modes(tmp_path, monkeypatch):
    eval_path = _write_jsonl(tmp_path / "eval.jsonl", _official_eval_rows())
    filler_path = tmp_path / "clean_b0.txt"
    filler_path.write_text("x", encoding="utf-8")
    base = ["--eval", str(eval_path), "--out", str(tmp_path / "o.jsonl"),
            "--filler", str(filler_path)]
    with pytest.raises(SystemExit):
        matrix.main(base + ["--compare-scoring", "fixed,production",
                            "--production-sample", "5"])
    with pytest.raises(SystemExit):
        matrix.main(base + ["--compare-scoring", "fixed,production",
                            "--scoring", "production"])
