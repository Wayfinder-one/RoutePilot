"""Graph v3 校验器测试：结构码表（spec §5.2）+ 启用码表（spec §5.3）+ Task-1 冻结修正。"""
import json
from pathlib import Path

import pytest

from router.routing.graph.model import graph_from_dict, graph_to_dict
from router.routing.graph.validate import (
    EnableContext,
    validate_graph_for_enable,
    validate_graph_structure,
)
from router.routing.tree import tree_from_dict
from tests.routing_fixtures import enable_context, graph_dict, huge_chain_graph, tree_dict

EDGE_ISSUE_CODES = {
    "edge_endpoint_missing",
    "edge_port_missing",
    "slot_conflict",
    "edge_cycle",
    "numeric_edge_condition_missing",
    "numeric_edge_condition_invalid",
    "boolean_edge_condition_invalid",
}


# ───────────────────────────── helpers ─────────────────────────────


def structure(raw):
    return validate_graph_structure(tree_from_dict(tree_dict(root=raw)))


def codes(raw):
    return {issue.code for issue in structure(raw)}


def edge_issue(raw, code):
    """断言 code 恰好出现，且每个该类 issue 的 node_id/edge_id/port 均为非空字符串。"""
    issues = [issue for issue in structure(raw) if issue.code == code]
    assert issues, f"expected issue {code}, got {[i.code for i in structure(raw)]}"
    for issue in issues:
        assert issue.node_id, (issue.code, issue.node_id)
        assert isinstance(issue.edge_id, str) and issue.edge_id, issue
        assert isinstance(issue.port, str) and issue.port, issue
        assert issue.level == "hard"
        assert issue.zh and issue.en
    return issues[0]


def with_targets(raw, *extra):
    """追加若干 target 节点（priority 依次递增）。"""
    base = len([node for node in raw["nodes"] if node["type"] == "target"])
    for index, node_id in enumerate(extra, start=1):
        raw["nodes"].append({
            "id": node_id, "type": "target", "priority": base + index,
            "position": None, "models": [{"model": "fast", "reasoning": None}],
        })
    return raw


def numeric_edge_raw(condition):
    """q 的数值端口 prob.easy → safe，用于数值条件非法性用例。"""
    raw = graph_dict()
    raw["edges"][1] = {
        "id": "e-q-safe", "source": "q", "source_port": "prob.easy",
        "target": "safe", "target_port": "in_1", "condition": condition,
    }
    return raw


def question_chain_raw(length):
    """start → q1 → … → qN 的 noul 问题链 + 兜底 target。"""
    raw = graph_dict()
    raw["nodes"] = [raw["nodes"][0]]  # 只留 start
    raw["edges"] = []
    for index in range(1, length + 1):
        raw["nodes"].append({
            "id": f"q{index}", "type": "question", "question_type": "noul",
            "instructions": "是否复杂", "position": None,
        })
    raw["nodes"].append({
        "id": "sink", "type": "target", "priority": 1, "position": None,
        "models": [{"model": "fast", "reasoning": None}],
    })
    previous = "start"
    for index in range(1, length + 1):
        raw["edges"].append({
            "id": f"e-q{index}", "source": previous,
            "source_port": "out" if previous == "start" else "yes",
            "target": f"q{index}", "target_port": "in_1",
            "condition": None if previous == "start" else {"op": "gt", "value": 0.5},
        })
        previous = f"q{index}"
    raw["error_fallback"] = "sink"
    return raw


def score_raw(criteria):
    raw = graph_dict()
    raw["nodes"].append({
        "id": "sc", "type": "question", "question_type": "score",
        "instructions": "评分", "position": None, "criteria": criteria,
    })
    raw["edges"].append({
        "id": "e-start-sc", "source": "start", "source_port": "out",
        "target": "sc", "target_port": "in_1", "condition": None,
    })
    return raw


# ─────────────────────── brief-verbatim tests ───────────────────────


def test_condition_can_activate_question_without_start_edge():
    raw = tree_dict()
    root = raw["root"]
    root["nodes"].insert(1, {"id": "c", "type": "condition", "source": "text", "contains_any": ["抢修"], "position": None})
    root["edges"][0].update({"source": "c", "source_port": "out", "id": "e-c-q"})
    assert validate_graph_structure(tree_from_dict(raw)) == []


