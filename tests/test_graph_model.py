import copy
import json
from pathlib import Path

import pytest

from router.routing.graph.model import (
    Condition,
    GraphRoot,
    Position,
    QuestionNode,
    canonical_json,
    graph_from_dict,
    graph_revision_of,
    graph_to_dict,
)
from router.routing.tree import (
    RoutingTree,
    Sticky,
    tree_behavior_dict,
    tree_from_dict,
    tree_storage_dict,
    tree_revision_of,
    tree_to_dict,
)
from tests.routing_fixtures import graph_dict, tree_dict

POWER_GRAPH = json.loads(
    (Path(__file__).with_name("fixtures") / "graph_power.json").read_text(encoding="utf-8")
)


def _node(tree, node_id):
    return next(node for node in tree.root.nodes if node.id == node_id)


def _with_condition(value):
    raw = graph_dict()
    raw["edges"][0]["condition"] = {"op": "gt", "value": value}
    return raw


def _with_score_criteria(levels):
    raw = graph_dict()
    raw["nodes"].append({
        "id": "sc", "type": "question", "question_type": "score",
        "instructions": "评分", "position": {"x": 300, "y": 100},
        "criteria": list(levels),
    })
    return raw


def test_full_storage_and_behavior_revision_are_independent():
    raw = tree_dict()
    tree = tree_from_dict(raw)
    moved = tree_dict()
    moved["root"]["nodes"][0]["position"] = {"x": 999, "y": 888}
    moved["root"]["nodes"][0]["title"] = "入口"
    moved["root"]["edges"].reverse()
    moved_tree = tree_from_dict(moved)
    assert tree_revision_of(tree) == tree_revision_of(moved_tree)
    assert tree_storage_dict(moved_tree)["root"]["nodes"][0]["position"] == {"x": 999, "y": 888}
    assert tree_storage_dict(moved_tree)["root"]["nodes"][0]["title"] == "入口"
    moved["root"]["edges"][0]["condition"] = {"op": "is", "value": False}
    assert tree_revision_of(tree) != tree_revision_of(tree_from_dict(moved))


def test_timezone_sticky_and_option_order_change_revision():
    tree = tree_from_dict(tree_dict())
    changed = tree_dict()
    changed["timezone"] = "UTC"
    assert tree_revision_of(tree) != tree_revision_of(tree_from_dict(changed))
    changed = tree_dict()
    changed["sticky"]["enabled"] = True
    assert tree_revision_of(tree) != tree_revision_of(tree_from_dict(changed))
    changed = tree_dict()
    changed["root"]["nodes"][1]["options"].reverse()
    assert tree_revision_of(tree) != tree_revision_of(tree_from_dict(changed))


def test_graph_roundtrip_preserves_config():
    raw = graph_dict()
    raw["nodes"] += [
        {"id": "gate", "type": "logic", "operator": "and", "title": "与门", "position": {"x": 600, "y": 0}},
        {"id": "cond", "type": "condition", "source": "text", "contains_any": ["抢修", "停电"], "position": {"x": 600, "y": 100}},
        {"id": "period", "type": "condition", "source": "periods", "periods": ["peak"], "position": {"x": 600, "y": 200}},
        {"id": "ask", "type": "question", "question_type": "noul", "instructions": "是否复杂", "position": {"x": 600, "y": 300}},
        {"id": "grade", "type": "question", "question_type": "score", "instructions": "评分", "criteria": [1, 5, 10], "position": {"x": 600, "y": 400}},
    ]
    raw["edges"].append({"id": "e-g-safe", "source": "gate", "source_port": "out", "target": "safe", "target_port": "in_2", "condition": None})
    root = graph_from_dict(raw)
    assert isinstance(root, GraphRoot)
    assert graph_from_dict(graph_to_dict(root)) == root
    tree = tree_from_dict(tree_dict(root=raw))
    assert isinstance(tree, RoutingTree)
    assert isinstance(tree.root, GraphRoot)
    assert isinstance(tree.sticky, Sticky)
    assert tree_from_dict(tree_to_dict(tree)) == tree
    assert tree_storage_dict(tree) == tree_dict(root=raw)
    behavior = tree_behavior_dict(tree)
    assert behavior["root"]["type"] == "graph"
    assert [node["id"] for node in behavior["root"]["nodes"]] == sorted(node["id"] for node in raw["nodes"])
    assert [(edge["target"], edge["target_port"]) for edge in behavior["root"]["edges"]] == [
        ("q", "in_1"), ("safe", "in_1"), ("safe", "in_2"),
    ]


