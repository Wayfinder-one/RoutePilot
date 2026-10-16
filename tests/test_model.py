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
    assert node_to_dict(n) == sample_dict() | {"branches": [
        {"id": "b1", "name": "8B医疗", "model": "qwen3-8b-medical", "min_score": 0.7,
         "action": "route",
         "rows": [{"signal": "domain", "value": "health", "weight": 0.5,
                   "conf_min": None}]}]}


def test_bad_shape_rejected():
    with pytest.raises(ValueError):
        node_from_dict({"id": "n1", "type": "wat", "branches": []})
    with pytest.raises(ValueError):
        node_from_dict({"id": "n1", "type": "score", "branches": [
            {"id": "b1", "rows": [{"signal": "domain", "value": "health",
                                   "weight": 1.5}]}]})
