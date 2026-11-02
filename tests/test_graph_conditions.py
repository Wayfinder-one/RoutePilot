import pytest

from router.routing.graph.conditions import (
    UNKNOWN,
    ConditionResult,
    conditionInvalidReason,
    evaluate_condition,
)
from router.routing.graph.model import (
    Condition,
    Edge,
    ConditionNode,
    GraphRoot,
    LogicNode,
    Position,
    QuestionNode,
    QuestionOption,
    StartNode,
    TargetNode,
    graph_from_dict,
)
from router.routing.graph.ports import Port, edge_type, next_slot, output_ports, slot_sort_key
from tests.routing_fixtures import graph_dict


def _question():
    return QuestionNode(
        "q", "choice", "选择",
        options=(QuestionOption("easy", "轻量", "简单请求"), QuestionOption("strong", "增强", "复杂请求")),
        position=Position(200.0, 0.0),
    )


def _start():
    return StartNode("start", position=Position(0.0, 0.0))


def _numeric_edge(op="gt", threshold=0.5, source_port="prob.strong"):
    return Edge("e-num", "q", source_port, "safe", "in_2", Condition(op, threshold) if op else None)


def _boolean_edge(condition=None):
    return Edge("e-bool", "start", "out", "q", "in_1", condition)


def test_unknown_constant_and_result_shape():
    assert UNKNOWN == "unknown"
    assert [field.name for field in ConditionResult.__dataclass_fields__.values()] == ["raw", "status", "value", "reason"]
    result = ConditionResult(0.9, "true", True, None)
    assert (result.raw, result.status, result.value, result.reason) == (0.9, "true", True, None)


def test_numeric_operators_each_decide_independently():
    question = _question()
    cases = [
        ("gt", 0.6, 0.5, True), ("gt", 0.5, 0.5, False),
        ("gte", 0.5, 0.5, True), ("gte", 0.49, 0.5, False),
        ("lt", 0.4, 0.5, True), ("lt", 0.5, 0.5, False),
        ("lte", 0.5, 0.5, True), ("lte", 0.51, 0.5, False),
        ("eq", 0.5, 0.5, True), ("eq", 0.6, 0.5, False),
        ("neq", 0.4, 0.5, True), ("neq", 0.5, 0.5, False),
    ]
    for op, raw, threshold, expected in cases:
        result = evaluate_condition(_numeric_edge(op=op, threshold=threshold), question, raw)
        assert result.raw == raw, (op, raw, threshold)
        assert result.status == ("true" if expected else "false"), (op, raw, threshold)
        assert result.value is expected, (op, raw, threshold)
        assert result.reason is None, (op, raw, threshold)


def test_boolean_condition_is_true_false_and_default():
    start = _start()
    is_true = evaluate_condition(_boolean_edge(Condition("is", True)), start, True)
    assert (is_true.status, is_true.value) == ("true", True)
    is_true_against_false = evaluate_condition(_boolean_edge(Condition("is", True)), start, False)
    assert (is_true_against_false.status, is_true_against_false.value) == ("false", False)
    is_false = evaluate_condition(_boolean_edge(Condition("is", False)), start, False)
    assert (is_false.status, is_false.value) == ("true", True)
    is_false_against_true = evaluate_condition(_boolean_edge(Condition("is", False)), start, True)
    assert (is_false_against_true.status, is_false_against_true.value) == ("false", False)
    for raw, expected in ((True, "true"), (False, "false")):
        default = evaluate_condition(_boolean_edge(None), start, raw)
        assert default.status == expected
        assert default.value is (expected == "true")


def test_unknown_upstream_yields_unknown():
    question = _question()
    numeric = evaluate_condition(_numeric_edge(op="gt", threshold=0.5), question, None)
    assert (numeric.raw, numeric.status, numeric.value) == (None, "unknown", UNKNOWN)
    assert numeric.reason is None
    boolean = evaluate_condition(_boolean_edge(None), _start(), None)
    assert (boolean.status, boolean.value) == ("unknown", UNKNOWN)
    explicit = evaluate_condition(_boolean_edge(Condition("is", False)), _start(), None)
    assert (explicit.status, explicit.value) == ("unknown", UNKNOWN)
    prob = evaluate_condition(_numeric_edge(source_port="prob.easy"), question, None)
    assert prob.status == "unknown"
    p_top = evaluate_condition(_numeric_edge(source_port="p_top"), question, None)
    assert p_top.status == "unknown"


