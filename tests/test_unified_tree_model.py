"""Unified routing tree model: four node types, recursive serde, walk and limits.

The trust boundary lives in ``router.pipeline.tree``: malformed JSON raises
``ValueError`` here so the API layer can turn it into a 422.  These tests pin
the wire format, the full out-edge walk (branch children, NL option targets and
both kinds of ``otherwise``) and the numeric limits of the frozen dataclasses.
"""

from __future__ import annotations

import pytest

from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Route,
    RoutingTree,
    Weight,
    When,
    node_from_dict,
    node_to_dict,
    tree_from_dict,
    tree_to_dict,
    walk_nodes,
)


def mixed_root():
    return {
        "id": "root", "type": "branch",
        "children": [{
            "id": "pii-nl", "type": "nl_choice",
            "when": {"signal": "pii", "op": "contains", "value": "email_address"},
            "instructions": "choose",
            "options": [
                {"key": "fast", "model": "qwen3-8b"},
                {"key": "deep", "description": "deep tasks",
                 "child": {"id": "deep-route", "type": "route",
                           "model": "deepseek-v3"}}
            ]
        }],
        "otherwise": {"id": "default", "type": "route", "model": "qwen3-8b"}
    }


def test_mixed_tree_round_trip_preserves_options_and_otherwise():
    root = node_from_dict(mixed_root())
    encoded = node_to_dict(root)
    assert [option["key"] for option in encoded["children"][0]["options"]] == ["fast", "deep"]
    assert encoded["otherwise"]["id"] == "default"
    assert node_to_dict(node_from_dict(encoded)) == encoded


def test_walk_nodes_covers_nl_child_model_target_and_otherwise():
    ids = {node.id for node in walk_nodes(node_from_dict(mixed_root()))}
    assert ids == {"root", "pii-nl", "pii-nl/options/fast", "deep-route", "default"}


def test_node_serde_is_stable_and_keeps_option_order():
    root = node_from_dict(mixed_root())
    encoded = node_to_dict(root)
    assert node_to_dict(node_from_dict(encoded)) == encoded
    options = encoded["children"][0]["options"]
    assert options[0] == {"key": "fast", "model": "qwen3-8b", "reasoning": None}
    assert options[1] == {
        "key": "deep", "description": "deep tasks",
        "child": {"id": "deep-route", "type": "route", "model": "deepseek-v3",
                  "reasoning": None, "weights": []},
    }


def test_direct_model_options_parse_into_auto_routes():
    root = node_from_dict(mixed_root())
    nl = root.children[0]
    fast, deep = nl.options
    assert (fast.key, fast.text, fast.capability_model) == ("fast", "", "qwen3-8b")
    assert fast.target == Route(id="pii-nl/options/fast", model="qwen3-8b")
    assert (deep.key, deep.text, deep.capability_model) == ("deep", "deep tasks", None)
    assert deep.target == Route(id="deep-route", model="deepseek-v3")


def test_route_and_block_serde_preserves_when_weights_and_limits():
    raw = {
        "id": "r", "type": "route", "model": "qwen3-32b", "reasoning": "high",
        "when": {"signal": "pii", "op": "contains", "value": "email_address",
                 "conf_min": 0.72},
        "weights": [{"signal": "complexity", "value": "hard", "weight": 0.8}],
        "min_score": 0.6,
    }
    route = node_from_dict(raw)
    assert route == Route(
        id="r", model="qwen3-32b", reasoning="high",
        when=When(signal="pii", value="email_address", op="contains", conf_min=0.72),
        weights=(Weight(signal="complexity", value="hard", weight=0.8),),
        min_score=0.6)
    encoded = node_to_dict(route)
    assert encoded == raw
    assert node_to_dict(node_from_dict(encoded)) == encoded

    block = node_from_dict({"id": "b", "type": "block", "weights": [
        {"signal": "jailbreak", "value": "jailbreak", "weight": 1}]})
    assert block == Block(id="b", weights=(
        Weight(signal="jailbreak", value="jailbreak", weight=1.0),))
    assert node_to_dict(block) == {"id": "b", "type": "block", "weights": [
        {"signal": "jailbreak", "value": "jailbreak", "weight": 1.0}]}