def test_invalid_timezone_is_a_structure_error():
    raw = tree_dict()
    raw["timezone"] = "Mars/Olympus"
    assert {i.code for i in validate_graph_structure(tree_from_dict(raw))} == {"invalid_timezone"}


def test_empty_target_models_saved_but_not_enabled():
    raw = tree_dict()
    raw["root"]["nodes"][-1]["models"] = []
    tree = tree_from_dict(raw)
    assert validate_graph_structure(tree) == []
    assert "target_models_required" in {i.code for i in validate_graph_for_enable(tree, enable_context())}


# ───────────────────────── id / endpoint ────────────────────────────


def test_duplicate_node_id():
    raw = graph_dict()
    raw["nodes"].append({
        "id": "q", "type": "question", "question_type": "choice", "instructions": "选择",
        "position": None, "options": [
            {"key": "easy", "label": "轻量", "capability": "简单请求"},
            {"key": "strong", "label": "增强", "capability": "复杂请求"},
        ],
    })
    assert codes(raw) == {"duplicate_node_id"}
    issue = next(issue for issue in structure(raw) if issue.code == "duplicate_node_id")
    assert issue.node_id == "q"


def test_duplicate_edge_id():
    raw = graph_dict()
    raw["edges"][1]["id"] = "e-start-q"
    assert codes(raw) == {"duplicate_edge_id"}
    issue = next(issue for issue in structure(raw) if issue.code == "duplicate_edge_id")
    assert issue.edge_id == "e-start-q"


def test_edge_endpoint_missing():
    raw = graph_dict()
    raw["edges"][1]["target"] = "ghost"
    assert codes(raw) == {"edge_endpoint_missing"}
    assert edge_issue(raw, "edge_endpoint_missing").node_id == "ghost"
    raw = graph_dict()
    raw["edges"].append({
        "id": "e-ghost-q", "source": "ghost", "source_port": "out",
        "target": "q", "target_port": "in_2", "condition": None,
    })
    assert codes(raw) == {"edge_endpoint_missing"}
    assert edge_issue(raw, "edge_endpoint_missing").node_id == "ghost"


def test_unknown_output_port_and_dangling_option_edge():
    raw = graph_dict()
    raw["edges"][1]["source_port"] = "selected.ghost"  # 选项删除后的悬空边
    assert codes(raw) == {"edge_port_missing"}
    issue = edge_issue(raw, "edge_port_missing")
    assert issue.node_id == "q" and issue.port == "selected.ghost"


def test_unknown_input_slot():
    raw = graph_dict()
    raw["edges"][0]["target_port"] = "in_0"  # 槽位编号从 in_1 起
    assert codes(raw) == {"edge_port_missing"}
    issue = edge_issue(raw, "edge_port_missing")
    assert issue.node_id == "q" and issue.port == "in_0"
    raw = graph_dict()
    raw["edges"][0]["target_port"] = "slot1"  # 不符合 in_N 格式
    assert codes(raw) == {"edge_port_missing"}
    raw = graph_dict()
    raw["edges"][0]["target_port"] = "in_9"
    assert codes(raw) == set()  # 槽位允许稀疏编号（删除不重排，空槽复用）
    raw = graph_dict()
    raw["edges"][0]["target_port"] = "in_400"
    assert codes(raw) == set()  # 槽位编号上限 = 边限额
    raw = graph_dict()
    raw["edges"][0]["target_port"] = "in_401"  # 超上限编号不得让控制台物化海量槽位行
    assert codes(raw) == {"edge_port_missing"}
    issue = edge_issue(raw, "edge_port_missing")
    assert issue.node_id == "q" and issue.port == "in_401"
    raw = graph_dict()
    raw["edges"][0]["target_port"] = "in_9999"
    assert codes(raw) == {"edge_port_missing"}


def test_edge_into_start_or_condition_has_no_input_slot():
    raw = graph_dict()
    raw["nodes"].append({"id": "c", "type": "condition", "source": "text", "contains_any": ["抢修"], "position": None})
    raw["edges"].append({
        "id": "e-c-start", "source": "c", "source_port": "out",
        "target": "start", "target_port": "in_1", "condition": None,
    })
    assert codes(raw) == {"edge_port_missing"}  # start 没有输入槽位（无边成环）


