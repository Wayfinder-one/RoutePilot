"""Presentation-boundary tests: machine events/records -> console fields."""
from __future__ import annotations

from router.api.request_presenter import present_record, present_result_event
from router.api.trace_presenter import present_trace

_DISPLAY_FIELDS = {"phase", "label", "cell", "summary", "facts", "tone", "kind_label"}


def machine(event):
    return {key: value for key, value in event.items() if key not in _DISPLAY_FIELDS}


def test_present_trace_enriches_events_without_losing_machine_fields():
    events = [
        {"event": "time", "active_periods": ["peak"], "timezone": "Asia/Shanghai"},
        {"event": "condition", "node_id": "c", "source": "periods", "value": True,
         "matched": ["peak"], "periods": ["peak"]},
        {"event": "final", "target_id": "safe", "kind": "target",
         "priority": 1,
         "candidates": [{"target_id": "safe", "priority": 1,
                         "via_node": "start", "via_port": "in_1", "wave": 0}]},
        {"event": "scheduled", "model": "fast", "reason": "target", "dry_run": False},
    ]
    originals = [machine(event) for event in events]

    presented = present_trace(events, has_time_period=True)

    assert [machine(event) for event in presented] == originals
    for event in presented:
        assert _DISPLAY_FIELDS <= set(event)
        assert event["label"]["zh"] and event["label"]["en"]
        assert event["summary"]["zh"] and event["summary"]["en"]
        assert event["cell"] not in (None, "")
        assert isinstance(event["facts"], list)
        assert event["kind_label"]["zh"] and event["kind_label"]["en"]

    assert presented[0]["phase"] == "period"
    assert presented[1]["phase"] == "period"
    assert presented[-1]["phase"] == "schedule"
    # Idempotent: an already-presented event is returned untouched.
    assert present_trace(presented, has_time_period=True) == presented


def test_present_trace_accepts_explicit_time_period_override():
    event = {"event": "time", "active_periods": [], "timezone": "UTC"}
    # Explicit override: the caller already knows whether a period condition ran.
    assert present_trace([event], has_time_period=False)[0]["phase"] == "initial"
    assert present_trace([event], has_time_period=True)[0]["phase"] == "period"
    # Default: derive from this graph's own events.
    assert present_trace([event])[0]["phase"] == "initial"


def test_present_trace_derives_time_phase_from_period_conditions_only():
    time_event = {"event": "time", "active_periods": [], "timezone": "UTC"}
    text_condition = {"event": "condition", "node_id": "c", "source": "text",
                      "value": True, "matched": ["x"]}
    period_condition = {"event": "condition", "node_id": "p", "source": "periods",
                        "value": True, "matched": ["peak"], "periods": ["peak"]}

    # Non-period conditions do not promote the time event.
    assert present_trace([time_event, text_condition])[0]["phase"] == "initial"
    # A real period condition does.
    assert present_trace([time_event, period_condition])[0]["phase"] == "period"


def test_present_trace_renders_routing_failure_event():
    event = {"event": "routing_error", "stage": "schedule",
             "error_class": "no_available_backend", "message": "none"}
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["phase"] == "routing_error"
    assert presented["label"] == {"zh": "路由中止", "en": "Routing stopped"}
    assert presented["cell"] == {"zh": "路由失败", "en": "Routing failed"}
    assert presented["tone"] == "bad"
    assert presented["facts"] == [
        [{"zh": "阶段", "en": "Stage"}, "schedule"],
        [{"zh": "原因", "en": "Reason"}, "no_available_backend"],
        [{"zh": "消息", "en": "Message"}, "none"],
    ]
    assert presented["kind_label"] == {"zh": "错误", "en": "Error"}


