"""Task 5 测试：engine 的 cond 求值 + evaluate + 试算轨迹（简报 Step 1 测试逐字采用）。

与简报的唯一出入（记任务报告）：SIG["domain"]["confidence"] 0.61 → 0.45。
简报注释写「0.61 → 档 1 < 门槛 2」，但按已固化的 conf_band 口径（切点含等号，
0.61 ≥ 0.60 → 档 2）恰过 conf_min=2，`assert not match` 必挂——简报测试与
其自带参考实现自相矛盾。为保住测试意图（低把握过不了门槛 2、0.75 档 2 过），
domain 置信度取档 1 中点 0.45；其余逐字未动。
"""

from sr.engine import match, evaluate, evaluate_trace, rows_to_tree, TEMPLATES
import sr.db as db

SIG = {   # 一份「编程且难」的信号包（规范键/档位）
    "domain": {"value": "computer_science", "confidence": 0.45},  # 档 1（简报 0.61 实为档 2，见文件头说明）
    "complexity": {"value": 1, "confidence": 0.88},
    "jailbreak": {"value": 0, "confidence": 0.97},
    "pii": {"value": [], "confidence": 0.9},
    "modality": {"value": "text", "confidence": 0.9},
    "user_feedback": {"value": None, "confidence": 0.4},
    "fact_check": {"value": "no_check", "confidence": 0.8},
    "keyword": {"value": []}, "language": {"value": "zh"}, "structure": {"value": "plain"},
    "context": {"value": 0}, "turns": {"value": 0}, "tenant": {"value": "anonymous"},
    "time_window": {"value": "weekday_day"}, "event": {"value": "none"},
    "similarity": {"value": 0}, "reask": {"value": "no"},
}
CUTS = [0.30, 0.60, 0.85]

def test_match_is_in_gte_contains():
    assert match([{"tag":"domain","op":"is","values":["computer_science"]}], SIG, CUTS)
    assert match([{"tag":"domain","op":"in","values":["math","computer_science"]}], SIG, CUTS)
    assert not match([{"tag":"jailbreak","op":"gte","values":[2]}], SIG, CUTS)
    assert match([{"tag":"pii","op":"contains","values":["cn_id_card"]}], SIG, CUTS) is False

def test_match_or_not():
    cond = [{"or":[{"tag":"domain","op":"is","values":["law"]},
                   {"tag":"fact_check","op":"is","values":["needs_check"]}]}]
    assert not match(cond, SIG, CUTS)
    assert match([{"not":{"tag":"jailbreak","op":"gte","values":[2]}}], SIG, CUTS)

def test_conf_min_gate():
    cond = [{"tag":"domain","op":"is","values":["computer_science"],"conf_min":2}]
    assert not match(cond, SIG, CUTS)                    # 0.45 → 档 1 < 门槛 2
    sig2 = {**SIG, "domain": {"value":"computer_science","confidence":0.75}}
    assert match(cond, sig2, CUTS)

def test_conf_min_on_rule_tag_never_matches():
    cond = [{"tag":"language","op":"is","values":["zh"],"conf_min":2}]
    assert not match(cond, SIG, CUTS)                    # 规则标签 confidence=None

def test_evaluate_first_hit_wins(tmp_path):
    conn = db.init_db(str(tmp_path/"t.db"))
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    sig = {**SIG, "domain": {"value": "computer_science", "confidence": 0.75}}  # 档 2 过门槛
    d = evaluate(nodes, sig, CUTS)
    assert d.kind == "route" and d.model == "qwen3-32b"  # 命中 3·编程与推理 第 0 分支
    assert d.hit_path == [3, 0]

def test_trace_reports_also_match(tmp_path):
    conn = db.init_db(str(tmp_path/"t.db"))
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    tr = evaluate_trace(nodes, SIG, CUTS)
    assert tr["path"][-1]["result"] == "hit"
    assert any(p["node"] == 0 and p["reason"] == "new_session" for p in tr["path"])
    assert "also_match" in tr

def test_fallback_catchall(tmp_path):
    conn = db.init_db(str(tmp_path/"t.db"))
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    flat = {**SIG, "domain": {"value":"other","confidence":0.4},
            "complexity": {"value":0,"confidence":0.4}, "modality": {"value":"text","confidence":0.4}}
    d = evaluate(nodes, flat, CUTS)
    assert d.model is not None                           # 兜底节点恒真（cond []）


# ── 第 8 个测试（补）：简报 Step 1 代码块实为 7 个函数、Step 4 与任务书均写 8/8，
#    且 TEMPLATES 在 Produces 接口里无覆盖——补四模板形状测试，内容自拟（记任务报告）。

def _rows(nodes):
    """模板树定义 → rows_to_tree 可吃的行 dict（形状同 db._seed/_seed_branch 落库）。"""
    import json as _json
    rows = []
    for n in nodes:
        rows.append({"idx": n["idx"], "parent": None, "name": n["name"],
                     "desc": n.get("desc"), "cond": "[]", "action": "null",
                     "trial": None, "enabled": 1})
        def walk(parent, branches):
            for b in branches:
                rows.append({"idx": b["idx"], "parent": parent, "name": "",
                             "desc": None, "cond": _json.dumps(b["cond"]),
                             "action": _json.dumps(b["action"]),
                             "trial": None, "enabled": 1})
                walk(b["idx"], b.get("branches", []))
        walk(n["idx"], n.get("branches", []))
    return rows

