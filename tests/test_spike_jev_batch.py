from pathlib import Path

import pytest

from router.routing.graph.validate import EnableContext, validate_graph_for_enable
from scripts.spike_jev_batch import (
    build_g3_tree,
    build_question_groups,
    load_samples,
    plan_request_budget,
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