def test_present_record_labels_hydrated_and_live_rows():
    hydrated = present_record({
        "id": "r1", "tree_id": "main", "selection_reason": "otherwise",
        "outcome_state": "completed", "upstream_status": 200,
        "routing_ms": 4.0, "total_ms": 12.0, "waves": 1,
    })
    assert hydrated["route_label"] == {"zh": "默认目标兜底", "en": "Default target fallback"}
    # 计算日志只展示路由决策成本：决策耗时 / 决策模型耗时 / 轮次。
    assert [pair[0]["en"] for pair in hydrated["metrics"]] == [
        "Decision time", "Decision model time", "Waves"]
    assert hydrated["metrics"][0][1] == "4ms"
    assert [pair[0]["en"] for pair in hydrated["fields"]][:2] == [
        "Request ID", "Decision group"]

    sticky = present_record({"outcome_state": "completed", "selection_reason": "sticky"})
    assert sticky["route_label"] == {"zh": "会话粘性直达", "en": "Session affinity"}

    live = present_record({
        "outcome_state": "completed", "selection_reason": "error_fallback",
        "trace": [{"event": "final", "kind": "otherwise", "target_id": "safe"}],
    })
    assert live["route_label"] == {"zh": "默认目标兜底", "en": "Default target fallback"}

    broken = present_record({"outcome_state": "routing_error"})
    assert broken["route_label"] == {"zh": "路由中止", "en": "Routing stopped"}


def test_present_trace_formats_time_window_and_condition_values():
    events = [
        {"event": "time", "active_periods": ["peak", "normal"], "timezone": "Asia/Shanghai"},
        {"event": "window", "tokens_before": 18, "tokens_after": 18, "truncated": False},
        {"event": "condition", "node_id": "cond-text", "source": "text",
         "matched": ["10kV", "跳闸"], "value": True},
    ]
    time_event, window_event, condition_event = present_trace(events)
    assert time_event["kind_label"] == {"zh": "时段", "en": "Time"}
    assert time_event["summary"]["zh"] == "Asia/Shanghai，生效 peak、normal"
    assert time_event["facts"][1] == [
        {"zh": "生效时段", "en": "Active periods"}, {"zh": "peak、normal", "en": "peak, normal"}]
    assert window_event["kind_label"] == {"zh": "窗口", "en": "Window"}
    assert window_event["summary"]["zh"] == "输入窗口：18 → 18 tok"
    assert window_event["facts"][0] == [{"zh": "Token", "en": "Tokens"}, "18 → 18"]
    assert condition_event["kind_label"] == {"zh": "条件", "en": "Condition"}
    assert condition_event["summary"]["zh"] == "cond-text：命中，匹配 10kV、跳闸"
    assert condition_event["facts"][1] == [
        {"zh": "匹配", "en": "Matched"}, {"zh": "10kV、跳闸", "en": "10kV, 跳闸"}]


