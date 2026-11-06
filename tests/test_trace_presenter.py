"""Presentation-boundary tests: machine events/records -> console fields."""
from __future__ import annotations

from router.api.request_presenter import present_record
from router.api.trace_presenter import present_trace

_DISPLAY_FIELDS = {"phase", "label", "cell", "summary", "facts", "tone"}


def machine(event):
    return {key: value for key, value in event.items() if key not in _DISPLAY_FIELDS}


def test_present_trace_enriches_events_without_losing_machine_fields():
    events = [
        {"event": "time", "active_periods": ["peak"], "timezone": "Asia/Shanghai"},
        {"event": "condition", "node_id": "c", "source": "periods", "value": True,
         "matched": ["peak"], "periods": ["peak"]},
        {"event": "final", "target_id": "safe", "kind": "target",
         "priority": 1, "candidates": ["safe"]},
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

    assert presented[0]["phase"] == "period"
    assert presented[1]["phase"] == "period"
    assert presented[-1]["phase"] == "schedule"
    # Idempotent: an already-presented event is returned untouched.
    assert present_trace(presented, has_time_period=True) == presented


def test_present_trace_uses_snapshot_time_period_fact():
    event = {"event": "time", "active_periods": [], "timezone": "UTC"}
    # Explicit snapshot fact: no configured periods -> folded into the initial stage.
    assert present_trace([event], has_time_period=False)[0]["phase"] == "initial"
    assert present_trace([event], has_time_period=True)[0]["phase"] == "period"
    # Direct callers keep the condition-derived fallback.
    assert present_trace([event])[0]["phase"] == "initial"


def test_present_trace_renders_routing_failure_event():
    event = {"event": "routing_error", "stage": "schedule",
             "error_class": "no_available_backend", "message": "none"}
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["phase"] == "routing_error"
    assert presented["label"] == {"zh": "路由中止", "en": "Routing stopped"}
    assert presented["cell"] == {"zh": "路由失败", "en": "Routing failed"}
    assert presented["tone"] == "bad"
    assert presented["facts"] == [[{"zh": "原因", "en": "Reason"}, "no_available_backend"]]


def test_present_record_labels_hydrated_and_live_rows():
    hydrated = present_record({
        "id": "r1", "tree_id": "main", "selection_reason": "otherwise",
        "outcome_state": "completed", "upstream_status": 200,
        "routing_ms": 4.0, "total_ms": 12.0, "waves": 1,
    })
    assert hydrated["route_label"] == {"zh": "默认目标兜底", "en": "Default target fallback"}
    assert [pair[0]["en"] for pair in hydrated["metrics"]] == [
        "Upstream", "Routing", "Total", "Waves"]
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
