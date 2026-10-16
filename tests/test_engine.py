from router.pipeline.engine import Decision, evaluate
from router.pipeline.model import Branch, Node, Row


def pipeline():
    return [
        Node(id="g1", type="gate", name="安全闸", branches=[
            Branch(id="gb", rows=[Row("jailbreak", "jailbreak", conf_min=0.6)],
                   action="block")]),
        Node(id="s1", type="score", name="打分", branches=[
            Branch(id="sb", rows=[Row("domain", "health", weight=1.0)],
                   model="med", min_score=0.5)]),
        Node(id="f", type="gate", name="兜底", branches=[
            Branch(id="fb", rows=[], action="route", model="qwen3-8b")]),
    ]


def test_safety_blocks_first():
    d, tr = evaluate(pipeline(), {
        "jailbreak": {"value": "jailbreak", "confidence": 0.9, "is_rule": False}})
    assert d.kind == "block" and len(tr) == 1      # stops at the first node


def test_score_route_then_fallback():
    d, _ = evaluate(pipeline(), {
        "jailbreak": {"value": "benign", "confidence": 0.9, "is_rule": False},
        "domain": {"value": "health", "confidence": 0.9, "is_rule": False}})
    assert (d.kind, d.model) == ("route", "med")
    d2, _ = evaluate(pipeline(), {})
    assert (d2.kind, d2.model) == ("route", "qwen3-8b")   # fallback


def test_no_fallback_is_none_not_exception():
    d, _ = evaluate([Node(id="x", type="gate", branches=[])], {})
    assert d.kind == "none"
