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
from sr.health import run_checkup

MODELS = {"qwen3-8b": {}, "qwen3-32b": {}, "claude-sonnet": {}}   # 测试替身：id 集合即可

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
