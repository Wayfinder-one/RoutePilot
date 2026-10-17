from router.pipeline.health import check_pipeline
from router.pipeline.model import Branch, Node, Row

MODELS = {"qwen3-8b", "med"}


def ok_pipeline():
    return [Node(id="f", type="gate", branches=[
        Branch(id="fb", rows=[], action="route", model="qwen3-8b")])]


def codes(issues):
    return {i.code for i in issues}


def test_ok():
    assert check_pipeline(ok_pipeline(), MODELS) == []


def test_missing_fallback_and_empty():
    assert "empty_pipeline" in codes(check_pipeline([], MODELS))
    n = Node(id="g", type="gate", branches=[
        Branch(id="b", rows=[Row("domain", "health", conf_min=0.5)],
               action="route", model="qwen3-8b")])
    assert "missing_fallback" in codes(check_pipeline([n], MODELS))


def test_unknown_model_and_signal():
    n = ok_pipeline()
    n[0].branches[0].model = "nope"
    n[0].branches.insert(0, Branch(id="b", rows=[Row("nope_sig", "x")],
                                   action="route", model="qwen3-8b"))
    got = codes(check_pipeline(n, MODELS))
    assert {"unknown_model", "unknown_signal"} <= got


def test_zero_weight_and_duplicate():
    row = Row("domain", "health", weight=0.0)
    n = Node(id="s", type="score", branches=[
        Branch(id="b1", rows=[row], model="med", min_score=0.5),
        Branch(id="b2", rows=[Row("domain", "health", weight=0.0)],
               model="med", min_score=0.5),
    ])
    got = codes(check_pipeline([n, *ok_pipeline()], MODELS))
    assert "zero_weight" in got and "duplicate_branch" in got


def test_dead_branch_exclusive_rows():
    # complexity can only equal one value: max attainable = 0.6/1.0 = 0.6 < 0.9
    n = Node(id="s", type="score", branches=[
        Branch(id="b1", rows=[Row("complexity", "easy", weight=0.4),
                              Row("complexity", "hard", weight=0.6)],
               model="med", min_score=0.9)])
    assert "dead_branch" in codes(check_pipeline([n, *ok_pipeline()], MODELS))


def test_duplicate_allows_different_min_score():
    # Same rows, different threshold = the legal high/low-confidence two-tier
    # pattern; the signature must include min_score (audit ruling A).
    n = Node(id="s", type="score", branches=[
        Branch(id="b1", rows=[Row("domain", "health", weight=1.0)],
               model="med", min_score=0.5),
        Branch(id="b2", rows=[Row("domain", "health", weight=1.0)],
               model="med", min_score=0.9)])
    assert "duplicate_branch" not in codes(check_pipeline([n, *ok_pipeline()], MODELS))


def test_gate_exclusive_rows_are_dead():
    n = Node(id="g", type="gate", branches=[
        Branch(id="b", rows=[Row("complexity", "easy"),
                             Row("complexity", "hard")],
               action="route", model="qwen3-8b")])
    assert "gate_exclusive_rows" in codes(check_pipeline([n], MODELS))


def test_empty_branch_must_be_last():
    n = Node(id="g", type="gate", branches=[
        Branch(id="b1", rows=[], action="route", model="qwen3-8b"),
        Branch(id="b2", rows=[Row("domain", "health")],
               action="route", model="med")])
    assert "empty_branch_shadow" in codes(check_pipeline([n], MODELS))