def test_condition_legality_precedes_unknown_raw():
    question = _question()
    missing = evaluate_condition(_numeric_edge(op=None), question, None)
    assert missing.status == "invalid"
    assert missing.value == UNKNOWN
    assert isinstance(missing.reason, str) and missing.reason
    oversized = evaluate_condition(Edge("e", "q", "prob.strong", "safe", "in_2", Condition("gt", 5e9)), question, None)
    assert oversized.status == "invalid"
    wrong_op = evaluate_condition(Edge("e", "start", "out", "q", "in_1", Condition("gt", 0.5)), _start(), None)
    assert wrong_op.status == "invalid"
    assert wrong_op.value == UNKNOWN


def test_invalid_conditions_report_reason():
    question = _question()
    no_condition = evaluate_condition(_numeric_edge(op=None), question, 0.9)
    assert no_condition.status == "invalid"
    assert no_condition.value == UNKNOWN
    assert isinstance(no_condition.reason, str) and no_condition.reason
    is_op = evaluate_condition(Edge("e", "q", "prob.strong", "safe", "in_2", Condition("is", True)), question, 0.9)
    assert is_op.status == "invalid" and is_op.value == UNKNOWN
    boolean_port = evaluate_condition(Edge("e", "start", "out", "q", "in_1", Condition("gt", 0.5)), _start(), True)
    assert boolean_port.status == "invalid"
    for threshold in (5e9, -5e9, 1e12):
        result = evaluate_condition(Edge("e", "q", "prob.strong", "safe", "in_2", Condition("gt", threshold)), question, 1e9)
        assert result.status == "invalid", threshold
        assert result.value == UNKNOWN
    boundary = evaluate_condition(Edge("e", "q", "prob.strong", "safe", "in_2", Condition("gt", 1e9)), question, 2e9)
    assert boundary.status == "true" and boundary.value is True
    ghost_port = evaluate_condition(Edge("e", "q", "prob.nope", "safe", "in_2", Condition("gt", 0.5)), question, 0.9)
    assert ghost_port.status == "invalid"
    assert "prob.nope" in ghost_port.reason


def test_numeric_raw_values_are_validated():
    question = _question()
    for raw in (True, "0.5", float("nan"), float("inf"), "x"):
        result = evaluate_condition(_numeric_edge(op="gt", threshold=0.5), question, raw)
        assert result.status == "invalid", raw
        assert result.value == UNKNOWN, raw
    result = evaluate_condition(_numeric_edge(op="gt", threshold=0.5), question, 1)
    assert result.status == "true" and result.value is True and result.raw == 1


def test_boolean_raw_values_are_validated():
    start = _start()
    for raw in (1, "yes", 0.5):
        result = evaluate_condition(_boolean_edge(None), start, raw)
        assert result.status == "invalid", raw
        assert result.value == UNKNOWN
    assert evaluate_condition(_boolean_edge(None), start, None).status == "unknown"


def test_condition_invalid_reason_helper():
    question = _question()
    assert conditionInvalidReason(_numeric_edge(op="gt", threshold=0.5), question) is None
    assert conditionInvalidReason(_boolean_edge(None), _start()) is None
    assert conditionInvalidReason(_boolean_edge(Condition("is", False)), _start()) is None
    illegal = conditionInvalidReason(_numeric_edge(op=None), question)
    assert isinstance(illegal, str) and illegal
    assert isinstance(conditionInvalidReason(Edge("e", "q", "prob.strong", "safe", "in_2", Condition("gt", 1e12)), question), str)


def test_output_ports_follow_node_types():
    assert [port.name for port in output_ports(_start())] == ["out"]
    choice_ports = [(port.name, port.kind) for port in output_ports(_question())]
    assert choice_ports == [
        ("selected.easy", "boolean"), ("selected.strong", "boolean"),
        ("prob.easy", "number"), ("prob.strong", "number"),
        ("p_top", "number"), ("margin", "number"),
    ]
    assert all(isinstance(port, Port) for port in output_ports(_question()))
    noul = QuestionNode("ask", "noul", "是否复杂")
    assert [(port.name, port.kind) for port in output_ports(noul)] == [("yes", "number"), ("no", "number")]
    score = QuestionNode("grade", "score", "评分")
    assert [(port.name, port.kind) for port in output_ports(score)] == [("value", "number")]
    assert [(port.name, port.kind) for port in output_ports(LogicNode("gate", "and"))] == [("out", "boolean")]
    assert [(port.name, port.kind) for port in output_ports(ConditionNode("cond", "text"))] == [("out", "boolean")]
    assert output_ports(TargetNode("safe", (), 1)) == ()
    with pytest.raises(ValueError, match="question_type"):
        output_ports(QuestionNode("bad", "rating", "非法"))


