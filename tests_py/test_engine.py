"""Task 5 测试：engine 的 cond 求值 + evaluate + 试算轨迹（简报 Step 1 测试逐字采用）。

与简报的唯一出入（记任务报告）：SIG["domain"]["confidence"] 0.61 → 0.45。
简报注释写「0.61 → 档 1 < 门槛 2」，但按已固化的 conf_band 口径（切点含等号，
0.61 ≥ 0.60 → 档 2）恰过 conf_min=2，`assert not match` 必挂——简报测试与
其自带参考实现自相矛盾。为保住测试意图（低把握过不了门槛 2、0.75 档 2 过），
domain 置信度取档 1 中点 0.45；其余逐字未动。
"""

from sr.engine import (RouteDecision, match, evaluate, evaluate_trace,
                       rows_to_tree, TEMPLATES)
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


# ── 收尾微修（本 commit）：or/not 子句体非 list/dict → 一律 False，不抛 ──────────
# 备注 1：旧实现 match([{"or": 5}]) 直接 TypeError 逃出（PUT 落库后 /v1 持久
# 500）；{"not": 5} 更隐蔽——_atom(5) 判 False 后「not False」反成 True（本该
# 无法求值 ⇒ False）。三类非 list/dict 形状统一口径：不抛 + 整体 False。

def test_match_non_list_or_not_bodies_are_false_not_crash():
    for c in ({"or": 5}, {"not": 5}, {"or": [5]}):
        assert match([c], SIG, CUTS) is False, c
    # AND 语义：非法子句不因前序合法子句已被求值而漏判
    legal = {"tag": "domain", "op": "is", "values": ["computer_science"]}
    assert match([legal, {"or": 5}], SIG, CUTS) is False
    assert match([legal, {"not": 5}], SIG, CUTS) is False
    # 正例不误伤：合法 or/not 仍按 §5.1 求值
    assert match([{"or": [legal]}], SIG, CUTS) is True
    assert match([{"not": {"tag": "jailbreak", "op": "gte", "values": [2]}}], SIG, CUTS) is True


# ── M4（audit Major）：引擎「绝不抛异常」契约（engine.py:323 docstring）三个洞 ──
# tests_py/test_engine.py:157-181 把「不抛」钉为契约，但用例没覆盖这些形状：
# ① gte/lte 的 values[0] 非数值（"2"）→ int >= str TypeError；
# ② action.fallback: null → list(None) TypeError；
# ③ trial.ratio 为字符串/null → random() < str TypeError；trial 缺 to 时产出
#    model=None, source='trial' → server 当 502 no_route（合法配置变永久 502）。
# 闸门只挡 PUT 路径，挡不住绕闸/历史库数据——这正是该契约存在的意义。

from sr.engine import _resolve                                  # noqa: E402


def test_gte_lte_non_numeric_bound_is_false_not_crash():
    """① values[0] 非数值：比较类 op 判 False（与「引擎无法求值 ⇒ False」同口径），
    不得抛 int >= str TypeError。"""
    assert match([{"tag": "complexity", "op": "gte", "values": ["2"]}], SIG, CUTS) is False
    assert match([{"tag": "complexity", "op": "lte", "values": ["2"]}], SIG, CUTS) is False
    assert match([{"tag": "complexity", "op": "gte", "values": [None]}], SIG, CUTS) is False
    # 合法数值比较不误伤
    assert match([{"tag": "complexity", "op": "gte", "values": [1]}], SIG, CUTS) is True
    assert match([{"tag": "complexity", "op": "lte", "values": [0]}], SIG, CUTS) is False


def test_contains_non_container_value_is_false_not_crash():
    """① 的 contains 面：信号值非容器（int）时判 False，不得 `x in 1` TypeError。"""
    sig = {**SIG, "keyword": {"value": 1}}          # keyword 正常是多标签（list）
    assert match([{"tag": "keyword", "op": "contains", "values": ["x"]}], sig, CUTS) is False
    assert match([{"tag": "keyword", "op": "contains", "values": ["x"]}], SIG, CUTS) is False


def test_resolve_survives_malformed_action_and_trial(monkeypatch):
    """②③ _resolve 对畸形 action/trial 不抛，且返回值必须是「可用的 RouteDecision」：
    缺 to 的试水不得产出 model=None（否则 server 当 502 no_route——合法配置变永久
    502），应忽略试水、走原 model。"""
    monkeypatch.setattr("sr.engine.random.random", lambda: 0.5)
    good = {"kind": "route", "model": "qwen3-32b", "fallback": ["qwen3-8b"]}
    cases = [
        # (名字, action, trial, 期望 fallbacks)
        ("fallback-null", {**good, "fallback": None}, None, []),      # ② list(None)
        ("fallback-non-list", {**good, "fallback": 7}, None, []),
        ("ratio-str", good, {"ratio": "0.9", "to": "qwen3-8b"}, ["qwen3-8b"]),
        ("ratio-null", good, {"ratio": None, "to": "qwen3-8b"}, ["qwen3-8b"]),
        ("ratio-out-of-range", good, {"ratio": 5, "to": "qwen3-8b"}, ["qwen3-8b"]),
        ("to-missing", good, {"ratio": 1.0}, ["qwen3-8b"]),           # ③ 试水缺 to
        ("to-null", good, {"ratio": 1.0, "to": None}, ["qwen3-8b"]),
        ("to-non-string", good, {"ratio": 1.0, "to": 7}, ["qwen3-8b"]),
    ]
    for name, action, trial, want_fallbacks in cases:
        d = _resolve(action, trial, [3, 1])                     # 旧实现逐个 TypeError
        assert isinstance(d, RouteDecision), name
        assert d.kind == "route", name
        assert d.model == "qwen3-32b", (name, d)                # 一律回落到原 action.model
        assert d.hit_path == [3, 1], name
        assert d.fallbacks == want_fallbacks, (name, d)


