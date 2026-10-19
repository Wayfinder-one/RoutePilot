"""SPEC-TREE-4 acceptance tests: derivation (section 3), evaluation
semantics (section 4), health checks (section 5) and the API contract
(section 6) for the split node type."""

import json

from router.pipeline.engine import evaluate
from router.pipeline.health import check_pipeline
from router.pipeline.model import Branch, Node, Row, node_from_dict
from router.pipeline.tree import count_leaves, derive, next_fork_key, \
    node_floor, signal_order

MODELS = {"qwen3-8b-medical", "qwen3-0.6b-medical", "deepseek-v3", "qwen3-8b"}


def demo_node():
    """The prototype tree: domain forks first, complexity second."""
    return node_from_dict({
        "id": "n3", "type": "split", "name": "学科 → 难度", "floor": 0.70,
        "branches": [
            {"id": "b4", "name": "8B 医疗", "action": "route",
             "model": "qwen3-8b-medical", "min_score": 0.55,
             "rows": [{"signal": "domain", "value": "health", "weight": 0.5},
                      {"signal": "complexity", "value": "hard", "weight": 0.5}]},
            {"id": "b5", "name": "0.6B 医疗", "action": "route",
             "model": "qwen3-0.6b-medical", "min_score": 0.45,
             "rows": [{"signal": "domain", "value": "health", "weight": 0.5},
                      {"signal": "complexity", "value": "easy", "weight": 0.5}]},
            {"id": "b6", "name": "DeepSeek 编程", "action": "route",
             "model": "deepseek-v3", "min_score": 0.55,
             "rows": [{"signal": "domain", "value": "computer_science",
                       "weight": 0.4},
                      {"signal": "complexity", "value": "hard", "weight": 0.6}]},
        ]})


def sig(value, conf, rule=False):
    return {"value": value, "confidence": conf, "is_rule": rule}


# ── section 3: derivation ────────────────────────────────────────────────

def test_derive_is_deterministic():
    n = demo_node()
    first = derive(n, n.branches, set())
    for _ in range(5):
        assert derive(n, n.branches, set()) == first
    assert first["key"] == "domain"
    assert [g["value"] for g in first["groups"]] == ["health", "computer_science"]
    # second layer forks only where ≥2 branches share the bucket (section 3.3):
    # health has two branches → forked by complexity; cs has one → flat leaves
    health = next(g for g in first["groups"] if g["value"] == "health")
    cs = next(g for g in first["groups"] if g["value"] == "computer_science")
    assert health["child"]["key"] == "complexity"
    assert "leaves" in cs["child"]


def test_signal_order_and_fork_key():
    n = demo_node()
    assert signal_order(n) == ["domain", "complexity"]
    assert next_fork_key(n, n.branches, set()) == "domain"
    assert next_fork_key(n, n.branches, {"domain"}) == "complexity"
    assert next_fork_key(n, n.branches, {"domain", "complexity"}) is None
    # a single branch never splits (section 3.3)
    single = [n.branches[0]]
    assert next_fork_key(n, single, set()) is None
    # equal values still fork: the trunk shows even with one group
    same = [Branch(id="x1", rows=[Row("language", "zh")]),
            Branch(id="x2", rows=[Row("language", "zh")])]
    lvl = derive(Node(id="z", type="split", branches=same), same, set())
    assert lvl["key"] == "language" and len(lvl["groups"]) == 1
    assert count_leaves(lvl) == 2


def test_root_override_requires_every_branch():
    n = demo_node()
    n.split_signal = "complexity"          # every branch carries it → honored
    assert derive(n, n.branches, set())["key"] == "complexity"
    n.split_signal = "language"            # some branch lacks it → fallback
    assert derive(n, n.branches, set())["key"] == "domain"


def test_missing_value_lands_in_pending_group():
    n = demo_node()
    n.branches.append(Branch(id="b9", name="无难度", model="qwen3-8b",
                             rows=[Row("domain", "health", weight=1.0)]))
    lvl = derive(n, n.branches, set())
    health = next(g for g in lvl["groups"] if g["value"] == "health")
    # complexity is no longer a valid fork key (b9 lacks it) → flat leaves
    assert "leaves" in health["child"]