def test_next_slot_uses_smallest_unoccupied_positive_number():
    root = graph_from_dict(graph_dict())
    assert next_slot(root, "q") == "in_2"
    assert next_slot(root, "safe") == "in_2"
    raw = graph_dict()
    raw["nodes"].append({"id": "wide", "type": "target", "priority": 2, "position": {"x": 800, "y": 0}, "models": [{"model": "slow", "reasoning": None}]})
    raw["nodes"].append({"id": "fresh", "type": "target", "priority": 3, "position": {"x": 800, "y": 100}, "models": [{"model": "slow", "reasoning": None}]})
    raw["edges"] += [
        {"id": "e-1", "source": "start", "source_port": "out", "target": "wide", "target_port": "in_1", "condition": None},
        {"id": "e-3", "source": "q", "source_port": "selected.easy", "target": "wide", "target_port": "in_3", "condition": None},
    ]
    wide_root = graph_from_dict(raw)
    assert next_slot(wide_root, "wide") == "in_2"
    assert next_slot(wide_root, "fresh") == "in_1"
    with pytest.raises(ValueError, match="no input slots"):
        next_slot(root, "start")
    with pytest.raises(ValueError, match="unknown node"):
        next_slot(root, "ghost")
    condition_root_raw = graph_dict()
    condition_root_raw["nodes"].append({"id": "cond", "type": "condition", "source": "text", "contains_any": ["抢修"]})
    with pytest.raises(ValueError, match="no input slots"):
        next_slot(graph_from_dict(condition_root_raw), "cond")


def test_slot_keys_reject_invalid_forms():
    assert slot_sort_key("in_1") == 1
    assert slot_sort_key("in_12") == 12
    assert slot_sort_key("in_400") == 400  # 槽位编号上限 = 边限额
    assert sorted(["in_10", "in_2"], key=slot_sort_key) == ["in_2", "in_10"]
    for bad in ("in_0", "in_01", "in", "in_x", "in_1x", "in_-1", "in_+1", "out", "in_ 1", "IN_1", 3, None):
        with pytest.raises(ValueError, match="in_N"):
            slot_sort_key(bad)
    for huge in ("in_401", "in_9999"):
        with pytest.raises(ValueError, match="400"):
            slot_sort_key(huge)


def test_next_slot_refuses_beyond_slot_limit():
    """400 个槽位都被占用后 next_slot 不得生成 in_401（超出边限额的无效编号）。"""
    root = GraphRoot("root", (TargetNode("wide", (), 1),), tuple(
        Edge(f"e{index}", "start", "out", "wide", f"in_{index}")
        for index in range(1, 401)
    ))
    with pytest.raises(ValueError, match="no free input slot"):
        next_slot(root, "wide")


def test_edge_type_reflects_source_port_kind():
    raw = graph_dict()
    raw["edges"].append({"id": "e-q2-safe", "source": "q", "source_port": "prob.easy", "target": "safe", "target_port": "in_2", "condition": {"op": "gt", "value": 0.5}})
    raw["edges"].append({"id": "e-q3-safe", "source": "q", "source_port": "p_top", "target": "safe", "target_port": "in_3", "condition": {"op": "gte", "value": 0.5}})
    root = graph_from_dict(raw)
    kinds = {edge.id: edge_type(root, edge) for edge in root.edges}
    assert kinds["e-start-q"] == "boolean"
    assert kinds["e-q-safe"] == "boolean"
    assert kinds["e-q2-safe"] == "number"
    assert kinds["e-q3-safe"] == "number"
    bogus_raw = graph_dict()
    bogus_raw["edges"].append({"id": "e-bad", "source": "q", "source_port": "prob.nope", "target": "safe", "target_port": "in_2", "condition": None})
    bogus_root = graph_from_dict(bogus_raw)
    with pytest.raises(ValueError, match="prob.nope"):
        edge_type(bogus_root, bogus_root.edges[-1])
    ghost_raw = graph_dict()
    ghost_raw["edges"].append({"id": "e-ghost", "source": "ghost", "source_port": "out", "target": "safe", "target_port": "in_2", "condition": None})
    with pytest.raises(ValueError, match="unknown node"):
        edge_type(graph_from_dict(ghost_raw), graph_from_dict(ghost_raw).edges[-1])
