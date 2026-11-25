"""Shared decision-event fixtures for the core/peripheral split tasks.

One real snapshot/record/event shape (no hand-rolled per-test variants) so the
kernel, the sink and the later peripheral log store all consume the same
contract.  ``async_test`` wraps coroutines with ``asyncio.run`` instead of
adding a pytest-asyncio dependency.
"""
import asyncio
import functools

from router.core_config import CoreSettings
from router.decision_events import build_decision_event
from router.routing.snapshot import build_snapshot
from router.routing.tree import tree_from_dict
from router.upstream.catalog import Backend
from tests.routing_fixtures import tree_dict


def async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def decision_snapshot():
    tree = tree_from_dict(tree_dict("main", enabled=True, default=True))
    backend = Backend(
        url="http://upstream.test/v1/chat/completions", headers={},
        upstream_name="real-fast", provider="local")
    return build_snapshot(
        tree=tree, backends={"fast": backend}, time_periods=[], jev=None,
        capabilities={"fast": "快速模型"})


def decision_record(event_id="e1", **overrides):
    value = {
        "id": event_id, "ts": "2026-11-25T10:00:00+08:00",
        "text": "秘密请求文本", "model": "fast", "upstream_model": "real-fast",
        "target_id": "safe", "tree_id": "main", "tree_revision": "t" * 64,
        "graph_selection": "default", "selection_reason": "target",
        "fallback_reason": None, "sticky_hit": 0, "session_hash": "h" * 64,
        "policy_path": "start/safe", "reasoning": None,
        "reasoning_warning": None, "routing_ms": 15.0, "waves": 0,
        "trace": [{"event": "final", "kind": "target", "model": "fast"}],
        "answers": {}, "node_labels": {},
    }
    value.update(overrides)
    return value


def valid_event(id="e1"):
    return build_decision_event(
        decision_record(id), decision_snapshot(),
        department="", requested_model="auto")


def decision_payload(**overrides):
    value = {
        "decision_id": "e1", "model": "real-fast", "logical_model": "fast",
        "provider": {"name": "local", "base_url": "http://upstream.test/v1",
                     "reasoning_format": "openai"},
        "reasoning": None, "reasoning_warning": None,
        "tree_id": "main", "tree_revision": "t" * 64,
        "target_id": "safe", "selection_reason": "target",
        "graph_selection": "default",
    }
    value.update(overrides)
    return value


def core_settings(tmp_path):
    return CoreSettings(graph_dir=str(tmp_path / "graphs"))
