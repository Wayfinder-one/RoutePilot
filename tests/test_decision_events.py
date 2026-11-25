"""Self-contained decision events: complete current graph, no snapshot ids, no secrets."""
import json

import pytest

from router.decision_events import (
    EVENT_TYPE,
    build_decision_event,
    validate_decision_event,
)
from tests.decision_fixtures import (
    decision_record,
    decision_snapshot,
    valid_event,
)


def test_event_embeds_current_graph_and_decision_without_snapshot_ids():
    event = build_decision_event(
        decision_record("r1"), decision_snapshot(), department="财务部", requested_model="auto")
    assert event["event_type"] == "routepilot.decision"
    assert event["graph"]["id"] == "main"
    assert event["graph"]["root"]["nodes"]
    assert event["request"] == {
        "text": "秘密请求文本", "requested_model": "auto",
        "department": "财务部", "session_hash": "h" * 64,
    }
    assert event["decision"]["model"] == "fast"
    assert event["trace"]
    assert "graph_snapshot_id" not in event
    assert "snapshot_revision" not in event["decision"]


def test_event_validation_rejects_missing_graph_and_secret_fields():
    event = build_decision_event(decision_record("r1"), decision_snapshot(), department="", requested_model="auto")
    event["graph"]["jev"]["api_key"] = "forbidden"
    with pytest.raises(ValueError, match="secret"):
        validate_decision_event(event)
    del event["graph"]
    with pytest.raises(ValueError, match="graph"):
        validate_decision_event(event)


def test_graph_document_is_complete_and_carries_no_key_values():
    event = valid_event()
    graph = event["graph"]
    # 完整当前图：envelope + 画布 + 自包含运行字段（非密钥）。
    assert {"id", "root", "canvas", "backends", "jev", "jev_window",
            "time_periods", "tree_revision", "updated_at"} <= set(graph)
    assert graph["root"]["id"] == "root"
    assert graph["backends"]["models"][0]["id"] == "fast"
    assert graph["jev"] is not None
    # key_env 只是环境变量名（允许保留），任何键名都不能是密钥字段。
    assert graph["jev"]["key_env"] == "ROUTER_JEV_API_KEY"
    assert "api_key" not in json.dumps(event, ensure_ascii=False)


def test_event_is_json_serialisable_so_one_post_carries_one_object():
    event = valid_event()
    assert json.loads(json.dumps(event, ensure_ascii=False)) == event


def test_routing_error_decision_keeps_the_selected_graph():
    event = build_decision_event(
        decision_record("r1", error_class="routing_failed",
                        model=None, upstream_model=None),
        decision_snapshot(), department="", requested_model="auto")
    assert event["decision"]["status"] == "routing_error"
    assert event["decision"]["model"] is None
    assert event["graph"]["id"] == "main"


def test_validate_rejects_unsupported_event_type_or_version():
    event = valid_event()
    event["event_type"] = "routepilot.other"
    with pytest.raises(ValueError, match="unsupported"):
        validate_decision_event(event)
    event = valid_event()
    event["schema_version"] = 2
    with pytest.raises(ValueError, match="unsupported"):
        validate_decision_event(event)


def test_validate_rejects_secrets_at_any_depth_including_lists():
    event = valid_event()
    event["trace"].append({"event": "headers",
                           "request_headers": {"Authorization": "Bearer secret"}})
    with pytest.raises(ValueError, match="secret"):
        validate_decision_event(event)
    event = valid_event()
    event["answers"] = {"q": [{"key_present": True}]}
    with pytest.raises(ValueError, match="secret"):
        validate_decision_event(event)


def test_validate_rejects_non_serialisable_and_non_object_events():
    with pytest.raises(ValueError, match="graph"):
        validate_decision_event([])
    event = valid_event()
    event["decision"]["routing_ms"] = float("nan")
    with pytest.raises(ValueError):
        validate_decision_event(event)


def test_event_type_constant_is_the_console_contract():
    assert EVENT_TYPE == "routepilot.decision"
