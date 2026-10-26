"""scripts/cheap_eval 的 CPU 单测（P1-E）。

全部用合成数据/注入后端：**不依赖 GPU、网络、numpy、scikit-learn**。
覆盖：统一指标库复用、策略评估与 CI 非劣判定、manifest/resume 拒绝、
四段划分与嵌套 CV 隔离性、真实默认树回放、可注入 C/S 客户端、报告 caveat、
以及 legacy 数据上的 analyze 端到端 CPU 冒烟。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import router.eval_metrics as eval_metrics  # noqa: E402
from cheap_eval import (cli, classifier, generate, io, metrics, report,  # noqa: E402
                        strategies)


# --------------------------------------------------------------------------- #
# 合成数据helpers
# --------------------------------------------------------------------------- #
def make_item(index: int, *, c_correct: bool, s_correct: bool,
              complexity: str = "easy", conf: float = 0.9,
              domain: str = "math") -> dict:
    hard = complexity == "hard"
    return {
        "id": f"q{index:03d}",
        "index": index,
        "question": f"question {index}",
        "gold": float(index),
        "c_correct": c_correct,
        "s_correct": s_correct,
        "c_output": "Answer: 1",
        "s_output": "Answer: 1",
        "c_pred": 1.0,
        "s_pred": 1.0,
        "c_latency_s": 0.5,
        "s_latency_s": 1.5,
        "c_n_new_tokens": 100,
        "c_truncated": False,
        "s_finish": "stop",
        "jev": {
            "complexity": {"value": complexity, "confidence": conf,
                           "is_rule": False, "p_yes": 0.2 if not hard else 0.9,
                           "margin": conf},
            "domain": {"value": domain, "confidence": 0.8, "is_rule": False,
                       "prob_by_value": {domain: 0.7},
                       "p_top": 0.7, "margin": 0.5},
            "fact_check": {"value": "no_check", "confidence": 0.1,
                           "is_rule": False, "p_yes": 0.2},
            "jailbreak": {"value": "benign", "confidence": 0.6,
                          "is_rule": False, "p_yes": 0.1},
            "modality": {"value": "text", "confidence": 0.99,
                         "is_rule": False, "p_top": 0.99},
        },
        "input": {"input_tokens": 30},
        "token_count": 30,
    }


def make_correctness_items(n: int = 40) -> list[dict]:
    """确定性的 C/S 正确性：C 约 60%，S 约 95%（有足够两类的区分度）。"""
    items = []
    for index in range(n):
        c_ok = index % 5 != 0                      # 80% 太容易，改成 3/5
        c_ok = index % 5 != 0 and index % 3 != 0   # ≈ 53%
        s_ok = index % 10 != 7                     # 90%
        items.append(make_item(index, c_correct=c_ok, s_correct=s_ok,
                               complexity="hard" if index % 4 == 0 else "easy",
                               conf=0.8 if index % 4 == 0 else 0.9))
    return items


def write_legacy_stage_files(out: Path, items: list[dict]) -> None:
    """按旧 MVP 的 JSONL schema 写 data/c_gen/s_gen/jev（无 manifest）。"""
    out.mkdir(parents=True, exist_ok=True)
    data_rows, c_rows, s_rows, jev_rows = [], [], [], []
    for item in items:
        data_rows.append({"id": item["id"], "index": item["index"],
                          "question": item["question"], "gold": item["gold"],
                          "gold_raw": str(item["gold"])})
        c_rows.append({"id": item["id"], "output": item["c_output"],
                       "predicted": item["c_pred"],
                       "correct": item["c_correct"],
                       "n_new_tokens": item["c_n_new_tokens"],
                       "latency_s": item["c_latency_s"],
                       "truncated": item["c_truncated"]})
        s_rows.append({"id": item["id"], "predicted": item["s_pred"],
                       "correct": item["s_correct"],
                       "output": item["s_output"],
                       "reasoning_output": "",
                       "finish_reason": item["s_finish"],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 50},
                       "latency_s": item["s_latency_s"]})
        jev_rows.append({"id": item["id"], "signals": item["jev"],
                         "input": item["input"],
                         "token_count": item["token_count"],
                         "info": {"engine": "jev", "model_name": "fake-jev",
                                  "model_dir": "/fake/jev",
                                  "window": "1024:0",
                                  "questions_sha256": "q-sha-1"}})
    for name, rows in (("data", data_rows), ("c_gen", c_rows),
                       ("s_gen", s_rows), ("jev", jev_rows)):
        io.write_jsonl_atomic(out / f"{name}.jsonl", rows)


def legacy_analyze_argv(out: Path, *extra: str) -> list[str]:
    return ["--out-dir", str(out), "--stages", "analyze", "--legacy-import",
            "--classifier-mode", "prior", "--n-boot", "120",
            "--limit", "40", *extra]


class FakeRouter:
    """测试用 router：概率 = 特征第 0 维（完全不看标签）。"""

    def __init__(self) -> None:
        self.fit_calls: list[dict] = []

    def fit(self, X_train, y_train, X_cal, y_cal) -> None:
        self.fit_calls.append({"n_train": len(X_train), "n_cal": len(X_cal),
                               "y_train": list(y_train)})

    def predict_proba(self, X) -> list[float]:
        return [float(row[0]) for row in X]

    def describe(self) -> dict:
        return {"type": "fake", "calibration": "identity"}


def score_items(n: int = 60) -> list[dict]:
    """合成可分的 C-正确概率特征（feature = item["score"]）。"""
    items = []
    for index in range(n):
        good = index % 2 == 0
        item = make_item(index, c_correct=good, s_correct=True)
        item["score"] = 0.9 if good else 0.1
        items.append(item)
    return items


# --------------------------------------------------------------------------- #
# A. 纯指标 / 策略评估（复用 router.eval_metrics）
# --------------------------------------------------------------------------- #
def test_eval_metrics_is_reused_not_reimplemented() -> None:
    assert metrics.Confusion is eval_metrics.Confusion
    assert metrics.bootstrap_delta_ci is eval_metrics.bootstrap_delta_ci
    assert metrics.ece is eval_metrics.ece


def test_evaluate_route_known_numbers() -> None:
    items = [make_item(0, c_correct=True, s_correct=True),
             make_item(1, c_correct=True, s_correct=False),
             make_item(2, c_correct=False, s_correct=True),
             make_item(3, c_correct=False, s_correct=False)]
    route_c = [True, True, True, True]
    entry = metrics.evaluate_route(items, route_c)
    assert entry["n"] == 4
    assert entry["cheap_coverage"] == 1.0
    assert entry["accuracy"] == 0.5                     # c: T,T,F,F
    assert entry["regret_count"] == 1                   # 题2 C 错 S 对
    assert entry["wasted_strong_count"] == 0
    assert entry["avg_cost_units"] == 1.0
    entry_s = metrics.evaluate_route(items, [False] * 4)
    assert entry_s["accuracy"] == 0.5                   # s: T,F,T,F
    assert entry_s["avg_cost_units"] == 10.0
    assert entry_s["cost_ratio_vs_always_s"] == 1.0
    # regret: route C 且 C 错而 S 对；wasted: route S 且 C 对
    entry_mix = metrics.evaluate_route(items, [True, False, True, False])
    assert entry_mix["regret_count"] == 1
    assert entry_mix["wasted_strong_count"] == 1
    assert entry_mix["cheap_coverage"] == 0.5


def test_cheap_sufficient_confusion_uses_unified_metrics() -> None:
    items = [make_item(0, c_correct=True, s_correct=True),
             make_item(1, c_correct=False, s_correct=True),
             make_item(2, c_correct=True, s_correct=True),
             make_item(3, c_correct=False, s_correct=True)]
    conf = metrics.cheap_sufficient_confusion(items, [True, True, False, False])
    # gold = C 是否对（T,F,T,F），pred = 是否走 C（T,T,F,F） → tp=1, fp=1, tn=1, fn=1
    assert (conf["tp"], conf["fp"], conf["tn"], conf["fn"]) == (1, 1, 1, 1)
    assert conf["fpr"] == 0.5 and conf["fdr"] == 0.5


def test_quality_delta_ci_and_noninferior_ci_lower_bound() -> None:
    # C 3/4 对、S 1/4 对 → route C 相对 always-S 的 Δacc = +0.5
    items = [make_item(0, c_correct=True, s_correct=True),
             make_item(1, c_correct=True, s_correct=False),
             make_item(2, c_correct=True, s_correct=False),
             make_item(3, c_correct=False, s_correct=False)]
    same = metrics.quality_delta_ci(items, [False] * 4, n_boot=200, seed=0)
    assert same["delta"] == 0.0 and same["ci_low"] == 0.0
    first = metrics.quality_delta_ci(items, [True] * 4, n_boot=500, seed=0)
    second = metrics.quality_delta_ci(items, [True] * 4, n_boot=500, seed=0)
    assert first == second                              # 确定性
    assert abs(first["delta"] - 0.5) < 1e-12
    # C 更差的反向例子
    worse = [make_item(0, c_correct=False, s_correct=True),
             make_item(1, c_correct=False, s_correct=True),
             make_item(2, c_correct=False, s_correct=True),
             make_item(3, c_correct=False, s_correct=True)]
    worse_ci = metrics.quality_delta_ci(worse, [True] * 4, n_boot=200, seed=0)
    assert worse_ci["delta"] == -1.0
    assert metrics.noninferior(worse_ci, 0.01) is False
    # 「点估计非劣、但 CI 下界不达标」的 ci 不能判非劣
    assert metrics.noninferior({"delta": -0.005, "ci_low": -0.03,
                                "ci_high": 0.02}, 0.01) is False
    assert metrics.noninferior({"delta": -0.005, "ci_low": -0.008,
                                "ci_high": 0.02}, 0.01) is True


def test_select_operating_point_rejects_point_estimate_only_winner() -> None:
    """复核问题 ② 的回归：点估计刚好在容差内、但 CI 下界越界的阈值必须被拒。"""
    items, scores = [], []
    for index in range(100):
        if index == 0:      # 唯一一条 C 错 S 对（regret），且分数最高
            items.append(make_item(0, c_correct=False, s_correct=True))
            scores.append(0.90)
        else:
            items.append(make_item(index, c_correct=True, s_correct=True))
            scores.append(0.99)
    full_route = metrics.route_at_threshold(scores, 0.5)
    full_ci = metrics.quality_delta_ci(items, full_route, n_boot=2000, seed=0)
    assert abs(full_ci["delta"] - (-0.01)) < 1e-12      # 点估计 == -tol
    assert metrics.noninferior(full_ci, 0.01) is False   # 但 CI 下界 < -tol
    selected = metrics.select_operating_point(items, scores, tol=0.01,
                                              n_boot=2000, seed=0)
    assert selected is not None
    assert selected["threshold"] == 0.99                # 把唯一 regret 题升级 S
    assert selected["cheap_coverage"] == 0.99
    assert selected["accuracy"] == 1.0
    assert selected["noninferior"] is True


def test_select_operating_point_max_coverage_under_ci() -> None:
    items, scores = [], []
    for index in range(60):
        good = index < 30
        items.append(make_item(index, c_correct=good, s_correct=True))
        scores.append(0.9 if good else 0.1)
    selected = metrics.select_operating_point(items, scores, tol=0.0,
                                              n_boot=500, seed=0)
    assert selected is not None
    assert selected["cheap_coverage"] == 0.5
    assert selected["delta_vs_always_s"]["delta"] == 0.0


def test_tolerance_sensitivity_labels_and_schema() -> None:
    items, scores = [], []
    for index in range(40):
        good = index < 20
        items.append(make_item(index, c_correct=good, s_correct=True))
        scores.append(0.9 if good else 0.1)
    out = metrics.tolerance_sensitivity(items, scores,
                                        tolerances=(0.0, 0.05),
                                        n_boot=300, seed=0)
    assert set(out) == {"0pp", "5pp"}
    for entry in out.values():
        assert entry is None or set(entry) >= {"cheap_coverage", "accuracy",
                                               "threshold", "delta_vs_always_s"}


def test_prior_summary_and_classification_diagnostics() -> None:
    items = [make_item(0, c_correct=True, s_correct=True),
             make_item(1, c_correct=True, s_correct=False),
             make_item(2, c_correct=False, s_correct=True),
             make_item(3, c_correct=False, s_correct=False)]
    prior = metrics.prior_summary(items, n_boot=200, seed=0)
    assert prior["n"] == 4
    assert prior["c_accuracy"] == 0.5
    assert prior["s_accuracy"] == 0.5
    assert prior["oracle_accuracy"] == 0.75
    assert prior["cross_table"] == {"both_correct": 1, "c_only_correct": 1,
                                    "s_only_correct": 1, "both_wrong": 1}
    for key in ("c", "s", "oracle"):
        ci = prior["accuracy_ci95"][key]
        assert ci["ci_low"] <= ci["accuracy"] <= ci["ci_high"]
    diag = metrics.classification_diagnostics([1.0, 0.0, 1.0], [True, False, True])
    assert diag["brier"] == pytest.approx(0.0)
    assert diag["ece"] == pytest.approx(0.0)
    assert diag["mean_prob"] == pytest.approx(2 / 3)


# --------------------------------------------------------------------------- #
# B. io / run manifest / resume
# --------------------------------------------------------------------------- #
def test_jsonl_atomic_roundtrip_and_strict_errors(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    io.write_jsonl_atomic(path, [{"id": "a", "v": 1}, {"id": "b", "v": 2}])
    assert io.read_jsonl(path) == [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    io.append_jsonl(path, {"id": "c", "v": 3})
    assert io.count_jsonl(path) == 3
    assert io.by_id(io.read_jsonl(path))["c"]["v"] == 3
    with pytest.raises(io.CheapEvalError):
        io.by_id([{"id": "a"}, {"id": "a"}])
    path.write_text('{"id": "a"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(io.JsonlError):
        io.read_jsonl(path)
    path.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(io.JsonlError):
        io.read_jsonl(path)


def test_manifest_lifecycle_and_resume_checks(tmp_path: Path) -> None:
    identity = {"dataset_sha256": "d1", "prompt": {"sha256": "p1"},
                "code": {"git_commit": "c0", "package_sha256": "k1"}}
    manifest = io.begin_stage(tmp_path, "c", identity=identity,
                              provenance={"host": "test"})
    assert manifest.status == "running"
    assert io.load_manifest(tmp_path, "c").status == "running"
    rows_path = io.stage_rows_path(tmp_path, "c")
    io.write_jsonl_atomic(rows_path, [{"id": "q1"}, {"id": "q2"}])
    assert io.probe_stage(tmp_path, "c", identity).state == "running"
    io.finish_stage(tmp_path, manifest)
    state = io.probe_stage(tmp_path, "c", identity)
    assert state.state == "complete" and state.artifact_rows == 2
    assert (tmp_path / io.RUN_MANIFEST_FILE).is_file()

    # 1) identity 不一致 → 拒绝，并带差异路径
    with pytest.raises(io.ManifestMismatchError) as excinfo:
        io.probe_stage(tmp_path, "c", {**identity, "prompt": {"sha256": "p2"}})
    assert "prompt.sha256" in str(excinfo.value)
    # 2) 允许显式放宽单个前缀（如重新 commit 后复用数据）
    relaxed = io.probe_stage(tmp_path, "c",
                             {**identity, "code": {"git_commit": "c1",
                                                   "package_sha256": "k1"}},
                             relax_prefixes=("code.git_commit",))
    assert relaxed.state == "complete"
    # 3) 产物被手工改动 → complete 也拒绝
    io.append_jsonl(rows_path, {"id": "q3"})
    with pytest.raises(io.ManifestMismatchError):
        io.probe_stage(tmp_path, "c", identity)


def test_manifest_missing_and_legacy_import(tmp_path: Path) -> None:
    io.write_jsonl_atomic(tmp_path / "c_gen.jsonl", [{"id": "q1"}])
    with pytest.raises(io.ManifestError):
        io.probe_stage(tmp_path, "c", {"dataset_sha256": "d1"})
    state = io.probe_stage(tmp_path, "c", {"dataset_sha256": "d1"},
                           allow_legacy=True)
    assert state.state == "legacy" and state.artifact_rows == 1
    missing_both = io.probe_stage(tmp_path, "s", {"x": 1})
    assert missing_both.state == "fresh"


def test_manifest_subset_identity_semantics(tmp_path: Path) -> None:
    identity = {"dataset_sha256": "d1", "prompt": {"sha256": "p1", "id": "x"}}
    manifest = io.begin_stage(tmp_path, "c", identity=identity)
    io.write_jsonl_atomic(io.stage_rows_path(tmp_path, "c"), [{"id": "q1"}])
    io.finish_stage(tmp_path, manifest)
    # expected 是落盘 identity 的子集 → 通过（便于以后加字段）
    state = io.probe_stage(tmp_path, "c", {"dataset_sha256": "d1"})
    assert state.state == "complete"


# --------------------------------------------------------------------------- #
# C. 四段划分 / 嵌套 CV / 校准
# --------------------------------------------------------------------------- #
def test_four_way_split_disjoint_deterministic_proportional() -> None:
    positions = list(range(100))
    labels = [index % 2 for index in positions]
    split = classifier.four_way_split(positions, labels, seed=7)
    assert set(split) == {"train", "calibrate", "select", "validation"}
    union: list[int] = []
    for segment in split.values():
        union.extend(segment)
    assert sorted(union) == positions                    # 互斥且覆盖
    assert len(set(union)) == len(union)
    for name, fraction in classifier.DEFAULT_HOLDOUT_FRACTIONS.items():
        assert abs(len(split[name]) - fraction * 100) <= 3, name
        assert len(set(labels[p] for p in split[name])) == 2, name
    assert classifier.four_way_split(positions, labels, seed=7) == split
    assert classifier.four_way_split(positions, labels, seed=8) != split


def test_stratified_folds_partition_and_determinism() -> None:
    positions = list(range(50))
    labels = [index % 3 == 0 for index in positions]
    folds = classifier.stratified_folds(positions, labels, n_folds=5, seed=3)
    union = sorted(p for fold in folds for p in fold)
    assert union == positions
    assert all(len(fold) == 10 for fold in folds)
    assert classifier.stratified_folds(positions, labels, n_folds=5,
                                       seed=3) == folds


def test_nested_cv_validation_covers_every_item_once() -> None:
    items = score_items(60)
    folds = [list(range(0, 20)), list(range(20, 40)), list(range(40, 60))]
    result = classifier.nested_cv_evaluate(
        items, feature_fn=lambda item: [item["score"]],
        router_factory=FakeRouter, folds=folds, tolerances=(0.01,),
        noninferior_tol=0.01, n_boot=120, seed=0)
    assert result["mode"] == "nested_cv"
    assert len(result["rounds"]) == 3
    pair_ids = [pair["id"] for pair in result["pairs"]]
    assert sorted(pair_ids) == sorted(item["id"] for item in items)
    assert len(set(pair_ids)) == len(pair_ids)
    assert result["validation"]["n"] == 60
    assert isinstance(result["validation"]["noninferior"], bool)
    json.dumps(result)                                  # 结果必须可 JSON 序列化
    assert result["validation"]["primary_tolerance"] == "1pp"
    for item_round in result["rounds"]:
        assert "thresholds" in item_round and "1pp" in item_round["thresholds"]
        sizes = item_round["sizes"]
        assert sizes["train"] > 0 and sizes["calibrate"] > 0
        assert sizes["select"] > 0 and sizes["validation"] == 20


def test_nested_cv_threshold_never_uses_validation_labels() -> None:
    items = score_items(60)
    folds = [list(range(0, 20)), list(range(20, 40)), list(range(40, 60))]
    kwargs = dict(feature_fn=lambda item: [item["score"]],
                  router_factory=FakeRouter, folds=folds, tolerances=(0.01,),
                  noninferior_tol=0.01, n_boot=120, seed=0)
    baseline = classifier.nested_cv_evaluate(items, **kwargs)
    perturbed = [dict(item) for item in items]
    # 翻转 fold0 validation（位置 0）的标签；它不在 fold0 的 train/calibrate/select 里
    perturbed[0] = dict(perturbed[0], c_correct=not perturbed[0]["c_correct"],
                        s_correct=not perturbed[0]["s_correct"])
    changed = classifier.nested_cv_evaluate(perturbed, **kwargs)
    assert (changed["rounds"][0]["thresholds"]
            == baseline["rounds"][0]["thresholds"])


def test_nested_cv_with_prior_router_runs_without_sklearn() -> None:
    items = make_correctness_items(40)
    result = classifier.nested_cv_evaluate(
        items, feature_fn=lambda item: [0.0],
        router_factory=lambda: classifier.PriorRouter(seed=0),
        outer_folds=3, tolerances=(0.0, 0.05), noninferior_tol=0.0,
        n_boot=80, seed=0)
    assert result["validation"]["n"] == 40
    assert result["rounds"][0]["classifier"]["type"] == "constant-prior"
    assert set(result["validation"]["tolerances"]) == {"0pp", "5pp"}


def test_holdout_validation_segment_is_isolated() -> None:
    items = make_correctness_items(40)
    result = classifier.holdout_evaluate(
        items, feature_fn=lambda item: [0.0],
        router_factory=lambda: classifier.PriorRouter(seed=0),
        tolerances=(0.01,), noninferior_tol=0.01, n_boot=80, seed=0)
    assert result["mode"] == "holdout"
    json.dumps(result)                                  # 无循环引用
    split = classifier.four_way_split(list(range(40)),
                                      [1 if item["c_correct"] else 0
                                       for item in items], seed=0)
    validation_ids = {items[position]["id"] for position in split["validation"]}
    pair_ids = {pair["id"] for pair in result["pairs"]}
    assert pair_ids == validation_ids
    assert result["validation"]["n"] == len(validation_ids)
    assert 0 < len(validation_ids) < 40


def test_learned_router_dependency_guard() -> None:
    factory = classifier.make_router_factory("learned", seed=0)
    router = factory()
    X = [[0.0], [1.0], [0.0], [1.0]]
    y = [0, 1, 0, 1]
    try:
        import sklearn  # noqa: F401
    except ImportError:
        with pytest.raises(classifier.ClassifierError):
            router.fit(X, y, X[:2], y[:2])
        return
    router.fit(X, y, X[:2], y[:2])
    probs = router.predict_proba([[0.2], [0.8]])
    assert len(probs) == 2 and all(0.0 <= p <= 1.0 for p in probs)
    assert router.describe()["calibration"] in (
        "platt(calibrate-segment)",
        "uncalibrated-fallback(calibrate 段单一标签)")


# --------------------------------------------------------------------------- #
# D. 基线 / 真实默认树回放
# --------------------------------------------------------------------------- #
def test_complexity_only_hard_gate_route() -> None:
    items = [make_item(0, c_correct=True, s_correct=True,
                       complexity="hard", conf=0.8),
             make_item(1, c_correct=True, s_correct=True,
                       complexity="hard", conf=0.5),
             make_item(2, c_correct=True, s_correct=True,
                       complexity="easy", conf=0.95)]
    route = strategies.complexity_only_hard_gate(items, 0.70)
    assert route == [False, True, True]                 # hard 且 conf 高 → S
    assert strategies.complexity_route(items, 0.70, mode="easy") == [False,
                                                                     False, True]


def test_real_default_pipeline_replays_gsm8k_like_to_fallback() -> None:
    items = make_correctness_items(5)
    replay = strategies.replay_real_default_pipeline(items)
    assert replay["name"] == "real_default_pipeline"
    assert replay["summary"]["n"] == 5
    assert replay["summary"]["model_counts"] == {"qwen3-8b": 5}
    assert replay["summary"]["kind_counts"] == {"route": 5}
    assert replay["summary"]["node_counts"] == {"fallback": 5}
    assert all(decision["node_id"] == "fallback"
               for decision in replay["decisions"])
    comparison = strategies.compare_candidate_sets(
        replay["summary"], cheap_model="Qwen3-0.6B", strong_model="qwen3.8-27b")
    assert comparison["overlap"] == []
    assert comparison["pair_matches_pipeline"] is False


def test_real_default_pipeline_custom_tree_blocks_jailbreak() -> None:
    from router.pipeline.tree import Block, Branch, Route, RoutingTree, When

    tree = RoutingTree(
        id="custom", name="安全闸", enabled=True,
        root=Branch(id="r", children=(
            Block("block", when=When("jailbreak", "jailbreak",
                                     conf_min=0.7)),),
            otherwise=Route("fallback", "qwen3-8b")))
    item = make_item(0, c_correct=True, s_correct=True)
    item["jev"]["jailbreak"] = {"value": "jailbreak", "confidence": 0.95,
                                "is_rule": False, "p_yes": 0.95}
    replay = strategies.replay_real_default_pipeline([item], tree)
    assert replay["summary"]["kind_counts"] == {"block": 1}
    assert replay["decisions"][0]["model"] is None
    assert replay["decisions"][0]["node_id"] == "block"


def test_tree_fingerprint_is_stable_across_default_loads() -> None:
    first = strategies.load_default_tree()
    second = strategies.load_default_tree()
    assert strategies.tree_fingerprint(first)["sha256"] == \
        strategies.tree_fingerprint(second)["sha256"]
    assert strategies.tree_fingerprint(first)["tree_id"] == "default"


def test_map_pipeline_to_arms_hook() -> None:
    replay = strategies.replay_real_default_pipeline(make_correctness_items(3))
    unmapped = strategies.map_pipeline_to_arms(replay["decisions"])
    assert unmapped["ready"] is False
    assert unmapped["unmapped_counts"].get("qwen3-8b") == 3
    mapped = strategies.map_pipeline_to_arms(replay["decisions"],
                                             {"qwen3-8b": "S"})
    assert mapped["ready"] is True and mapped["mapped_counts"] == {"S": 3}


# --------------------------------------------------------------------------- #
# E. generate：答案抽取 / 可注入 C/S 客户端
# --------------------------------------------------------------------------- #
def test_extract_number_precedence_and_scoring() -> None:
    assert generate.extract_number(r"\boxed{120000}") == 120000.0
    assert generate.extract_number("work\nAnswer: 1,234.5") == 1234.5
    assert generate.extract_number("#### 18") == 18.0
    assert generate.extract_number("total is 42") == 42.0
    assert generate.extract_number(None) is None
    assert generate.extract_number("no digits here") is None
    assert generate.numbers_equal(1.0, 1.0 + 1e-9) is True
    assert generate.numbers_equal(1.0, 1.1) is False
    assert generate.gold_from_answer("...\n#### 18") == 18.0
    item = {"id": "q1", "gold": 18.0}
    record = generate.score_generation(item, {"output": "Answer: 18",
                                              "n_new_tokens": 5,
                                              "truncated": False,
                                              "latency_s": 0.1})
    assert record["correct"] is True and record["predicted"] == 18.0
    assert record["n_new_tokens"] == 5


def test_c_client_batch_split_and_callback() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def generate_batch(self, questions, *, max_new_tokens):
            self.calls.append(list(questions))
            return [{"output": f"Answer: {question.split()[-1]}",
                     "n_new_tokens": 3, "truncated": False,
                     "latency_s": 0.01}
                    for question in questions]

    backend = FakeBackend()
    client = generate.CClient("/fake/model", max_new_tokens=32,
                              batch_size=2, backend=backend)
    items = [{"id": f"q{i}", "question": f"question {i}", "gold": float(i)}
             for i in range(5)]
    written: list[dict] = []
    records = client.generate_many(items, on_row=written.append)
    assert [len(call) for call in backend.calls] == [2, 2, 1]
    assert len(records) == 5 and len(written) == 5
    assert records[2]["predicted"] == 2.0 and records[2]["correct"] is True
    assert client.decode_identity()["batch_size"] == 2
    assert client.decode_identity()["max_new_tokens"] == 32


def test_s_client_injected_transport_retry_and_many() -> None:
    calls: list[dict] = []
    slept: list[float] = []

    def transport(payload: dict) -> dict:
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("temporary network error")
        return {"choices": [{"message": {"content": "Answer: 7",
                                         "reasoning_content": "why"},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": 11}}

    client = generate.SClient("http://fake/v1/chat/completions", "model-x",
                              max_tokens=64, transport=transport,
                              sleep=slept.append, retries=2)
    result = client.generate("q")
    assert result["output"] == "Answer: 7"
    assert result["reasoning_output"] == "why"
    assert result["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 11
    assert len(calls) == 2 and slept == [2.0]          # 第一次失败后重试
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 64
    assert calls[0]["chat_template_kwargs"] == {"enable_thinking": False}

    # content 为分段列表时也能解析；generate_many 并发路径
    def list_transport(payload: dict) -> dict:
        return {"choices": [{"message": {"content": [
            {"type": "text", "text": "Answer: "}, {"type": "text", "text": "9"}]
        }, "finish_reason": "stop"}]}

    client2 = generate.SClient("http://fake", "m", transport=list_transport)
    assert client2.generate("q")["output"] == "Answer: 9"
    items = [{"id": f"q{i}", "question": f"q{i}", "gold": 9.0}
             for i in range(4)]
    written: list[dict] = []
    records = client2.generate_many(items, concurrency=2, on_row=written.append)
    assert len(records) == 4 and len(written) == 4
    assert all(record["correct"] for record in records)


def test_model_fingerprint_hash_modes(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"hidden": 8}', encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"fake-weights")
    full = generate.model_fingerprint(model_dir, include_weights=True)
    config_only = generate.model_fingerprint(model_dir, include_weights=False)
    assert full["weights_hashed"] is True
    assert full["files"]["model.safetensors"]["sha256"]
    assert config_only["weights_hashed"] is False
    assert "sha256" not in config_only["files"]["model.safetensors"]
    assert full["sha256"] != config_only["sha256"]
    assert generate.model_fingerprint(tmp_path / "missing")["sha256"] is None


def test_real_candidate_hook_spec() -> None:
    assert generate.real_candidate_spec(None, None) is None
    assert generate.real_candidate_spec("http://x", None) is None
    spec = generate.real_candidate_spec("http://x/v1/chat/completions",
                                        "qwen3-8b", max_tokens=32)
    assert spec is not None and spec.ready
    client = spec.client(transport=lambda payload: {
        "choices": [{"message": {"content": "Answer: 1"},
                     "finish_reason": "stop"}]})
    assert client.generate("q")["output"] == "Answer: 1"
    assert generate.REAL_CANDIDATE_REQUIREMENTS["status"] == "pending"


# --------------------------------------------------------------------------- #
# F. legacy 数据上的 analyze 端到端（CPU，无 sklearn）
# --------------------------------------------------------------------------- #
def test_legacy_analyze_end_to_end_and_resume(tmp_path: Path) -> None:
    items = make_correctness_items(40)
    write_legacy_stage_files(tmp_path, items)
    assert cli.main(legacy_analyze_argv(tmp_path)) == 0
    results_path = tmp_path / "results.json"
    summary_path = tmp_path / "summary.md"
    assert results_path.is_file() and summary_path.is_file()
    assert (tmp_path / "analyze.manifest.json").is_file()
    results = json.loads(results_path.read_text(encoding="utf-8"))
    summary = summary_path.read_text(encoding="utf-8")

    # 数字正确性（与合成数据一致）
    assert results["prior"]["n"] == len(items)
    assert results["analysis"]["classifier_mode"] == "prior"
    assert results["lineage"]["kind"] == "legacy"
    # 基线齐全 + 命名修正
    assert set(results["strategies"]) >= {
        "always_C", "always_S", "oracle", "complexity_only_hard_gate",
        "complexity_only_easy_gate"}
    assert "complexity_hard_conf70" not in results["strategies"]
    # 真实默认树回放（GSM8K 风格信号 → 兜底 qwen3-8b），质量标 N/A
    pipeline = results["baselines"]["real_default_pipeline"]
    assert pipeline["summary"]["model_counts"] == {"qwen3-8b": len(items)}
    assert pipeline["quality_comparable"] is False
    assert "quality_n_a_reason" in pipeline
    # 报告必须写清三个复核口径
    assert "complexity_only_hard_gate" in summary
    assert "real_default_pipeline" in summary
    assert "C/S 对（Qwen3-0.6B / qwen3.8-27b）≠ 默认树的候选集" in summary
    assert "1pp 容差 ≈ 2/200 题" in summary
    assert "legacy" in summary and "数据血统警告" in summary
    assert "CI 下界" in summary
    assert "quality/cost 标 N/A" in summary

    # resume：manifest 一致 → 复用（不重写 results.json）
    before = results_path.read_bytes()
    assert cli.main(legacy_analyze_argv(tmp_path)) == 0
    assert results_path.read_bytes() == before

    # manifest 不一致 → 拒绝（analysis 参数变了）
    with pytest.raises(io.ManifestMismatchError):
        cli.main(legacy_analyze_argv(tmp_path, "--seed", "1"))

    # 输入数据被改 → 拒绝（analyze identity 的输入 sha 变化）
    io.append_jsonl(tmp_path / "c_gen.jsonl",
                    {"id": "q999", "output": "x", "predicted": 0.0,
                     "correct": False})
    with pytest.raises(io.ManifestMismatchError):
        cli.main(legacy_analyze_argv(tmp_path))


def test_entrypoint_returns_clean_error_for_legacy_lineage(
        tmp_path: Path, capsys) -> None:
    write_legacy_stage_files(tmp_path, make_correctness_items(6))
    code = cli.entrypoint(["--out-dir", str(tmp_path), "--stages", "analyze",
                           "--limit", "6", "--n-boot", "40"])
    assert code == 2
    err = capsys.readouterr().err
    assert "ERROR:" in err and "manifest" in err


def test_generation_stage_requires_manifest_unless_legacy(tmp_path: Path) -> None:
    io.write_jsonl_atomic(tmp_path / "data.jsonl", [{
        "id": "q0", "index": 0, "question": "q", "gold": 1.0}])
    strict = cli.parse_args(["--out-dir", str(tmp_path), "--stages", "c",
                             "--limit", "1", "--sample-size", "1"])
    with pytest.raises(io.ManifestError):
        cli._load_data_rows(strict)
    legacy = cli.parse_args(["--out-dir", str(tmp_path), "--stages", "c",
                             "--limit", "1", "--sample-size", "1",
                             "--legacy-import"])
    assert cli._load_data_rows(legacy)[0]["id"] == "q0"


def test_jev_code_resolution_fallback_and_tolerances_validation(
        tmp_path: Path) -> None:
    args = cli.parse_args(["--out-dir", str(tmp_path), "--stages", "jev",
                           "--jev-code", str(tmp_path / "missing")])
    assert cli._resolve_jev_code(args) == REPO_ROOT       # 回退当前仓库
    fake_root = tmp_path / "repo"
    (fake_root / "router/signals").mkdir(parents=True)
    (fake_root / "router/signals/jev.py").write_text("x = 1\n",
                                                     encoding="utf-8")
    args2 = cli.parse_args(["--out-dir", str(tmp_path), "--stages", "jev",
                            "--jev-code", str(fake_root)])
    assert cli._resolve_jev_code(args2) == fake_root.resolve()
    with pytest.raises(SystemExit):
        cli.parse_args(["--out-dir", str(tmp_path), "--stages", "analyze",
                        "--tolerances", "abc"])


def test_cli_backward_compatible_flags() -> None:
    args = cli.parse_args([
        "--out-dir", "/tmp/x", "--stages", "data,c,s,jev,analyze",
        "--limit", "50", "--sample-size", "200", "--seed", "0",
        "--no-resume", "--cf-model", "/m", "--c-batch", "8",
        "--s-url", "http://x", "--s-model", "m", "--s-max-tokens", "512",
        "--jev-code", "/code", "--jev-model", "/jev",
        "--cost-s", "10", "--cost-s-sensitivity", "20",
        "--noninferior-tol", "0.02", "--min-deploy-coverage", "0.2"])
    assert args.stages == ["data", "c", "s", "jev", "analyze"]
    assert args.resume is False
    assert args.noninferior_tol == 0.02
    assert args.split_mode == "nested_cv"
    with pytest.raises(SystemExit):
        cli.parse_args(["--out-dir", "/tmp/x", "--stages", "unknown"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--out-dir", "/tmp/x", "--limit", "0"])


# --------------------------------------------------------------------------- #
# G. 生成阶段（c）的 manifest/resume 语义（注入假 C 后端，不碰 GPU）
# --------------------------------------------------------------------------- #
def test_c_stage_manifest_resume_and_reject(tmp_path: Path,
                                            monkeypatch) -> None:
    data_rows = [{"id": f"q{i}", "index": i, "question": f"q {i}",
                  "gold": float(i)} for i in range(4)]
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    common = ["--out-dir", str(tmp_path), "--stages", "c", "--limit", "4",
              "--sample-size", "4", "--cf-model", str(model_dir),
              "--model-hash", "config", "--c-batch", "2"]
    args = cli.parse_args(common)
    # data 阶段的合法产物 + manifest
    io.write_jsonl_atomic(tmp_path / "data.jsonl", data_rows)
    data_manifest = io.begin_stage(
        tmp_path, "data", identity={"dataset": cli._dataset_identity(args)})
    io.finish_stage(tmp_path, data_manifest)

    calls = {"batches": 0}

    class FakeBackend:
        def generate_batch(self, questions, *, max_new_tokens):
            calls["batches"] += 1
            return [{"output": f"Answer: {question.split()[-1]}",
                     "n_new_tokens": 2, "truncated": False,
                     "latency_s": 0.0} for question in questions]

    class FakeClient(generate.CClient):
        def __init__(self, model_path, **kwargs):
            kwargs["backend"] = FakeBackend()
            super().__init__(model_path, **kwargs)

    monkeypatch.setattr(cli.generate, "CClient", FakeClient)

    records = cli.stage_c(args, data_rows)
    assert sorted(records) == [f"q{i}" for i in range(4)]
    assert all(record["correct"] for record in records.values())
    assert calls["batches"] == 2                        # batch=2 → 2 批
    manifest = io.load_manifest(tmp_path, "c")
    assert manifest.status == "complete" and manifest.artifact["rows"] == 4

    # resume：manifest 一致 → 不重新生成
    cli.stage_c(args, data_rows)
    assert calls["batches"] == 2

    # 解码参数变化（batch_size 16≠2 之外的 max_new_tokens）→ 拒绝复用
    changed = cli.parse_args(common + ["--c-max-new-tokens", "512"])
    with pytest.raises(io.ManifestMismatchError):
        cli.stage_c(changed, data_rows)

    # --no-resume：清空重跑
    reset = cli.parse_args(common + ["--c-max-new-tokens", "512",
                                     "--no-resume"])
    cli.stage_c(reset, data_rows)
    assert calls["batches"] == 4


def test_data_manifest_identity_mismatch_rejected(tmp_path: Path) -> None:
    args = cli.parse_args(["--out-dir", str(tmp_path), "--stages", "analyze",
                           "--limit", "2", "--sample-size", "2"])
    rows = [{"id": "q0", "index": 0, "question": "q", "gold": 1.0}]
    io.write_jsonl_atomic(tmp_path / "data.jsonl", rows)
    manifest = io.begin_stage(
        tmp_path, "data", identity={"dataset": cli._dataset_identity(args)})
    io.finish_stage(tmp_path, manifest)
    other = cli.parse_args(["--out-dir", str(tmp_path), "--stages", "analyze",
                            "--limit", "2", "--sample-size", "3"])
    with pytest.raises(io.ManifestMismatchError):
        cli._load_data_rows(other)


# --------------------------------------------------------------------------- #
# H. learned 模式在缺少 sklearn 时的降级（有 sklearn 则走真实学习路径）
# --------------------------------------------------------------------------- #
def test_analyze_learned_mode_graceful_without_sklearn(tmp_path: Path) -> None:
    try:
        import sklearn  # noqa: F401
        has_sklearn = True
    except ImportError:
        has_sklearn = False
    items = make_correctness_items(40)
    write_legacy_stage_files(tmp_path, items)
    argv = ["--out-dir", str(tmp_path), "--stages", "analyze",
            "--legacy-import", "--limit", "40", "--n-boot", "120"]
    assert cli.main(argv) == 0
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    learned = results["learned"]
    if has_sklearn:
        assert learned and "validation" in learned
        assert learned["validation"]["n"] == 40
        assert "learned cheap_sufficient 分类器" in summary
    else:
        assert learned and "scikit-learn" in learned["error"]
        assert "learned 路由不可用" in summary
        assert results["conclusion"]["deployable"] is False


# --------------------------------------------------------------------------- #
# I. S 阶段（OpenAI 兼容端点）的 manifest/resume 语义（注入假 transport，不联网）
# --------------------------------------------------------------------------- #
def test_s_stage_manifest_resume_and_reject(tmp_path: Path,
                                            monkeypatch) -> None:
    data_rows = [{"id": f"q{i}", "index": i, "question": f"q {i}",
                  "gold": 42.0} for i in range(3)]
    common = ["--out-dir", str(tmp_path), "--stages", "s", "--limit", "3",
              "--sample-size", "3", "--s-url", "http://fake/v1/chat/completions",
              "--s-model", "fake-model", "--s-max-tokens", "64"]
    args = cli.parse_args(common)
    io.write_jsonl_atomic(tmp_path / "data.jsonl", data_rows)
    data_manifest = io.begin_stage(
        tmp_path, "data", identity={"dataset": cli._dataset_identity(args)})
    io.finish_stage(tmp_path, data_manifest)

    calls = {"n": 0}

    def transport(payload: dict) -> dict:
        calls["n"] += 1
        return {"choices": [{"message": {"content": "Answer: 42"},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": 3}}

    class FakeSClient(generate.SClient):
        def __init__(self, url, model, **kwargs):
            kwargs["transport"] = transport
            super().__init__(url, model, **kwargs)

    monkeypatch.setattr(cli.generate, "SClient", FakeSClient)
    monkeypatch.setattr(cli.generate, "probe_served_models",
                        lambda url, **kwargs: None)

    records = cli.stage_s(args, data_rows)
    assert sorted(records) == [f"q{i}" for i in range(3)]
    assert all(record["correct"] for record in records.values())
    assert calls["n"] == 3
    manifest = io.load_manifest(tmp_path, "s")
    assert manifest.status == "complete" and manifest.artifact["rows"] == 3

    cli.stage_s(args, data_rows)                        # resume 复用
    assert calls["n"] == 3

    changed = cli.parse_args([*common, "--s-model", "other-model"])
    with pytest.raises(io.ManifestMismatchError):
        cli.stage_s(changed, data_rows)


# --------------------------------------------------------------------------- #
# J. real 候选 hook 与 holdout 报告路径
# --------------------------------------------------------------------------- #
def test_real_stage_hook_runs_with_fake_client(tmp_path: Path,
                                               monkeypatch) -> None:
    data_rows = [{"id": f"q{i}", "index": i, "question": f"q {i}",
                  "gold": 42.0} for i in range(2)]
    base = ["--out-dir", str(tmp_path), "--limit", "2", "--sample-size", "2",
            "--s-url", "http://fake/v1/chat/completions"]
    with pytest.raises(SystemExit):
        cli.stage_real(cli.parse_args([*base, "--stages", "real"]), data_rows)
    args = cli.parse_args([*base, "--stages", "real",
                           "--real-url", "http://fake-real/v1/chat/completions",
                           "--real-model", "qwen3-8b"])
    io.write_jsonl_atomic(tmp_path / "data.jsonl", data_rows)
    data_manifest = io.begin_stage(
        tmp_path, "data", identity={"dataset": cli._dataset_identity(args)})
    io.finish_stage(tmp_path, data_manifest)

    def transport(payload: dict) -> dict:
        return {"choices": [{"message": {"content": "Answer: 42"},
                             "finish_reason": "stop"}]}

    class FakeSClient(generate.SClient):
        def __init__(self, url, model, **kwargs):
            kwargs["transport"] = transport
            super().__init__(url, model, **kwargs)

    monkeypatch.setattr(cli.generate, "SClient", FakeSClient)
    monkeypatch.setattr(cli.generate, "probe_served_models",
                        lambda url, **kwargs: None)
    records = cli.stage_real(args, data_rows)
    assert all(record["correct"] for record in records.values())
    manifest = io.load_manifest(tmp_path, "real")
    assert manifest.identity["endpoint"] == {
        "url": "http://fake-real/v1/chat/completions", "model": "qwen3-8b"}


def test_holdout_split_mode_end_to_end(tmp_path: Path) -> None:
    write_legacy_stage_files(tmp_path, make_correctness_items(40))
    argv = legacy_analyze_argv(tmp_path, "--split-mode", "holdout")
    assert cli.main(argv) == 0
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    learned = results["learned"]
    assert learned["mode"] == "holdout"
    assert learned["validation"]["n"] < 40          # 只在 validation 段算指标
    assert "final-validation 段（独立 holdout）" in summary
    assert len(learned["pairs"]) == learned["validation"]["n"]
