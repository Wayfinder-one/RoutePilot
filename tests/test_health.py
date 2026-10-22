"""Health checks for the explicit-tree model."""

from router.pipeline.health import check_pipeline
from router.pipeline.model import Station, TreeNode, Weight

MODELS = {"qwen3-8b", "med", "qwen3-8b-medical", "qwen3-0.6b-medical",
          "deepseek-v3"}


def node(nid, signal=None, value="", conf=None, kind="leaf", action="route",
         model=None, min_score=None, weights=None, children=None,
         reasoning=None):
    return TreeNode(id=nid, signal=signal, value=value, conf_min=conf,
                    kind=kind, action=action, model=model,
                    min_score=min_score, weights=weights or [],
                    children=children or [], reasoning=reasoning)


def fallback():
    return Station(id="f", type="gate", name="兜底",
                   root=node("n4", signal=None, action="route", model="qwen3-8b"))


def codes(issues):
    return {i.code for i in issues}


def test_default_stations_are_healthy():
    from router.store.repositories import default_pipeline
    assert check_pipeline(default_pipeline(), MODELS) == []


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


def test_duplicate_node_id_is_hard():
    """Two nodes with the same id in one tree: the console would edit the
    wrong node, so the save must be rejected."""
    stations = [Station(id="a", type="gate", name="x", root=node(
        "r", signal=None, kind="fork", children=[
            node("dup", "domain", "health", 0.5, model="med"),
            node("dup", "complexity", "hard", 0.5, model="med")])), fallback()]
    issues = check_pipeline(stations, MODELS)
    hit = [i for i in issues if i.code == "duplicate_node_id"]
    assert hit and hit[0].level == "hard"


def test_nested_duplicate_node_id_reports_once():
    """The id walk must stay out of the recursive _check_node: a duplicate pair
    nested below depth 1 used to be reported once per ancestor, i.e. once per
    re-visit of the same subtree."""
    stations = [Station(id="a", type="gate", name="x", root=node(
        "r", signal=None, kind="fork", children=[
            node("fork", "domain", "health", 0.5, kind="fork", children=[
                node("dup", "domain", "health", 0.5, model="med"),
                node("dup", "complexity", "hard", 0.5, model="med")])])),
        fallback()]
    hits = [i for i in check_pipeline(stations, MODELS)
            if i.code == "duplicate_node_id"]
    assert len(hits) == 1


def test_duplicate_station_id_is_hard():
    stations = [
        Station(id="dup", type="gate", name="a", root=node(
            "r1", signal=None, kind="fork", children=[
                node("c", "domain", "health", 0.5, model="med")])),
        Station(id="dup", type="gate", name="b", root=node(
            "r2", "complexity", "hard", 0.5, model="med")),
        fallback()]
    issues = check_pipeline(stations, MODELS)
    hit = [i for i in issues if i.code == "duplicate_station_id"]
    assert hit and hit[0].level == "hard"


def test_ignored_fields_warn_only():
    """Fields that are set but can never take effect are warnings, not errors:
    the tree still runs, the console tells the operator what is ignored."""
    stations = [
        Station(id="a", type="gate", name="x", root=node(
            "r", None, "", 0.5, kind="fork", model="med",
            min_score=0.5, action="block", children=[
                node("c", "domain", "health", 0.5, action="block",
                     model="med", min_score=0.5)])),
        fallback()]
    issues = check_pipeline(stations, MODELS)
    ignored = [i for i in issues if i.code == "ignored_field"]
    fields = {i.field for i in ignored}
    assert {"conf_min", "model", "action", "min_score"} <= fields
    assert all(i.level == "warn" for i in ignored)
    assert not [i for i in issues if i.level == "hard"]


def test_min_score_on_a_leaf_without_weights_warns():
    stations = [
        Station(id="a", type="gate", name="x", root=node(
            "r", signal=None, kind="fork", children=[
                node("c", "domain", "health", 0.5, model="med",
                     min_score=0.5)])),
        fallback()]
    issues = check_pipeline(stations, MODELS)
    hit = [i for i in issues if i.code == "ignored_field"
           and i.field == "min_score"]
    assert hit and hit[0].level == "warn"


# --------------------------------------------------------------------------- #
# §3.3: 分树叶子的推理选择（model_reasoning 传入时才校验）
# --------------------------------------------------------------------------- #
DECLARED = {"declared": True, "supported": True, "format": "openai",
            "efforts": [{"id": "off", "wire": None},
                        {"id": "high", "wire": "high"}],
            "default": None, "supports_reasoning_effort": True}


def reasoning_station(reasoning, *, action="route", model="qwen3-8b"):
    return Station(id="a", type="gate", name="x", root=node(
        "r", signal=None, kind="fork", children=[
            node("c", "domain", "health", 0.5, action=action, model=model,
                 reasoning=reasoning)]))


def test_reasoning_levels_are_optional_for_old_callers():
    """Old callers pass only known_models: reasoning is not validated (compat)."""
    stations = [reasoning_station("high"), fallback()]
    assert codes(check_pipeline(stations, MODELS)) == set()


def test_reasoning_without_route_is_hard():
    stations = [reasoning_station("off", action="block", model=None), fallback()]
    issues = check_pipeline(stations, MODELS, {"qwen3-8b": DECLARED})
    hit = [i for i in issues if i.code == "reasoning_without_route"]
    assert hit and hit[0].level == "hard"
    assert hit[0].model == "" and hit[0].reasoning == "off"


def test_reasoning_unsupported_when_model_undeclared():
    stations = [reasoning_station("high"), fallback()]
    for capability in (None, {"declared": False, "supported": None,
                              "format": "openai", "efforts": [],
                              "default": None},
                       {"declared": True, "supported": False,
                        "format": "openai", "efforts": [], "default": None}):
        issues = check_pipeline(stations, MODELS, {"qwen3-8b": capability})
        hit = [i for i in issues if i.code == "reasoning_unsupported"]
        assert hit and hit[0].level == "hard"
        assert hit[0].model == "qwen3-8b" and hit[0].reasoning == "high"


def test_reasoning_unsupported_when_level_not_declared():
    stations = [reasoning_station("max"), fallback()]
    issues = check_pipeline(stations, MODELS, {"qwen3-8b": DECLARED})
    hit = [i for i in issues if i.code == "reasoning_unsupported"]
    assert hit and hit[0].reasoning == "max" and hit[0].model == "qwen3-8b"


def test_reasoning_declared_level_is_healthy():
    for level in ("off", "high"):
        stations = [reasoning_station(level), fallback()]
        assert check_pipeline(stations, MODELS, {"qwen3-8b": DECLARED}) == []


def test_reasoning_check_ignores_unknown_model():
    """A bogus model is already flagged by unknown_model; don't double-report."""
    stations = [reasoning_station("high", model="nope"), fallback()]
    issues = check_pipeline(stations, MODELS, {"nope": None})
    got = codes(issues)
    assert "unknown_model" in got
    assert "reasoning_unsupported" not in got


def test_pipeline_with_reasoning_is_healthy_via_default_models():
    """The seeded pipeline has no reasoning selected; adding the map keeps it green."""
    from router.store.repositories import default_pipeline
    stations = default_pipeline()
    assert check_pipeline(stations, MODELS) == []
    assert check_pipeline(stations, MODELS, {}) == []
