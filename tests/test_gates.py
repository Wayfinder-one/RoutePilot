from router.pipeline.model import Branch, Node, Row
from router.pipeline.gates import eval_gate


SIG = {"jailbreak": {"value": "jailbreak", "confidence": 0.9, "is_rule": False},
       "keyword": {"value": ["医疗急症"], "confidence": 1.0, "is_rule": True}}


def gate():
    return Node(id="g", type="gate", branches=[
        Branch(id="b1", name="拦越狱",
               rows=[Row("jailbreak", "jailbreak", conf_min=0.6)], action="block"),
        Branch(id="b2", name="急症",
               rows=[Row("keyword", "医疗急症", conf_min=0.5)],
               action="route", model="qwen3-8b-medical"),
    ])


def test_block_on_confident_hit():
    out = eval_gate(gate(), SIG)
    assert out["decision"]["kind"] == "block"
    assert out["trace"]["branch"]["name"] == "拦越狱"


def test_conf_below_threshold_passes_through():
    low = {"jailbreak": {"value": "jailbreak", "confidence": 0.4, "is_rule": False}}
    out = eval_gate(gate(), {**low, "keyword": SIG["keyword"]})
    assert out["decision"]["kind"] == "route"
    assert out["decision"]["model"] == "qwen3-8b-medical"


def test_empty_rows_always_hit_fallback():
    n = Node(id="f", type="gate",
             branches=[Branch(id="fb", rows=[], action="route", model="qwen3-8b")])
    out = eval_gate(n, {})
    assert out["decision"]["model"] == "qwen3-8b"


def test_all_miss():
    out = eval_gate(gate(), {})
    assert out["decision"] is None and out["trace"]["result"] == "miss"