def test_walk_nodes_visits_every_out_edge_in_configuration_order():
    root = Branch(
        id="root",
        children=(
            NLChoice(
                id="nl", instructions="pick",
                options=(
                    NLOption(key="fast", text="",
                             target=Route(id="nl/options/fast", model="m1"),
                             capability_model="m1"),
                    NLOption(key="deep", text="deep",
                             target=Branch(
                                 id="deep",
                                 children=(Route(id="deep/one", model="m2"),),
                                 otherwise=Route(id="deep-fallback", model="m0"))),
                ),
                otherwise=Route(id="nl-fallback", model="m0")),
            Route(id="after", model="m3"),
        ),
        otherwise=Block(id="blocked"))
    assert [node.id for node in walk_nodes(root)] == [
        "root", "nl", "nl/options/fast", "deep", "deep/one", "deep-fallback",
        "nl-fallback", "after", "blocked"]


def test_tree_serde_round_trip_preserves_metadata():
    raw = {
        "id": "t1", "name": "Tree one", "enabled": True, "root": mixed_root(),
        "tree_revision": "abc123", "updated_at": "2026-10-26T10:00:00Z",
    }
    tree = tree_from_dict(raw)
    assert isinstance(tree, RoutingTree)
    assert (tree.id, tree.name, tree.enabled) == ("t1", "Tree one", True)
    assert tree.root.id == "root"
    encoded = tree_to_dict(tree)
    assert (encoded["id"], encoded["name"], encoded["enabled"]) == (
        "t1", "Tree one", True)
    assert encoded["tree_revision"] == "abc123"
    assert encoded["updated_at"] == "2026-10-26T10:00:00Z"
    assert tree_to_dict(tree_from_dict(encoded)) == encoded
    assert encoded["root"] == node_to_dict(tree.root)
    assert encoded["root"]["children"][0]["when"] == {
        "signal": "pii", "op": "contains", "value": "email_address"}


def test_tree_serde_fills_contract_defaults_for_absent_fields():
    tree = tree_from_dict({"id": "t2", "root": {
        "id": "r", "type": "route", "model": "qwen3-8b"}})
    assert (tree.name, tree.enabled, tree.tree_revision, tree.updated_at) == (
        "", False, "", "")
    assert tree_to_dict(tree) == {
        "id": "t2", "name": "", "enabled": False,
        "root": {"id": "r", "type": "route", "model": "qwen3-8b",
                 "reasoning": None, "weights": []},
        "tree_revision": "", "updated_at": "",
    }


def test_absent_optional_node_fields_fall_back_to_contract_defaults():
    nl = node_from_dict({"id": "n", "type": "nl_choice"})
    assert nl == NLChoice(id="n", instructions="", options=())
    assert (nl.min_prob, nl.min_margin, nl.otherwise, nl.when) == (
        0.45, 0.10, None, None)
    nulled = node_from_dict({
        "id": "n2", "type": "nl_choice", "instructions": None,
        "options": None, "min_prob": None, "min_margin": None, "when": None,
        "otherwise": None})
    assert nulled == NLChoice(id="n2", instructions="", options=())
    assert node_from_dict({"id": "r", "type": "route", "model": "m"}) == Route(
        id="r", model="m")
    assert node_from_dict({"id": "b", "type": "branch"}) == Branch(id="b")


