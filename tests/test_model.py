import pytest

from router.pipeline.model import Node, node_from_dict, node_to_dict


def sample_dict():
    return {"id": "n1", "type": "score", "name": "领域打分", "branches": [
        {"id": "b1", "name": "8B医疗", "model": "qwen3-8b-medical", "min_score": 0.7,
         "rows": [{"signal": "domain", "value": "health", "weight": 0.5}]}]}


def test_roundtrip():
    n = node_from_dict(sample_dict())
    assert n.type == "score"
    assert n.branches[0].rows[0].weight == 0.5
    # SPEC-TREE-4 §2.2: node objects carry floor / split_signal (null for
    # non-split nodes, and old payloads without the keys deserialize fine).
    assert node_to_dict(n) == sample_dict() | {
        "floor": None, "split_signal": None, "branches": [
            {"id": "b1", "name": "8B医疗", "model": "qwen3-8b-medical",
             "min_score": 0.7, "action": "route",
             "rows": [{"signal": "domain", "value": "health", "weight": 0.5,
                       "conf_min": None}]}]}


def test_split_roundtrip():
    d = {"id": "n3", "type": "split", "name": "学科 → 难度", "floor": 0.70,
         "split_signal": None, "branches": [
            {"id": "b4", "name": "8B 医疗", "action": "route",
             "model": "qwen3-8b-medical", "min_score": 0.55,
             "rows": [{"signal": "domain", "value": "health", "weight": 0.5},
                      {"signal": "complexity", "value": "hard", "weight": 0.5}]}]}
    n = node_from_dict(d)
    assert n.type == "split" and n.floor == 0.70 and n.split_signal is None
    assert n.branches[0].rows[0].weight == 0.5      # split rows use weight
    assert node_to_dict(n) == d | {"branches": [
        {"id": "b4", "name": "8B 医疗", "action": "route",
         "model": "qwen3-8b-medical", "min_score": 0.55,
         "rows": [{"signal": "domain", "value": "health", "weight": 0.5,
                   "conf_min": None},
                  {"signal": "complexity", "value": "hard", "weight": 0.5,
                   "conf_min": None}]}]}
    # old payloads without the two keys, and out-of-range floor
    old = node_from_dict({"id": "n1", "type": "split", "branches": []})
    assert old.floor is None and old.split_signal is None
    with pytest.raises(ValueError):
        node_from_dict({"id": "n1", "type": "split", "floor": 1.5, "branches": []})


def test_bad_shape_rejected():
    with pytest.raises(ValueError):
        node_from_dict({"id": "n1", "type": "wat", "branches": []})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n1", "type": "score", "branches": [
            {"id": "b1", "rows": [{"signal": "domain", "value": "health",
                                   "weight": 1.5}]}]})
