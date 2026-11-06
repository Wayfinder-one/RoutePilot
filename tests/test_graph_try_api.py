"""`POST /admin/try`：完整 graph envelope 草稿试算（spec §4.3，Task 7）。

覆盖 root 优先 envelope 合并矩阵、root-only 默认值、draft 标记、三类 answers 的
序列化、complete outcome（candidates/waves/selection_reason/fallback_reason）、
503 语义，以及草稿试算的副作用不变量（不落库、不发布 snapshot、不写
affinity/inflight、不读已存 root）。
"""
from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.jev.window import MIN_INPUT_TOKENS, estimate_tokens
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.store.db import connect
from router.store.repositories import SettingsRepo
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import graph_dict, huge_chain_graph, test_settings


_DISPLAY_FIELDS = {"phase", "label", "cell", "summary", "facts", "tone"}


def _machine_event(event):
    return {key: value for key, value in event.items() if key not in _DISPLAY_FIELDS}
from tests.test_graph_evaluator import (
    POWER_GRAPH,
    SCENARIO_BY_ID,
    SCENARIO_DATA,
    FakeJev,
    RecordingAffinity,
    RecordingBalancer,
    run_scenario,
)

MODELS = ("qwen3-8b", "qwen3-32b", "deepseek-v3")


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"answers": {}, "usage": {}})


