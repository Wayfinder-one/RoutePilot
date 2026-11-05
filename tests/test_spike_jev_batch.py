import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

from router.jev.client import NoulAnswer
from router.routing.graph.validate import EnableContext, validate_graph_for_enable
from scripts.spike_jev_batch import (
    BudgetExceeded,
    ResponseRecord,
    Sample,
    SpikeAbort,
    SpikeTransport,
    build_g3_tree,
    build_question_groups,
    calibrate_budget,
    condition_passes,
    expand_questions,
    load_samples,
    main,
    percentile,
    plan_request_budget,
    replay_g3,
    route_outcome,
    run_equivalence,
    run_live_g3,
    threshold_records,
    wilson_interval,
    window_budget,
)


def test_full_budget_is_1980_and_each_stage_is_explicit():
    budget = plan_request_budget(80)
    assert (budget.h1, budget.repeats, budget.scan, budget.concurrency, budget.reserve) == (960, 60, 120, 640, 200)
    assert budget.total == 1980


def test_budget_rejects_unknown_scenario_and_bad_sample_count():
    with pytest.raises(ValueError):
        plan_request_budget(0)
    with pytest.raises(ValueError):
        plan_request_budget(81)
    with pytest.raises(ValueError):
        plan_request_budget(20, "unknown")


def test_groups_and_g3_use_real_question_shapes():
    groups = build_question_groups()
    assert {key: len(value) for key, value in groups.items()} == {"G1": 2, "G2": 3, "G3": 4}
    assert [q.question_type for q in groups["G3"].values()] == ["choice", "choice", "noul", "score"]
    tree = build_g3_tree()
    issues = validate_graph_for_enable(tree, EnableContext(
        known_models={"mock"}, model_reasoning={"mock": None},
        known_periods=set(), jev_available=True,
    ))
    assert issues == []
    assert tree.root.error_fallback == "t-error"
    assert [(edge.source_port, edge.target, edge.condition.op, edge.condition.value)
            for edge in tree.root.edges[1:4]] == [
        ("prob.strong", "q2", "gt", 0.65),
        ("prob.strong", "q3", "gte", 0.5),
        ("yes", "q4", "gt", 0.5),
    ]


def test_load_samples_validates_and_applies_limit(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text("".join(
        f'{{"id":"s{i}","text":"文本{i}","length_bucket":"short","boundary_tags":[]}}\n'
        for i in range(60)
    ), encoding="utf-8")
    loaded = load_samples(path)
    assert len(loaded) == 60
    assert [item.id for item in loaded[:2]] == ["s0", "s1"]
    assert len(load_samples(path, 1)) == 1


class FakeHTTPResponse:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode()
        self.status = status

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.body


def success_body():
    return {"answers": {"urgent": {"type": "noul", "noul": 0.6}},
            "usage": {"input_tokens": 123, "output_tokens": 0}}


def test_transport_sends_wire_and_never_needs_to_log_key(monkeypatch):
    captured = {}
    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data)
        return FakeHTTPResponse(success_body())
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    transport = SpikeTransport("http://jev.test", "d1-2B", api_key="secret", sleep_seconds=0)
    record = transport.post("state", {"urgent": build_question_groups()["G1"]["urgent"]}, "test")
    assert isinstance(record, ResponseRecord) and record.input_tokens == 123
    assert captured["authorization"] == "Bearer secret"
    assert captured["payload"]["questions"]["urgent"] == {"type": "noul", "instructions": "这条用户请求描述的告警或故障是否紧急，是否需要立即处置？"}


def test_transport_stops_on_second_consecutive_5xx(monkeypatch):
    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", None, BytesIO(b"{}"))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    transport = SpikeTransport("http://jev.test", "d1-2B", sleep_seconds=0)
    question = {"urgent": build_question_groups()["G1"]["urgent"]}
    transport.post("state", question, "test")
    with pytest.raises(SpikeAbort):
        transport.post("state", question, "test")
    assert transport.count == 2


