import pytest

from router.routing.model import (
    Branch,
    Jev,
    RoutingTree,
    Target,
    tree_from_dict,
    tree_revision_of,
    tree_to_dict,
    walk_nodes,
)
from tests.routing_fixtures import jev_dict, target_dict, tree_dict


def test_roundtrip_preserves_candidate_order_and_sticky():
    tree = tree_from_dict(tree_dict())
    assert isinstance(tree, RoutingTree)
    assert isinstance(tree.root, Jev)
    assert isinstance(tree.root.options[0].target, Target)
    assert [option.key for option in tree.root.options] == ["fast", "strong"]
    assert tree.sticky.enabled is True
    assert tree.sticky.ttl_seconds == 1800
    assert tree_to_dict(tree)["root"] == tree_dict()["root"]


def test_revision_covers_behavior_not_name_or_enabled():
    first = tree_from_dict(tree_dict())
    renamed = tree_from_dict({**tree_dict(), "name": "另一个名字", "enabled": True})
    assert tree_revision_of(first) == tree_revision_of(renamed)
    reordered = tree_dict()
    reordered["root"]["options"] = list(reversed(reordered["root"]["options"]))
    assert tree_revision_of(tree_from_dict(reordered)) != tree_revision_of(first)
    no_sticky = tree_dict(sticky={"enabled": False, "ttl_seconds": 1800})
    assert tree_revision_of(tree_from_dict(no_sticky)) != tree_revision_of(first)


def test_unknown_fields_and_root_conditions_are_rejected():
    with pytest.raises(ValueError, match="unknown"):
        tree_from_dict({**tree_dict(), "signals": {}})
    raw = tree_dict(root=target_dict())
    raw["root"]["when"] = {"periods": ["白天"]}
    with pytest.raises(ValueError, match="root"):
        tree_from_dict(raw)
    raw = tree_dict(root=target_dict())
    raw["root"]["weights"] = []
    with pytest.raises(ValueError, match="weights"):
        tree_from_dict(raw)


def test_jev_options_can_only_be_targets():
    raw = tree_dict()
    raw["root"]["options"][0]["target"] = jev_dict("nested")
    with pytest.raises(ValueError, match="target"):
        tree_from_dict(raw)


def test_branch_and_walk_nodes_include_fallbacks():
    root = {
        "id": "root", "type": "branch",
        "children": [{**jev_dict(), "when": {"periods": ["夜间"]}}],
        "otherwise": target_dict("default", ("standard",)),
    }
    tree = tree_from_dict(tree_dict(root=root))
    assert isinstance(tree.root, Branch)
    ids = {node.id for node in walk_nodes(tree.root)}
    assert {"root", "choose", "fast-target", "strong-target", "safe", "default"} <= ids
