"""`POST /admin/try`：完整 graph envelope 草稿试算（spec §4.3，Task 7）。

覆盖 root 优先 envelope 合并矩阵、root-only 默认值、draft 标记、三类 answers 的
序列化、complete outcome（candidates/waves/selection_reason/fallback_reason）、
503 语义，以及草稿试算的副作用不变量（不落库、不发布 snapshot、不写
affinity/inflight、不读已存 root）。

无 ``tree_id``/``root`` 的请求走已发布路由集按部门选图（管理端验证部门选择）：
部门精确命中 → 部门图，未知/空部门 → 默认图，``graph_selection`` 报告原因；
带 ``tree_id`` 或草稿 ``root`` 的请求保持 spec §4.3 envelope 语义。
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.window import MIN_INPUT_TOKENS, estimate_tokens
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.store.db import connect
from router.store.routing_trees import RoutingTreeRepo
from router.upstream.runtime_keys import tree_scope
from tests.routing_fixtures import (
    graph_dict,
    huge_chain_graph,
    runtime_dict,
    runtime_for_models,
    runtime_from_catalog,
    test_settings,
    tree_dict,
)


_DISPLAY_FIELDS = {"phase", "label", "cell", "summary", "facts", "tone", "kind_label"}


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
            {"id": "start", "type": "start"},
            {"id": "q-choice", "type": "question", "question_type": "choice",
             "instructions": "选择能力",
             "options": [
                 {"key": "easy", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "q-noul", "type": "question", "question_type": "noul",
             "instructions": "是否需要电力知识？"},
            {"id": "q-score", "type": "question", "question_type": "score",
             "instructions": "难度评分", "criteria": [1, 10]},
            {"id": "t-choice", "type": "target", "priority": 4, "title": "轻量目标",
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
            {"id": "t-noul", "type": "target", "priority": 3,
             "models": [{"model": "qwen3-32b", "reasoning": None}]},
            {"id": "t-score", "type": "target", "priority": 2,
             "models": [{"model": "deepseek-v3", "reasoning": None}]},
            {"id": "safe", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
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


def catalog_runtime(app):
    """把当前数据库目录显式固化成一次草稿的运行字段（测试辅助，生产无此回退）。"""
    conn = connect(app.state.settings.db_path)
    try:
        return runtime_from_catalog(conn)
    finally:
        conn.close()


def models_runtime(*model_ids):
    runtime = runtime_dict()
    runtime["backends"]["models"] = [
        {"id": model_id, "upstream": model_id, "provider": "local", "profile": {}}
        for model_id in model_ids
    ]
    return runtime


def root_payload(app, root, *, text="你好", **extra):
    """root-only 草稿必须携带完整运行字段（不再从数据库生成隐式快照）。"""
    return {**catalog_runtime(app), "root": root, "text": text, **extra}


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
            for table in ("changes", "request_logs")
        }
    finally:
        conn.close()


def create_stored_tree(client, *, root=None, tree_id="main", timezone="Asia/Shanghai",
                       sticky=None, name="测试图"):
    """创建已存画布：保存现在执行画布自身完整校验，运行目录必须覆盖 root 的模型。"""
    resolved_root = graph_dict() if root is None else root
    runtime = models_runtime(*MODELS)
    known = set(MODELS)
    for node in resolved_root.get("nodes") or ():
        if not isinstance(node, dict) or node.get("type") != "target":
            continue
        for slot in node.get("models") or ():
            model_id = slot.get("model") if isinstance(slot, dict) else slot
            if isinstance(model_id, str) and model_id not in known:
                known.add(model_id)
                runtime["backends"]["models"].append({
                    "id": model_id, "upstream": model_id,
                    "provider": "local", "profile": {},
                })
    response = client.post("/admin/trees", json={
        "id": tree_id,
        "name": name,
        "root": resolved_root,
        "timezone": timezone,
        "sticky": sticky or {"enabled": True, "ttl_seconds": 1800},
        **runtime,
    })
    assert response.status_code == 201, response.text
    return response.json()["tree"]


def portable_graph(model="fast"):
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "target", "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-target", "source": "start", "source_port": "out",
            "target": "target", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "target",
    }


def portable_envelope(tree_id="portable"):
    return {
        "id": tree_id,
        "name": "便携配置",
        "root": portable_graph(),
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        **runtime_dict(),
    }


def create_direct_model_tree(client, tree_id, model):
    """无问题节点的直连图（start → target(model)）：不触发 JEV，选图断言只看模型。"""
    root = {
        'id': 'root', 'type': 'graph',
        'nodes': [
            {'id': 'start', 'type': 'start'},
            {'id': 'target', 'type': 'target', 'priority': 1,
             'models': [{'model': model, 'reasoning': None}]},
        ],
        'edges': [{
            'id': 'e-start-target', 'source': 'start', 'source_port': 'out',
            'target': 'target', 'target_port': 'in_1', 'condition': None,
        }],
        'error_fallback': 'target',
    }
    raw = tree_dict(tree_id, root=root)
    raw.update(runtime_for_models([model]))
    payload = {key: value for key, value in raw.items()
               if key not in {'enabled', 'default'}}
    response = client.post('/admin/trees', json=payload)
    assert response.status_code == 201, response.text
    assert client.post(f'/admin/trees/{tree_id}/enable').status_code == 200


def set_departments(client, tree_id, departments):
    """走真实部门设置 API（普通保存路径：校验 + revision 发布）。"""
    current = client.get(f'/admin/trees/{tree_id}').json()['tree']
    response = client.put(f'/admin/trees/{tree_id}/departments', json={
        'departments': departments,
        'base_document_sha256': current['document_sha256'],
    })
    assert response.status_code == 200, response.text


def test_create_tree_roundtrips_embedded_runtime_without_keys(client, app):
    response = client.post("/admin/trees", json=portable_envelope())

    assert response.status_code == 201, response.text
    tree = response.json()["tree"]
    assert tree["backends"]["models"][0]["upstream"] == "json-upstream"
    assert tree["jev"]["model"] == "json-jev"
    assert "api_key" not in json.dumps(tree)
    stored = app.state.graph_repo.get("portable")
    assert stored.runtime is not None
    assert stored.runtime.backends.models[0]["upstream"] == "json-upstream"


def test_create_without_runtime_fields_is_rejected(client):
    payload = portable_envelope("legacy")
    for field in runtime_dict():
        payload.pop(field)
    payload["root"] = portable_graph("qwen3-8b")

    response = client.post("/admin/trees", json=payload)

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["code"] == "graph_parse_error"
    assert "runtime fields must appear together" in response.text
    assert client.get("/admin/trees/legacy").status_code == 404


def test_partial_runtime_create_returns_graph_parse_error(client):
    payload = portable_envelope("partial")
    for field in ("jev", "jev_window", "time_periods"):
        payload.pop(field)

    response = client.post("/admin/trees", json=payload)

    assert response.status_code == 422
    assert "graph_parse_error" in response.text


def test_try_uses_embedded_runtime_for_unsaved_draft(client, app):
    engine, _jev, _balancer, _affinity = install_engine(app)
    payload = portable_envelope()
    payload.pop("id")
    payload.pop("name")

    response = client.post("/admin/try", json={
        **payload, "text": "你好", "session_id": "",
    })

    assert response.status_code == 200, response.text
    snapshot = engine.snapshots[-1]
    assert snapshot.backends["fast"].url == "http://json-only.test/v1/chat/completions"
    assert snapshot.capabilities == {"fast": "来自 JSON 的能力"}
    assert snapshot.jev.model == "json-jev"


def test_try_without_runtime_inherits_stored_tree(client, app):
    created = client.post("/admin/trees", json=portable_envelope())
    assert created.status_code == 201, created.text
    engine, _jev, _balancer, _affinity = install_engine(app)

    response = client.post("/admin/try", json={"tree_id": "portable", "text": "你好"})

    assert response.status_code == 200, response.text
    assert engine.snapshots[-1].jev.model == "json-jev"
    assert engine.snapshots[-1].backends["fast"].upstream_name == "json-upstream"


def test_root_only_try_without_runtime_fields_is_422(client, app):
    """无 tree_id 且无完整运行字段的草稿检查绝不回退数据库生成隐式快照。"""
    created = client.post("/admin/trees", json=portable_envelope())
    assert created.status_code == 201, created.text
    assert client.post("/admin/trees/portable/enable").status_code == 200
    engine, _jev, _balancer, _affinity = install_engine(app)

    response = client.post("/admin/try", json={"root": portable_graph(), "text": "你好"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["code"] == "graph_parse_error"
    assert "runtime fields must appear together" in response.text
    assert engine.calls == 0


def test_root_only_try_accepts_a_full_self_contained_envelope(client, app):
    engine, _jev, _balancer, _affinity = install_engine(app)
    payload = portable_envelope()
    payload.pop("id")
    payload.pop("name")

    response = client.post("/admin/try", json={
        **payload, "root": portable_graph(), "text": "你好"})

    assert response.status_code == 200, response.text
    assert engine.snapshots[-1].jev.model == "json-jev"
    assert engine.snapshots[-1].capabilities == {"fast": "来自 JSON 的能力"}


# ── 实际路由集选图：部门命中 / 未知兜底 / 空部门兜底 ────────────────────

def test_try_without_draft_uses_department_route_set(client, app):
    """无 root/tree_id 时按已发布路由集选图：部门精确命中否则默认图。"""
    create_direct_model_tree(client, 'main', 'fast')
    create_direct_model_tree(client, 'finance', 'strong')
    set_departments(client, 'finance', ['财务部'])

    response = client.post('/admin/try', json={
        'department': '财务部', 'text': '请处理这个请求', 'session_id': '',
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['tree_id'] == 'finance'
    assert body['graph_selection'] == 'department'
    assert body['outcome']['model'] == 'strong'
    # 路由集路径是已发布快照，不是草稿：revision 来自选中快照，不发 warnings。
    selected = app.state.snapshot_set.snapshots['finance']
    assert body['tree_revision'] == selected.tree_revision
    assert body['snapshot_revision'] == selected.snapshot_revision
    assert body['draft'] is False
    assert body['warnings'] == []

    response = client.post('/admin/try', json={
        'department': '未知部门', 'text': '请处理这个请求', 'session_id': '',
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['tree_id'] == 'main'
    assert body['graph_selection'] == 'default'
    assert body['outcome']['model'] == 'fast'

    # 空 department（显式空串 / 仅空白）都等价于未声明部门 → 默认图。
    for department in ('', '   ', None):
        response = client.post('/admin/try', json={
            'department': department, 'text': '请处理这个请求', 'session_id': '',
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['tree_id'] == 'main'
        assert body['graph_selection'] == 'default'
        assert body['outcome']['model'] == 'fast'

    # 缺失 department 字段同样走默认图。
    response = client.post('/admin/try', json={
        'text': '请处理这个请求', 'session_id': '',
    })
    assert response.status_code == 200, response.text
    assert response.json()['tree_id'] == 'main'
    assert response.json()['graph_selection'] == 'default'


@pytest.mark.parametrize(("department", "detail"), [
    (123, 'try department must be a string'),
    (['财务部'], 'try department must be a string'),
    ({'name': '财务部'}, 'try department must be a string'),
    ('部' * 65, 'invalid department'),
    ('a\tb', 'invalid department'),
    ('a\x00b', 'invalid department'),
    ('a\x7fb', 'invalid department'),
])
def test_try_route_set_rejects_invalid_department(client, department, detail):
    """department 必须是非空可展示字符串：类型/长度/控制字符在选图前拒绝。"""
    response = client.post('/admin/try', json={
        'department': department, 'text': '请处理这个请求', 'session_id': '',
    })
    assert response.status_code == 422, response.text
    assert response.json()['detail'] == detail


def test_try_route_set_without_any_tree_is_no_active_tree(client):
    response = client.post('/admin/try', json={'text': '请处理这个请求'})

    assert response.status_code == 503, response.text
    assert response.json()['detail'] == 'no_active_tree'


def test_try_route_set_without_default_tree_is_no_default_tree(client, app):
    create_direct_model_tree(client, 'main', 'fast')
    app.state.snapshot_set = replace(app.state.snapshot_set, default_tree_id=None)

    response = client.post('/admin/try', json={'department': '财务部', 'text': '你好'})

    assert response.status_code == 503, response.text
    assert response.json()['detail'] == 'no_default_tree'


def test_try_route_set_unavailable_is_no_valid_snapshot(client, app):
    app.state.snapshot_set = None

    response = client.post('/admin/try', json={'text': '你好'})

    assert response.status_code == 503, response.text
    assert response.json()['detail'] == 'no_valid_snapshot'


def test_try_tree_id_ignores_department_and_reports_no_graph_selection(client):
    """带 tree_id 的既有路径仍是草稿 envelope 语义，不参与路由集部门选择。"""
    create_direct_model_tree(client, 'main', 'fast')
    set_departments(client, 'main', ['财务部'])

    response = client.post('/admin/try', json={
        'tree_id': 'main', 'department': '财务部', 'text': '你好'})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['graph_selection'] is None
    assert body['draft'] is False
    assert body['tree_id'] == 'main'


# ── 副作用不变量 ───────────────────────────────────────────────────────

def test_root_draft_is_side_effect_free_and_marks_draft(client, app):
    engine, _jev, balancer, affinity = install_engine(app)
    before_snapshot = app.state.snapshot_set
    before_revision = before_snapshot.snapshot_revision
    before_counts = row_counts(app)

    response = client.post("/admin/try", json=root_payload(
        app, three_answer_graph(), text="抢修工单", session_id="try-session",
        sticky={"enabled": True, "ttl_seconds": 1800},
    ))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is True
    assert body["tree_id"] == "draft"
    assert body["outcome"]["target_id"] == "t-choice"
    # 草稿路径不参与路由集选图：graph_selection 显式为 null。
    assert body["graph_selection"] is None
    assert engine.calls == 1

    # 不发布 snapshot（对象与 revision 都不变）
    assert app.state.snapshot_set is before_snapshot
    assert app.state.snapshot_set.snapshot_revision == before_revision
    # 不落库
    assert row_counts(app) == before_counts
    # 不写粘性：只读 affinity（dry-run touch=False），绝不 put
    assert len(affinity) == 0
    assert affinity.puts == []
    assert affinity.gets and all(item["touch"] is False for item in affinity.gets)
    # dry-run 不计 inflight（root-only 草稿图的 tree.id 是 "draft"）
    assert balancer.calls and all(item["dry_run"] is True for item in balancer.calls)
    assert all(item["scope"] == tree_scope("draft") for item in balancer.calls)
    assert all(balancer.inflight(model, scope=tree_scope("draft")) == 0 for model in MODELS)


def test_root_only_defaults_and_does_not_read_stored_trees(client, app, monkeypatch):
    engine, _jev, _balancer, _affinity = install_engine(app)

    def forbidden(self, tree_id):
        raise AssertionError("root-only try must not read stored routing trees")

    monkeypatch.setattr(RoutingTreeRepo, "get", forbidden)

    response = client.post("/admin/try", json=root_payload(app, three_answer_graph()))

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


def test_try_uses_draft_jev_window_without_publishing(client, app, monkeypatch):
    engine, jev, _balancer, _affinity = install_engine(app)
    published_before = app.state.snapshot_set.snapshot_revision
    payload = root_payload(app, three_answer_graph())
    payload["jev_window"] = {
        "input_tokens": MIN_INPUT_TOKENS, "context_tokens": 4096,
    }

    long_text = "国网营销数字化改造项目背景。" * 500
    response = client.post("/admin/try", json={**payload, "text": long_text})

    assert response.status_code == 200, response.text
    window = next(event for event in response.json()["trace"] if event["event"] == "window")
    assert window["truncated"] is True
    assert window["tokens_after"] <= MIN_INPUT_TOKENS
    assert window["tokens_before"] > MIN_INPUT_TOKENS
    assert estimate_tokens(jev.calls[0]["text"]) <= MIN_INPUT_TOKENS
    assert app.state.snapshot_set.snapshot_revision == published_before


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
    # 带 tree_id 的草稿路径同样不参与路由集选图。
    assert body["graph_selection"] is None
    draft = engine.snapshots[0].tree
    assert draft.id == "main"
    assert draft.timezone == "UTC"
    assert draft.sticky.enabled is True
    # dry-run：即使 sticky enabled + session_id 也不写粘性、不计 inflight
    assert affinity.puts == []
    assert len(affinity) == 0
    assert all(item["dry_run"] is True for item in balancer.calls)
    # /admin/try 不发布路由集：存储画布仍是草稿，启用集合保持为空。
    assert app.state.snapshot_set.snapshots == {}


def test_try_unknown_tree_id_is_404_and_skips_engine(client, app):
    engine, *_rest = install_engine(app)
    response = client.post("/admin/try", json={"tree_id": "nope", "text": "你好"})
    assert response.status_code == 404
    assert engine.calls == 0


# ── 响应序列化 ─────────────────────────────────────────────────────────

def test_try_serializes_all_answer_types_and_complete_outcome(client, app):
    _engine, _jev, _balancer, _affinity = install_engine(app)

    response = client.post("/admin/try", json=root_payload(app, three_answer_graph(), text="抢修工单"))

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
    assert all(set(issue) == {"code", "zh", "en", "node_id", "edge_id", "port",
                              "level", "params"}
               for issue in body["warnings"])


def test_try_fixed_peak_scenario_matches_engine(client, app):
    periods = client.put("/admin/time-periods", json={"periods": SCENARIO_DATA["periods"]})
    assert periods.status_code == 200, periods.text
    # 启动种子（环境 URL/model）已在目录里创建 default 服务；本用例只需确认目录。
    services = client.get("/admin/jev").json()["services"]
    assert [(service["id"], service["model"]) for service in services] == [
        ("default", "d1-2B")]
    install_engine(app, SCENARIO_BY_ID["s3"])
    scenario = SCENARIO_BY_ID["s3"]

    runtime = catalog_runtime(app)
    response = client.post("/admin/try", json={
        **runtime,
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
    # 真实时段条件参与计算：time 与 condition 事件都归入「时段判断」阶段。
    assert body["trace"][0]["phase"] == "period"
    assert body["trace"][0]["label"] == {"zh": "时段判断", "en": "Time period"}
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


def test_try_without_period_condition_keeps_time_event_initial(client, app):
    """部署有生效时段，但草稿图从不判断时段：time 事件必须留在「初始计算」。"""
    periods = client.put("/admin/time-periods", json={"periods": SCENARIO_DATA["periods"]})
    assert periods.status_code == 200, periods.text
    install_engine(app)

    response = client.post("/admin/try", json=root_payload(app, three_answer_graph()))

    assert response.status_code == 200, response.text
    body = response.json()
    assert _machine_event(body["trace"][0])["event"] == "time"
    assert body["trace"][0]["phase"] == "initial"
    assert body["trace"][0]["label"] == {"zh": "初始计算", "en": "Initial evaluation"}
    # 没有任何 condition/source=periods 事件。
    assert not [
        event for event in body["trace"]
        if event["event"] == "condition" and event["source"] == "periods"
    ]


# ── 422 / 503 ─────────────────────────────────────────────────────────

ISSUE_KEYS = {"code", "zh", "en", "node_id", "edge_id", "port", "level",
              "params"}


def test_try_parse_error_is_issue_array_and_skips_engine(client, app):
    engine, *_rest = install_engine(app)
    # root-only 草稿必须带完整运行字段：否则先在「四字段成组」处 422，坏节点从未被
    # 解析。这里断言坏节点的具体解析 detail，证明进入的是图解析路径。
    response = client.post("/admin/try", json=root_payload(app, {
        "id": "root", "type": "graph",
        "nodes": [{"id": "n", "type": "bogus"}], "edges": [],
    }, text="你好"))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    issue = detail[0]
    assert set(issue) == ISSUE_KEYS
    assert issue["code"] == "graph_parse_error"
    assert issue["level"] == "hard"
    assert issue["zh"] and issue["en"]
    assert "unknown node type 'bogus'" in issue["zh"]
    assert "runtime fields must appear together" not in issue["zh"]
    assert engine.calls == 0


def test_try_structure_errors_are_issue_arrays_with_locators(client, app):
    engine, *_rest = install_engine(app)
    root = three_answer_graph()
    root["nodes"].append({"id": "start-2", "type": "start"})
    response = client.post("/admin/try", json=root_payload(app, root))
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
    response = client.post("/admin/try", json=root_payload(app, broken))
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
    response = client.post("/admin/try", json=root_payload(app, huge_chain_graph()))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all(set(issue) == ISSUE_KEYS for issue in detail)
    assert any(issue["code"] == "limit_exceeded" for issue in detail)
    assert engine.calls == 0


def test_try_invalid_timezone_is_422_without_entering_engine(client, app):
    engine, *_rest = install_engine(app)
    before_snapshot = app.state.snapshot_set.snapshot_revision
    before_counts = row_counts(app)

    response = client.post("/admin/try", json=root_payload(
        app, three_answer_graph(), timezone="Mars/Olympus"))

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    issue = next(item for item in detail if item["code"] == "invalid_timezone")
    assert set(issue) == ISSUE_KEYS
    assert engine.calls == 0
    assert app.state.snapshot_set.snapshot_revision == before_snapshot
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
        "图解析失败：root 不符合 sr-routing-graph v4 结构（timezone must be a nonempty string）")
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


def test_try_without_tree_id_or_root_uses_published_route_set(client, app):
    """无 tree_id/root 不再要求草稿 envelope：改为读已发布路由集（零启用图 503）。"""
    engine, *_rest = install_engine(app)
    response = client.post("/admin/try", json={"text": "你好"})
    assert response.status_code == 503
    assert response.json()["detail"] == "no_active_tree"
    assert engine.calls == 0


def test_try_no_available_backend_is_still_503(client, app):
    engine, *_rest = install_engine(app)
    before_snapshot = app.state.snapshot_set.snapshot_revision
    before_counts = row_counts(app)
    root = three_answer_graph()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []

    response = client.post("/admin/try", json=root_payload(app, root, text="抢修"))

    assert response.status_code == 503, response.text
    assert engine.calls == 1
    assert app.state.snapshot_set.snapshot_revision == before_snapshot
    assert row_counts(app) == before_counts


# ── 展示层（Task 7）：/admin/try 与 /v1 共用同一 trace presenter ────────


def test_try_trace_events_carry_presentation_fields(client, app):
    _engine, _jev, _balancer, _affinity = install_engine(app)

    response = client.post("/admin/try", json=root_payload(app, three_answer_graph(), text="抢修工单"))

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


def test_try_rejects_removed_runtime_control_field(client):
    """草稿 DTO 不接受已删除的运行配置控制字段：未知字段必须 422。"""
    payload = portable_envelope()
    payload.pop("id")
    payload.pop("name")

    response = client.post("/admin/try", json={
        **payload, "runtime": None, "text": "你好"})

    assert response.status_code == 422, response.text
    assert any(item["type"] == "extra_forbidden" for item in response.json()["detail"])
