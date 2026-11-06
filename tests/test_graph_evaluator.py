"""GraphEvaluator：三值定点、确定性槽位、波次、候选与机器 trace。

本文件同时提供五场景矩阵测试（test_graph_scenarios.py）与草稿试算测试
（test_graph_try_api.py）复用的测试替身：FakeJev、RecordingBalancer、
RecordingAffinity，以及 snapshot/tree 构造与组合根 helper。
求值断言直接读 GraphOutcome；粘性、熔断、dry-run、inflight 等调度断言在
test_routing_service.py。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from router.jev.client import (
    ChoiceAnswer,
    JevError,
    NoulAnswer,
    ScoreAnswer,
)
from router.jev.config import JevSettings
from router.jev.window import apply_window
from router.routing.affinity import SessionAffinity
from router.routing.conditions import TimePeriod
from router.routing.graph.conditions import UNKNOWN
from router.routing.graph.compiled import compile_graph
from router.routing.graph.evaluator import GraphEvaluator, GraphOutcome, combine_gate
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import build_snapshot
from router.routing.tree import tree_from_dict
from router.routing.types import (
    Candidate,
    NoActiveTreeError,
    RoutingMiss,
    RoutingRequest,
    WaveTrace,
)
from router.upstream.catalog import Backend

FIXTURES = Path(__file__).parent / "fixtures"
POWER_GRAPH = json.loads((FIXTURES / "graph_power.json").read_text(encoding="utf-8"))
SCENARIO_DATA = json.loads((FIXTURES / "graph_scenarios.json").read_text(encoding="utf-8"))
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIO_DATA["scenarios"]}
PERIODS = [TimePeriod.from_dict(period) for period in SCENARIO_DATA["periods"]]
MODELS = ("qwen3.8-flash-sft", "deepseek-v3", "qwen3-32b", "qwen3-8b")
BACKENDS = {
    model: Backend(url=f"http://upstream.test/{model}", headers={}, upstream_name=model, provider="local")
    for model in MODELS
}
NOW = datetime.fromisoformat("2026-11-02T12:00:00+08:00")
DISPLAY_KEYS = {"phase", "label", "cell", "summary", "facts", "tone"}


class FakeJev:
    def __init__(self, scenario):
        self.scenario = scenario
        self.calls = []

    async def choose_many(self, settings, text, questions):
        self.calls.append({"text": text, "keys": list(questions)})
        if self.scenario.get("fail", {}).get("wave") == len(self.calls):
            raise JevError(self.scenario["fail"].get("message", "timeout"))
        result = {}
        for node_id, question in questions.items():
            fixture = self.scenario["answers"].get(node_id)
            if fixture is None:
                # 缺失答案属于 JEV 失败，必须由求值层转成失败波次。
                raise JevError(f"JEV response is missing answer for {node_id!r}")
            if question.question_type == "choice":
                probabilities = fixture["probs"]
                ranking = sorted(probabilities, key=lambda key: -probabilities[key])
                top = probabilities[ranking[0]]
                second = probabilities[ranking[1]]
                result[node_id] = ChoiceAnswer(probabilities, ranking, top, top - second, {"input_tokens": 20}, 12.0)
            elif question.question_type == "noul":
                result[node_id] = NoulAnswer(fixture["yes"], 1 - fixture["yes"], {"input_tokens": 20}, 12.0)
            else:
                result[node_id] = ScoreAnswer(fixture["value"], {"input_tokens": 20}, 12.0)
        return result


class RecordingBalancer(LoadBalancer):
    """记录 acquire 的 models/preferred/dry_run，其余调度语义复用真实 LoadBalancer。"""

    def __init__(self, breaker=None):
        super().__init__(breaker)
        self.calls = []

    def acquire(self, models, *, preferred=None, dry_run=False):
        self.calls.append({"models": list(models), "preferred": preferred, "dry_run": dry_run})
        return super().acquire(models, preferred=preferred, dry_run=dry_run)


class RecordingAffinity(SessionAffinity):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gets = []
        self.puts = []

    def get(self, session_id, *, tree_revision, policy_path, allowed_models, touch=True):
        self.gets.append({
            "session_id": session_id,
            "tree_revision": tree_revision,
            "policy_path": policy_path,
            "allowed_models": list(allowed_models),
            "touch": touch,
        })
        return super().get(
            session_id,
            tree_revision=tree_revision,
            policy_path=policy_path,
            allowed_models=allowed_models,
            touch=touch,
        )

    def put(self, session_id, model, *, target_id, tree_revision, policy_path, ttl_seconds):
        self.puts.append({
            "session_id": session_id,
            "model": model,
            "target_id": target_id,
            "tree_revision": tree_revision,
            "policy_path": policy_path,
            "ttl_seconds": ttl_seconds,
        })
        return super().put(
            session_id,
            model,
            target_id=target_id,
            tree_revision=tree_revision,
            policy_path=policy_path,
            ttl_seconds=ttl_seconds,
        )


def power_tree(*, root=None, sticky=None):
    return tree_from_dict({
        "id": "power-priority",
        "name": "电力优先决策组",
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": POWER_GRAPH if root is None else root,
    })


def custom_tree(nodes, edges, *, error_fallback, sticky=None):
    return tree_from_dict({
        "id": "custom",
        "name": "",
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": {
            "id": "root",
            "type": "graph",
            "nodes": nodes,
            "edges": edges,
            "error_fallback": error_fallback,
        },
    })


def make_snapshot(tree=None, *, backends=None, periods=None, jev=True, input_tokens=1024):
    tree = power_tree() if tree is None else tree
    return build_snapshot(
        tree=tree,
        backends=BACKENDS if backends is None else backends,
        time_periods=list(PERIODS if periods is None else periods),
        jev=JevSettings("http://jev.test", "d1-2B") if jev else None,
        capabilities={},
        jev_input_tokens=input_tokens,
        jev_context_tokens=4096,
        compiled=compile_graph(tree.root),
    )


def run_evaluate(
    scenario_id,
    *,
    tree=None,
    text=None,
    now=None,
    input_tokens=1024,
    trace_sink=None,
):
    """求值层入口：直接返回 GraphOutcome + 本次求值写入的机器事件。"""
    scenario = SCENARIO_BY_ID[scenario_id]
    jev = FakeJev(scenario)
    snapshot = make_snapshot(power_tree() if tree is None else tree, input_tokens=input_tokens)
    trace = [] if trace_sink is None else trace_sink
    outcome = asyncio.run(GraphEvaluator(jev).evaluate(
        snapshot,
        RoutingRequest(scenario["text"] if text is None else text),
        now=datetime.fromisoformat(scenario["now"]) if now is None else now,
        trace_sink=trace,
    ))
    return outcome, jev, trace


def run_scenario(
    scenario_id,
    *,
    scenario=None,
    tree=None,
    dry_run=False,
    session_id="",
    balancer=None,
    affinity=None,
    text=None,
    now=None,
    input_tokens=1024,
    trace_sink=None,
):
    """组合根入口：真实 GraphEvaluator + ModelScheduler，供路由/试算测试复用。"""
    scenario = SCENARIO_BY_ID[scenario_id] if scenario is None else scenario
    jev = FakeJev(scenario)
    balancer = RecordingBalancer() if balancer is None else balancer
    affinity = RecordingAffinity() if affinity is None else affinity
    service = RoutingService(GraphEvaluator(jev), ModelScheduler(affinity, balancer))
    snapshot = make_snapshot(power_tree() if tree is None else tree, input_tokens=input_tokens)
    decision = asyncio.run(service.route(
        snapshot,
        RoutingRequest(scenario["text"] if text is None else text, session_id=session_id),
        now=datetime.fromisoformat(scenario["now"]) if now is None else now,
        dry_run=dry_run,
        trace_sink=trace_sink,
    ))
    return decision, jev, balancer, affinity


def evaluate_custom(
    nodes,
    edges,
    *,
    error_fallback,
    scenario=None,
    sticky=None,
    text="",
    now=NOW,
    trace_sink=None,
):
    tree = custom_tree(nodes, edges, error_fallback=error_fallback, sticky=sticky)
    jev = FakeJev(scenario or {"answers": {}})
    trace = [] if trace_sink is None else trace_sink
    outcome = asyncio.run(GraphEvaluator(jev).evaluate(
        make_snapshot(tree, periods=[]),
        RoutingRequest(text),
        now=now,
        trace_sink=trace,
    ))
    return outcome, jev, trace


def run_custom(
    nodes,
    edges,
    *,
    error_fallback,
    scenario=None,
    sticky=None,
    text="",
    now=NOW,
    dry_run=False,
    session_id="",
    balancer=None,
    affinity=None,
    trace_sink=None,
):
    tree = custom_tree(nodes, edges, error_fallback=error_fallback, sticky=sticky)
    jev = FakeJev(scenario or {"answers": {}})
    balancer = RecordingBalancer() if balancer is None else balancer
    affinity = RecordingAffinity() if affinity is None else affinity
    service = RoutingService(GraphEvaluator(jev), ModelScheduler(affinity, balancer))
    decision = asyncio.run(service.route(
        make_snapshot(tree, periods=[]),
        RoutingRequest(text, session_id=session_id),
        now=now,
        dry_run=dry_run,
        trace_sink=trace_sink,
    ))
    return decision, jev, balancer, affinity


def node(node_id, node_type, **fields):
    return {"id": node_id, "type": node_type, **fields}


def edge(edge_id, source, source_port, target, target_port, condition=None):
    return {
        "id": edge_id,
        "source": source,
        "source_port": source_port,
        "target": target,
        "target_port": target_port,
        "condition": condition,
    }


def choice_options(*keys):
    return [
        {"key": key, "label": key.upper(), "capability": f"{key} capability"}
        for key in keys
    ]


# ---------------------------------------------------------------------------
# combine_gate 纯函数
# ---------------------------------------------------------------------------


def test_combine_gate_three_valued_truth_tables():
    assert combine_gate("not", [True]) is False
    assert combine_gate("not", [False]) is True
    assert combine_gate("not", [UNKNOWN]) == UNKNOWN
    assert combine_gate("not", [True, False]) == UNKNOWN
    assert combine_gate("and", [True, True]) is True
    assert combine_gate("and", [False, UNKNOWN]) is False
    assert combine_gate("and", [True, UNKNOWN]) == UNKNOWN
    assert combine_gate("and", [True]) == UNKNOWN
    assert combine_gate("or", [False, False]) is False
    assert combine_gate("or", [True, UNKNOWN]) is True
    assert combine_gate("or", [False, UNKNOWN]) == UNKNOWN
    assert combine_gate("or", [False]) == UNKNOWN
    # 防御层：未知算子必须显式失败，绝不静默按 OR 处理（校验层已拦截合法图）。
    with pytest.raises(ValueError, match="unknown logic operator"):
        combine_gate("xor", [True, False])


# ---------------------------------------------------------------------------
# 终局异常路径
# ---------------------------------------------------------------------------


def test_evaluate_without_active_tree_raises_no_active_tree():
    evaluator = GraphEvaluator(FakeJev({"answers": {}}))
    snapshot = build_snapshot(
        tree=None, backends=BACKENDS, time_periods=[], jev=None, capabilities={},
    )
    with pytest.raises(NoActiveTreeError):
        asyncio.run(evaluator.evaluate(snapshot, RoutingRequest("你好"), now=NOW))


def test_evaluate_requires_compiled_snapshot_without_rebuilding_topology():
    from dataclasses import replace

    snapshot = replace(make_snapshot(), compiled=None)
    evaluator = GraphEvaluator(FakeJev({"answers": {}}))
    with pytest.raises(RoutingMiss, match="compiled graph"):
        asyncio.run(evaluator.evaluate(snapshot, RoutingRequest("你好"), now=NOW))


def test_wave_guard_rejects_more_than_32_waves():
    """_MAX_WAVES 防御：33 连问链（校验层本应拦截，求值层仍须自保）在 32 波后 RoutingMiss。"""
    count = 33
    nodes = [node("start", "start")]
    edges = [edge("e0", "start", "out", "q1", "in_1")]
    answers = {}
    for index in range(1, count + 1):
        nodes.append(node(f"q{index}", "question", question_type="noul", instructions="继续？"))
        answers[f"q{index}"] = {"yes": 0.9}
        if index > 1:
            edges.append(edge(
                f"e{index - 1}", f"q{index - 1}", "yes", f"q{index}", "in_1",
                {"op": "gt", "value": 0.5},
            ))
    nodes.append(node("sink", "target", priority=1, models=[{"model": "qwen3-8b", "reasoning": None}]))
    with pytest.raises(RoutingMiss, match="32 waves"):
        evaluate_custom(
            nodes, edges, error_fallback="sink",
            scenario={"answers": answers},
        )


def test_invalid_fallback_target_raises_routing_miss():
    with pytest.raises(RoutingMiss, match="error_fallback"):
        evaluate_custom([node("start", "start")], [], error_fallback="ghost")


def test_invalid_fallback_leaves_partial_trace_in_external_sink():
    trace = []
    with pytest.raises(RoutingMiss, match="error_fallback"):
        evaluate_custom(
            [node("start", "start")], [], error_fallback="ghost", trace_sink=trace,
        )
    assert [event["event"] for event in trace[:2]] == ["time", "window"]
    assert not any(event["event"] == "routing_error" for event in trace)


# ---------------------------------------------------------------------------
# trace 契约（§3.9）：求值层只发机器事件
# ---------------------------------------------------------------------------


def test_trace_events_follow_spec_schema_and_order():
    outcome, _jev, events = run_evaluate("s1")

    assert events[0] == {"event": "time", "active_periods": ["normal"], "timezone": "Asia/Shanghai"}
    assert events[1]["event"] == "window"
    assert set(events[1]) == {"event", "tokens_before", "tokens_after", "truncated"}
    assert events[1]["truncated"] is False

    condition_events = [event for event in events if event["event"] == "condition"]
    assert [event["node_id"] for event in condition_events] == ["cond-peak", "cond-text"]
    for event in condition_events:
        assert set(event) == {"event", "node_id", "source", "matched", "value"}
        assert isinstance(event["value"], bool)

    slot_events = [event for event in events if event["event"] == "slot"]
    # 两轮（第 0 轮 + 第 1 波后）每轮全部 15 条边各一条。
    assert len(slot_events) == 2 * len(POWER_GRAPH["edges"])
    for event in slot_events:
        assert set(event) == {
            "event", "wave", "edge_id", "node_id", "port",
            "source", "source_port", "raw", "condition", "status",
        }
        assert event["status"] in {"true", "false", "unknown", "invalid"}
    assert {event["edge_id"] for event in slot_events} == {edge["id"] for edge in POWER_GRAPH["edges"]}

    gate_events = [event for event in events if event["event"] == "gate"]
    assert gate_events
    for event in gate_events:
        assert set(event) == {"event", "wave", "node_id", "operator", "inputs", "value"}
        for item in event["inputs"]:
            assert set(item) == {"port", "raw", "condition", "status"}

    activate_events = [event for event in events if event["event"] == "activate"]
    assert activate_events
    for event in activate_events:
        assert set(event) == {"event", "wave", "node_id", "via_port"}

    wave_events = [event for event in events if event["event"] == "wave"]
    assert wave_events
    for event in wave_events:
        assert set(event) == {"event", "wave", "questions", "duration_ms", "failed", "error"}

    question_events = [event for event in events if event["event"] == "question"]
    assert [event["node_id"] for event in question_events] == ["capability", "domain"]
    for event in question_events:
        assert set(event) >= {"event", "wave", "node_id", "ports", "duration_ms", "usage"}
        assert event["duration_ms"] == 12.0
        assert event["usage"] == {"input_tokens": 20}
    choice_event = next(event for event in question_events if event["node_id"] == "capability")
    assert set(choice_event) == {
        "event", "wave", "node_id", "ports", "duration_ms", "usage",
        "selected", "p_top", "margin",
    }
    assert choice_event["selected"] == "strong"
    assert choice_event["ports"]["prob.strong"] == 0.84
    assert choice_event["ports"]["selected.standard"] is False
    assert choice_event["ports"]["p_top"] == 0.84
    assert choice_event["ports"]["margin"] == 0.72

    final_event = events[-1]
    assert final_event["event"] == "final"
    assert set(final_event) == {"event", "target_id", "kind", "candidates", "priority"}
    assert final_event["target_id"] == "power-target"
    assert final_event["kind"] == "target"
    assert final_event["priority"] == 5
    assert final_event["candidates"] == [
        {"target_id": "power-target", "priority": 5, "via_node": "and-power", "via_port": "in_1", "wave": 1},
        {"target_id": "strong-target", "priority": 3, "via_node": "capability", "via_port": "in_1", "wave": 1},
    ]

    # 求值层没有 display 字段，也不发出任何调度事件。
    assert all(set(event).isdisjoint(DISPLAY_KEYS) for event in events)
    assert not any(event["event"] in {"schedule", "scheduled", "sticky_hit"} for event in events)

    assert outcome.kind == "target"
    assert outcome.selection_reason == "target"
    assert outcome.fallback_reason is None
    assert outcome.failure_reason is None
    assert [candidate.target_id for candidate in outcome.candidates] == ["power-target", "strong-target"]


def test_evaluator_has_no_sticky_lease_or_scheduled_surface():
    evaluator = GraphEvaluator(FakeJev(SCENARIO_BY_ID["s1"]))
    assert not hasattr(evaluator, "affinity")
    assert not hasattr(evaluator, "balancer")

    outcome, _jev, events = run_evaluate("s1")
    assert isinstance(outcome, GraphOutcome)
    assert not hasattr(outcome, "lease")
    assert not hasattr(outcome, "model")
    assert not any(event["event"] in {"schedule", "scheduled", "sticky_hit"} for event in events)


def test_direct_evaluation_emits_only_graph_stages():
    outcome, _jev, events = evaluate_custom(
        [
            node("start", "start"),
            node("direct", "target", priority=1, models=[{"model": "qwen3-8b", "reasoning": None}]),
        ],
        [edge("direct-edge", "start", "out", "direct", "in_1")],
        error_fallback="direct",
    )
    assert outcome.target.id == "direct"
    assert [event["event"] for event in events[-1:]] == ["final"]
    assert not any(event["event"] in {"schedule", "scheduled", "sticky_hit"} for event in events)


def test_window_is_applied_once_and_conditions_use_truncated_text(monkeypatch):
    from router.routing.graph import evaluator as evaluator_module

    real_apply_window = evaluator_module.apply_window
    calls = []

    def counting_apply_window(text, input_tokens):
        calls.append((text, input_tokens))
        return real_apply_window(text, input_tokens)

    monkeypatch.setattr(evaluator_module, "apply_window", counting_apply_window)

    long_text = "无关铺垫" * 500 + "抢修" + "收尾说明" * 500
    expected_window = apply_window(long_text, 256)
    assert expected_window.truncated is True
    assert "抢修" not in expected_window.text

    outcome, jev, events = run_evaluate(
        "s1", text=long_text, input_tokens=256, now=NOW,
    )
    assert len(calls) == 1
    assert jev.calls[0]["text"] == expected_window.text

    window_event = next(event for event in events if event["event"] == "window")
    assert window_event["truncated"] is True
    assert window_event["tokens_before"] == expected_window.tokens_before
    assert window_event["tokens_after"] == expected_window.tokens_after
    condition = next(
        event for event in events
        if event["event"] == "condition" and event["node_id"] == "cond-text"
    )
    assert condition["matched"] == []
    assert condition["value"] is False
    assert outcome.target.id == "power-target"


# ---------------------------------------------------------------------------
# 不变式 1：未触发问题保持 unknown，不是 0/false
# ---------------------------------------------------------------------------


def test_untriggered_question_ports_stay_unknown_not_zero_or_false():
    outcome, _jev, events = run_evaluate("s1")

    assert "clarify" not in outcome.answers
    assert [event["node_id"] for event in events if event["event"] == "question"] == ["capability", "domain"]
    assert not [
        event for event in events
        if event["event"] == "activate" and event["node_id"] == "clarify"
    ]
    clarify_slots = [
        event for event in events
        if event["event"] == "slot" and event["node_id"] == "clarify"
    ]
    # 第 0 轮上游门未定 → 槽位 unknown（不是 false/0）；后续轮次门确定 false 是正常三值结果。
    assert clarify_slots and clarify_slots[0]["wave"] == 0
    assert clarify_slots[0]["status"] == "unknown"
    assert clarify_slots[0]["raw"] is None
    # 未执行的问题从不写 question 事件/端口，因此 yes/no 不会以 0/false 出现。
    clarify_ports = [
        event["ports"] for event in events
        if event["event"] == "question" and event["node_id"] == "clarify"
    ]
    assert clarify_ports == []


# ---------------------------------------------------------------------------
# 不变式 2：AND 遇 false / OR 遇 true 提前确定，确定后不反转
# ---------------------------------------------------------------------------


def test_and_false_and_or_true_are_determined_early_and_never_flip():
    nodes = [
        node("start", "start"),
        node("q", "question", question_type="choice", instructions="选择能力",
             options=choice_options("easy", "strong")),
        node("cond-true", "condition", source="text", contains_any=["命中"]),
        node("cond-false", "condition", source="text", contains_any=["绝不出现的词"]),
        node("or-gate", "logic", operator="or"),
        node("and-gate", "logic", operator="and"),
        node("t-or", "target", priority=1, models=[{"model": "deepseek-v3"}]),
        node("t-and", "target", priority=2, models=[{"model": "qwen3-32b"}]),
    ]
    edges = [
        edge("e-start-q", "start", "out", "q", "in_1"),
        edge("e-cond-true-or", "cond-true", "out", "or-gate", "in_1"),
        edge("e-q-or", "q", "selected.easy", "or-gate", "in_2"),
        edge("e-cond-false-and", "cond-false", "out", "and-gate", "in_1"),
        edge("e-q-and", "q", "selected.easy", "and-gate", "in_2"),
        edge("e-or-target", "or-gate", "out", "t-or", "in_1"),
        edge("e-and-target", "and-gate", "out", "t-and", "in_1"),
    ]
    outcome, _jev, events = evaluate_custom(
        nodes, edges,
        error_fallback="t-and",
        scenario={"answers": {"q": {"probs": {"easy": 0.9, "strong": 0.1}}}},
        text="命中",
    )

    gates = {event["node_id"]: event for event in events if event["event"] == "gate"}
    # OR 在第 0 轮就因为一个 true 确定（另一个输入还是 unknown）。
    assert gates["or-gate"]["wave"] == 0
    assert gates["or-gate"]["value"] is True
    assert any(item["status"] == "unknown" for item in gates["or-gate"]["inputs"])
    # AND 在第 0 轮就因为一个 false 确定（另一个输入还是 unknown）。
    assert gates["and-gate"]["wave"] == 0
    assert gates["and-gate"]["value"] is False
    assert any(item["status"] == "unknown" for item in gates["and-gate"]["inputs"])
    # 门一旦确定不重复计算，也不会被后续波次反转。
    assert len([event for event in events if event["event"] == "gate"]) == 2
    assert outcome.target.id == "t-or"
    assert [candidate.target_id for candidate in outcome.candidates] == ["t-or"]
    and_slots = [
        event for event in events
        if event["event"] == "slot" and event["node_id"] == "t-and"
    ]
    assert and_slots and all(event["status"] == "false" for event in and_slots)


# ---------------------------------------------------------------------------
# 不变式 3：同波缺答案整波作废，前一波 candidates 也作废
# ---------------------------------------------------------------------------


def test_missing_answer_in_wave_voids_prior_candidates_and_falls_back():
    nodes = [
        node("start", "start"),
        node("q1", "question", question_type="choice", instructions="选择",
             options=choice_options("easy", "strong")),
        node("q2", "question", question_type="noul", instructions="继续？"),
        node("target-a", "target", priority=1, models=[{"model": "deepseek-v3"}]),
        node("target-b", "target", priority=2, models=[{"model": "qwen3-32b"}]),
        node("target-safe", "target", priority=3, models=[{"model": "qwen3-8b"}]),
    ]
    edges = [
        edge("e-start-q1", "start", "out", "q1", "in_1"),
        edge("e-q1-a", "q1", "selected.strong", "target-a", "in_1"),
        edge("e-q1-q2", "q1", "prob.strong", "q2", "in_1", {"op": "gt", "value": 0.65}),
        edge("e-q2-b", "q2", "yes", "target-b", "in_1", {"op": "gt", "value": 0.5}),
    ]
    outcome, jev, events = evaluate_custom(
        nodes, edges,
        error_fallback="target-safe",
        # q2 的答案缺失：FakeJev 在第 2 波抛 JevError。
        scenario={"answers": {"q1": {"probs": {"easy": 0.3, "strong": 0.7}}}},
    )

    assert [call["keys"] for call in jev.calls] == [["q1"], ["q2"]]
    assert len(outcome.waves) == 2
    assert outcome.waves[0].failed is False
    assert outcome.waves[1].failed is True
    assert outcome.waves[1].questions == ("q2",)
    # 第一波确实激活过 target-a，但失败后整波作废，最终 candidates 清空。
    assert any(
        event["event"] == "activate" and event["node_id"] == "target-a"
        for event in events
    )
    assert outcome.candidates == ()
    assert outcome.target.id == "target-safe"
    assert outcome.fallback_reason == "error_fallback"
    assert outcome.failure_reason == outcome.waves[1].error
    assert outcome.kind == "error_fallback"
    final_event = events[-1]
    assert final_event["kind"] == "error_fallback"
    assert final_event["candidates"] == []


# ---------------------------------------------------------------------------
# 不变式 7：节点/边重排不改变结果与顺序
# ---------------------------------------------------------------------------


def test_node_and_edge_reordering_do_not_change_result():
    baseline, _jev, _baseline_events = run_evaluate("s1")
    reversed_root = {
        **POWER_GRAPH,
        "nodes": list(reversed(POWER_GRAPH["nodes"])),
        "edges": list(reversed(POWER_GRAPH["edges"])),
    }
    reordered, _jev2, reordered_events = run_evaluate("s1", tree=power_tree(root=reversed_root))

    assert reordered.target.id == baseline.target.id == "power-target"
    assert reordered.candidates == baseline.candidates
    assert [(wave.wave, wave.questions, wave.failed) for wave in reordered.waves] == [
        (wave.wave, wave.questions, wave.failed) for wave in baseline.waves
    ]
    assert [candidate.target_id for candidate in reordered.candidates] == ["power-target", "strong-target"]
    assert reordered.candidates == (
        Candidate("power-target", 5, "and-power", "in_1", 1),
        Candidate("strong-target", 3, "capability", "in_1", 1),
    )

    def ordering(events):
        return [
            (event["event"], event.get("node_id"), event.get("port"), event.get("status"))
            for event in events
            if event["event"] in {"slot", "gate", "activate", "question"}
        ]

    assert ordering(reordered_events) == ordering(_baseline_events)
    assert [event["node_id"] for event in reordered_events if event["event"] == "question"] == [
        "capability", "domain",
    ]


# ---------------------------------------------------------------------------
# 端口回填细节：score / noul、同波部分答案、多 true 槽位
# ---------------------------------------------------------------------------


def test_score_question_fills_value_and_feeds_numeric_gate():
    nodes = [
        node("start", "start"),
        node("q", "question", question_type="score", instructions="难度评分",
             criteria=[1, 10]),
        node("t-high", "target", priority=1, models=[{"model": "deepseek-v3"}]),
        node("t-safe", "target", priority=2, models=[{"model": "qwen3-8b"}]),
    ]
    edges = [
        edge("e-start-q", "start", "out", "q", "in_1"),
        edge("e-q-high", "q", "value", "t-high", "in_1", {"op": "gt", "value": 5}),
    ]
    outcome, _jev, events = evaluate_custom(
        nodes, edges,
        error_fallback="t-safe",
        scenario={"answers": {"q": {"value": 7.5}}},
    )
    question_event = next(event for event in events if event["event"] == "question")
    assert question_event["ports"] == {"value": 7.5}
    assert outcome.target.id == "t-high"
    assert outcome.candidates == (Candidate("t-high", 1, "q", "in_1", 1),)


class _PartialJev:
    """第一波两个问题只返回一个答案：整波必须作废，不能使用部分答案。"""

    def __init__(self):
        self.calls = []

    async def choose_many(self, settings, text, questions):
        self.calls.append(list(questions))
        node_id = next(iter(questions))
        return {node_id: NoulAnswer(0.9, 0.1, {"input_tokens": 20}, 12.0)}


def test_wave_with_partial_answers_is_voided_without_filling_ports():
    nodes = [
        node("start", "start"),
        node("q1", "question", question_type="noul", instructions="问题一"),
        node("q2", "question", question_type="noul", instructions="问题二"),
        node("t-safe", "target", priority=1, models=[{"model": "qwen3-8b"}]),
    ]
    edges = [
        edge("e-start-q1", "start", "out", "q1", "in_1"),
        edge("e-start-q2", "start", "out", "q2", "in_1"),
    ]
    tree = custom_tree(nodes, edges, error_fallback="t-safe")
    trace = []
    outcome = asyncio.run(GraphEvaluator(_PartialJev()).evaluate(
        make_snapshot(tree, periods=[]),
        RoutingRequest("请求"),
        now=NOW,
        trace_sink=trace,
    ))
    assert len(outcome.waves) == 1
    assert outcome.waves[0].failed is True
    assert outcome.waves[0].questions == ("q1", "q2")
    assert outcome.answers == {}
    assert not [event for event in trace if event["event"] == "question"]
    assert outcome.candidates == ()
    assert outcome.target.id == "t-safe"


def test_multiple_true_slots_use_smallest_numeric_slot_as_via():
    nodes = [
        node("start", "start"),
        node("cond", "condition", source="text", contains_any=["命中"]),
        node("t", "target", priority=1, models=[{"model": "deepseek-v3"}]),
        node("t-safe", "target", priority=2, models=[{"model": "qwen3-8b"}]),
    ]
    edges = [
        # in_2 来自 start（恒 true），in_1 来自 condition（true）→ 必须选 in_1。
        edge("e-start-t", "start", "out", "t", "in_2"),
        edge("e-cond-t", "cond", "out", "t", "in_1"),
    ]
    outcome, _jev, events = evaluate_custom(
        nodes, edges,
        error_fallback="t-safe",
        text="命中",
    )
    assert outcome.candidates == (Candidate("t", 1, "cond", "in_1", 0),)
    activation = next(
        event for event in events
        if event["event"] == "activate" and event["node_id"] == "t"
    )
    assert activation["via_port"] == "in_1"


# ---------------------------------------------------------------------------
# 优先级语义：数字越大越优先，候选按 priority 降序
# ---------------------------------------------------------------------------


def test_higher_priority_number_wins_and_candidates_sort_descending():
    nodes = [
        node("start", "start"),
        node("q", "question", question_type="choice", instructions="选择能力",
             options=choice_options("easy", "strong")),
        node("t-low", "target", priority=1, models=[{"model": "deepseek-v3"}]),
        node("t-high", "target", priority=5, models=[{"model": "qwen3-32b"}]),
    ]
    edges = [
        edge("e-start-q", "start", "out", "q", "in_1"),
        edge("e-q-low", "q", "selected.strong", "t-low", "in_1"),
        edge("e-q-high", "q", "selected.strong", "t-high", "in_1"),
    ]
    outcome, _jev, events = evaluate_custom(
        nodes, edges,
        error_fallback="t-low",
        scenario={"answers": {"q": {"probs": {"easy": 0.1, "strong": 0.9}}}},
    )

    assert outcome.target.id == "t-high"
    assert outcome.candidates == (
        Candidate("t-high", 5, "q", "in_1", 1),
        Candidate("t-low", 1, "q", "in_1", 1),
    )
    final_event = events[-1]
    assert final_event["priority"] == 5
    assert [candidate["target_id"] for candidate in final_event["candidates"]] == [
        "t-high", "t-low",
    ]