def test_slot_conflict():
    raw = graph_dict()
    raw["edges"].append({
        "id": "e-start-q-2", "source": "start", "source_port": "out",
        "target": "q", "target_port": "in_1", "condition": None,
    })
    assert codes(raw) == {"slot_conflict"}
    issue = edge_issue(raw, "slot_conflict")
    assert issue.node_id == "q" and issue.port == "in_1"


# ─────────────────────────── cycles ─────────────────────────────────


def test_cycle_between_question_and_gate():
    raw = with_targets(graph_dict(), "t2")
    raw["nodes"].append({"id": "gate", "type": "logic", "operator": "and", "position": None})
    raw["edges"] = [
        {"id": "e-start-q", "source": "start", "source_port": "out", "target": "q", "target_port": "in_1", "condition": None},
        {"id": "e-q-safe", "source": "q", "source_port": "selected.easy", "target": "safe", "target_port": "in_1", "condition": None},
        {"id": "e-q-gate", "source": "q", "source_port": "prob.strong", "target": "gate", "target_port": "in_1", "condition": {"op": "gt", "value": 0.5}},
        {"id": "e-start-gate", "source": "start", "source_port": "out", "target": "gate", "target_port": "in_2", "condition": None},
        {"id": "e-gate-q", "source": "gate", "source_port": "out", "target": "q", "target_port": "in_2", "condition": None},
    ]
    assert codes(raw) == {"edge_cycle"}
    issue = edge_issue(raw, "edge_cycle")
    assert issue.node_id in {"q", "gate"}  # 回边成环处


def test_self_loop_is_a_cycle():
    raw = graph_dict()
    raw["edges"].append({
        "id": "e-q-q", "source": "q", "source_port": "selected.easy",
        "target": "q", "target_port": "in_2", "condition": None,
    })
    assert codes(raw) == {"edge_cycle"}
    issue = edge_issue(raw, "edge_cycle")
    assert issue.node_id == "q" and issue.edge_id == "e-q-q"


# ─────────────────────── edge conditions ────────────────────────────


def test_numeric_condition_missing():
    raw = numeric_edge_raw(None)
    assert codes(raw) == {"numeric_edge_condition_missing"}
    issue = edge_issue(raw, "numeric_edge_condition_missing")
    assert issue.edge_id == "e-q-safe" and issue.port == "in_1" and issue.node_id == "safe"


def test_numeric_condition_invalid_operator():
    raw = numeric_edge_raw({"op": "is", "value": True})  # 数值端口不能用布尔算子
    assert codes(raw) == {"numeric_edge_condition_invalid"}
    issue = edge_issue(raw, "numeric_edge_condition_invalid")
    assert issue.edge_id == "e-q-safe" and issue.port == "in_1"


def test_numeric_condition_threshold_magnitude():
    raw = numeric_edge_raw({"op": "gt", "value": 10 ** 10})  # |threshold| > 1e9
    assert codes(raw) == {"numeric_edge_condition_invalid"}
    raw = numeric_edge_raw({"op": "gt", "value": 10 ** 9})
    assert codes(raw) == set()  # 恰在 1e9 边界内合法


def test_numeric_condition_non_finite_threshold_defensive():
    raw = numeric_edge_raw({"op": "gt", "value": 0.5})
    tree = tree_from_dict(tree_dict(root=raw))
    condition = tree.root.edges[1].condition
    object.__setattr__(condition, "value", float("nan"))  # 绕过冻结：解析层已拦截，这里只测校验器防御分支
    assert {issue.code for issue in validate_graph_structure(tree)} == {"numeric_edge_condition_invalid"}


def test_boolean_condition_must_be_null_or_is():
    raw = graph_dict()
    raw["edges"][0]["condition"] = {"op": "gt", "value": 0.5}  # 布尔端口 start.out 配数值算子
    assert codes(raw) == {"boolean_edge_condition_invalid"}
    issue = edge_issue(raw, "boolean_edge_condition_invalid")
    assert issue.edge_id == "e-start-q" and issue.port == "in_1"
    raw = graph_dict()
    raw["edges"][0]["condition"] = {"op": "is", "value": False}
    assert codes(raw) == set()  # 合法 is 条件
    assert codes(graph_dict()) == set()  # null 条件（= true）合法


