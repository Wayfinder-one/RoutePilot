"""Explicit-tree engine: ordered children, leaves, weighted competition, trace."""

from router.pipeline.engine import evaluate
from router.pipeline.model import Station, TreeNode, Weight


def sig(value, conf, rule=False):
    return {"value": value, "confidence": conf, "is_rule": rule}


def node(nid, signal=None, value="", conf=None, kind="leaf", action="route",
         model=None, min_score=None, weights=None, children=None):
    return TreeNode(id=nid, signal=signal, value=value, conf_min=conf,
                    kind=kind, action=action, model=model,
                    min_score=min_score, weights=weights or [],
                    children=children or [])


def fallback():
    return Station(id="f", name="兜底", root=node(
        "n4", signal=None, action="route", model="qwen3-8b"))


def subject_station():
    """③ 学科 → 难度: unconditional entry -> 学科 forks -> 难度 leaves."""
    return Station(id="s3", name="学科 → 难度", root=node(
        "n3", signal=None, kind="fork", children=[
            node("n3a", "domain", "health", 0.7, kind="fork", children=[
                node("n3a1", "complexity", "hard", 0.7,
                     model="qwen3-8b-medical"),
                node("n3a2", "complexity", "easy", 0.7,
                     model="qwen3-0.6b-medical"),
            ]),
            node("n3b", "domain", "computer_science", 0.7, kind="fork",
                 children=[node("n3b1", "complexity", "hard", 0.7,
                                model="deepseek-v3")]),
        ]))


def test_ordered_children_first_match():
    station = subject_station()
    d, tr = evaluate([station, fallback()], {
        "domain": sig("health", 0.93), "complexity": sig("easy", 0.9)})
    assert (d.kind, d.model, d.node_id) == ("route", "qwen3-0.6b-medical", "n3a2")
    entry = tr[0]
    assert entry["result"] == "hit"
    assert [(p["signal"], p["value"], p["pass"]) for p in entry["path"]] == [
        ("domain", "health", True), ("complexity", "easy", True)]


def test_confidence_floor_records_the_cut():
    d, tr = evaluate([subject_station(), fallback()], {
        "domain": sig("health", 0.5), "complexity": sig("hard", 0.9)})
    assert (d.kind, d.model) == ("route", "qwen3-8b")   # station missed -> fallback
    entry = tr[0]
    assert entry["result"] == "miss"
    assert entry["path"][0] == {"signal": "domain", "value": "health",
                                "conf": 0.5, "conf_min": 0.7, "m": 1,
                                "pass": False}


def test_rule_signal_skips_the_floor():
    station = Station(id="kw", name="关键词", root=node(
        "n2", signal=None, kind="fork", children=[
            node("n2a", "keyword", "医疗急症", 0.5,
                 model="qwen3-8b-medical")]))
    d, tr = evaluate([station, fallback()], {
        "keyword": {"value": ["医疗急症"], "confidence": None, "is_rule": True}})
    assert (d.kind, d.model) == ("route", "qwen3-8b-medical")
    step = tr[0]["path"][0]
    assert step["conf"] == 1.0 and step["conf_min"] is None


def test_direct_hit_leaf_and_block_action():
    st = Station(id="s", name="闸", root=node(
        "r", signal=None, kind="fork", children=[
            node("blk", "jailbreak", "jailbreak", 0.3, action="block"),
            node("ok", "jailbreak", "benign", 0.3,
                 model="qwen3-8b")]))
    d, _ = evaluate([st, fallback()], {"jailbreak": sig("jailbreak", 0.9)})
    assert (d.kind, d.model, d.node_id) == ("block", None, "blk")
    d2, _ = evaluate([st, fallback()], {"jailbreak": sig("benign", 0.9)})
    assert (d2.kind, d2.model, d2.node_id) == ("route", "qwen3-8b", "ok")