def test_resolve_ignores_unknown_model_references_in_fallback_and_trial(monkeypatch):
    """绕闸坏数据里的「不存在的模型」按透出处理（体检 hard 拦保存为主闸；引擎只
    保证不抛、不产出 model=None）：fallback 链原样带上（上游解析失败由 server 的
    错误信封负责），trial.to 是字符串就照掷签改道。"""
    monkeypatch.setattr("sr.engine.random.random", lambda: 0.5)
    d = _resolve({"kind": "route", "model": "qwen3-32b",
                  "fallback": ["ghost-model"]}, None, [3, 1])
    assert d.model == "qwen3-32b" and d.fallbacks == ["ghost-model"]
    d2 = _resolve({"kind": "route", "model": "qwen3-32b"},
                  {"ratio": 1.0, "to": "ghost-model"}, [3, 1])
    assert d2.model == "ghost-model" and d2.source == "trial"


def test_resolve_legal_trial_still_works(monkeypatch):
    """正例不误伤：ratio 命中 + to 合法 → 仍改道 source='trial'。"""
    monkeypatch.setattr("sr.engine.random.random", lambda: 0.1)
    d = _resolve({"kind": "route", "model": "qwen3-32b",
                  "fallback": ["qwen3-8b"]}, {"ratio": 0.5, "to": "qwen3-8b"}, [3, 1])
    assert d.model == "qwen3-8b" and d.source == "trial"
    assert d.fallbacks == ["qwen3-8b"]
    monkeypatch.setattr("sr.engine.random.random", lambda: 0.9)   # 未命中 → 原 model
    assert _resolve({"kind": "route", "model": "qwen3-32b"},
                    {"ratio": 0.5, "to": "qwen3-8b"}, [3, 1]).source == "tree"


def test_evaluate_never_raises_on_malformed_actions(tmp_path):
    """端到端（真种子树）：把命中分支的 action 换成各种畸形形状 → evaluate 不抛，
    且返回值是可用 RouteDecision（model 不为 None）。"""
    conn = db.init_db(str(tmp_path / "t.db"))
    base = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    hit = next(b for n in base if n.idx == 5 for b in n.branches if b.idx == 50)
    for bad in ({"kind": "route", "model": "qwen3-8b", "fallback": None},
                {"kind": "route", "model": "qwen3-8b", "fallback": "qwen3-32b"},
                {"kind": "route", "model": "qwen3-8b",
                 "trial": {"ratio": "0.5", "to": "qwen3-32b"}}):
        hit.action = bad
        d = evaluate(base, SIG, CUTS)                  # 旧实现 TypeError 逃出
        assert isinstance(d, RouteDecision) and d.model, bad
        tr = evaluate_trace(base, SIG, CUTS)
        assert isinstance(tr["decision"], RouteDecision), bad


# ── C1 Minor 4：`_EMPTY_ACTION` 模块级可变 dict 被共享返回 ───────────────────────
# 旧实现：action 非 dict 时 `_action_of`/`_resolve` 返回同一个模块级 dict，任何调用方
# 就地改它都会污染之后所有「空 action」分支（跨请求串味）。修法：每次新建 dict。

def test_action_of_returns_fresh_dict_each_call():
    """`_action_of` 对非 dict action 每次返回新 dict（不是同一模块级对象）。"""
    from sr.engine import _EMPTY_ACTION, _action_of, Branch

    b = Branch(idx=1, cond=[], action=None, trial=None, enabled=True, kids=[])
    a1, a2 = _action_of(b), _action_of(b)
    assert a1 == {"kind": "route"} and a2 == {"kind": "route"}
    assert a1 is not a2, "两次调用返回了同一对象（模块级共享可变 dict）"
    a1["model"] = "polluted"                       # 就地改不得污染下一次
    assert _action_of(b) == {"kind": "route"}
    assert _EMPTY_ACTION == {"kind": "route"}, _EMPTY_ACTION


def test_resolve_does_not_share_empty_action_dict():
    """`_resolve(None, …)` 的兜底同样不得写回模块级 dict。"""
    from sr.engine import _EMPTY_ACTION, _resolve

    d = _resolve(None, None, [1, 0])
    assert d.model is None and d.kind == "route"
    assert _EMPTY_ACTION == {"kind": "route"}, _EMPTY_ACTION