def test_transport_aborts_on_third_consecutive_transport_error_and_resets(monkeypatch):
    """I3：连续 3 次 URLError/超时即中止；中间任何一个 HTTP 响应都会重置计数。"""
    calls = {"n": 0}

    def flaky(request, timeout):
        calls["n"] += 1
        if calls["n"] == 3:
            return FakeHTTPResponse(success_body())
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    transport = SpikeTransport("http://jev.test", "d1-2B", sleep_seconds=0)
    question = {"urgent": build_question_groups()["G1"]["urgent"]}
    transport.post("state", question, "test")   # transport error 1
    transport.post("state", question, "test")   # transport error 2
    transport.post("state", question, "test")   # HTTP 200 -> 计数清零
    transport.post("state", question, "test")   # transport error 1（清零后重新计数）
    transport.post("state", question, "test")   # transport error 2
    with pytest.raises(SpikeAbort):
        transport.post("state", question, "test")  # transport error 3 -> abort
    assert transport.count == 6


def test_transport_rejects_request_before_io_at_cap(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args: (_ for _ in ()).throw(AssertionError("network used")))
    transport = SpikeTransport("http://jev.test", "d1-2B", max_requests=0, sleep_seconds=0)
    with pytest.raises(BudgetExceeded):
        transport.post("state", {"urgent": build_question_groups()["G1"]["urgent"]}, "test")
    assert transport.count == 0


def test_wilson_zero_flip_still_has_nonzero_upper_bound():
    low, high = wilson_interval(0, 100)
    assert low == 0.0
    assert high == pytest.approx(0.036994807476, abs=1e-12)
    with pytest.raises(ValueError):
        wilson_interval(1, 0)


def test_condition_gt_and_gte_are_not_merged():
    tree = build_g3_tree()
    q3 = next(node for node in tree.root.nodes if node.id == "q3")
    assert condition_passes(q3, "yes", "gt", 0.5, 0.5) is False
    assert condition_passes(q3, "yes", "gte", 0.5, 0.5) is True


def test_strict_a_validates_all_but_c_ignores_inactive_failures():
    question = build_question_groups()["G3"]["q1"]
    probabilities = {
        candidate.label: 0.1 if candidate.key == "strong" else 0.9
        for candidate in question.options
    }
    q1 = ResponseRecord("test", 200, {
        "answers": {"q1": {"type": "choice", "probabilities": probabilities}},
        "usage": {"input_tokens": 10},
    }, None, 1.0, 10)
    failed = ResponseRecord("test", 500, None, "HTTP 500", 1.0, None)
    records = {"q1": q1, "q2": failed, "q3": failed, "q4": failed}
    tree = build_g3_tree()
    c_decision = replay_g3(tree, "文本", records, strict_all=False)
    a_decision = replay_g3(tree, "文本", records, strict_all=True)
    assert route_outcome(c_decision, len(c_decision.waves))["activated"] == ("q1",)
    assert route_outcome(c_decision, len(c_decision.waves))["failed"] is False
    assert route_outcome(a_decision, 1)["failed"] is True
    assert route_outcome(a_decision, 1)["target"] == "t-error"


class FakeH1Transport:
    def __init__(self):
        self.count = 0

    def post(self, state, questions, stage):
        self.count += 1
        answers = {}
        for key, question in questions.items():
            if question.question_type == "choice":
                labels = [item.label for item in question.options]
                probabilities = {label: 0.7 / 1 if i == 0 else 0.3 / (len(labels) - 1)
                                 for i, label in enumerate(labels)}
                answers[key] = {"type": "choice", "probabilities": probabilities}
            elif question.question_type == "noul":
                answers[key] = {"type": "noul", "noul": 0.6}
            else:
                answers[key] = {"type": "score", "score": 0,
                                "legend": {str(i): value for i, value in enumerate(question.criteria)}}
        return ResponseRecord(stage, 200, {"answers": answers, "usage": {"input_tokens": 100}},
                              None, 1.0, 100)


def test_small_and_full_share_cache_without_repeating_requests():
    samples = [Sample(f"s{i}", f"文本{i}", "short") for i in range(60)]
    groups = build_question_groups()
    tree = build_g3_tree()
    fake = FakeH1Transport()
    cache = {}
    run_equivalence(samples[:20], groups, tree, fake, cache=cache,
                    group_names=("G1", "G2"), repeat_samples=20)
    assert fake.count == 20 * 7 + 20 * 2
    second = run_equivalence(samples, groups, tree, fake, cache=cache,
                             group_names=("G1", "G2", "G3"), repeat_samples=20)
    assert fake.count == 60 * 12 + 20 * 3
    assert len(second["routes"]) == 60
    assert all(row["a_http_requests"] == 1 for row in second["routes"])