# ─────────────────────────── logic gates ────────────────────────────


def test_gate_slot_counts():
    for operator, edges in (
        ("and", 0), ("and", 1), ("or", 1),
        ("not", 0), ("not", 2),
    ):
        raw = graph_dict()
        raw["nodes"].append({"id": "gate", "type": "logic", "operator": operator, "position": None})
        for index in range(edges):
            raw["edges"].append({
                "id": f"e-start-gate-{index}", "source": "start", "source_port": "out",
                "target": "gate", "target_port": f"in_{index + 1}", "condition": None,
            })
        assert codes(raw) == {"logic_slot_count"}, (operator, edges)
        issue = next(issue for issue in structure(raw) if issue.code == "logic_slot_count")
        assert issue.node_id == "gate"
    raw = graph_dict()
    raw["nodes"].append({"id": "gate", "type": "logic", "operator": "and", "position": None})
    for index in range(2):  # AND 两个入边 → 合法
        raw["edges"].append({
            "id": f"e-start-gate-{index}", "source": "start", "source_port": "out",
            "target": "gate", "target_port": f"in_{index + 1}", "condition": None,
        })
    assert codes(raw) == set()


def test_logic_operator_invalid_defensive():
    from router.routing.graph.model import LogicNode

    root = graph_from_dict(graph_dict())
    gate = LogicNode("gate", "xor")  # 绕过解析层直接构造：校验器需兜住非法算子
    object.__setattr__(root, "nodes", root.nodes + (gate,))
    issues = validate_graph_structure(root)
    assert {issue.code for issue in issues} == {"logic_operator_invalid"}
    assert issues[0].node_id == "gate"


# ───────────────────────── question nodes ───────────────────────────


def test_question_instructions_required():
    raw = graph_dict()
    raw["nodes"][1]["instructions"] = "  "
    assert codes(raw) == {"question_incomplete"}
    assert next(issue for issue in structure(raw) if issue.code == "question_incomplete").node_id == "q"


def test_choice_option_rules():
    raw = graph_dict()
    raw["nodes"][1]["options"] = raw["nodes"][1]["options"][:1]  # < 2 个选项
    assert codes(raw) == {"question_incomplete"}
    raw = graph_dict()
    raw["nodes"][1]["options"][0]["key"] = ""
    raw["edges"][1]["source_port"] = "selected.strong"  # 保持连线不悬空，聚焦选项规则
    assert codes(raw) == {"question_incomplete"}
    raw = graph_dict()
    raw["nodes"][1]["options"][1]["key"] = "easy"
    assert codes(raw) == {"question_incomplete"}  # key 重复
    raw = graph_dict()
    raw["nodes"][1]["options"][0]["label"] = " "
    assert codes(raw) == {"question_incomplete"}
    raw = graph_dict()
    raw["nodes"][1]["options"][1]["label"] = "轻量"
    assert codes(raw) == {"question_incomplete"}  # label 重复
    raw = graph_dict()
    raw["nodes"][1]["options"][0]["capability"] = ""
    assert codes(raw) == {"question_incomplete"}
    for issue in structure(raw):
        assert issue.zh and issue.en


def test_choice_options_upper_bound_255():
    raw = graph_dict()
    raw["nodes"][1]["options"] = [
        {"key": f"opt{index}", "label": f"选项{index}", "capability": "能力"}
        for index in range(255)
    ]
    raw["edges"][1]["source_port"] = "selected.opt0"  # 保持连线不悬空
    assert codes(raw) == set()  # 255 恰在上限内
    raw["nodes"][1]["options"].append({"key": "extra", "label": "超出", "capability": "能力"})
    raw["nodes"][1]["options"].append({"key": "extra2", "label": "超出2", "capability": "能力"})
    assert codes(raw) == {"limit_exceeded"}
    issue = next(issue for issue in structure(raw) if issue.code == "limit_exceeded")
    assert issue.node_id == "q"


