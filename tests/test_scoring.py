from router.pipeline.model import Branch, Node, Row
from router.pipeline.scoring import branch_score, eval_score


def br(bid, min_score, rows, model="m1"):
    return Branch(id=bid, rows=[Row(signal=s, value=v, weight=w)
                                for s, v, w in rows],
                  min_score=min_score, model=model)


SIG = {"domain": {"value": "health", "confidence": 0.9, "is_rule": False},
       "complexity": {"value": "hard", "confidence": 0.8, "is_rule": False}}


def test_weighted_attainment():
    b = br("b1", 0.7, [("domain", "health", 0.5), ("complexity", "hard", 0.5)])
    d = branch_score(b, SIG)
    assert abs(d["score"] - 0.85) < 1e-9       # (0.45 + 0.40) / 1.0
    assert d["passed"] is True


def test_zero_weight_scores_zero():
    d = branch_score(br("b1", 0.1, []), SIG)
    assert d["score"] == 0.0 and d["passed"] is False


def test_winner_is_max_passed():
    # low: (0.5*0.8)/0.5 = 0.80, min 0.3 -> passed; high: (0.45+0.40)/1.0 = 0.85,
    # min 0.8 -> passed. Winner = higher score = high.
    n = Node(id="n", type="score", branches=[
        br("low", 0.3, [("complexity", "hard", 0.5)], model="m-low"),
        br("high", 0.8, [("domain", "health", 0.5), ("complexity", "hard", 0.5)],
           model="m-high"),
    ])
    out = eval_score(n, SIG)
    assert out["decision"]["model"] == "m-high"
    winners = [b for b in out["trace"]["board"] if b["winner"]]
    assert len(winners) == 1 and winners[0]["branch_id"] == "high"


def test_tie_keeps_branch_order():
    n = Node(id="n", type="score", branches=[
        br("first", 0.5, [("domain", "health", 0.5)], model="m-first"),
        br("second", 0.5, [("domain", "health", 0.5)], model="m-second"),
    ])
    out = eval_score(n, SIG)
    assert out["decision"]["model"] == "m-first"


def test_all_under_min_is_miss():
    n = Node(id="n", type="score",
             branches=[br("b", 0.99, [("domain", "health", 1.0)])])
    out = eval_score(n, SIG)
    assert out["decision"] is None and out["trace"]["result"] == "miss"