def test_threshold_records_keep_gt_and_gte_separate():
    question = build_question_groups()["G1"]["urgent"]
    a = NoulAnswer(0.5, 0.5, {}, 0.0)
    b = NoulAnswer(0.5001, 0.4999, {}, 0.0)
    rows = threshold_records("s1", "G1", "urgent", question, a, b)
    gt = next(row for row in rows if row["op"] == "gt" and row["threshold"] == 0.5)
    gte = next(row for row in rows if row["op"] == "gte" and row["threshold"] == 0.5)
    assert gt["flipped"] is True
    assert gte["flipped"] is False


def test_percentile_uses_nearest_rank():
    assert percentile([10, 20, 30, 40], 0.5) == 20
    assert percentile([10, 20, 30, 40], 0.95) == 40


def test_expand_questions_has_unique_ids_and_stable_shapes():
    source = build_question_groups()["G2"]
    expanded = expand_questions(source, 8)
    assert list(expanded) == [f"q{i}" for i in range(1, 9)]
    assert [q.question_type for q in expanded.values()] == [
        "choice", "score", "noul", "choice", "score", "noul", "choice", "score"
    ]


class EarlyStopTransport:
    def __init__(self):
        self.count = 0

    def post(self, state, questions, stage):
        self.count += 1
        assert list(questions) == ["q1"]
        question = questions["q1"]
        probabilities = {
            item.label: 0.1 if item.key == "strong" else 0.9
            for item in question.options
        }
        return ResponseRecord(stage, 200, {
            "answers": {"q1": {"type": "choice", "probabilities": probabilities}},
            "usage": {"input_tokens": 100},
        }, None, 1.0, 100)


def test_live_current_path_stops_after_failed_activation_condition():
    fake = EarlyStopTransport()
    decision, records = run_live_g3(build_g3_tree(), "文本", fake)
    assert fake.count == 1
    assert len(records) == 1
    assert tuple(decision.answers) == ("q1",)


class ScriptedH1Transport:
    """确定性的 H1 假传输：可丢某条答案制造 invalid，也可只让 repeat 路径漂移。"""

    def __init__(self, *, drop=None, repeat_shift=0.0):
        self.count = 0
        self.drop = drop
        self.repeat_shift = repeat_shift

    def post(self, state, questions, stage):
        self.count += 1
        answers = {}
        for key, question in questions.items():
            if self.drop is not None and self.drop(stage, state, key):
                continue
            if question.question_type == "choice":
                labels = [item.label for item in question.options]
                probabilities = {label: 0.7 if i == 0 else 0.3 / (len(labels) - 1)
                                 for i, label in enumerate(labels)}
                answers[key] = {"type": "choice", "probabilities": probabilities}
            elif question.question_type == "noul":
                shift = self.repeat_shift if stage == "H1-repeat" else 0.0
                answers[key] = {"type": "noul", "noul": 0.6 + shift}
            else:
                answers[key] = {"type": "score", "score": 0,
                                "legend": {str(i): value for i, value in enumerate(question.criteria)}}
        return ResponseRecord(stage, 200, {"answers": answers, "usage": {"input_tokens": 100}},
                              None, 1.0, 100)


def test_h1_invalid_comparison_is_conditional_and_kept_in_invalid_comparisons():
    samples = [Sample(f"s{i}", f"文本{i}", "short") for i in range(4)]
    fake = ScriptedH1Transport(
        drop=lambda stage, state, key: stage == "H1-A" and "文本0" in state and key == "urgent")
    result = run_equivalence(samples, build_question_groups(), build_g3_tree(), fake,
                             cache={}, group_names=("G1",), repeat_samples=0)
    summary = result["summary"]
    assert summary["status"] == "conditional"
    assert summary["fail_reasons"] == []
    assert summary["noise_ok"] is False
    assert any("invalid comparison" in reason for reason in summary["conditional_reasons"])
    assert [(row["sample_id"], row["group"], row["question_id"], row["kind"])
            for row in result["invalid_comparisons"]] == [("s0", "G1", "urgent", "a_b")]
    assert result["invalid_comparisons"][0]["a_error"] is not None
    assert summary["groups"]["G1"]["valid_comparisons"] == 7
    assert summary["groups"]["G1"]["invalid_comparisons"] == 1