def test_score_criteria_required_and_invalid():
    assert codes(score_raw(None)) == {"score_criteria_required"}
    issue = next(issue for issue in structure(score_raw(None)) if issue.code == "score_criteria_required")
    assert issue.node_id == "sc"
    assert codes(score_raw([1, 10])) == set()
    assert codes(score_raw([1, 5, 10])) == set()
    assert codes(score_raw([1])) == {"score_criteria_invalid"}  # 少于 2 档
    assert codes(score_raw([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])) == {"score_criteria_invalid"}  # 多于 10 档
    issue = next(issue for issue in structure(score_raw([1])) if issue.code == "score_criteria_invalid")
    assert issue.node_id == "sc"


# ─────────────────────── reachability / chain ────────────────────────


def test_question_unreachable_from_start_and_conditions():
    raw = graph_dict()
    raw["edges"] = [raw["edges"][1]]  # 摘掉 start→q，也没有任何 condition 指向 q
    assert codes(raw) == {"question_unreachable"}
    issue = next(issue for issue in structure(raw) if issue.code == "question_unreachable")
    assert issue.node_id == "q"


def test_question_chain_depth_limit():
    assert codes(question_chain_raw(8)) == set()  # 恰 8 波合法
    assert codes(question_chain_raw(9)) == {"limit_exceeded"}  # 9 波超限
    issue = next(issue for issue in structure(question_chain_raw(9)) if issue.code == "limit_exceeded")
    assert issue.zh and issue.en


# ─────────────────────────── targets ────────────────────────────────


def test_duplicate_and_nonpositive_priority():
    raw = with_targets(graph_dict(), "t2")
    raw["nodes"][-1]["priority"] = 1
    assert codes(raw) == {"target_priority_invalid"}
    issue = next(issue for issue in structure(raw) if issue.code == "target_priority_invalid")
    assert issue.node_id == "t2"
    raw = with_targets(graph_dict(), "t0")
    raw["nodes"][-1]["priority"] = 0  # 必须是正整数
    assert codes(raw) == {"target_priority_invalid"}
    assert next(issue for issue in structure(raw) if issue.code == "target_priority_invalid").node_id == "t0"


def test_non_integer_priority_uses_specific_issue_code():
    """§5.2：非整数 priority（如 1.5）应报 target_priority_invalid，而不是笼统的 graph_parse_error。"""
    raw = with_targets(graph_dict(), "t2")
    raw["nodes"][-1]["priority"] = 1.5
    assert codes(raw) == {"target_priority_invalid"}
    issue = next(issue for issue in structure(raw) if issue.code == "target_priority_invalid")
    assert issue.node_id == "t2"
    raw = with_targets(graph_dict(), "t2")
    raw["nodes"][-1]["priority"] = "2"  # 非数字仍属解析错误（解析层保持严格类型）
    issues = validate_graph_structure(tree_dict(root=raw))
    assert {issue.code for issue in issues} == {"graph_parse_error"}


def test_fallback_missing_or_not_target():
    raw = graph_dict()
    raw["error_fallback"] = None
    assert codes(raw) == {"fallback_missing"}
    issue = next(issue for issue in structure(raw) if issue.code == "fallback_missing")
    assert issue.port == "error_fallback"
    raw = graph_dict()
    raw["error_fallback"] = "q"  # 指向 question，不是 target
    assert codes(raw) == {"fallback_missing"}
    assert next(issue for issue in structure(raw) if issue.code == "fallback_missing").node_id == "q"
    raw = graph_dict()
    raw["error_fallback"] = "ghost"  # 指向不存在的节点
    assert codes(raw) == {"fallback_missing"}
    assert next(issue for issue in structure(raw) if issue.code == "fallback_missing").node_id == "ghost"


def test_condition_node_config():
    raw = graph_dict()
    raw["nodes"].append({"id": "c", "type": "condition", "source": "text", "position": None})  # contains_any 空
    assert codes(raw) == {"condition_config_invalid"}
    assert next(issue for issue in structure(raw) if issue.code == "condition_config_invalid").node_id == "c"
    raw = graph_dict()
    raw["nodes"].append({"id": "c", "type": "condition", "source": "periods", "periods": [], "position": None})
    assert codes(raw) == {"condition_config_invalid"}
    raw = graph_dict()
    raw["nodes"].append({"id": "c", "type": "condition", "source": "text", "contains_any": ["抢修"], "position": None})
    assert codes(raw) == set()  # 非空 contains_any 合法