def test_weighted_leaf_scores_and_min_score():
    st = Station(id="s", name="打分", root=node(
        "r", signal=None, kind="fork", children=[
            node("l", "domain", "health", 0.5, model="med", min_score=0.6,
                 weights=[Weight("complexity", "hard", 0.5),
                          Weight("jailbreak", "benign", 0.5)])]))
    signals = {"domain": sig("health", 0.9),
               "complexity": sig("hard", 0.9),
               "jailbreak": sig("benign", 0.5)}
    d, tr = evaluate([st, fallback()], signals)
    assert (d.kind, d.model, d.node_id) == ("route", "med", "l")
    assert abs(d.score - 0.7) < 1e-9            # (0.5*0.9 + 0.5*0.5) / 1.0
    board = tr[0]["board"]
    assert board[0]["node_id"] == "l" and board[0]["winner"] is True

    # below min_score -> station misses
    d2, tr2 = evaluate([st, fallback()], {
        "domain": sig("health", 0.9), "complexity": sig("hard", 0.5),
        "jailbreak": sig("benign", 0.5)})
    assert (d2.kind, d2.model) == ("route", "qwen3-8b")   # fallback
    assert tr2[0]["board"][0]["passed"] is False


def test_sibling_weighted_leaves_compete_by_score():
    st = Station(id="s", name="竞争", root=node(
        "r", signal=None, kind="fork", children=[
            node("low", "domain", "health", 0.5, model="m-low", min_score=0.1,
                 weights=[Weight("domain", "health", 1.0)]),
            node("high", "jailbreak", "benign", 0.5, model="m-high",
                 min_score=0.1,
                 weights=[Weight("jailbreak", "benign", 1.0)])]))
    signals = {"domain": sig("health", 0.6),
               "jailbreak": sig("benign", 0.9),
               "complexity": sig("hard", 1.0)}
    d, _ = evaluate([st, fallback()], signals)
    assert d.model == "m-high"                  # higher score wins


def test_direct_hit_beats_a_weighted_sibling_regardless_of_order():
    """A direct hit is an explicit rule; weighted leaves only compete when no
    matched child yields one (2026-10-23: was max-score-always-wins)."""
    st = Station(id="s", name="顺序", root=node(
        "r", signal=None, kind="fork", children=[
            node("w", "domain", "health", 0.5, model="m-weighted",
                 min_score=0.1, weights=[Weight("domain", "health", 1.0)]),
            node("d", "jailbreak", "benign", 0.5, model="m-direct")]))
    signals = {"domain": sig("health", 0.99),   # weighted score 0.99 ...
               "jailbreak": sig("benign", 0.9)}
    d, _ = evaluate([st, fallback()], signals)
    assert (d.model, d.node_id) == ("m-direct", "d")   # ... still loses


def test_a_bubbled_score_never_passes_a_direct_hit():
    """A weighted leaf deep in one subtree must not outscore a direct hit in
    a sibling subtree (the old max-score rule let it bubble past)."""
    st = Station(id="s", name="冒泡", root=node(
        "r", signal=None, kind="fork", children=[
            node("sub", "domain", "health", 0.5, kind="fork", children=[
                node("w", "complexity", "hard", 0.5, model="m-weighted",
                     min_score=0.1,
                     weights=[Weight("complexity", "hard", 1.0)])]),
            node("d", "domain", "health", 0.5, model="m-direct")]))
    signals = {"domain": sig("health", 0.9), "complexity": sig("hard", 0.99)}
    d, _ = evaluate([st, fallback()], signals)
    assert (d.model, d.node_id) == ("m-direct", "d")


def test_station_order_and_fallback():
    first = Station(id="a", name="一", root=node(
        "r", signal=None, kind="fork", children=[
            node("x", "domain", "math", 0.5, model="m1")]))
    d, tr = evaluate([first, fallback()], {"domain": sig("health", 0.9)})
    assert (d.kind, d.model) == ("route", "qwen3-8b")
    assert [t["result"] for t in tr] == ["miss", "hit"]


def test_no_fallback_is_none_not_exception():
    d, _ = evaluate([Station(id="s", name="x",
                            root=node("r", signal=None, kind="fork",
                                      children=[node("c", "domain", "health",
                                                     0.5, model="m")]))], {})
    assert d.kind == "none"
