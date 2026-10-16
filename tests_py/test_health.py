"""Task 10 测试：体检八项（简报 Step 1 测试逐字采用 + 折入项单测）。

与简报的出入（记任务报告）：
- 追加 test_empty_values_is_hard_error：折入项（Task 5 评审遗留）——cond 子句
  ``values`` 为空列表会让 engine._atom 取 ``values[0]`` 抛 IndexError；§10 CHECKS
  表没有独立的「子句语法」检查项，控制者裁定并入 bad_ref（hard），本测试钉死
  该行为（实现侧声明见 sr/health.py check_bad_ref 注释）。
"""

import pytest, copy
import sr.db as db
from sr.engine import rows_to_tree
from sr.tags import TAGS
from sr.health import check_bad_ref, run_checkup

# 测试替身：id 集合即可。必须覆盖 balanced 种子树引用的全部模型——否则 bad_ref
# 会因「引用了不存在的模型」恒有 hard，new 的「正例不误报」断言就失去分辨力。
MODELS = {"qwen3-8b": {}, "qwen3-32b": {}, "claude-sonnet": {},
          "sdxl-image": {}, "llava-omni": {}}

@pytest.fixture
def base_nodes(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))                    # balanced 模板 8 节点
    return rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())

def codes(findings): return {f.code for f in findings}

def test_no_fallback_is_hard_error(base_nodes):
    nodes = [n for n in base_nodes if n.kind != "fallback"]
    f = run_checkup(nodes, MODELS, TAGS)
    assert any(x.code == "no_fallback" and x.level == "hard" for x in f)

def test_bad_ref_hard(base_nodes):
    base_nodes[2].branches[0].cond = [{"tag": "domain", "op": "is", "values": ["astrology"]}]
    assert "bad_ref" in codes(run_checkup(base_nodes, MODELS, TAGS))

def test_conf_on_rule_tag_warns(base_nodes):
    base_nodes[1].branches[0].cond = [{"tag": "language", "op": "is",
                                       "values": ["zh"], "conf_min": 2}]
    assert "conf_on_rule" in codes(run_checkup(base_nodes, MODELS, TAGS))

def test_special_misplaced_hard(base_nodes):
    base_nodes[3].branches[0].action = {"kind": "special", "special": "sticky"}
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert any(x.code == "special_misplaced" and x.level == "hard" for x in f)

def test_dead_branch_and_duplicate_warn(base_nodes):
    dup = copy.deepcopy(base_nodes[3].branches[0])   # 与 3 号节点首分支条件完全相同
    dup.idx = 39
    base_nodes[3].branches.append(dup)
    f = run_checkup(base_nodes, MODELS, TAGS)
    cs = codes(f)
    assert "duplicate" in cs                          # 同 cond 两条
    assert "dead_branch" in cs                        # 后者被前者完全覆盖
    assert all(x.level != "hard" for x in f if x.code in {"duplicate", "dead_branch"})


# ── 折入项单测（控制者裁定，Task 5 评审遗留） ──────────────────────────────────