@pytest.fixture
def app(tmp_path):
    return create_app(
        # 预置决策服务配置：graph 引擎要求 snapshot.jev 存在（网络调用由 FakeJev 接管）。
        test_settings(tmp_path, jev_url="http://jev.test", jev_model="d1-2B"),
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ── graph fixtures ─────────────────────────────────────────────────────

def three_answer_graph() -> dict:
    """一个波次内同时触发 choice / noul / score 三类问题与三个候选 target。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
            {"id": "q-choice", "type": "question", "question_type": "choice",
             "instructions": "选择能力", "position": {"x": 180, "y": -80},
             "options": [
                 {"key": "easy", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "q-noul", "type": "question", "question_type": "noul",
             "instructions": "是否需要电力知识？", "position": {"x": 180, "y": 40}},
            {"id": "q-score", "type": "question", "question_type": "score",
             "instructions": "难度评分", "criteria": [1, 10], "position": {"x": 180, "y": 160}},
            {"id": "t-choice", "type": "target", "priority": 4, "title": "轻量目标",
             "models": [{"model": "qwen3-8b", "reasoning": None}], "position": {"x": 420, "y": -80}},
            {"id": "t-noul", "type": "target", "priority": 3,
             "models": [{"model": "qwen3-32b", "reasoning": None}], "position": {"x": 420, "y": 40}},
            {"id": "t-score", "type": "target", "priority": 2,
             "models": [{"model": "deepseek-v3", "reasoning": None}], "position": {"x": 420, "y": 160}},
            {"id": "safe", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}], "position": {"x": 420, "y": 280}},
        ],
        "edges": [
            {"id": "e-start-choice", "source": "start", "source_port": "out",
             "target": "q-choice", "target_port": "in_1", "condition": None},
            {"id": "e-start-noul", "source": "start", "source_port": "out",
             "target": "q-noul", "target_port": "in_1", "condition": None},
            {"id": "e-start-score", "source": "start", "source_port": "out",
             "target": "q-score", "target_port": "in_1", "condition": None},
            {"id": "e-choice-target", "source": "q-choice", "source_port": "selected.strong",
             "target": "t-choice", "target_port": "in_1", "condition": None},
            {"id": "e-noul-target", "source": "q-noul", "source_port": "yes",
             "target": "t-noul", "target_port": "in_1", "condition": {"op": "gt", "value": 0.5}},
            {"id": "e-score-target", "source": "q-score", "source_port": "value",
             "target": "t-score", "target_port": "in_1", "condition": {"op": "gte", "value": 5}},
        ],
        "error_fallback": "safe",
    }


THREE_ANSWERS = {
    "answers": {
        "q-choice": {"probs": {"easy": 0.15, "strong": 0.85}},
        "q-noul": {"yes": 0.9},
        "q-score": {"value": 7.5},
    },
}


class CapturingService:
    """记录每次 route 的 snapshot（合并结果），其余委托真实组合根。"""

    def __init__(self, inner):
        self.inner = inner
        self.snapshots = []
        self.calls = 0

    async def route(self, snapshot, request, *, now=None, dry_run=False, trace_sink=None):
        self.calls += 1
        self.snapshots.append(snapshot)
        return await self.inner.route(
            snapshot, request, now=now, dry_run=dry_run, trace_sink=trace_sink,
        )


def install_engine(app, scenario=None):
    jev = FakeJev(THREE_ANSWERS if scenario is None else scenario)
    affinity = RecordingAffinity()
    balancer = RecordingBalancer(app.state.breaker)
    app.state.affinity = affinity
    app.state.balancer = balancer
    app.state.routing_service = CapturingService(
        RoutingService(GraphEvaluator(jev), ModelScheduler(affinity, balancer)),
    )
    return app.state.routing_service, jev, balancer, affinity


def row_counts(app) -> dict:
    conn = connect(app.state.settings.db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("changes", "config_revisions", "request_logs")
        }
    finally:
        conn.close()


def create_stored_tree(client, *, root=None, tree_id="main", timezone="Asia/Shanghai",
                       sticky=None, name="测试图"):
    response = client.post("/admin/trees", json={
        "id": tree_id,
        "name": name,
        "root": graph_dict() if root is None else root,
        "timezone": timezone,
        "sticky": sticky or {"enabled": True, "ttl_seconds": 1800},
    })
    assert response.status_code == 201, response.text
    return response.json()["tree"]


# ── 副作用不变量 ───────────────────────────────────────────────────────

def test_root_draft_is_side_effect_free_and_marks_draft(client, app):
    engine, _jev, balancer, affinity = install_engine(app)
    before_snapshot = app.state.snapshot
    before_revision = before_snapshot.snapshot_revision
    before_counts = row_counts(app)

    response = client.post("/admin/try", json={
        "root": three_answer_graph(),
        "text": "抢修工单",
        "session_id": "try-session",
        "sticky": {"enabled": True, "ttl_seconds": 1800},
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is True
    assert body["tree_id"] == "draft"
    assert body["outcome"]["target_id"] == "t-choice"
    assert engine.calls == 1

    # 不发布 snapshot（对象与 revision 都不变）
    assert app.state.snapshot is before_snapshot
    assert app.state.snapshot.snapshot_revision == before_revision
    # 不落库
    assert row_counts(app) == before_counts
    # 不写粘性：只读 affinity（dry-run touch=False），绝不 put
    assert len(affinity) == 0
    assert affinity.puts == []
    assert affinity.gets and all(item["touch"] is False for item in affinity.gets)
    # dry-run 不计 inflight
    assert balancer.calls and all(item["dry_run"] is True for item in balancer.calls)
    assert all(balancer.inflight(model) == 0 for model in MODELS)


def test_root_only_defaults_and_does_not_read_stored_trees(client, app, monkeypatch):
    engine, _jev, _balancer, _affinity = install_engine(app)

    def forbidden(self, tree_id):
        raise AssertionError("root-only try must not read stored routing trees")

    monkeypatch.setattr(RoutingTreeRepo, "get", forbidden)

    response = client.post("/admin/try", json={"root": three_answer_graph(), "text": "你好"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is True
    assert body["tree_id"] == "draft"
    draft = engine.snapshots[0].tree
    assert draft.id == "draft"
    assert draft.name == ""
    assert draft.timezone == "Asia/Shanghai"
    assert draft.sticky.enabled is False
    assert draft.sticky.ttl_seconds == 1800


def test_try_uses_live_jev_window_without_publishing(client, app, monkeypatch):
    engine, jev, _balancer, _affinity = install_engine(app)
    conn = connect(app.state.settings.db_path)
    try:
        SettingsRepo(conn).set("jev_input_tokens", str(MIN_INPUT_TOKENS))
        conn.commit()
    finally:
        conn.close()
    published_before = app.state.snapshot.snapshot_revision

    long_text = "国网营销数字化改造项目背景。" * 500
    response = client.post("/admin/try", json={"root": three_answer_graph(), "text": long_text})

    assert response.status_code == 200, response.text
    window = next(event for event in response.json()["trace"] if event["event"] == "window")
    assert window["truncated"] is True
    assert window["tokens_after"] <= MIN_INPUT_TOKENS
    assert window["tokens_before"] > MIN_INPUT_TOKENS
    assert estimate_tokens(jev.calls[0]["text"]) <= MIN_INPUT_TOKENS
    assert app.state.snapshot.snapshot_revision == published_before


# ── envelope 合并矩阵 ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("fragment", "expected_timezone", "expected_sticky"),
    [
        ({}, "Asia/Shanghai", {"enabled": True, "ttl_seconds": 1800}),
        ({"timezone": "UTC"}, "UTC", {"enabled": True, "ttl_seconds": 1800}),
        ({"sticky": {"enabled": False, "ttl_seconds": 60}}, "Asia/Shanghai",
         {"enabled": False, "ttl_seconds": 60}),
        ({"timezone": "UTC", "sticky": {"enabled": False, "ttl_seconds": 60}}, "UTC",
         {"enabled": False, "ttl_seconds": 60}),
    ],
)
def test_envelope_explicit_fields_override_and_missing_fields_inherit(
    client, app, fragment, expected_timezone, expected_sticky,
):
    engine, _jev, _balancer, _affinity = install_engine(app)
    create_stored_tree(client)

    response = client.post("/admin/try", json={
        "root": three_answer_graph(),
        "tree_id": "main",
        "text": "抢修",
        **fragment,
    })

    assert response.status_code == 200, response.text
    body = response.json()
    draft = engine.snapshots[0].tree
    assert draft.id == "main"
    assert draft.name == "测试图"
    assert draft.timezone == expected_timezone
    assert {"enabled": draft.sticky.enabled, "ttl_seconds": draft.sticky.ttl_seconds} == expected_sticky
    # root 覆盖已存图：不继承已存 root
    assert {node.id for node in draft.root.nodes} == {
        "start", "q-choice", "q-noul", "q-score",
        "t-choice", "t-noul", "t-score", "safe",
    }
    assert body["draft"] is True
    assert body["trace"][0]["timezone"] == expected_timezone


def test_tree_id_only_uses_stored_envelope_and_is_not_draft(client, app):
    engine, _jev, balancer, affinity = install_engine(app)
    create_stored_tree(
        client, root=three_answer_graph(), timezone="UTC",
        sticky={"enabled": True, "ttl_seconds": 1800},
    )

    response = client.post("/admin/try", json={
        "tree_id": "main", "text": "抢修", "session_id": "tree-session",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is False
    assert body["tree_id"] == "main"
    assert body["outcome"]["target_id"] == "t-choice"
    draft = engine.snapshots[0].tree
    assert draft.id == "main"
    assert draft.timezone == "UTC"
    assert draft.sticky.enabled is True
    # dry-run：即使 sticky enabled + session_id 也不写粘性、不计 inflight
    assert affinity.puts == []
    assert len(affinity) == 0
    assert all(item["dry_run"] is True for item in balancer.calls)
    assert app.state.snapshot.tree is None


def test_try_unknown_tree_id_is_404_and_skips_engine(client, app):
    engine, *_rest = install_engine(app)
    response = client.post("/admin/try", json={"tree_id": "nope", "text": "你好"})
    assert response.status_code == 404
    assert engine.calls == 0


# ── 响应序列化 ─────────────────────────────────────────────────────────

def test_try_serializes_all_answer_types_and_complete_outcome(client, app):
    _engine, _jev, _balancer, _affinity = install_engine(app)

    response = client.post("/admin/try", json={"root": three_answer_graph(), "text": "抢修工单"})

    assert response.status_code == 200, response.text
    body = response.json()
    answers = body["answers"]
    assert set(answers) == {"q-choice", "q-noul", "q-score"}

    choice = answers["q-choice"]
    assert set(choice) == {"probabilities", "ranking", "p_top", "margin", "usage", "duration_ms"}
    assert choice["probabilities"] == {"easy": 0.15, "strong": 0.85}
    assert choice["ranking"] == ["strong", "easy"]
    assert choice["p_top"] == pytest.approx(0.85)
    assert choice["margin"] == pytest.approx(0.7)
    assert choice["usage"] == {"input_tokens": 20}
    assert choice["duration_ms"] == 12.0

    noul = answers["q-noul"]
    assert set(noul) == {"yes", "no", "usage", "duration_ms"}
    assert noul["yes"] == pytest.approx(0.9)
    assert noul["no"] == pytest.approx(0.1)
    assert noul["usage"] == {"input_tokens": 20}
    assert noul["duration_ms"] == 12.0

    score = answers["q-score"]
    assert set(score) == {"value", "usage", "duration_ms"}
    assert score["value"] == 7.5
    assert score["usage"] == {"input_tokens": 20}
    assert score["duration_ms"] == 12.0

    outcome = body["outcome"]
    assert outcome["type"] == "hit"
    assert outcome["target_id"] == "t-choice"
    assert outcome["model"] == "qwen3-8b"
    assert outcome["policy_path"] == "t-choice"
    assert outcome["selection_reason"] == "target"
    assert outcome["fallback_reason"] is None
    assert outcome["candidates"] == [
        {"target_id": "t-choice", "priority": 4, "via_node": "q-choice", "via_port": "in_1", "wave": 1},
        {"target_id": "t-noul", "priority": 3, "via_node": "q-noul", "via_port": "in_1", "wave": 1},
        {"target_id": "t-score", "priority": 2, "via_node": "q-score", "via_port": "in_1", "wave": 1},
    ]
    assert outcome["waves"] == [{
        "wave": 1,
        "questions": ["q-choice", "q-noul", "q-score"],
        "duration_ms": 12.0,
        "failed": False,
        "error": None,
    }]
    assert _machine_event(body["trace"][-1]) == {
        "event": "scheduled", "model": "qwen3-8b", "reason": "target", "dry_run": True,
    }
    assert body["draft"] is True
    assert isinstance(body["warnings"], list)
    assert all(set(issue) == {"code", "zh", "en", "node_id", "edge_id", "port", "level"}
               for issue in body["warnings"])


def test_try_fixed_peak_scenario_matches_engine(client, app):
    periods = client.put("/admin/time-periods", json={"periods": SCENARIO_DATA["periods"]})
    assert periods.status_code == 200, periods.text
    jev = client.put("/admin/jev", json={
        "base_url": "http://jev.test", "model": "NeoHorse-Jev-4B", "timeout_seconds": 5,
        "instructions": DEFAULT_INSTRUCTIONS, "state_template": DEFAULT_STATE_TEMPLATE,
    })
    assert jev.status_code == 200, jev.text
    install_engine(app, SCENARIO_BY_ID["s3"])
    scenario = SCENARIO_BY_ID["s3"]

    response = client.post("/admin/try", json={
        "root": copy.deepcopy(POWER_GRAPH),
        "text": scenario["text"],
        "now": scenario["now"],
    })

    assert response.status_code == 200, response.text
    body = response.json()
    expected = scenario["expected"]
    assert body["draft"] is True
    assert body["outcome"]["target_id"] == expected["final"] == "deep-target"
    assert len(body["outcome"]["candidates"]) == expected["candidates"] == 2
    assert len(body["outcome"]["waves"]) == expected["waves"] == 2
    assert body["outcome"]["selection_reason"] == "target"
    assert body["outcome"]["fallback_reason"] is None
    assert _machine_event(body["trace"][0]) == {
        "event": "time", "active_periods": ["peak"], "timezone": "Asia/Shanghai",
    }
    assert body["trace"][-1]["event"] == "scheduled"
    assert body["trace"][-1]["dry_run"] is True
    # 启用级校验只作为 warnings 返回：未登记模型不阻塞试算
    assert any(issue["code"] == "unknown_model" for issue in body["warnings"])

    reference, *_rest = run_scenario("s3")
    assert body["outcome"]["target_id"] == reference.target.id
    assert [item["target_id"] for item in body["outcome"]["candidates"]] == [
        candidate.target_id for candidate in reference.candidates
    ]
    assert body["outcome"]["waves"][-1]["questions"] == list(reference.waves[-1].questions)


# ── 422 / 503 ─────────────────────────────────────────────────────────

ISSUE_KEYS = {"code", "zh", "en", "node_id", "edge_id", "port", "level"}


def test_try_parse_error_is_issue_array_and_skips_engine(client, app):
    engine, *_rest = install_engine(app)
    response = client.post("/admin/try", json={
        "root": {"id": "root", "type": "graph",
                 "nodes": [{"id": "n", "type": "bogus"}], "edges": []},
        "text": "你好",
    })
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert issue["level"] == "hard"
    assert issue["zh"] and issue["en"]
    assert engine.calls == 0


def test_try_structure_errors_are_issue_arrays_with_locators(client, app):
    engine, *_rest = install_engine(app)
    root = three_answer_graph()
    root["nodes"].append({"id": "start-2", "type": "start", "position": {"x": 0, "y": 0}})
    response = client.post("/admin/try", json={"root": root, "text": "你好"})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert any(issue["code"] == "start_count" for issue in detail)
    assert all(set(issue) == ISSUE_KEYS for issue in detail)
    assert engine.calls == 0

    broken = {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "safe", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
        ],
        "edges": [
            {"id": "e-ghost", "source": "start", "source_port": "out",
             "target": "ghost", "target_port": "in_1", "condition": None},
        ],
        "error_fallback": "safe",
    }
    response = client.post("/admin/try", json={"root": broken, "text": "你好"})
    assert response.status_code == 422, response.text
    issue = next(item for item in response.json()["detail"]
                 if item["code"] == "edge_endpoint_missing")
    assert issue["node_id"] == "ghost"
    assert issue["edge_id"] == "e-ghost"
    assert issue["port"] == "in_1"
    assert engine.calls == 0


def test_try_huge_chain_returns_422_limit_issue_not_500(client, app):
    """终审 Critical 回归：1200 节点链的草稿试算必须 422 + Issue 数组，绝不 500。"""
    engine, *_rest = install_engine(app)
    response = client.post("/admin/try", json={"root": huge_chain_graph(), "text": "你好"})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all(set(issue) == ISSUE_KEYS for issue in detail)
    assert any(issue["code"] == "limit_exceeded" for issue in detail)
    assert engine.calls == 0


def test_try_invalid_timezone_is_422_without_entering_engine(client, app):
    engine, *_rest = install_engine(app)
    before_snapshot = app.state.snapshot.snapshot_revision
    before_counts = row_counts(app)

    response = client.post("/admin/try", json={
        "root": three_answer_graph(), "timezone": "Mars/Olympus", "text": "你好",
    })

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    issue = next(item for item in detail if item["code"] == "invalid_timezone")
    assert set(issue) == ISSUE_KEYS
    assert engine.calls == 0
    assert app.state.snapshot.snapshot_revision == before_snapshot
    assert row_counts(app) == before_counts


# ── 显式 null 语义（base 428a35d 回归） ────────────────────────────────

def test_try_explicit_timezone_null_is_parse_error_not_inherited(client, app):
    engine, *_rest = install_engine(app)
    create_stored_tree(client, timezone="UTC")

    response = client.post("/admin/try", json={
        "root": three_answer_graph(), "tree_id": "main", "text": "抢修", "timezone": None,
    })

    assert response.status_code == 422, response.text
    issue = response.json()["detail"][0]
    assert issue["code"] == "graph_parse_error"
    assert issue["zh"] == (
        "图解析失败：root 不符合 sr-routing-graph v3 结构（timezone must be a nonempty string）")
    assert engine.calls == 0


def test_try_explicit_sticky_null_resets_to_default_not_inherited(client, app):
    engine, *_rest = install_engine(app)
    create_stored_tree(client, sticky={"enabled": True, "ttl_seconds": 900})

    response = client.post("/admin/try", json={
        "root": three_answer_graph(), "tree_id": "main", "text": "抢修", "sticky": None,
    })

    assert response.status_code == 200, response.text
    draft = engine.snapshots[0].tree
    assert draft.sticky.enabled is False
    assert draft.sticky.ttl_seconds == 1800


def test_try_requires_root_or_tree_id(client, app):
    engine, *_rest = install_engine(app)
    response = client.post("/admin/try", json={"text": "你好"})
    assert response.status_code == 422
    assert response.json()["detail"] == "try needs root or tree_id"
    assert engine.calls == 0


def test_try_no_available_backend_is_still_503(client, app):
    engine, *_rest = install_engine(app)
    before_snapshot = app.state.snapshot.snapshot_revision
    before_counts = row_counts(app)
    root = three_answer_graph()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []

    response = client.post("/admin/try", json={"root": root, "text": "抢修"})

    assert response.status_code == 503, response.text
    assert engine.calls == 1
    assert app.state.snapshot.snapshot_revision == before_snapshot
    assert row_counts(app) == before_counts


# ── 展示层（Task 7）：/admin/try 与 /v1 共用同一 trace presenter ────────


def test_try_trace_events_carry_presentation_fields(client, app):
    _engine, _jev, _balancer, _affinity = install_engine(app)

    response = client.post("/admin/try", json={"root": three_answer_graph(), "text": "抢修工单"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace"]
    for event in body["trace"]:
        assert _DISPLAY_FIELDS <= set(event), event["event"]
        for field in ("label", "summary"):
            assert event[field]["zh"] and event[field]["en"]
        assert event["cell"] not in (None, "")
        assert isinstance(event["facts"], list)
        assert event["phase"]
    assert body["trace"][-1]["event"] == "scheduled"
    assert body["trace"][-1]["phase"] == "schedule"