def test_present_trace_formats_question_values_as_raw_probabilities():
    event = {
        "event": "question", "wave": 1, "node_id": "capability",
        "selected": "strong", "p_top": 0.84, "margin": 0.72, "duration_ms": 9.0,
        "ports": {
            "prob.strong": 0.84, "prob.easy": 0.16,
            "selected.strong": True, "selected.easy": False,
            "p_top": 0.84, "margin": 0.72,
        },
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["kind_label"] == {"zh": "问题", "en": "Question"}
    assert presented["summary"]["zh"] == "capability：strong（p_top 0.84，margin 0.72）"
    assert presented["summary"]["en"] == "capability: strong (p_top 0.84, margin 0.72)"
    assert presented["facts"][0] == [
        {"zh": "概率", "en": "Probabilities"}, "strong 0.84 · easy 0.16"]
    assert presented["facts"][1] == [{"zh": "耗时", "en": "Duration"}, "9 ms"]


def test_present_trace_formats_noul_and_score_questions():
    noul = present_trace([{
        "event": "question", "wave": 1, "node_id": "domain", "duration_ms": 7.0,
        "ports": {"yes": 0.92, "no": 0.08},
    }], has_time_period=False)[0]
    assert noul["summary"]["zh"] == "domain：P(yes) 0.92，P(no) 0.08"
    assert noul["facts"][0][1] == "yes 0.92 · no 0.08"

    score = present_trace([{
        "event": "question", "wave": 1, "node_id": "complexity", "duration_ms": 4.0,
        "ports": {"value": 0.7},
    }], has_time_period=False)[0]
    assert score["summary"]["zh"] == "complexity：value 0.7"
    assert score["facts"][0] == [{"zh": "值", "en": "Value"}, "0.7"]


def test_present_trace_formats_slot_comparison_and_boolean_passthrough():
    numeric = present_trace([{
        "event": "slot", "wave": 1, "edge_id": "e1", "node_id": "power-target",
        "port": "in_1", "source": "capability", "source_port": "selected.strong",
        "raw": 0.84, "condition": {"op": "gt", "value": 0.65}, "status": "true",
    }], has_time_period=False)[0]
    assert numeric["kind_label"] == {"zh": "槽位", "en": "Slot"}
    assert numeric["summary"]["zh"] == "capability.selected.strong 0.84 > 0.65 → true"
    assert numeric["facts"] == [[{"zh": "目标", "en": "Target"}, "power-target.in_1"]]

    boolean = present_trace([{
        "event": "slot", "wave": 0, "edge_id": "e2", "node_id": "q", "port": "in_1",
        "source": "start", "source_port": "out", "raw": True, "condition": None,
        "status": "true",
    }], has_time_period=False)[0]
    assert boolean["summary"]["zh"] == "start.out true → true"

    invalid = present_trace([{
        "event": "slot", "wave": 1, "edge_id": "e3", "node_id": "t", "port": "in_1",
        "source": "q", "source_port": "value", "raw": 0.5, "condition": None,
        "status": "invalid",
    }], has_time_period=False)[0]
    assert invalid["summary"]["zh"] == "q.value 0.5 （未配置阈值） → invalid"
    assert invalid["summary"]["en"] == "q.value 0.5 (no threshold) → invalid"


def test_present_trace_renders_unknown_slots_without_a_threshold_note():
    event = {
        "event": "slot", "wave": 0, "edge_id": "e4", "node_id": "capability",
        "port": "in_1", "source": "capability", "source_port": "selected.standard",
        "raw": None, "condition": None, "status": "unknown",
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["summary"]["zh"] == "capability.selected.standard — → unknown"
    assert presented["summary"]["en"] == "capability.selected.standard — → unknown"


def test_present_trace_formats_gate_operator_and_inputs():
    event = {
        "event": "gate", "wave": 1, "node_id": "and-power", "operator": "and",
        "value": True,
        "inputs": [
            {"port": "in_1", "raw": 0.84, "condition": {"op": "gt", "value": 0.65},
             "status": "true"},
            {"port": "in_2", "raw": 0.92, "condition": {"op": "gt", "value": 0.7},
             "status": "true"},
        ],
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["kind_label"] == {"zh": "门", "en": "Gate"}
    assert presented["summary"]["zh"] == "and-power：true AND true → true"
    assert presented["facts"] == [
        [{"zh": "in_1", "en": "in_1"}, "0.84 > 0.65 → true"],
        [{"zh": "in_2", "en": "in_2"}, "0.92 > 0.7 → true"],
    ]


def test_present_trace_formats_unary_gate_with_operator():
    event = {"event": "gate", "wave": 1, "node_id": "unary-cap", "operator": "custom",
             "value": False,
             "inputs": [{"port": "in_1", "raw": True, "condition": None, "status": "true"}]}
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["summary"]["zh"] == "unary-cap：CUSTOM true → false"
    assert presented["summary"]["en"] == "unary-cap: CUSTOM true → false"


def test_present_trace_formats_wave_duration_and_activation():
    wave = present_trace([{
        "event": "wave", "wave": 1, "questions": ["capability", "domain"],
        "duration_ms": 9.0, "failed": False, "error": None,
    }], has_time_period=False)[0]
    assert wave["kind_label"] == {"zh": "轮次", "en": "Wave"}
    assert wave["summary"]["zh"] == "第 1 轮 JEV：2 个问题，耗时 9 ms"

    activate = present_trace([{
        "event": "activate", "wave": 1, "node_id": "power-target", "via_port": "in_1",
    }], has_time_period=False)[0]
    assert activate["kind_label"] == {"zh": "激活", "en": "Activation"}
    assert activate["summary"]["zh"] == "power-target（via in_1）"
    assert activate["facts"] == [
        [{"zh": "节点", "en": "Node"}, "power-target"],
        [{"zh": "入口", "en": "Input"}, "in_1"],
    ]


def test_present_trace_keeps_raw_probability_precision():
    presented = present_trace([{
        "event": "question", "wave": 1, "node_id": "capability",
        "p_top": 0.145, "margin": 0.285, "duration_ms": 5.0,
        "ports": {"prob.a": 0.145, "prob.b": 0.855,
                  "selected.a": True, "selected.b": False,
                  "p_top": 0.145, "margin": 0.285},
    }], has_time_period=False)[0]
    assert presented["summary"]["zh"] == "capability：a（p_top 0.145，margin 0.285）"
    assert presented["facts"][0] == [
        {"zh": "概率", "en": "Probabilities"}, "b 0.855 · a 0.145"]


def test_present_trace_formats_final_priority_decision():
    event = {
        "event": "final", "target_id": "power-target", "kind": "target", "priority": 5,
        "candidates": [
            {"target_id": "power-target", "priority": 5, "via_node": "and-power",
             "via_port": "in_1", "wave": 1},
            {"target_id": "strong-target", "priority": 3, "via_node": "capability",
             "via_port": "in_1", "wave": 1},
        ],
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["kind_label"] == {"zh": "候选", "en": "Candidates"}
    assert presented["summary"]["zh"] == (
        "power-target p5、strong-target p3 → power-target 胜出")
    assert presented["facts"] == [
        [{"zh": "power-target", "en": "power-target"},
         {"zh": "p5 · via and-power.in_1 · 第 1 轮 ✓",
          "en": "p5 · via and-power.in_1 · wave 1 ✓"}],
        [{"zh": "strong-target", "en": "strong-target"},
         {"zh": "p3 · via capability.in_1 · 第 1 轮",
          "en": "p3 · via capability.in_1 · wave 1"}],
    ]


def test_present_trace_formats_fallback_and_no_candidate_final():
    fallback = present_trace([{
        "event": "fallback", "target_id": "safe-target", "reason": "timeout",
    }], has_time_period=False)[0]
    assert fallback["summary"]["zh"] == "JEV 失败，启用错误兜底 safe-target（timeout）"
    assert fallback["facts"] == [
        [{"zh": "目标", "en": "Target"}, "safe-target"],
        [{"zh": "原因", "en": "Reason"}, "timeout"],
    ]

    final = present_trace([{
        "event": "final", "target_id": "safe-target", "kind": "otherwise",
        "priority": 1, "candidates": [],
    }], has_time_period=False)[0]
    assert final["summary"]["zh"] == "无候选，使用兜底目标 safe-target"
    assert final["facts"][0] == [{"zh": "目标", "en": "Target"}, "safe-target"]


def test_present_trace_formats_schedule_and_scheduled_values():
    schedule = present_trace([{
        "event": "schedule", "target_id": "power-target",
        "configured_models": ["qwen3-8b", "qwen3-32b"],
        "registered_models": ["qwen3-8b", "qwen3-32b"],
        "available_models": ["qwen3-8b"],
        "unavailable": {"qwen3-32b": "breaker_open"},
        "inflight": {"qwen3-8b": 1},
        "sticky": {"status": "miss", "preferred_model": "qwen3-8b"},
    }], has_time_period=False)[0]
    assert schedule["kind_label"] == {"zh": "调度", "en": "Schedule"}
    assert schedule["summary"]["zh"] == "目标 power-target：1 个可用模型"
    assert schedule["facts"][0] == [
        {"zh": "可用模型", "en": "Available models"}, "qwen3-8b"]
    assert schedule["facts"][2] == [{"zh": "在途", "en": "In flight"}, "qwen3-8b: 1"]
    assert schedule["facts"][3] == [
        {"zh": "会话粘性", "en": "Session affinity"}, "miss → qwen3-8b"]

    scheduled = present_trace([{
        "event": "scheduled", "model": "qwen3-8b", "reason": "target", "dry_run": False,
    }], has_time_period=False)[0]
    assert scheduled["summary"]["zh"] == "最终调度 qwen3-8b（target）"
    assert scheduled["facts"] == [
        [{"zh": "模型", "en": "Model"}, "qwen3-8b"],
        [{"zh": "原因", "en": "Reason"}, "target"],
    ]


def test_present_result_event_has_outcome_kind_label():
    outcome = present_result_event({
        "outcome_state": "completed", "model": "qwen3-8b",
        "upstream_status": 200, "total_ms": 12.0,
    })
    assert outcome["kind_label"] == {"zh": "结果", "en": "Outcome"}
    # 上游状态与端到端耗时不属于路由决策，不再进入执行结果事实。
    assert outcome["facts"] == [[{"zh": "模型", "en": "Model"}, "qwen3-8b"]]