def _route_models(nodes):
    """全树（含子分支）route 去向模型集合。"""
    out = set()
    def walk(branches):
        for b in branches:
            if b.action.get("kind") == "route":
                out.add(b.action["model"])
            walk(b.kids)
    for n in nodes:
        walk(n.branches)
    return out

def test_templates_four_shapes():
    assert set(TEMPLATES) == {"balanced", "saver", "quality", "compliance"}
    # saver：所有分支（含子分支）去向阶梯最低档 qwen3-8b
    assert _route_models(rows_to_tree(_rows(TEMPLATES["saver"]))) == {"qwen3-8b"}
    # compliance：可疑 ≥1 或 敏感信息非空 一律 block
    comp = rows_to_tree(_rows(TEMPLATES["compliance"]))
    assert evaluate(comp, {**SIG, "jailbreak": {"value": 1, "confidence": 0.9}}, CUTS).kind == "block"
    assert evaluate(comp, {**SIG, "pii": {"value": ["email_address"], "confidence": 0.9}}, CUTS).kind == "block"
    # quality：仅 context 档 0 落最低档；context 档 2 的夜间简单请求不再降档 → 兜底高档
    qual = rows_to_tree(_rows(TEMPLATES["quality"]))
    short = {**SIG, "complexity": {"value": 0, "confidence": 0.9}}
    assert evaluate(qual, short, CUTS).model == "qwen3-8b"
    night = {**SIG, "complexity": {"value": 0, "confidence": 0.9},
             "context": {"value": 2}, "time_window": {"value": "night"}}
    assert evaluate(qual, night, CUTS).model == "qwen3-32b"


# ── Task 6 折入项（Task 5 评审 Minor b，已批准）：真·全 miss 场景——兜底分支停用
#    的内存树上，evaluate 返回防御决定，evaluate_trace 的 decision 与之恒等
#    （{path, also_match, decision} 形状恒成立，decision 不再出现 None）。

_MISS_SIG = {   # 精心全 miss：不命中安全/多媒体/编程/法律/成本/专属任一分支
    "domain": {"value": "other", "confidence": 0.4},
    "complexity": {"value": 0, "confidence": 0.4},
    "jailbreak": {"value": 0, "confidence": 0.9},
    "pii": {"value": [], "confidence": 0.9},
    "modality": {"value": "text", "confidence": 0.9},
    "user_feedback": {"value": None, "confidence": 0.4},
    "fact_check": {"value": "no_check", "confidence": 0.8},
    "keyword": {"value": []}, "language": {"value": "zh"}, "structure": {"value": "plain"},
    "context": {"value": 2}, "turns": {"value": 0}, "tenant": {"value": "anonymous"},
    "time_window": {"value": "weekday_day"}, "event": {"value": "none"},
    "similarity": {"value": 0}, "reask": {"value": "no"},
}

def test_full_miss_defensive_decision(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    conn.execute("UPDATE tree SET enabled=0 WHERE idx=90")   # 停用兜底分支 → 真·全 miss
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    d = evaluate(nodes, _MISS_SIG, CUTS)
    assert d.model is None and d.source == "fallback"        # 防御决定
    tr = evaluate_trace(nodes, _MISS_SIG, CUTS)
    assert tr["decision"] == d                               # trace 决策与 evaluate 恒等
    assert tr["path"] and all(p["result"] != "hit" for p in tr["path"])


# ── I1 解析层兜底（终审 Important 末段）：体检是主闸，但历史库/绕闸数据仍可能
#    带非法子句落库——引擎绝不能崩（/v1 持久 500 的代价），一律求值 False。

def test_atom_never_crashes_on_malformed_clauses():
    """旧闸门三类漏拦的 cond，engine 求值必须回布尔值而不是抛异常。

    - 明确按「无法求值 ⇒ False」的：嵌套 or（旧实现 KeyError 'tag'）、
      op×kind 不匹配、非整数 conf_min、空/缺 values、未知 op、未知标签。
    - ``not`` 内层裹 or/not 这类「旧实现也不崩」的形状只要求不抛异常（它的真值
      由内层子句的可求值性决定，不在本测试钉口径）。
    """
    must_be_false = [
        {"or": [{"or": [{"tag": "domain", "op": "is", "values": ["law"]}]}]},   # KeyError 'tag'
        {"tag": "complexity", "op": "contains", "values": [1]},                 # ordered×contains
        {"tag": "pii", "op": "gte", "values": ["email_address"]},               # multi×gte
        {"tag": "domain", "op": "is", "values": ["law"], "conf_min": "x"},      # conf_min 非 int
        {"tag": "domain", "op": "is", "values": ["law"], "conf_min": 1.5},
        {"tag": "domain", "op": "is", "values": []},                           # 空 values
        {"tag": "domain", "op": "is"},                                          # 缺 values
        {"tag": "domain", "op": "nope", "values": ["law"]},                     # 未知 op
        {"tag": "no_such_tag", "op": "is", "values": ["x"]},                    # 未知标签
    ]
    for c in must_be_false:
        assert match([c], SIG, CUTS) is False, c           # 不抛异常即达标
    for c in ({"not": {"or": [{"tag": "domain", "op": "is", "values": ["law"]}]}},
              {"or": [{"not": {"tag": "domain", "op": "is", "values": ["law"]}}]},
              {"not": {"not": {"tag": "domain", "op": "is", "values": ["law"]}}}):
        assert isinstance(match([c], SIG, CUTS), bool), c
    assert evaluate([], SIG, CUTS).model is None          # 空树同样走防御决定