def test_h1_repeat_drift_above_tolerance_is_conditional_with_noise_off():
    samples = [Sample(f"s{i}", f"文本{i}", "short") for i in range(4)]
    fake = ScriptedH1Transport(repeat_shift=0.05)
    result = run_equivalence(samples, build_question_groups(), build_g3_tree(), fake,
                             cache={}, group_names=("G1",), repeat_samples=4)
    summary = result["summary"]
    assert summary["max_ab"] == 0.0
    assert summary["max_repeat"] == pytest.approx(0.05)
    assert summary["repeat"]["observations"] == 4
    assert summary["noise_ok"] is False
    assert summary["fail_reasons"] == []
    assert any("max_repeat" in reason for reason in summary["conditional_reasons"])
    assert summary["status"] == "conditional"


def test_h1_zero_flip_aggregated_threshold_keeps_wilson_high():
    samples = [Sample(f"s{i}", f"文本{i}", "short") for i in range(4)]
    result = run_equivalence(samples, build_question_groups(), build_g3_tree(),
                             ScriptedH1Transport(), cache={},
                             group_names=("G1", "G2"), repeat_samples=0)
    aggregated = result["thresholds"]["aggregated"]
    assert aggregated
    for row in aggregated:
        assert row["flips"] == 0
        assert row["rate"] == 0.0
        assert row["wilson_low"] == 0.0
        assert row["wilson_high"] > 0.0
    noul_row = next(row for row in aggregated if row["group"] == "G1"
                    and row["question_id"] == "urgent" and row["port"] == "yes"
                    and row["op"] == "gt" and row["threshold"] == 0.5)
    assert noul_row["flips"] == 0 and noul_row["total"] == 4
    assert noul_row["wilson_high"] > 0.0


def test_window_budget_includes_calibration_safety_and_minimum_input():
    enabled = window_budget(4096, 2048, raw_overhead=512, calibration=1.25, safety=128)
    assert enabled.effective_context == 3174
    assert enabled.enabled is True
    assert enabled.state_budget == 2048
    disabled = window_budget(4096, 2048, raw_overhead=2919, calibration=1.25, safety=128)
    assert disabled.enabled is False
    assert disabled.state_budget < 256


def test_calibration_is_an_upper_bound_not_mean_fit():
    result = calibrate_budget([
        {"status": 200, "wire_est_tokens": 100, "input_tokens": 110},
        {"status": 200, "wire_est_tokens": 200, "input_tokens": 180},
    ])
    assert result["calibration"] == 1.1
    assert result["safety"] == 128
    assert all(item["input_tokens"] <= item["wire_est_tokens"] * result["calibration"] + result["safety"]
               for item in result["records"])


def test_dry_run_prints_budget_without_network(tmp_path, capsys):
    samples = tmp_path / "samples.jsonl"
    rows = [{"id": f"s{i}", "text": "文本", "length_bucket": "short", "boundary_tags": []} for i in range(60)]
    samples.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    code = main(["--samples", str(samples), "--scenario", "all", "--dry-run", "--out-dir", str(tmp_path / "out")])
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["planned_requests"] == 1740
    assert not (tmp_path / "out").exists()


def test_capacity_observations_use_the_posted_rendered_state():
    """H2 发送的是 render_state(state_text)，校准观测必须用同一份 wire 定义。"""
    import scripts.spike_jev_batch as spike

    class RecordingTransport(ScriptedH1Transport):
        def __init__(self):
            super().__init__()
            self.states = []

        def post(self, state, questions, stage):
            self.states.append(state)
            return super().post(state, questions, stage)

    groups = build_question_groups()
    transport = RecordingTransport()
    capacity = spike.run_capacity(
        transport, groups, build_g3_tree(), scan_repeats=1, rounds=1, workers=1)
    state_text = capacity["scan"]["state_text"]
    rendered = spike.render_state(state_text)
    assert transport.states and all(state == rendered for state in transport.states)
    rows = spike._observations_from_capacity(capacity, groups)
    assert rows
    for row in rows:
        questions = expand_questions(groups["G2"], row["question_count"])
        canonical = spike.canonical_questions(questions)
        assert row["state_est_tokens"] == spike.estimate_tokens(rendered)
        assert row["wire_est_tokens"] == spike.estimate_tokens(rendered + "\n" + canonical)
        # 模板开销必须计入：用它估算裸 state_text 会系统性偏小。
        assert row["wire_est_tokens"] > spike.estimate_tokens(state_text + "\n" + canonical)