def test_blank_condition_keywords_and_periods_are_rejected():
    """§2.3：contains_any 必须是非空字符串数组；空白项是永不匹配的死条件（终审 Important）。"""
    for contains_any in ([""], [" "], ["抢修", ""], ["\t"]):
        raw = graph_dict()
        raw["nodes"].append({
            "id": "c", "type": "condition", "source": "text",
            "contains_any": contains_any, "position": None,
        })
        assert codes(raw) == {"condition_config_invalid"}, contains_any
        issue = next(issue for issue in structure(raw) if issue.code == "condition_config_invalid")
        assert issue.node_id == "c"
        assert issue.zh and issue.en
    for periods in ([""], [" "], ["peak", " "]):
        raw = graph_dict()
        raw["nodes"].append({
            "id": "c", "type": "condition", "source": "periods",
            "periods": periods, "position": None,
        })
        assert codes(raw) == {"condition_config_invalid"}, periods
        assert next(issue for issue in structure(raw) if issue.code == "condition_config_invalid").node_id == "c"


# ─────────────────────────── limits ─────────────────────────────────


def test_node_limit_exceeded():
    raw = {"id": "root", "type": "graph", "nodes": [
        {"id": "start", "type": "start", "position": None},
    ], "edges": [], "error_fallback": "t0"}
    for index in range(200):  # 共 201 个节点 > 200
        raw["nodes"].append({
            "id": f"t{index}", "type": "target", "priority": index + 1,
            "position": None, "models": [{"model": "fast", "reasoning": None}],
        })
    assert codes(raw) == {"limit_exceeded"}
    raw["nodes"].pop()  # 恰 200 个节点合法
    assert codes(raw) == set()


def test_question_limit_exceeded():
    raw = {"id": "root", "type": "graph", "nodes": [
        {"id": "start", "type": "start", "position": None},
    ], "edges": [], "error_fallback": "sink"}
    raw["nodes"].append({
        "id": "sink", "type": "target", "priority": 1, "position": None,
        "models": [{"model": "fast", "reasoning": None}],
    })
    for index in range(33):  # 33 个问题 > 32
        raw["nodes"].append({
            "id": f"q{index}", "type": "question", "question_type": "noul",
            "instructions": "是否复杂", "position": None,
        })
        raw["edges"].append({
            "id": f"e-start-q{index}", "source": "start", "source_port": "out",
            "target": f"q{index}", "target_port": "in_1", "condition": None,
        })
    assert codes(raw) == {"limit_exceeded"}
    raw["nodes"].pop()
    raw["edges"].pop()
    assert codes(raw) == set()  # 32 个问题合法


def test_huge_chain_returns_limit_exceeded_without_recursion():
    """终审 Critical 回归：1200 节点链必须返回 limit_exceeded，而不是 RecursionError → 500。"""
    issues = validate_graph_structure(huge_chain_graph())  # raw dict：走完整解析 + 校验入口
    assert {issue.code for issue in issues} == {"limit_exceeded"}
    assert all(issue.zh and issue.en and issue.level == "hard" for issue in issues)


def test_cycle_dfs_is_iterative_beyond_python_recursion_limit():
    """显式栈 DFS：深度远超 Python 递归上限的链也必须正常返回，不依赖调用方兜底。"""
    from router.routing.graph.validate import _cycle_back_edges

    count = 5000  # > sys.getrecursionlimit() (1000)
    adjacency = {
        f"n{index}": [(f"n{index + 1}", f"e{index}", "in_1")]
        for index in range(count - 1)
    }
    adjacency[f"n{count - 1}"] = []
    assert _cycle_back_edges(adjacency) == []

    adjacency[f"n{count - 1}"] = [("n0", "e-cycle", "in_1")]
    assert _cycle_back_edges(adjacency) == [("n0", "e-cycle", "in_1")]


# ─────────────────────── determinism / parse ────────────────────────


