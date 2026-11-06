"""跨运行时图契约：同一组 v3 fixture 同时驱动 Python 服务端与 Node 控制台。

每项 fixture 是手写的 ``sr-routing-graph`` v3 文档，含：

* ``issue_codes``：硬校验码集合（排序、去重、不含 hint）；
* ``behavior_sha256``：有效文档的 ``tree_revision_of``（hash of
  ``canonical_json(tree_behavior_dict)``），非法文档为 ``null``（两端跳过 hash 断言）。

Python 侧的管线与生产一致：``parse_graph_document``（严格解析）→
``validate_graph_structure``（结构校验）→ ``tree_revision_of``（canonical hash）。
Node 侧由 ``scripts/graph_contract_test.mjs`` 走 ``RTGraph.parseImport`` /
``RTGraph.validate`` / ``RTGraph.behaviorCanonical``；两端对同一 fixture 必须得到
相同的 issue code 集合与相同的 64 位小写十六进制 hash。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from router.routing.document import parse_graph_document
from router.routing.graph.validate import validate_graph_structure
from router.routing.tree import tree_revision_of

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "graph_contracts.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte", "eq", "neq"})
REQUIRED_FIXTURES = {
    "valid-choice-noul-score",
    "valid-node-edge-shuffle",
    "valid-display-edits",
    "valid-integral-numbers",
    "invalid-empty-graph",
    "invalid-old-otherwise-target",
    "invalid-choice-single-option",
    "invalid-numeric-condition-missing",
}

FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
BY_NAME = {fixture["name"]: fixture for fixture in FIXTURES}


def python_contract(document: dict) -> tuple[list[str], str | None]:
    """按生产路径解析 → 校验 → 计算 behavior hash。

    严格解析失败（未知字段、错误 kind/version、类型非法）统一折叠为
    ``graph_parse_error``；解析成功但存在硬校验错误时 hash 为 ``None``。
    """
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    try:
        tree = parse_graph_document(
            raw,
            tree_id="contract",
            enabled=False,
            updated_at="",
            document_sha256="",
        )
    except ValueError:
        return ["graph_parse_error"], None
    issues = validate_graph_structure(tree)
    codes = sorted({issue.code for issue in issues if issue.level != "hint"})
    return codes, (None if codes else tree_revision_of(tree))


def test_fixture_inventory_is_well_formed() -> None:
    assert FIXTURES, "graph contract fixture set is empty"
    assert len({fixture["name"] for fixture in FIXTURES}) == len(FIXTURES), "duplicate fixture name"
    for fixture in FIXTURES:
        assert set(fixture) == {"name", "document", "issue_codes", "behavior_sha256"}
        assert isinstance(fixture["name"], str) and fixture["name"]
        assert isinstance(fixture["document"], dict)
        assert fixture["issue_codes"] == sorted(set(fixture["issue_codes"]))
        expected = fixture["behavior_sha256"]
        if expected is None:
            assert fixture["issue_codes"], "an invalid fixture must declare at least one hard issue code"
        else:
            assert isinstance(expected, str) and HEX64.fullmatch(expected), (
                "valid fixture behavior_sha256 must be 64 lowercase hex characters"
            )
            assert fixture["issue_codes"] == [], "a valid fixture must not declare issue codes"


def test_fixture_set_covers_the_contract_matrix() -> None:
    assert REQUIRED_FIXTURES <= set(BY_NAME)

    valid = [fixture for fixture in FIXTURES if fixture["behavior_sha256"] is not None]
    assert len(valid) >= 4
    roots = [fixture["document"]["root"] for fixture in valid]
    node_types = {node["type"] for root in roots for node in root["nodes"]}
    question_types = {
        node["question_type"] for root in roots for node in root["nodes"]
        if node["type"] == "question"
    }
    conditions = [edge["condition"] for root in roots for edge in root["edges"]]
    assert {"start", "condition", "logic", "question", "target"} <= node_types
    assert {"choice", "noul", "score"} <= question_types
    assert any(condition is None or condition["op"] == "is" for condition in conditions), (
        "no boolean edge in the valid fixture set"
    )
    assert any(
        condition is not None and condition["op"] in NUMERIC_OPS for condition in conditions
    ), "no numeric edge in the valid fixture set"

    # 主 fixture 必须自成一张覆盖全部节点类型与问题类型的图。
    main_root = BY_NAME["valid-choice-noul-score"]["document"]["root"]
    main_types = {node["type"] for node in main_root["nodes"]}
    main_questions = {
        node["question_type"] for node in main_root["nodes"] if node["type"] == "question"
    }
    assert {"start", "condition", "logic", "question", "target"} <= main_types
    assert {"choice", "noul", "score"} <= main_questions

    main_hash = BY_NAME["valid-choice-noul-score"]["behavior_sha256"]
    assert BY_NAME["valid-node-edge-shuffle"]["behavior_sha256"] == main_hash
    assert BY_NAME["valid-display-edits"]["behavior_sha256"] == main_hash
    assert BY_NAME["valid-integral-numbers"]["behavior_sha256"] != main_hash

    old_field_root = BY_NAME["invalid-old-otherwise-target"]["document"]["root"]
    assert old_field_root["otherwise_target"] == "target-a", (
        "the old-field fixture must exercise the rejected otherwise_target key"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=[fixture["name"] for fixture in FIXTURES])
def test_python_matches_the_shared_contract(fixture: dict) -> None:
    codes, digest = python_contract(fixture["document"])
    assert codes == fixture["issue_codes"], (
        f"Python issue codes for {fixture['name']} do not match the fixture"
    )
    if fixture["behavior_sha256"] is None:
        assert digest is None
    else:
        assert digest == fixture["behavior_sha256"], (
            f"Python behavior hash for {fixture['name']} does not match the fixture; "
            "the Node test asserts the same value"
        )