@pytest.mark.parametrize("raw", [
    {},
    {"id": ""},
    {"id": "x"},
    {"id": "x", "type": "leaf"},
    {"id": "x", "type": "route"},
    {"id": "x", "type": "route", "model": ""},
    {"id": "x", "type": "route", "model": 5},
    {"id": "x", "type": "route", "model": "m", "reasoning": 5},
    {"id": "x", "type": "route", "model": "m", "children": []},
    {"id": "x", "type": "route", "model": "m", "otherwise": {
        "id": "y", "type": "route", "model": "m"}},
    {"id": "x", "type": "route", "model": "m", "instructions": "nope"},
    {"id": "x", "type": "route", "model": "m", "min_score": "high"},
    {"id": "x", "type": "route", "model": "m", "min_score": 1.5},
    {"id": "x", "type": "route", "model": "m", "weights": {}},
    {"id": "x", "type": "route", "model": "m",
     "weights": [{"value": "hard"}]},
    {"id": "x", "type": "route", "model": "m",
     "weights": [{"signal": "complexity", "value": "hard", "weight": "heavy"}]},
    {"id": "x", "type": "block", "model": "m"},
    {"id": "b", "type": "branch", "children": {}},
    {"id": "b", "type": "branch", "model": "m"},
    {"id": "b", "type": "branch", "weights": []},
    {"id": "n", "type": "nl_choice", "instructions": 5},
    {"id": "n", "type": "nl_choice", "options": {}},
    {"id": "n", "type": "nl_choice", "children": []},
    {"id": "n", "type": "nl_choice", "min_prob": "0.5"},
    {"id": "n", "type": "nl_choice", "min_prob": 1.2},
    {"id": "n", "type": "nl_choice", "min_margin": "x"},
    {"id": "n", "type": "nl_choice", "options": [{"key": "", "model": "m"}]},
    {"id": "n", "type": "nl_choice", "options": [
        {"key": "k", "model": "m", "child": {"id": "c", "type": "route",
                                             "model": "m"}}]},
    {"id": "n", "type": "nl_choice", "options": [{"key": "k"}]},
    {"id": "n", "type": "nl_choice", "options": [{"key": "k", "model": ""}]},
    {"id": "n", "type": "nl_choice", "options": [
        {"key": "k", "child": {"id": "c", "type": "route", "model": "m"}}]},
    {"id": "n", "type": "nl_choice", "options": [
        {"key": "k", "description": "  ", "child": {
            "id": "c", "type": "route", "model": "m"}}]},
    {"id": "n", "type": "nl_choice", "options": [
        {"key": "k", "description": "d", "child": None}]},
    {"id": "r", "type": "route", "model": "m", "when": "pii"},
    {"id": "r", "type": "route", "model": "m", "when": {"value": "x"}},
    {"id": "r", "type": "route", "model": "m", "when": {"signal": 1}},
    {"id": "r", "type": "route", "model": "m", "when": {
        "signal": "pii", "op": "in", "value": "x"}},
    {"id": "r", "type": "route", "model": "m", "when": {
        "signal": "pii", "value": "x", "conf_min": "lo"}},
    {"id": "r", "type": "route", "model": "m", "when": {
        "signal": "pii", "value": "x", "conf_min": 1.2}},
    {"id": "r", "type": "route", "model": "m", "when": {
        "signal": "pii", "value": 5}},
])
def test_node_from_dict_rejects_malformed_nodes(raw):
    with pytest.raises(ValueError):
        node_from_dict(raw)


@pytest.mark.parametrize("raw", [
    {},
    {"id": "t"},
    {"id": "t", "root": None},
    {"id": "t", "enabled": "yes", "root": {"id": "r", "type": "route",
                                           "model": "m"}},
    {"id": "t", "name": 5, "root": {"id": "r", "type": "route", "model": "m"}},
    {"id": "t", "root": "nope"},
    {"id": "", "root": {"id": "r", "type": "route", "model": "m"}},
    "not an object",
])
def test_tree_from_dict_rejects_malformed_trees(raw):
    with pytest.raises(ValueError):
        tree_from_dict(raw)


def test_routing_tree_fixtures_expose_dataclass_builders():
    from tests.routing_tree_fixtures import mixed_tree, nl, route

    when = When(signal="pii", value="email_address", op="contains")
    leaf = route("leaf", "qwen3-8b", when=when)
    assert leaf == Route(id="leaf", model="qwen3-8b", when=when)

    tree = mixed_tree()
    assert isinstance(tree, RoutingTree)
    assert isinstance(tree.root, Branch)
    encoded = tree_to_dict(tree)
    assert tree_to_dict(tree_from_dict(encoded)) == encoded

    choice = nl("choice", (
        NLOption(key="fast", text="",
                 target=route("choice/options/fast", "m1"),
                 capability_model="m1"),
        NLOption(key="deep", text="deep", target=route("deep", "m2")),
    ), otherwise=route("fallback", "m0"))
    assert isinstance(choice, NLChoice)
    assert [option.key for option in choice.options] == ["fast", "deep"]
    assert choice.otherwise.id == "fallback"
    assert choice.instructions


def test_mixed_tree_fixture_has_signal_conditions_for_scope_tests():
    from tests.routing_tree_fixtures import mixed_tree

    nodes = list(walk_nodes(mixed_tree().root))
    when_keys = {node.when.signal for node in nodes if node.when is not None}
    weight_keys = {weight.signal for node in nodes
                   if isinstance(node, (Route, Block))
                   for weight in node.weights}
    assert {"pii", "time_window"} <= when_keys
    assert "complexity" in weight_keys
    assert any(isinstance(node, NLChoice) and len(node.options) >= 2
               for node in nodes)


def test_tree_from_dict_accepts_sqlite_integer_enabled():
    root = {"id": "r", "type": "route", "model": "m"}
    assert tree_from_dict({"id": "t", "enabled": 1, "root": root}).enabled is True
    assert tree_from_dict({"id": "t", "enabled": 0, "root": root}).enabled is False
