"""Health checks for the explicit-tree model."""

from router.pipeline.health import check_pipeline
from router.pipeline.model import Station, TreeNode, Weight

MODELS = {"qwen3-8b", "med", "qwen3-8b-medical", "qwen3-0.6b-medical",
          "deepseek-v3"}


def node(nid, signal=None, value="", conf=None, kind="leaf", action="route",
         model=None, min_score=None, weights=None, children=None):
    return TreeNode(id=nid, signal=signal, value=value, conf_min=conf,
                    kind=kind, action=action, model=model,
                    min_score=min_score, weights=weights or [],
                    children=children or [])


def fallback():
    return Station(id="f", type="gate", name="兜底",
                   root=node("n4", signal=None, action="route", model="qwen3-8b"))


def codes(issues):
    return {i.code for i in issues}


def test_default_stations_are_healthy():
    from router.store.repositories import default_pipeline
    assert check_pipeline(default_pipeline("heuristic"), MODELS) == []


def test_empty_pipeline():
    assert "empty_pipeline" in codes(check_pipeline([], MODELS))


def test_missing_fallback():
    stations = [Station(id="s", type="gate", name="x",
                        root=node("r", signal=None, kind="fork",
                                  children=[node("c", "domain", "health", 0.5,
                                                 model="med")]))]
    assert "missing_fallback" in codes(check_pipeline(stations, MODELS))


def test_unconditional_station_must_be_last():
    stations = [
        Station(id="a", type="gate", name="直达",
                root=node("r", signal=None, action="route", model="med")),
        fallback()]
    assert "station_shadow" in codes(check_pipeline(stations, MODELS))


def test_unknown_model_and_signal():
    stations = [
        Station(id="a", type="gate", name="x", root=node(
            "r", signal=None, kind="fork", children=[
                node("c", "telepathy", "yes", 0.5, model="nope")])),
        fallback()]
    got = codes(check_pipeline(stations, MODELS))
    assert "unknown_signal" in got and "unknown_model" in got


def test_missing_model_on_route_leaf():
    stations = [Station(id="a", type="gate", name="x", root=node(
        "r", signal=None, kind="fork",
        children=[node("c", "domain", "health", 0.5)])), fallback()]
    assert "missing_model" in codes(check_pipeline(stations, MODELS))


def test_leaf_and_fork_shape():
    stations = [
        Station(id="a", type="gate", name="x", root=node(
            "r", signal=None, kind="fork", children=[
                node("c", "domain", "health", 0.5, model="med",
                     kind="fork")])),
        fallback()]
    got = codes(check_pipeline(stations, MODELS))
    assert "empty_fork" in got


def test_rule_signal_floor_warns_only():
    stations = [
        Station(id="kw", type="gate", name="关键词", root=node(
            "r", signal=None, kind="fork", children=[
                node("c", "keyword", "医疗急症", 0.5, model="med")])),
        fallback()]
    issues = check_pipeline(stations, MODELS)
    assert "rule_signal_floor" in codes(issues)
    assert all(i.level == "warn" for i in issues if i.code == "rule_signal_floor")


def test_weighted_leaf_without_min_score_warns():
    stations = [
        Station(id="a", type="score", name="x", root=node(
            "r", signal=None, kind="fork", children=[
                node("c", "domain", "health", 0.5, model="med",
                     weights=[Weight("complexity", "hard", 0.5)])])),
        fallback()]
    issues = check_pipeline(stations, MODELS)
    assert "missing_min_score" in codes(issues)
    assert all(i.level == "warn" for i in issues
               if i.code == "missing_min_score")


def test_duplicate_conditions_warn():
    stations = [Station(id="a", type="gate", name="x", root=node(
        "r", signal=None, kind="fork", children=[
            node("c1", "domain", "health", 0.5, model="med"),
            node("c2", "domain", "health", 0.5, model="med")])), fallback()]
    issues = check_pipeline(stations, MODELS)
    assert "duplicate_condition" in codes(issues)
    assert all(i.level == "warn" for i in issues
               if i.code == "duplicate_condition")