def test_issue_list_is_order_independent():
    def build(reverse):
        raw = graph_dict()
        raw["nodes"].append({
            "id": "q", "type": "question", "question_type": "choice", "instructions": "选择",
            "position": None, "options": [
                {"key": "easy", "label": "轻量", "capability": "简单请求"},
                {"key": "strong", "label": "增强", "capability": "复杂请求"},
            ],
        })
        raw["edges"].append({
            "id": "e-start-q-2", "source": "start", "source_port": "out",
            "target": "q", "target_port": "in_1", "condition": None,
        })
        raw["edges"][1] = {
            "id": "e-q-safe", "source": "q", "source_port": "prob.easy",
            "target": "safe", "target_port": "in_1", "condition": None,
        }
        if reverse:
            raw["nodes"].reverse()
            raw["edges"].reverse()
        return validate_graph_structure(tree_from_dict(tree_dict(root=raw)))

    forward = build(False)
    backward = build(True)
    assert forward == backward
    assert {issue.code for issue in forward} == {
        "duplicate_node_id", "slot_conflict", "numeric_edge_condition_missing",
    }


def test_graph_parse_error_for_raw_dict():
    issues = validate_graph_structure({"type": "tree"})
    assert {issue.code for issue in issues} == {"graph_parse_error"}
    assert issues[0].zh and issues[0].en
    issues = validate_graph_structure({"id": "root", "type": "graph", "nodes": [{"id": "s", "type": "start"}], "edges": []})
    assert {issue.code for issue in issues} == {"fallback_missing"}  # 两个兜底都未配置


def test_power_fixture_roundtrip_exact():
    """graph_power fixture 已按序列化默认省略规范化：parse→serialize 必须 exact roundtrip。"""
    raw = json.loads((Path(__file__).with_name("fixtures") / "graph_power.json").read_text(encoding="utf-8"))
    assert graph_to_dict(graph_from_dict(raw)) == raw


# ────────────────────────── enable codes ────────────────────────────


def test_enable_equals_structure_when_clean():
    tree = tree_from_dict(tree_dict())
    assert validate_graph_for_enable(tree, enable_context()) == validate_graph_structure(tree)


def test_unknown_model_and_reasoning_unsupported():
    raw = graph_dict()
    raw["nodes"][2]["models"] = [{"model": "slow", "reasoning": None}]
    assert codes(raw) == set()
    assert {i.code for i in validate_graph_for_enable(tree_from_dict(tree_dict(root=raw)), enable_context())} == {"unknown_model"}
    raw = graph_dict()
    raw["nodes"][2]["models"][0]["reasoning"] = "high"  # ctx 声明 fast 只支持 None
    assert codes(raw) == set()
    assert {i.code for i in validate_graph_for_enable(tree_from_dict(tree_dict(root=raw)), enable_context())} == {"reasoning_unsupported"}


def test_unknown_period_is_enable_level():
    raw = graph_dict()
    raw["nodes"].append({"id": "c", "type": "condition", "source": "periods", "periods": ["holiday"], "position": None})
    assert codes(raw) == set()  # 结构级只要求 periods 非空
    issues = validate_graph_for_enable(tree_from_dict(tree_dict(root=raw)), enable_context())
    assert {i.code for i in issues} == {"unknown_period"}
    assert next(i for i in issues if i.code == "unknown_period").node_id == "c"


def test_decision_service_unavailable_requires_questions():
    tree = tree_from_dict(tree_dict())
    context = EnableContext(
        known_models={"fast"},
        model_reasoning={"fast": None},
        known_periods={"peak", "normal", "offpeak"},
        jev_available=False,
    )
    assert "decision_service_unavailable" in {i.code for i in validate_graph_for_enable(tree, context)}
    raw = graph_dict()
    raw["nodes"] = [raw["nodes"][0], raw["nodes"][2]]  # start + target，无问题节点
    raw["edges"] = [{
        "id": "e-start-safe", "source": "start", "source_port": "out",
        "target": "safe", "target_port": "in_1", "condition": None,
    }]
    tree = tree_from_dict(tree_dict(root=raw))
    assert validate_graph_for_enable(tree, context) == []


def test_enable_includes_structure_issues():
    raw = graph_dict()
    raw["nodes"][1]["instructions"] = ""
    tree = tree_from_dict(tree_dict(root=raw))
    enable_codes = {i.code for i in validate_graph_for_enable(tree, enable_context())}
    assert "question_incomplete" in enable_codes
    assert {i.code for i in validate_graph_structure(tree)} <= enable_codes


def test_enable_rejects_wrong_context_type():
    with pytest.raises(TypeError):
        validate_graph_for_enable(tree_from_dict(tree_dict()), None)