def test_floor_default():
    n = demo_node()
    assert node_floor(n) == 0.70
    n.floor = 0.55
    assert node_floor(n) == 0.55


# ── section 4: evaluation ────────────────────────────────────────────────

def test_full_path_direct_hit_without_scoring():
    d, tr = evaluate([demo_node()], {
        "domain": sig("health", 0.93), "complexity": sig("hard", 0.91)})
    assert (d.kind, d.model, d.branch_id) == ("route", "qwen3-8b-medical", "b4")
    assert d.score is None                          # nothing was scored
    entry = tr[0]
    assert entry["type"] == "split" and entry["result"] == "hit"
    assert entry["board"] == []                     # no scoring happened
    assert entry["path"] == [
        {"signal": "domain", "value": "health", "conf": 0.93,
         "floor": 0.70, "pass": True},
        {"signal": "complexity", "value": "hard", "conf": 0.91,
         "floor": 0.70, "pass": True}]


def test_remaining_conditions_score_and_winner():
    # a fourth branch whose second condition is NOT a fork key: it stays on the
    # leaf and competes by weight (section 4.2)
    n = demo_node()
    n.branches.append(Branch(id="b9", name="医疗宽", model="qwen3-8b",
                             min_score=0.4,
                             rows=[Row("domain", "health", weight=0.3),
                                   Row("jailbreak", "benign", weight=0.7)]))
    # complexity is no longer forkable (b9 lacks it): one layer, scored leaves
    signals = {"domain": sig("health", 0.93),
               "complexity": sig("hard", 0.91),
               "jailbreak": sig("benign", 0.8)}
    d, tr = evaluate([n], signals)
    assert d.kind == "route"
    board = {b["branch_id"]: b for b in tr[0]["board"]}
    assert board["b4"]["passed"] and board["b4"]["score"] == 0.91
    assert board["b9"]["passed"]                    # 0.7*0.8 = 0.56 >= 0.4
    assert d.branch_id == "b4"                      # 0.91 beats 0.56
    assert len(tr[0]["path"]) == 1                  # only domain forked


def test_floor_gates_entry_and_trace_records_the_cut():
    d, tr = evaluate([demo_node()], {
        "domain": sig("health", 0.50),              # below 0.70
        "complexity": sig("hard", 0.91)})
    assert d.kind == "none"                         # node misses, chain goes on
    entry = tr[0]
    assert entry["result"] == "miss"
    assert entry["path"][-1]["pass"] is False       # the failing layer recorded
    assert entry["board"] == []


def test_rule_signal_layer_skips_floor():
    n = Node(id="r", type="split", branches=[
        Branch(id="l1", name="中文", model="qwen3-8b",
               rows=[Row("language", "zh")]),
        Branch(id="l2", name="英文", model="deepseek-v3",
               rows=[Row("language", "en")])])
    d, tr = evaluate([n], {"language": sig("zh", None, rule=True)})
    assert (d.kind, d.model) == ("route", "qwen3-8b")
    step = tr[0]["path"][0]
    assert step["conf"] == 1.0 and step["floor"] is None   # 确定性：无门槛


def test_no_group_matches_then_fallback_fires():
    fallback = Node(id="f", type="gate", branches=[
        Branch(id="fb", rows=[], action="route", model="qwen3-8b")])
    d, tr = evaluate([demo_node(), fallback], {
        "domain": sig("math", 0.9), "complexity": sig("hard", 0.9)})
    assert (d.kind, d.model, d.node_id) == ("route", "qwen3-8b", "f")
    assert [t["result"] for t in tr] == ["miss", "hit"]


