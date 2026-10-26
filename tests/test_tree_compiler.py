"""Pure capability expansion for the unified routing tree (compile step).

Compilation is what turns the stored tree (direct ``model`` candidates carry no
text) into the **effective** tree a request actually runs: direct candidates
become ``"<model>: <capability>"`` strings, child candidates keep their stored
description.  The step is pure — the stored tree is never mutated and candidate
order is never sorted.
"""

from __future__ import annotations

import pytest

from router.pipeline.compiler import (
    compile_tree,
    effective_node_from_dict,
    effective_node_to_dict,
    tree_signal_requirements,
)
from router.pipeline.scope import SignalScope
from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Route,
    RoutingTree,
    When,
    node_to_dict,
)

CAPABILITIES = {"m1": "fast tasks", "m2": "deep tasks"}


def compiler_tree():
    return RoutingTree(
        id="t", name="T", enabled=True,
        root=NLChoice(
            id="root", instructions="choose",
            options=(
                NLOption("m1", "", Route("root/options/m1", "m1"), "m1"),
                NLOption("m2", "", Route("root/options/m2", "m2"), "m2"),
                NLOption("child", "child branch", Route("child-route", "m3")),
            ),
            otherwise=Route("fallback", "m1")))


def test_compile_expands_only_model_options_without_mutating_tree():
    tree = compiler_tree()
    compiled = compile_tree(tree, {"m1": "fast tasks", "m2": "deep tasks"})
    nl = compiled.root
    assert [option.text for option in nl.options[:2]] == ["m1: fast tasks", "m2: deep tasks"]
    assert nl.options[2].text == "child branch"
    assert [option.text for option in tree.root.options] == ["", "", "child branch"]


def test_compile_copies_containers_deeply_and_reuses_terminals():
    tree = RoutingTree(
        id="t", name="T", enabled=True, tree_revision="rev-1",
        root=Branch(id="root", children=(
            NLChoice(id="nl", instructions="choose", options=(
                NLOption("m1", "", Route("nl/options/m1", "m1"), "m1"),
                NLOption("child", "child branch", NLChoice(
                    id="inner", instructions="inner choose",
                    options=(NLOption("m2", "", Route("inner/options/m2",
                                                     "m2"), "m2"),),
                    otherwise=Route("inner-fallback", "m1"))),
            ), otherwise=Route("nl-fallback", "m1")),
        ), otherwise=Block("blocked")))
    compiled = compile_tree(tree, CAPABILITIES)

    assert compiled is not tree
    assert compiled.root is not tree.root
    assert compiled.root.otherwise is tree.root.otherwise      # terminal: reused
    nl = compiled.root.children[0]
    assert nl is not tree.root.children[0]
    assert nl.otherwise is not None
    inner = nl.options[1].target
    assert isinstance(inner, NLChoice) and inner.options[0].text == "m2: deep tasks"
    # the stored tree is untouched (pure step)
    assert [option.text for option in tree.root.children[0].options] == [
        "", "child branch"]
    # envelope metadata is preserved
    assert (compiled.id, compiled.name, compiled.enabled,
            compiled.tree_revision) == ("t", "T", True, "rev-1")


def test_compile_never_reorders_candidates():
    tree = compiler_tree()
    compiled = compile_tree(tree, CAPABILITIES)
    assert [option.key for option in compiled.root.options] == [
        "m1", "m2", "child"]
    assert [option.key for option in tree.root.options] == ["m1", "m2", "child"]


def test_compile_rejects_a_model_without_a_capability_text():
    tree = compiler_tree()
    with pytest.raises(ValueError):
        compile_tree(tree, {})
    with pytest.raises(ValueError):
        compile_tree(tree, {"m1": "fast tasks", "m2": "   "})


def test_effective_serde_adds_only_the_compiled_option_text():
    compiled = compile_tree(compiler_tree(), CAPABILITIES)
    encoded = effective_node_to_dict(compiled.root)
    plain = node_to_dict(compiled.root)

    # direct model candidates gain `text`; their base shape is identical
    assert encoded["options"][0] == {
        "key": "m1", "model": "m1", "reasoning": None, "text": "m1: fast tasks"}
    assert {key: value for key, value in encoded["options"][0].items()
            if key != "text"} == plain["options"][0]
    # child candidates are unchanged (their text is already the description)
    assert encoded["options"][2] == plain["options"][2]
    # everything that is not an NL option is byte-for-byte the original serde
    assert encoded["otherwise"] == plain["otherwise"]
    assert encoded["instructions"] == plain["instructions"]
    assert "when" not in encoded["otherwise"]          # null keys still omitted


def test_effective_serde_round_trips_a_compiled_tree():
    compiled = compile_tree(compiler_tree(), CAPABILITIES)
    restored = effective_node_from_dict(effective_node_to_dict(compiled.root))
    assert restored == compiled.root
    assert [option.text for option in restored.options] == [
        "m1: fast tasks", "m2: deep tasks", "child branch"]
    assert [option.capability_model for option in restored.options] == [
        "m1", "m2", None]


def test_compile_stage_exposes_signal_requirements_for_the_snapshot():
    root = Branch(
        id="root",
        children=(
            Route("office", "m1",
                  when=When("time_window", "work_hours", op="contains")),
            NLChoice(id="nl", instructions="choose",
                     when=When("pii", "email_address", op="contains"),
                     options=(
                         NLOption("a", "", Route("nl/options/a", "m1"), "m1"),
                         NLOption("b", "", Route("nl/options/b", "m2"), "m2"),
                     ),
                     otherwise=Route("nl-fallback", "m1")),
        ),
        otherwise=Route("fallback", "m1"))
    compiled = compile_tree(RoutingTree(id="t", name="T", enabled=True,
                                        root=root), CAPABILITIES)

    required, scope = tree_signal_requirements(compiled.root)
    assert required == frozenset({"time_window", "pii"})
    assert isinstance(scope, SignalScope)
    assert scope.signals == frozenset({"pii"})
    assert scope.pii_types == frozenset({"email_address"})
    assert not scope.full
