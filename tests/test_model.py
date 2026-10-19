"""Explicit-tree model: round-trip + the trust boundary (malformed -> ValueError)."""

import pytest

from router.pipeline.model import (Station, TreeNode, Weight, node_from_dict,
                                   node_to_dict, station_from_dict,
                                   station_to_dict)


def sample_node() -> dict:
    return {
        "id": "n1", "signal": None, "value": "", "conf_min": None,
        "kind": "fork", "action": "route", "model": None, "min_score": None,
        "weights": [], "children": [
            {"id": "n1a", "signal": "domain", "value": "health",
             "conf_min": 0.7, "kind": "fork", "action": "route",
             "model": None, "min_score": None, "weights": [], "children": [
                 {"id": "n1a1", "signal": "complexity", "value": "hard",
                  "conf_min": 0.7, "kind": "leaf", "action": "route",
                  "model": "qwen3-8b-medical", "min_score": None,
                  "weights": [], "children": []},
                 {"id": "n1a2", "signal": "complexity", "value": "easy",
                  "conf_min": None, "kind": "leaf", "action": "route",
                  "model": "qwen3-0.6b-medical", "min_score": 0.4,
                  "weights": [{"signal": "domain", "value": "health",
                               "weight": 0.5}], "children": []},
             ]},
        ],
    }


def test_station_roundtrip():
    d = {"id": "s1", "type": "split", "name": "学科 → 难度",
         "root": sample_node()}
    st = station_from_dict(d)
    assert st.type == "split"
    assert st.root.kind == "fork" and len(st.root.children) == 1
    leaf = st.root.children[0].children[1]
    assert leaf.min_score == 0.4 and leaf.weights[0].weight == 0.5
    assert station_to_dict(st) == d


def test_leaf_with_weights_roundtrip():
    leaf = TreeNode(id="l", signal="domain", value="health", conf_min=0.6,
                    kind="leaf", action="block", weights=[
                        Weight("pii", "cn_id_card", 0.7)])
    d = node_to_dict(leaf)
    assert node_from_dict(d) == leaf


def test_shape_validation():
    with pytest.raises(ValueError):
        station_from_dict({"id": "s", "type": "wat", "root": None})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n", "kind": "fork", "children": []})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n", "kind": "fork",
                        "weights": [{"signal": "domain", "value": "health"}],
                        "children": [{"id": "c"}]})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n", "kind": "leaf",
                        "children": [{"id": "c"}]})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n", "action": "explode"})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n", "signal": "domain", "value": "health",
                        "conf_min": 1.5})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n", "signal": "domain", "value": "health",
                        "weights": [{"signal": "domain", "value": "health",
                                     "weight": -0.1}]})


def test_unconditional_leaf_is_legal():
    d = {"id": "f", "signal": None, "value": "", "conf_min": None,
         "kind": "leaf", "action": "route", "model": "qwen3-8b",
         "min_score": None, "weights": [], "children": []}
    n = node_from_dict(d)
    assert n.signal is None and n.kind == "leaf"
    assert node_to_dict(n) == d