def test_min_score_blocks_scoring_winner():
    n = demo_node()
    n.branches.append(Branch(id="b9", name="医疗宽", model="qwen3-8b",
                             min_score=0.9,           # 0.56 < 0.9 → cannot win
                             rows=[Row("domain", "health", weight=0.3),
                                   Row("jailbreak", "benign", weight=0.7)]))
    d, _ = evaluate([n], {"domain": sig("health", 0.93),
                          "jailbreak": sig("benign", 0.8)})
    assert d.kind == "none"


# ── section 5: health ────────────────────────────────────────────────────

def codes(issues):
    return {i.code for i in issues}


def test_health_split_rules():
    ok = [Node(id="f", type="gate", branches=[
        Branch(id="fb", rows=[], action="route", model="qwen3-8b")])]
    n = demo_node()
    issues = check_pipeline([n, *ok], MODELS)
    # the demo tree is hard-clean; its leaves are fully determined by the two
    # layers, so their min_score is dead — a warn, not a blocker (section 5)
    assert [i for i in issues if i.level == "hard"] == []
    assert codes(issues) == {"split_dead_min_score"}

    # warn: empty split node
    empty = Node(id="e", type="split", branches=[])
    assert "split_empty" in codes(check_pipeline([empty, *ok], MODELS))

    # hard: dead branch under the same path
    n2 = demo_node()
    n2.branches.append(Branch(id="b4dup", name="重复", model="qwen3-8b-medical",
                              min_score=0.55,
                              rows=[Row("domain", "health", weight=0.5),
                                    Row("complexity", "hard", weight=0.5)]))
    assert "split_dead_branch" in codes(check_pipeline([n2, *ok], MODELS))

    # hard: orphan value — a fork-key row with an empty value falls into the
    # 「待选」 group no request can enter (section 5)
    n3 = demo_node()
    n3.branches.append(Branch(id="b9", name="缺难度", model="qwen3-8b",
                              rows=[Row("domain", "health", weight=1.0),
                                    Row("complexity", "", weight=1.0)]))
    got = codes(check_pipeline([n3, *ok], MODELS))
    assert "split_orphan_value" in got

    # warn: min_score on a fully determined path (leaf rows fully consumed
    # by the layers it sits under)
    n4 = Node(id="d4", type="split", branches=[
        Branch(id="z1", name="医疗直达", model="qwen3-8b", min_score=0.9,
               rows=[Row("domain", "health")]),
        Branch(id="z2", name="编程直达", model="deepseek-v3", min_score=0.5,
               rows=[Row("domain", "computer_science")])])
    got = check_pipeline([n4, *ok], MODELS)
    assert "split_dead_min_score" in codes(got)
    assert all(i.level == "warn" for i in got if i.code == "split_dead_min_score")

    # warn: floor set but every fork key is a rule signal
    n5 = Node(id="d5", type="split", floor=0.8, branches=[
        Branch(id="z1", name="中文", model="qwen3-8b",
               rows=[Row("language", "zh")]),
        Branch(id="z2", name="英文", model="deepseek-v3",
               rows=[Row("language", "en")])])
    got = check_pipeline([n5, *ok], MODELS)
    assert "split_rule_floor" in codes(got)
    # mixed tree: model-signal layers still read the floor → no warning
    n6 = demo_node()
    n6.floor = 0.8
    assert "split_rule_floor" not in codes(check_pipeline([n6, *ok], MODELS))


# ── section 6: API ───────────────────────────────────────────────────────