def test_empty_values_is_hard_error(base_nodes):
    """折入项：values 空列表 → 引擎 IndexError（Task 5 评审），体检必须 hard 拦截。
    CHECKS 表无子句语法检查项，并入 bad_ref 同级（hard），见 sr/health.py 注释。"""
    base_nodes[4].branches[0].cond = [{"tag": "domain", "op": "is", "values": []}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert any(x.code == "bad_ref" and x.level == "hard" for x in f)


# ── I1（终审 Important）：cond 语法闸门三段补齐，均 hard ─────────────────────────
# 漏拦三类实测均 PUT /admin/tree 200，随后 /v1 持久 500：
#   ① or/not 内层再嵌 or/not（engine._atom 收到 {"or": …} → KeyError 'tag'）
#   ② op 与标签 kind 不匹配（ordered 配 contains → int 不是容器 / pii 配 gte）
#   ③ conf_min 非整数（"x" → '<' not supported between int and str）


def _bad_ref_hard(findings):
    return [f for f in findings if f.code == "bad_ref" and f.level == "hard"]


def test_nested_or_is_hard_bad_ref(base_nodes):
    """① or 内层再嵌 or → hard bad_ref（引擎只会 KeyError 'tag'）。"""
    base_nodes[1].branches[0].cond = [
        {"or": [{"or": [{"tag": "domain", "op": "is", "values": ["law"]}]},
                {"tag": "fact_check", "op": "is", "values": ["needs_check"]}]}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert _bad_ref_hard(f), [x.to_json() for x in f]


def test_nested_or_atom_is_hard_bad_ref(base_nodes):
    """① or 内层的 not 包装同样非法（§5.1 or/not 只包普通子句）。"""
    base_nodes[1].branches[0].cond = [
        {"or": [{"not": {"tag": "domain", "op": "is", "values": ["law"]}},
                {"tag": "fact_check", "op": "is", "values": ["needs_check"]}]}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert _bad_ref_hard(f), [x.to_json() for x in f]


def test_nested_not_inner_is_hard_bad_ref(base_nodes):
    """① not 内层裹 or/not → hard bad_ref。"""
    base_nodes[1].branches[0].cond = [
        {"not": {"or": [{"tag": "domain", "op": "is", "values": ["law"]}]}}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert _bad_ref_hard(f), [x.to_json() for x in f]


def test_legal_or_still_passes(base_nodes):
    """合法 or（两个普通子句）/ 合法 not（单个普通子句）不误报（I1 不得过拦）。"""
    base_nodes[1].branches[0].cond = [
        {"or": [{"tag": "domain", "op": "is", "values": ["law"]},
                {"tag": "fact_check", "op": "is", "values": ["needs_check"]}]}]
    base_nodes[2].branches[0].cond = [
        {"not": {"tag": "complexity", "op": "is", "values": [1]}}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert not _bad_ref_hard(f), [x.to_json() for x in f]


def test_ordered_tag_with_contains_is_hard_bad_ref(base_nodes):
    """② ordered 标签（int 档位）配 contains → hard bad_ref（int 不是容器）。"""
    base_nodes[1].branches[0].cond = [
        {"tag": "complexity", "op": "contains", "values": [1]}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert _bad_ref_hard(f), [x.to_json() for x in f]


def test_multi_tag_with_gte_is_hard_bad_ref(base_nodes):
    """② 列表值标签（pii）配 gte → hard bad_ref（list >= list 崩）。"""
    base_nodes[1].branches[0].cond = [
        {"tag": "pii", "op": "gte", "values": ["email_address"]}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert _bad_ref_hard(f), [x.to_json() for x in f]


def test_normal_op_kind_combos_still_pass(base_nodes):
    """② 合法 op×kind 组合不误报：ordered 用 gte/lte/is、multi 用 contains、
    choice 用 in（I1 只拦真会崩的组合）。"""
    base_nodes[1].branches[0].cond = [
        {"tag": "complexity", "op": "gte", "values": [1]},
        {"tag": "pii", "op": "contains", "values": ["email_address"]},
        {"tag": "domain", "op": "in", "values": ["law", "math"]}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert not _bad_ref_hard(f), [x.to_json() for x in f]


def test_conf_min_non_int_is_hard_bad_ref(base_nodes):
    """③ conf_min 非整数（"x" / 1.5 / True）→ hard bad_ref，不得落库后 /v1 500。"""
    for bad in ("x", 1.5, True):
        base_nodes[1].branches[0].cond = [
            {"tag": "domain", "op": "is", "values": ["law"], "conf_min": bad}]
        f = run_checkup(base_nodes, MODELS, TAGS)
        assert _bad_ref_hard(f), (bad, [x.to_json() for x in f])


def test_conf_min_int_is_legal(base_nodes):
    """③ conf_min 整数合法（正例：conf_min 本身是 §5.1 语法的一部分）。"""
    base_nodes[1].branches[0].cond = [
        {"tag": "domain", "op": "is", "values": ["law"], "conf_min": 2}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert not _bad_ref_hard(f), [x.to_json() for x in f]


def test_rule_tag_with_conf_min_stays_warn_not_hard(base_nodes):
    """I1 关键提醒：规则标签配 conf_min 仍按 §10 走 conf_on_rule **warn**，
    不得因为新加的 conf_min 类型闸门连带改成 hard（终审已确认的陷阱）。"""
    base_nodes[1].branches[0].cond = [
        {"tag": "language", "op": "is", "values": ["zh"], "conf_min": 2}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert any(x.code == "conf_on_rule" and x.level == "warn" for x in f), \
        [x.to_json() for x in f]
    assert not _bad_ref_hard(f), [x.to_json() for x in f]


# ── 收尾微修（本 commit）备注 1：or/not 子句体非 list/dict → hard bad_ref ────────
# 旧实现：check_bad_ref 的 clause_errors 直接 `for sc in c["or"]` → TypeError 逃出
# run_checkup（PUT /admin/tree 500，库无写入）；更早的 check_dead_branch
# （_cond_key）与 check_unused_tags（walk）同样会先炸。三类形状：or 体非 list、
# not 体非 dict、or 臂非 dict（后者原本已由 atom 报「条件子句不是对象」）。


def test_or_not_body_non_list_is_hard_bad_ref(base_nodes):
    for cond in ([{"or": 5}], [{"not": 5}], [{"or": [5]}]):
        base_nodes[1].branches[0].cond = cond
        f = run_checkup(base_nodes, MODELS, TAGS)      # 旧实现此处 TypeError
        assert _bad_ref_hard(f), (cond, [x.to_json() for x in f])


def test_legal_or_not_bodies_still_pass_after_shape_gate(base_nodes):
    """形状闸门不得误伤合法 or/not（or 体 list、not 体 dict）。"""
    base_nodes[1].branches[0].cond = [
        {"or": [{"tag": "domain", "op": "is", "values": ["law"]},
                {"tag": "fact_check", "op": "is", "values": ["needs_check"]}]}]
    base_nodes[2].branches[0].cond = [
        {"not": {"tag": "complexity", "op": "is", "values": [1]}}]
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert not _bad_ref_hard(f), [x.to_json() for x in f]


# ── 端点级回归：PUT /admin/tree 三类形状 → 422 且树零写入 ──────────────────────
# 自建最小 app（模式照抄 test_admin.py：ASGITransport + Python 3.14 垫片），
# 以免改动 test_admin.py——本任务改动范围限本文件与同批三个模块。

import asyncio as _asyncio                                  # noqa: E402
import httpx as _httpx                                      # noqa: E402
from sr.server import create_app as _create_app             # noqa: E402
from sr.tagger import HeuristicTagger                       # noqa: E402

_asyncio.set_event_loop(_asyncio.new_event_loop())          # 3.14 起无隐式环


@pytest.fixture
def app(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    conn.close()
    return _create_app(str(tmp_path / "t.db"), HeuristicTagger())


def _admin_get(app, path):
    async def go():
        async with _httpx.AsyncClient(transport=_httpx.ASGITransport(app=app),
                                      base_url="http://t") as c:
            return await c.get(path)
    return _asyncio.get_event_loop().run_until_complete(go())


def _admin_put(app, path, json=None):
    async def go():
        async with _httpx.AsyncClient(transport=_httpx.ASGITransport(app=app),
                                      base_url="http://t") as c:
            return await c.put(path, json=json)
    return _asyncio.get_event_loop().run_until_complete(go())


def _admin_post_raw(app, path, content, content_type="application/x-yaml"):
    """原始文本 body（/admin/import 收 YAML 文本包，非 JSON）。"""
    async def go():
        async with _httpx.AsyncClient(transport=_httpx.ASGITransport(app=app),
                                      base_url="http://t") as c:
            return await c.post(path, content=content,
                                headers={"Content-Type": content_type})
    return _asyncio.get_event_loop().run_until_complete(go())


def test_put_tree_non_list_or_not_body_422_zero_write(app):
    base = _admin_get(app, "/admin/tree").json()
    for cond in ([{"or": 5}], [{"not": 5}], [{"or": [5]}]):
        t = _admin_get(app, "/admin/tree").json()
        b40 = next(b for n in t["nodes"] if n["idx"] == 4
                   for b in n["branches"] if b["idx"] == 40)
        b40["cond"] = cond
        r = _admin_put(app, "/admin/tree", t)
        assert r.status_code == 422, (cond, r.status_code, r.text)
        assert any(f["code"] == "bad_ref" and f["level"] == "hard"
                   for f in r.json()["findings"]), (cond, r.text)
    after = _admin_get(app, "/admin/tree").json()             # 整树零写入
    assert after["nodes"] == base["nodes"], "nodes 被改动"
    assert after["version"] == base["version"], "tree_version 被改动"


# ── M3 / M4 / M5（audit Major）：route action 形状闸门（think / fallback / trial）──
# M3：action.think 无值域闸门 → to_upstream 对值域外 think 抛 ValueError，_call_*
#     只捕 UpstreamError/httpx.HTTPError → /v1 每条请求持久 500。
# M4：action.fallback: null（`list(None)`）/ trial.ratio 非数值（`random() < str`）/
#     trial.to 必填且引用的模型必须存在（缺 to 时 engine 产出 model=None →
#     server 当 502 no_route，合法配置变永久 502）。
# M5：_checkup_or_422 对 run_checkup 无 try/except——体检自身崩溃 = PUT 500。
# 这些检查并入 check_bad_ref（§10 无独立「action 形状」项；与本模块既有的
# 「子句引用了引擎无法求值的东西」同族），硬级。

def test_health_rejects_malformed_trial_and_fallback_shapes(base_nodes):
    """体检（主闸）必须把这些绕闸数据拦在落库前：trial.ratio 值域、trial.to 必填且
    模型存在、fallback 必须是 list（null 非法）且元素为存在的模型 id、think 值域。"""
    def hard(nodes):
        return check_bad_ref(nodes, MODELS, TAGS, True)

    b50 = next(b for n in base_nodes if n.idx == 5 for b in n.branches if b.idx == 50)
    good = {"kind": "route", "model": "qwen3-8b", "fallback": ["qwen3-32b"],
            "think": "light"}
    b50.action = dict(good)
    assert not hard(base_nodes), [f.to_json() for f in hard(base_nodes)]   # 正例

    for bad in ({"ratio": "0.5", "to": "qwen3-32b"}, {"ratio": 1.5, "to": "qwen3-32b"},
                {"ratio": -0.1, "to": "qwen3-32b"}, {"ratio": None, "to": "qwen3-32b"},
                {"ratio": 0.5}, {"ratio": 0.5, "to": "ghost-model"},
                {"ratio": 0.5, "to": None}, "not-a-dict"):
        b50.action = {**good, "trial": bad}
        assert hard(base_nodes), bad
    b50.action = dict(good)
    for bad in (None, "qwen3-32b", 7, [None], ["ghost-model"]):
        b50.action = {**good, "fallback": bad}
        assert hard(base_nodes), bad
    for bad in ("crazy", 5, ["deep"], True):
        b50.action = {**good, "think": bad}
        assert hard(base_nodes), bad
    b50.action = {**good, "think": None}
    assert not hard(base_nodes), [f.to_json() for f in hard(base_nodes)]


def test_health_shape_findings_are_hard_with_readable_messages(base_nodes):
    """形状类 findings 一律 hard bad_ref + 双语可读 message（控制台首屏直指原因）。"""
    b50 = next(b for n in base_nodes if n.idx == 5 for b in n.branches if b.idx == 50)
    b50.action = {"kind": "route", "model": "qwen3-8b", "think": "crazy",
                  "fallback": None, "trial": {"ratio": "0.5"}}
    f = check_bad_ref(base_nodes, MODELS, TAGS, True)
    assert f and all(x.level == "hard" and x.code == "bad_ref" for x in f), \
        [x.to_json() for x in f]
    assert all(x.message_zh and x.message_en for x in f)
    text = " ".join(x.message_zh + x.message_en for x in f)
    assert "think" in text and "fallback" in text and "trial" in text, text


def test_run_checkup_does_not_crash_on_fallback_null(base_nodes):
    """M5 同源：``run_checkup`` 收到 fallback: null / "x" / [null] 不得自身抛
    TypeError（旧实现 refs 组装 `*act.get("fallback", [])` 直接崩）。"""
    b50 = next(b for n in base_nodes if n.idx == 5 for b in n.branches if b.idx == 50)
    for bad in (None, "x", [None]):
        b50.action = {"kind": "route", "model": "qwen3-8b", "fallback": bad}
        findings = run_checkup(base_nodes, MODELS, TAGS)      # 旧实现此处 TypeError
        assert isinstance(findings, list) and findings
        assert any(x.code == "bad_ref" and x.level == "hard" for x in findings), bad


# ── M5（audit Major）：体检闸门自身崩溃不得变成 PUT 500 ────────────────────────
# 旧实现 _checkup_or_422 对 run_checkup 无 try/except：体检内的任何异常都逃逸成
# 500（真 uvicorn 下是 HTML/无信封）。修法：run_checkup 调用点加保护——体检内部
# 异常降级为 hard finding code="checkup_error"（不掩盖问题：仍是 422 拦截）。

def test_checkup_error_degrades_to_hard_finding_not_500(app, monkeypatch):
    """run_checkup 抛异常 → PUT /admin/tree 回 422 + hard checkup_error（不是 500）。"""
    import sr.health as health_mod

    def _boom(*a, **k):
        raise RuntimeError("SENTINEL-CHECKUP-CRASH")

    # A1：run_checkup_safe 在 sr.health，查的是 sr.health 模块全局名
    monkeypatch.setattr(health_mod, "run_checkup", _boom)
    before = _admin_get(app, "/admin/tree").json()
    r = _admin_put(app, "/admin/tree", before)
    assert r.status_code == 422, (r.status_code, r.text)
    f = r.json()["findings"]
    assert f and f[0]["code"] == "checkup_error" and f[0]["level"] == "hard", r.text
    assert "SENTINEL" not in r.text                      # 不回吐异常原文
    after = _admin_get(app, "/admin/tree").json()
    assert after == before


def test_import_checkup_crash_422_not_500(app, monkeypatch):
    """import 路径同样受保护（config.import_yaml 里也有 run_checkup 调用点）。"""
    import sr.config as config_mod
    import sr.health as health_mod

    def _boom(*a, **k):
        raise RuntimeError("SENTINEL-CHECKUP-CRASH")

    monkeypatch.setattr(health_mod, "run_checkup", _boom)
    yml = _admin_get(app, "/admin/export").text
    r = _admin_post_raw(app, "/admin/import", yml)
    assert r.status_code == 422, (r.status_code, r.text)
    assert r.json()["findings"][0]["code"] == "checkup_error", r.text
    assert "SENTINEL" not in r.text


# ── M5 端点级：PUT /admin/tree 的 fallback 三种畸形 → 422（绝不 500）且库零写入 ──

def _route_action_of(t, node, branch, key):
    return next(b for n in t["nodes"] if n["idx"] == node
                for b in n["branches"] if b["idx"] == branch)[key]


def test_put_tree_fallback_null_422_zero_write(app):
    """旧实现：fallback 键在且值为 null → `*None` TypeError 逃出 run_checkup
    （PUT 500）。现在必须是 422 + 可读 hard finding + 库零写入。"""
    base = _admin_get(app, "/admin/tree").json()
    for bad in (None, "x", [None]):
        t = _admin_get(app, "/admin/tree").json()
        _route_action_of(t, 9, 90, "action")["fallback"] = bad
        r = _admin_put(app, "/admin/tree", t)
        assert r.status_code == 422, (bad, r.status_code, r.text)
        f = [x for x in r.json()["findings"] if x["code"] == "bad_ref"]
        assert f and all(x["level"] == "hard" for x in f), (bad, r.text)
        assert all(x["message"]["zh"] and x["message"]["en"] for x in f), bad
    after = _admin_get(app, "/admin/tree").json()
    assert after["nodes"] == base["nodes"] and after["version"] == base["version"]


def test_put_tree_bad_think_422_zero_write(app):
    """M3 端点级：think 值域外 → 422 + hard bad_ref（三条写路径之一）。"""
    base = _admin_get(app, "/admin/tree").json()
    t = _admin_get(app, "/admin/tree").json()
    _route_action_of(t, 9, 90, "action")["think"] = "crazy"
    r = _admin_put(app, "/admin/tree", t)
    assert r.status_code == 422, (r.status_code, r.text)
    assert any(x["code"] == "bad_ref" and x["level"] == "hard"
               for x in r.json()["findings"]), r.text
    after = _admin_get(app, "/admin/tree").json()
    assert after["nodes"] == base["nodes"] and after["version"] == base["version"]


def test_import_bad_think_422_zero_write(app):
    """M3 写路径三（import）：坏 think 包 → 422 findings；树零写入。"""
    import yaml
    before = _admin_get(app, "/admin/tree").json()
    data = yaml.safe_load(_admin_get(app, "/admin/export").text)
    node9 = next(n for n in data["tree"]["nodes"] if n["idx"] == 9)
    node9["branches"][0]["action"]["think"] = "crazy"
    r = _admin_post_raw(app, "/admin/import",
                        yaml.safe_dump(data, allow_unicode=True))
    assert r.status_code == 422, (r.status_code, r.text)
    assert any(f["code"] == "bad_ref" and f["level"] == "hard"
               for f in r.json()["findings"]), r.text
    assert _admin_get(app, "/admin/tree").json() == before


# ── C1 批次 Important：`action.kind` 缺失/拼错 = 合法配置变永久 502 ────────────
# 旧闸门 `if act.get("kind") != "route": continue`（health.py:419）在 kind 缺失或
# 拼错时**整体跳过**该 action 的校验：`{"kind":"rout"}` / `{}` 实测 PUT 200 落库、
# findings 空，随后命中该分支的 /v1 因引擎产出 model=None 而恒 502 no_route。
# 修法：kind 不在 {route, block, special} → hard bad_ref；三种 kind 各自的必填字段
# （route.model 的存在性沿用既有 refs 检查、block.block_msg、special.special）同族 hard。
# 「special 只能在起点节点」是既有的 check_special_misplaced，本闸门不重复报。

def _b90(nodes):
    """出厂树节点 9 分支 90 的 Branch（C1 用例的统一改动点）。"""
    return next(b for n in nodes if n.idx == 9
                for b in n.branches if b.idx == 90)


# 简报验收的五类畸形 action（逐类 PUT 必须 422 + hard + 库零写入）
_MALFORMED_ACTIONS = [
    {},                        # kind 缺失（旧实现：整体跳过 → PUT 200）
    {"kind": "rout"},          # kind 拼错（旧实现：整体跳过 → PUT 200）
    {"kind": "route"},         # route 无 model（旧实现已由 refs 的 None 拦下）
    {"kind": "block"},         # block 无 block_msg
    {"kind": "special"},       # special 无 special 值
]


@pytest.mark.parametrize("action", _MALFORMED_ACTIONS)
def test_action_kind_malformed_is_hard_bad_ref(base_nodes, action):
    """五类畸形 action 一律 hard bad_ref（C1 Important 的单元级验收）。"""
    _b90(base_nodes).action = action
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert _bad_ref_hard(f), (action, [x.to_json() for x in f])
    assert not any(x.code == "checkup_error" for x in f), [x.to_json() for x in f]


def test_action_kind_gate_rejects_unknown_values(base_nodes):
    """kind 值域外（None / 数字 / 空串）同属 hard bad_ref，不是只有缺键才拦。"""
    for bad in (None, 5, "", "Route", "blocks"):
        _b90(base_nodes).action = {"kind": bad}
        f = check_bad_ref(base_nodes, MODELS, TAGS, True)
        assert _bad_ref_hard(f), (bad, [x.to_json() for x in f])


def test_action_kind_gate_does_not_touch_factory_tree(base_nodes):
    """出厂种子树零 hard：新闸门不得误伤既定形状（route/block/special 三种都合法）。"""
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert not [x for x in f if x.level == "hard"], [x.to_json() for x in f]


def test_action_kind_gate_accepts_legal_actions(base_nodes):
    """三种 kind 的合法形状都放行；special 的位置问题归 check_special_misplaced。"""
    _b90(base_nodes).action = {"kind": "route", "model": "qwen3-8b"}
    assert not _bad_ref_hard(check_bad_ref(base_nodes, MODELS, TAGS, True))
    _b90(base_nodes).action = {"kind": "block",
                               "block_msg": {"zh": "拦截", "en": "Blocked"}}
    assert not _bad_ref_hard(check_bad_ref(base_nodes, MODELS, TAGS, True))
    base_nodes[0].branches[0].action = {"kind": "special", "special": "sticky"}
    assert not _bad_ref_hard(check_bad_ref(base_nodes, MODELS, TAGS, True))


@pytest.mark.parametrize("bad_msg", [
    "Blocked",                            # 非对象 → server 的 msg_map.get 会 AttributeError
    {"zh": "拦截"},                        # 缺 en（§5.1/§6.5 双语形状）
    {"en": "Blocked"},                    # 缺 zh
    {"zh": "", "en": "Blocked"},          # 空串等同缺失
    {"zh": "拦截", "en": 5},              # 非字符串
])
def test_block_action_requires_bilingual_block_msg(base_nodes, bad_msg):
    """block 必须有 §5.1 形状的双语 block_msg（缺/错形状 → hard bad_ref）。"""
    _b90(base_nodes).action = {"kind": "block", "block_msg": bad_msg}
    f = check_bad_ref(base_nodes, MODELS, TAGS, True)
    assert _bad_ref_hard(f), (bad_msg, [x.to_json() for x in f])


def test_block_action_with_legal_block_msg_passes(base_nodes):
    """正例：双语 block_msg 是合法形状（出厂树节点 1 就是它）。"""
    _b90(base_nodes).action = {"kind": "block",
                               "block_msg": {"zh": "拦截", "en": "Blocked"}}
    assert not _bad_ref_hard(check_bad_ref(base_nodes, MODELS, TAGS, True))


@pytest.mark.parametrize("bad_special", [None, "sticky2", "ladder", "", 3, "unlock"])
def test_special_action_requires_known_special_value(base_nodes, bad_special):
    """special 值必须是 sticky/ladder_up/ladder_next（缺/未知 → hard bad_ref）。"""
    base_nodes[0].branches[0].action = {"kind": "special", "special": bad_special}
    f = check_bad_ref(base_nodes, MODELS, TAGS, True)
    assert _bad_ref_hard(f), (bad_special, [x.to_json() for x in f])


def test_special_placement_still_reported_only_by_special_misplaced(base_nodes):
    """提醒落实：special 落在非起点节点由 special_misplaced 报，本闸门不重复报
    （值域合法时不产生 bad_ref）。"""
    base_nodes[3].branches[0].action = {"kind": "special", "special": "sticky"}
    f = check_bad_ref(base_nodes, MODELS, TAGS, True)
    assert not _bad_ref_hard(f), [x.to_json() for x in f]
    assert any(x.code == "special_misplaced" and x.level == "hard"
               for x in run_checkup(base_nodes, MODELS, TAGS))


# ── C1 Minor 1：refs 无条件 append(to) → 幻影「模型 None」finding ───────────────

def test_trial_missing_to_reports_exactly_one_finding(base_nodes):
    """trial 缺 to：只报「to 必填」这一条，不再多报「引用了不存在的模型 None」。"""
    _b90(base_nodes).action = {"kind": "route", "model": "qwen3-8b",
                               "trial": {"ratio": 0.5}}
    hits = [x for x in check_bad_ref(base_nodes, MODELS, TAGS, True)
            if x.node == 9 and x.branch == 90]
    assert len(hits) == 1, [x.to_json() for x in hits]
    assert "trial.to" in hits[0].message_zh, hits[0].to_json()
    assert "不存在的模型" not in hits[0].message_zh, hits[0].to_json()


def test_fallback_null_element_reports_exactly_one_finding(base_nodes):
    """fallback:[null]：只报「元素必须是模型 id 字符串」，不再多报幻影 None 模型。"""
    _b90(base_nodes).action = {"kind": "route", "model": "qwen3-8b",
                               "fallback": [None]}
    hits = [x for x in check_bad_ref(base_nodes, MODELS, TAGS, True)
            if x.node == 9 and x.branch == 90]
    assert len(hits) == 1, [x.to_json() for x in hits]
    assert "fallback" in hits[0].message_zh, hits[0].to_json()


def test_fallback_unknown_model_still_reported(base_nodes):
    """去重不得放过真·未知模型：fallback 里的合法字符串 id 仍走 refs 检查。"""
    _b90(base_nodes).action = {"kind": "route", "model": "qwen3-8b",
                               "fallback": ["ghost-model"]}
    hits = [x for x in check_bad_ref(base_nodes, MODELS, TAGS, True)
            if x.node == 9 and x.branch == 90]
    assert len(hits) == 1 and "ghost-model" in hits[0].message_zh, \
        [x.to_json() for x in hits]


# ── C1 Minor 2：action 形状 findings 复用 bad() → fix_clause 误导 ──────────────

def test_action_shape_findings_use_action_scoped_fix_not_fix_clause(base_nodes):
    """值域/形状类（think / fallback 非 list / trial.ratio）→ fix_action；
    模型引用类（未知模型 / trial.to）→ fix_model；两类都绝不给 fix_clause
    （fix_clause 指向「改条件」，对 action 是误导）。"""
    _b90(base_nodes).action = {"kind": "route", "model": "qwen3-8b",
                               "think": "crazy", "fallback": None,
                               "trial": {"ratio": "0.5", "to": "ghost-model"}}
    hits = [x for x in check_bad_ref(base_nodes, MODELS, TAGS, True)
            if x.node == 9 and x.branch == 90]
    assert len(hits) >= 4, [x.to_json() for x in hits]   # think+fallback+ratio+未知模型
    assert not any(x.fix == {"action": "fix_clause"} for x in hits), \
        [x.to_json() for x in hits]

    def fix_of(kw):
        got = [x.fix for x in hits if kw in x.message_zh]
        assert len(got) == 1, (kw, [x.to_json() for x in hits])
        return got[0]

    assert fix_of("think") == {"action": "fix_action"}
    assert fix_of("fallback") == {"action": "fix_action"}
    assert fix_of("trial.ratio") == {"action": "fix_action"}
    assert fix_of("不存在的模型") == {"action": "fix_model"}


def test_model_reference_findings_keep_fix_model(base_nodes):
    """模型引用类（未知 model / trial.to 引用未知模型 / fallback 元素非 id）→ fix_model。"""
    _b90(base_nodes).action = {"kind": "route", "model": "ghost-model"}
    hits = [x for x in check_bad_ref(base_nodes, MODELS, TAGS, True)
            if x.node == 9 and x.branch == 90]
    assert hits and all(x.fix == {"action": "fix_model"} for x in hits), \
        [x.to_json() for x in hits]


def test_clause_findings_keep_fix_clause(base_nodes):
    """条件子句类仍归 fix_clause（本次改动不得反向误伤既有建议）。"""
    _b90(base_nodes).cond = [{"tag": "no-such-tag", "op": "is", "values": [1]}]
    hits = [x for x in check_bad_ref(base_nodes, MODELS, TAGS, True)
            if x.node == 9 and x.branch == 90]
    assert hits and all(x.fix == {"action": "fix_clause"} for x in hits), \
        [x.to_json() for x in hits]


# ── C1 Minor 3：check_dead_branch 在形状守卫之前 `b.action.get` → 守卫不可达 ────
# 旧实现 CHECKS 表首项 check_dead_branch（health.py:169）先做 `b.action.get("kind")`，
# action 非 dict 时 AttributeError 逃出 run_checkup，被 run_checkup_safe 兜成通用
# checkup_error —— check_bad_ref 里「action 必须是对象」的精确 finding 事实不可达。

@pytest.mark.parametrize("bad_action", [None, "route", 7, ["route"], True])
def test_non_dict_action_yields_precise_finding_not_checkup_error(base_nodes,
                                                                  bad_action):
    """action 非 dict → run_checkup 自身不崩，出精确 bad_ref（不是 checkup_error）。

    旧实现在这里 AttributeError（生产上 = 422 checkup_error，精确原因丢失）。
    """
    _b90(base_nodes).action = bad_action
    f = run_checkup(base_nodes, MODELS, TAGS)
    assert not any(x.code == "checkup_error" for x in f), [x.to_json() for x in f]
    hits = [x for x in f if x.code == "bad_ref" and x.node == 9 and x.branch == 90]
    assert hits and hits[0].level == "hard", [x.to_json() for x in f]
    assert "action" in hits[0].message_zh


def test_non_dict_action_survives_all_checks_with_hits(base_nodes):
    """带 hits 跑（check_no_hits 也读 b.action）时同样不得崩。"""
    _b90(base_nodes).action = None
    f = run_checkup(base_nodes, MODELS, TAGS, hits={})
    assert not any(x.code == "checkup_error" for x in f), [x.to_json() for x in f]
    assert any(x.code == "bad_ref" and x.level == "hard" for x in f)


@pytest.mark.parametrize("bad_action", [None, "route", 7, ["route"]])
def test_non_dict_action_does_not_crash_other_checks(base_nodes, bad_action):
    """逐个检查函数直调同样不崩（check_duplicate / check_special_misplaced /
    check_dead_branch / check_no_hits 都读 b.action.get）。"""
    from sr.health import (check_dead_branch, check_duplicate, check_no_hits,
                           check_special_misplaced)
    _b90(base_nodes).action = bad_action
    for fn in (check_dead_branch, check_duplicate, check_special_misplaced):
        fn(base_nodes, MODELS, TAGS, False)
    check_no_hits(base_nodes, MODELS, TAGS, False, hits={})


def test_import_bad_action_kind_422_zero_write(app):
    """写路径三（import）：缺 kind 的 action 整包拒绝 422 + hard bad_ref，
    树零写入（与 PUT / POST nodes 三条写路径同过体检）。"""
    import yaml
    before = _admin_get(app, "/admin/tree").json()
    data = yaml.safe_load(_admin_get(app, "/admin/export").text)
    node9 = next(n for n in data["tree"]["nodes"] if n["idx"] == 9)
    node9["branches"][0]["action"] = {"model": "qwen3-8b"}      # 缺 kind
    r = _admin_post_raw(app, "/admin/import",
                        yaml.safe_dump(data, allow_unicode=True))
    assert r.status_code == 422, (r.status_code, r.text)
    f = [x for x in r.json()["findings"] if x["code"] == "bad_ref"]
    assert f and all(x["level"] == "hard" for x in f), r.text
    assert "kind" in f[0]["message"]["zh"], f[0]
    assert _admin_get(app, "/admin/tree").json() == before


def test_import_checkup_crash_config_layer_returns_findings(tmp_path):
    """同一保护的模块契约：config.import_yaml 里 run_checkup 崩 → 回 findings
    （不是异常逃逸）。"""
    import sr.config as config_mod
    import sr.health as health_mod

    conn = db.init_db(str(tmp_path / "cfg-boom.db"))
    try:
        text = config_mod.export_yaml(conn)
        orig = health_mod.run_checkup
        # run_checkup_safe 自 A1 起在 sr.health，查的是 sr.health 模块全局名
        health_mod.run_checkup = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("SENTINEL-CHECKUP-CRASH"))
        try:
            findings = config_mod.import_yaml(conn, text)
        finally:
            health_mod.run_checkup = orig
        assert isinstance(findings, list) and findings
        assert findings[0]["code"] == "checkup_error"
        assert "SENTINEL" not in str(findings)
    finally:
        conn.close()


# ── M9（audit Major）：出厂树零告警 ────────────────────────────────────────────
# 审核报告 §2-M9：四档难度收敛两档后，balanced 分支 31（难→32b 深度）与 32
# （原「较难→32b 轻度」cond 逐字相同）使全新库体检必出 duplicate + dead_branch
# 两条 warn——出厂树应是零 findings 范本，现在告警信号被永久稀释。修复后：
# 30 学科+难+把握度门槛→32b 深度；31 难→32b 深度；32 简单→8b 轻度。

def test_factory_tree_checkup_has_no_hard_no_warn(tmp_path):
    """全新库 run_checkup（不经 run_checkup_safe，直出原始结论）：零 hard、零 warn；
    仅允许 unused_tags info（17 标签非全部被出厂树引用）。"""
    conn = db.init_db(str(tmp_path / "t.db"))
    nodes = rows_to_tree(conn.execute("SELECT * FROM tree ORDER BY idx").fetchall())
    findings = run_checkup(nodes, MODELS, TAGS)
    assert [f.to_json() for f in findings if f.level in ("hard", "warn")] == []
    assert all(f.level == "info" for f in findings)