def test_unknown_field_rejected():
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][1]["icon"] = "question"
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["edges"][0]["label"] = "主线"
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][1]["options"][0]["hint"] = "提示"
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][2]["models"][0]["weight"] = 1
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["edges"][0]["condition"] = {"op": "gt", "value": 0.5, "strict": True}
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["legend"] = []
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict({**tree_dict(), "scale": 1})
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"].append({"id": "llm", "type": "llm"})
    with pytest.raises(ValueError, match="unknown node type"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][1]["question_type"] = "rating"
    with pytest.raises(ValueError, match="question_type"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"].append({"id": "gate", "type": "logic", "operator": "xor"})
    with pytest.raises(ValueError, match="operator"):
        tree_from_dict(raw)
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"].append({"id": "cond", "type": "condition", "source": "regex"})
    with pytest.raises(ValueError, match="source"):
        tree_from_dict(raw)


def test_position_may_be_absent():
    raw = graph_dict()
    for node in raw["nodes"]:
        node.pop("position")
    tree = tree_from_dict(tree_dict(root=raw))
    assert all(node.position is None for node in tree.root.nodes)
    assert tree_revision_of(tree) == tree_revision_of(tree_from_dict(tree_dict()))
    assert all("position" not in node for node in tree_storage_dict(tree)["root"]["nodes"])
    raw = graph_dict()
    raw["nodes"][0]["position"] = None
    assert tree_from_dict(tree_dict(root=raw)).root.nodes[0].position is None
    node = tree_from_dict(tree_dict()).root.nodes[0]
    assert isinstance(node.position, Position)
    assert node.position == Position(0.0, 0.0)
    assert node.position.x == 0.0 and isinstance(node.position.x, float)
    bad = tree_dict(root=graph_dict())
    bad["root"]["nodes"][0]["position"] = "left"
    with pytest.raises(ValueError, match="position"):
        tree_from_dict(bad)
    bad["root"]["nodes"][0]["position"] = {"x": 1}
    with pytest.raises(ValueError, match="position"):
        tree_from_dict(bad)
    bad["root"]["nodes"][0]["position"] = {"x": float("nan"), "y": 0}
    with pytest.raises(ValueError, match="finite"):
        tree_from_dict(bad)
    bad["root"]["nodes"][0]["position"] = {"x": float("inf"), "y": 0}
    with pytest.raises(ValueError, match="finite"):
        tree_from_dict(bad)
    bad["root"]["nodes"][0]["position"] = {"x": 0, "y": 0, "z": 9}
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(bad)


def test_numeric_threshold_must_be_finite():
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            tree_from_dict(tree_dict(root=_with_condition(value)))
    bad = tree_dict(root=_with_condition(True))
    with pytest.raises(ValueError, match="condition.value"):
        tree_from_dict(bad)
    bad["root"]["edges"][0]["condition"] = {"op": "gt"}
    with pytest.raises(ValueError, match="condition.value"):
        tree_from_dict(bad)
    bad["root"]["edges"][0]["condition"] = {"op": "gt", "value": "0.5"}
    with pytest.raises(ValueError, match="condition.value"):
        tree_from_dict(bad)
    bad["root"]["edges"][0]["condition"] = {"op": "between", "value": 1}
    with pytest.raises(ValueError, match="condition.op"):
        tree_from_dict(bad)
    raw = _with_condition(1)
    tree = tree_from_dict(tree_dict(root=raw))
    condition = tree.root.edges[0].condition
    assert isinstance(condition, Condition)
    assert condition == Condition("gt", 1.0)
    assert isinstance(condition.value, float)
    raw = graph_dict()
    raw["edges"][0]["condition"] = {"op": "is", "value": False}
    assert tree_from_dict(tree_dict(root=raw)).root.edges[0].condition == Condition("is", False)
    raw["edges"][0]["condition"] = {"op": "is", "value": 1}
    with pytest.raises(ValueError, match="boolean"):
        tree_from_dict(tree_dict(root=raw))


def test_choice_options_preserve_config_order():
    tree = tree_from_dict(tree_dict())
    question = _node(tree, "q")
    assert isinstance(question, QuestionNode)
    assert [option.key for option in question.options] == ["easy", "strong"]
    assert [option.label for option in question.options] == ["轻量", "增强"]
    assert [option.capability for option in question.options] == ["简单请求", "复杂请求"]
    stored = tree_storage_dict(tree)["root"]["nodes"][1]["options"]
    assert stored == graph_dict()["nodes"][1]["options"]
    behavior_options = tree_behavior_dict(tree)["root"]["nodes"]
    question_behavior = next(node for node in behavior_options if node["id"] == "q")
    assert [option["key"] for option in question_behavior["options"]] == ["easy", "strong"]
    raw = graph_dict()
    raw["nodes"][1]["options"][0] = "easy"
    with pytest.raises(ValueError, match="option"):
        tree_from_dict(tree_dict(root=raw))
    raw = graph_dict()
    raw["nodes"][1]["options"] = "easy,strong"
    with pytest.raises(ValueError, match="options"):
        tree_from_dict(tree_dict(root=raw))


def test_score_criteria_must_be_finite_levels():
    tree = tree_from_dict(tree_dict(root=_with_score_criteria([1, 10])))
    score = _node(tree, "sc")
    assert score.criteria == (1.0, 10.0)
    assert all(isinstance(level, float) and level == level for level in score.criteria)
    behavior = tree_behavior_dict(tree)["root"]["nodes"]
    assert next(node for node in behavior if node["id"] == "sc")["criteria"] == [1.0, 10.0]
    base = tree_revision_of(tree)
    assert tree_revision_of(tree_from_dict(tree_dict(root=_with_score_criteria([1.0, 10.0])))) == base
    assert tree_revision_of(tree_from_dict(tree_dict(root=_with_score_criteria([2, 10])))) != base
    for bad in ([1, "x"], [1, None], [1, float("nan")], [1, float("inf")], [1, True], [1, {}], "1,10", [1, 10, None]):
        with pytest.raises(ValueError):
            tree_from_dict(tree_dict(root=_with_score_criteria(bad)))


def test_question_fields_are_type_specific():
    """§2.3/§2.8：只有 choice 接受 options、只有 score 接受 criteria；惰性字段在解析层拒绝，
    否则语义无变化的编辑会 bump behavior revision 并让粘性失效。"""
    raw = graph_dict()
    raw["nodes"][1]["criteria"] = [1, 10]  # choice + criteria
    with pytest.raises(ValueError, match="criteria"):
        tree_from_dict(tree_dict(root=raw))

    raw = graph_dict()
    raw["nodes"].append({
        "id": "ask", "type": "question", "question_type": "noul", "instructions": "是否复杂",
        "options": [{"key": "yes", "label": "是", "capability": "能力"}],
    })
    with pytest.raises(ValueError, match="options"):
        tree_from_dict(tree_dict(root=raw))

    raw = graph_dict()
    raw["nodes"].append({
        "id": "ask", "type": "question", "question_type": "noul", "instructions": "是否复杂",
        "criteria": [1, 10],
    })
    with pytest.raises(ValueError, match="criteria"):
        tree_from_dict(tree_dict(root=raw))

    raw = graph_dict()
    raw["nodes"].append({
        "id": "grade", "type": "question", "question_type": "score", "instructions": "评分",
        "criteria": [1, 10], "options": [{"key": "a", "label": "A", "capability": "能力"}],
    })
    with pytest.raises(ValueError, match="options"):
        tree_from_dict(tree_dict(root=raw))

    # 合法的 choice 仍保留 options；合法的 score 仍保留 criteria（revision 依赖它们）。
    tree = tree_from_dict(tree_dict(root=_with_score_criteria([1, 10])))
    behavior = {node["id"]: node for node in tree_behavior_dict(tree)["root"]["nodes"]}
    assert "criteria" not in behavior["q"]  # choice 不带 criteria
    assert "options" not in behavior["sc"]  # score 不带 options
    assert behavior["sc"]["criteria"] == [1.0, 10.0]
    assert [option["key"] for option in behavior["q"]["options"]] == ["easy", "strong"]


def test_display_fields_are_excluded_from_revision():
    base = tree_revision_of(tree_from_dict(tree_dict()))
    changed = tree_dict()
    changed["root"]["nodes"][2]["capability"] = "新的能力说明"
    assert tree_revision_of(tree_from_dict(changed)) == base
    changed = tree_dict()
    changed["root"]["nodes"][1]["title"] = "选择问题"
    changed["root"]["nodes"][2]["title"] = "兜底目标"
    assert tree_revision_of(tree_from_dict(changed)) == base
    changed = tree_dict()
    changed["root"]["edges"][0]["id"] = "e-renamed"
    assert tree_revision_of(tree_from_dict(changed)) == base
    changed = tree_dict()
    changed["root"]["nodes"].reverse()
    assert tree_revision_of(tree_from_dict(changed)) == base


def test_behavior_changes_change_revision():
    base = tree_revision_of(tree_from_dict(tree_dict()))
    changed = tree_dict()
    changed["root"]["nodes"][2]["priority"] = 2
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["nodes"][2]["models"][0]["model"] = "slow"
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["nodes"][2]["models"][0]["reasoning"] = "high"
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["nodes"][1]["instructions"] = "挑选"
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["nodes"][1]["options"][0]["capability"] = "通用请求"
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["nodes"][1]["options"][0]["label"] = "轻量档"
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["error_fallback"] = "other"
    assert tree_revision_of(tree_from_dict(changed)) != base
    changed = tree_dict()
    changed["root"]["edges"][1]["target_port"] = "in_2"
    assert tree_revision_of(tree_from_dict(changed)) != base


def test_numeric_normalization_is_revision_stable():
    first = tree_revision_of(tree_from_dict(tree_dict(root=_with_condition(1))))
    second = tree_revision_of(tree_from_dict(tree_dict(root=_with_condition(1.0))))
    assert first == second
    raw = tree_dict(root=graph_dict())
    raw["root"]["nodes"][0]["position"] = {"x": 1, "y": 2}
    moved = tree_dict(root=graph_dict())
    moved["root"]["nodes"][0]["position"] = {"x": 1.0, "y": 2.0}
    assert tree_revision_of(tree_from_dict(raw)) == tree_revision_of(tree_from_dict(moved))
    node = tree_from_dict(raw).root.nodes[0]
    assert node.position == Position(1.0, 2.0)


def test_tree_envelope_strict():
    raw = tree_dict()
    raw["enabled"] = "yes"
    with pytest.raises(ValueError, match="enabled"):
        tree_from_dict(raw)
    raw = tree_dict()
    raw["sticky"]["window"] = 60
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict(raw)
    for ttl in (0, -1, True, "600", 1.5, 31536001):
        raw = tree_dict(sticky={"enabled": True, "ttl_seconds": ttl})
        with pytest.raises(ValueError, match="ttl_seconds"):
            tree_from_dict(raw)
    raw = tree_dict()
    raw["root"]["type"] = "tree"
    with pytest.raises(ValueError, match="graph"):
        tree_from_dict(raw)
    with pytest.raises(ValueError, match="graph"):
        tree_from_dict({**tree_dict(), "root": None})
    raw = tree_dict()
    raw["root"]["error_fallback"] = "missing-target"
    tree = tree_from_dict(raw)
    assert tree.root.error_fallback == "missing-target"


def test_otherwise_target_is_rejected_as_an_unknown_field():
    raw = tree_dict()
    raw["root"]["otherwise_target"] = "safe"
    with pytest.raises(ValueError, match="unknown fields: otherwise_target"):
        tree_from_dict(raw)


def test_huge_int_raises_value_error():
    """溢出量级的整数必须报 ValueError（契约），而不是 OverflowError（Task 2 复审发现）。"""
    raw = graph_dict()
    raw["nodes"][0]["position"] = {"x": 10 ** 400, "y": 0}
    with pytest.raises(ValueError, match="finite"):
        tree_from_dict(tree_dict(root=raw))
    raw = graph_dict()
    raw["edges"][0]["condition"] = {"op": "gt", "value": 10 ** 400}
    with pytest.raises(ValueError, match="finite"):
        tree_from_dict(tree_dict(root=raw))
    raw = graph_dict()
    raw["nodes"].append({
        "id": "sc", "type": "question", "question_type": "score",
        "instructions": "评分", "position": None, "criteria": [1, 10 ** 400],
    })
    with pytest.raises(ValueError, match="finite"):
        tree_from_dict(tree_dict(root=raw))


def test_public_surface_lives_in_concrete_modules():
    """No re-export barrel: graph primitives come from their concrete modules."""
    import router.routing.graph as graph_package

    from router.routing.graph.conditions import (
        UNKNOWN,
        ConditionResult,
        evaluate_condition,
    )
    from router.routing.codec import slot_sort_key
    from router.routing.graph.ports import edge_type, next_slot, output_ports

    assert UNKNOWN == "unknown"
    assert not hasattr(graph_package, "ConditionNode")
    assert not hasattr(graph_package, "graph_revision_of")
    assert canonical_json({"b": 1, "a": [1.0, True, "电网"]}) == '{"a":[1.0,true,"电网"],"b":1}'
    with pytest.raises(ValueError):
        canonical_json(float("nan"))
    tree = tree_from_dict(tree_dict())
    root_revision = graph_revision_of(tree.root)
    assert root_revision == graph_revision_of(graph_from_dict(graph_to_dict(tree.root)))
    assert root_revision != tree_revision_of(tree)  # envelope adds timezone/sticky
    with pytest.raises(TypeError):
        graph_revision_of(tree)  # trees use tree_revision_of; no duck typing
    assert callable(evaluate_condition) and callable(output_ports)
    assert callable(next_slot) and callable(edge_type) and callable(slot_sort_key)
    assert graph_from_dict(graph_to_dict(tree.root)) == tree.root


def test_shared_sample_revision_is_order_and_display_invariant():
    """共享样例：节点/边输入顺序反转、title/position 编辑都不改变 revision。

    同时锁定重构前的 revision 值：行为不变时 revision 必须逐字节一致。
    """
    samples = {
        "fixture": (tree_dict(), "81a318d8a40117baa68247c89d835b8d65e3977853c608929f48332cef1ee1f8"),
        "power": (tree_dict(root=POWER_GRAPH), "2ce3fa7347f53b88465d9930f781a54a0368f9f228c354ed45c4072a3fb9d6b7"),
    }
    for name, (raw, expected) in samples.items():
        assert tree_revision_of(tree_from_dict(raw)) == expected, name

        permuted = copy.deepcopy(raw)
        permuted["root"]["nodes"].reverse()
        permuted["root"]["edges"].reverse()
        assert tree_revision_of(tree_from_dict(permuted)) == expected, name

        decorated = copy.deepcopy(raw)
        for node in decorated["root"]["nodes"]:
            node["title"] = "标题编辑"
            node["position"] = {"x": 123, "y": 456}
        for edge in decorated["root"]["edges"]:
            edge["id"] = "renamed-" + edge["id"]
        assert tree_revision_of(tree_from_dict(decorated)) == expected, name