def test_api_split_contract(tmp_path):
    from fastapi.testclient import TestClient

    from router.api.app import create_app
    from router.config import Settings
    from router.store.db import connect

    app = create_app(Settings(db_path=str(tmp_path / "t.db")))
    with TestClient(app) as c:
        # GET /signals: kind + free per signal
        body = c.get("/admin/signals").json()
        by_key = {s["key"]: s for s in body["signals"]}
        assert by_key["jailbreak"]["kind"] == "model"
        assert by_key["context"]["kind"] == "rule"
        assert by_key["keyword"]["free"] and by_key["tenant"]["free"]
        assert not by_key["domain"]["free"]

        # PUT /pipeline: split nodes accepted, persisted, served back
        pipe = c.get("/admin/pipeline").json()
        split = {"id": "nsplit", "type": "split", "name": "学科 → 难度",
                 "floor": 0.70, "split_signal": None,
                 "branches": [
                     {"id": "sb1", "name": "8B 医疗", "action": "route",
                      "model": "qwen3-8b-medical", "min_score": 0.55,
                      "rows": [{"signal": "domain", "value": "health",
                                "weight": 0.5, "conf_min": None},
                               {"signal": "complexity", "value": "hard",
                                "weight": 0.5, "conf_min": None}]},
                     {"id": "sb2", "name": "0.6B 医疗", "action": "route",
                      "model": "qwen3-0.6b-medical", "min_score": 0.45,
                      "rows": [{"signal": "domain", "value": "health",
                                "weight": 0.5, "conf_min": None},
                               {"signal": "complexity", "value": "easy",
                                "weight": 0.5, "conf_min": None}]}]}
        fallback = pipe["nodes"][-1]
        pipe["nodes"][-1] = split
        pipe["nodes"].append(fallback)
        r = c.put("/admin/pipeline", json=pipe)
        assert r.status_code == 200, r.text
        # leaves fully determined by the layers → dead min_score warns
        assert {w["code"] for w in r.json()["warnings"]} == {"split_dead_min_score"}
        served = c.get("/admin/pipeline").json()
        node = next(n for n in served["nodes"] if n["id"] == "nsplit")
        assert node["floor"] == 0.70 and node["split_signal"] is None

        # hard health issue → 422 pointing at node and branch
        bad = json.loads(json.dumps(pipe))
        bad["nodes"][-2]["branches"].append(
            {"id": "dup", "name": "重复", "action": "route",
             "model": "qwen3-8b-medical", "min_score": 0.55,
             "rows": [{"signal": "domain", "value": "health", "weight": 0.5,
                       "conf_min": None},
                      {"signal": "complexity", "value": "hard", "weight": 0.5,
                       "conf_min": None}]})
        r = c.put("/admin/pipeline", json=bad)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert any(i["code"] == "split_dead_branch"
                   and i["node_id"] == "nsplit" and i["branch_id"] == "dup"
                   for i in detail)

        # POST /try: split trace with path + board
        r = c.post("/admin/try", json={"text": "我发烧了应该挂哪个科"})
        assert r.status_code == 200
        entry = next(t for t in r.json()["trace"] if t["type"] == "split")
        assert isinstance(entry["path"], list) and isinstance(entry["board"], list)

        # warn-only save succeeds and reports the warnings
        warn_pipe = json.loads(json.dumps(pipe))
        warn_pipe["nodes"][-2]["branches"][0]["min_score"] = None  # still ok
        warn_pipe["nodes"][-2]["floor"] = 0.8
        # make an all-rule fork: replace domain rows with language rows
        for b in warn_pipe["nodes"][-2]["branches"]:
            b["rows"] = [{"signal": "language", "value": "zh" if b["id"] == "sb1"
                          else "en", "weight": 1.0, "conf_min": None}]
        r = c.put("/admin/pipeline", json=warn_pipe)
        assert r.status_code == 200
        assert any(w["code"] == "split_rule_floor" for w in r.json()["warnings"])


def test_storage_roundtrip_floor(tmp_path):
    from router.store.db import connect, migrate
    from router.store.repositories import PipelineRepo

    conn = connect(str(tmp_path / "s.db"))
    migrate(conn)
    nodes = [demo_node(),
             Node(id="f", type="gate", branches=[
                 Branch(id="fb", rows=[], action="route", model="qwen3-8b")])]
    nodes[0].floor = 0.65
    nodes[0].split_signal = "domain"
    PipelineRepo(conn).save(nodes)
    loaded = PipelineRepo(conn).load()
    assert loaded[0].floor == 0.65 and loaded[0].split_signal == "domain"
    assert loaded[1].floor is None and loaded[1].split_signal is None
    conn.close()
