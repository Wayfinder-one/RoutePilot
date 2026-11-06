import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.api.routes_jev import router as jev_router
from router.control.config import ConfigurationService
from router.jev.client import ChoiceAnswer
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_INPUT_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
    apply_window,
    estimate_tokens,
)
from router.routing.graph.compiled import compile_graph
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.tree import tree_from_dict
from router.routing.snapshot import build_snapshot, read_jev_window
from router.routing.types import RoutingRequest
from router.store.db import connect, migrate
from router.store.repositories import SettingsRepo
from router.store.routing_trees import GraphChangedDuringLoad, RoutingTreeRepo
from router.upstream.catalog import Backend
from tests.routing_fixtures import test_settings, tree_dict



# ── window 单元测试 ────────────────────────────────────────────────────

def test_estimate_tokens_counts_cjk_as_one_and_ascii_as_quarter():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("你好abcd") == 3          # 2 + ceil(4/4)
    assert estimate_tokens("你好abcd") < estimate_tokens("你好你好")


def test_apply_window_keeps_short_text_untouched():
    text = "帮我写一段关于秋天的短文案"
    result = apply_window(text, 1024)
    assert result.truncated is False
    assert result.text == text
    assert result.tokens_before == result.tokens_after == estimate_tokens(text)


@pytest.mark.parametrize("budget", [MIN_INPUT_TOKENS, 512, 1024])
def test_apply_window_truncates_to_budget_and_keeps_head_and_tail(budget):
    head = "开头背景：国网营销数字化改造项目。"
    tail = "结尾诉求：请给出分阶段纠偏措施。"
    body = "计量采集、电价电费核算、供电服务工单、新能源并网四个子系统。" * 300
    result = apply_window(head + body + tail, budget)
    assert result.truncated is True
    assert result.tokens_before > budget
    assert result.tokens_after <= budget
    assert result.text.startswith(head[:6])
    assert result.text.endswith(tail[-6:])
    assert "已截断" in result.text


def test_apply_window_floors_below_minimum_budget():
    text = "字" * 5000
    result = apply_window(text, 10)
    assert result.truncated is True
    assert result.tokens_after <= MIN_INPUT_TOKENS


# ── snapshot 读取 ──────────────────────────────────────────────────────

def _db(tmp_path):
    path = str(tmp_path / "win.db")
    conn = connect(path)
    migrate(conn)
    return conn


def test_read_jev_window_defaults_and_overrides(tmp_path):
    conn = _db(tmp_path)
    try:
        assert read_jev_window(conn) == (DEFAULT_INPUT_TOKENS, DEFAULT_CONTEXT_TOKENS)
        repo = SettingsRepo(conn)
        repo.set("jev_context_tokens", "8192")
        repo.set("jev_input_tokens", "2048")
        conn.commit()
        assert read_jev_window(conn) == (2048, 8192)
        repo.set("jev_input_tokens", "99999")       # 超过 context -> 收口
        conn.commit()
        assert read_jev_window(conn) == (8192, 8192)
        repo.set("jev_context_tokens", "not-a-number")  # 非法 -> 回默认；input 收口到默认 context
        conn.commit()
        assert read_jev_window(conn) == (DEFAULT_CONTEXT_TOKENS, DEFAULT_CONTEXT_TOKENS)
        repo.set("jev_context_tokens", "256")           # 低于上下限 -> 收口到 512
        repo.set("jev_input_tokens", "256")
        conn.commit()
        assert read_jev_window(conn) == (MIN_INPUT_TOKENS, MIN_CONTEXT_TOKENS)
    finally:
        conn.close()


def test_read_jev_window_ignores_garbage(tmp_path):
    conn = _db(tmp_path)
    try:
        repo = SettingsRepo(conn)
        repo.set("jev_context_tokens", "abc")
        repo.set("jev_input_tokens", "-5")
        conn.commit()
        input_tokens, context_tokens = read_jev_window(conn)
        assert context_tokens == DEFAULT_CONTEXT_TOKENS
        assert input_tokens == MIN_INPUT_TOKENS
    finally:
        conn.close()


# ── admin API ──────────────────────────────────────────────────────────

class Publisher:
    """SnapshotPublisher 桩：save_window 走 locked/prepare/publish 契约（Task 2）。"""

    def __init__(self):
        self.published = []

    @contextmanager
    def locked(self):
        yield self

    def prepare(self, conn, tree, fingerprint):
        return SimpleNamespace(tree=tree, fingerprint=fingerprint)

    def publish(self, prepared):
        self.published.append(prepared)


def make_client(tmp_path):
    path = str(tmp_path / "api.db")
    conn = connect(path)
    migrate(conn)
    conn.close()
    app = FastAPI()
    app.state.settings = SimpleNamespace(db_path=path)
    app.state.graph_repo = RoutingTreeRepo(tmp_path / "routing-graph")
    app.state.graph_repo.initialize()
    app.state.publisher = Publisher()
    app.include_router(jev_router, prefix="/admin")
    return TestClient(app), app, path


def test_get_window_returns_defaults(tmp_path):
    client, _, _ = make_client(tmp_path)
    body = client.get("/admin/jev/window").json()
    assert body["context_tokens"] == DEFAULT_CONTEXT_TOKENS
    assert body["input_tokens"] == DEFAULT_INPUT_TOKENS
    assert body["defaults"] == {"context_tokens": DEFAULT_CONTEXT_TOKENS, "input_tokens": DEFAULT_INPUT_TOKENS}
    assert body["min_context_tokens"] == MIN_CONTEXT_TOKENS
    assert body["min_input_tokens"] == MIN_INPUT_TOKENS


def test_put_window_persists_and_publishes_snapshot(tmp_path):
    client, app, path = make_client(tmp_path)
    body = client.put("/admin/jev/window", json={"context_tokens": 8192, "input_tokens": 2048}).json()
    assert (body["context_tokens"], body["input_tokens"]) == (8192, 2048)
    assert len(app.state.publisher.published) == 1
    conn = connect(path)
    try:
        assert read_jev_window(conn) == (2048, 8192)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"context_tokens": 1024, "input_tokens": 4096},   # 关系不合法
        {"context_tokens": 256},                           # 低于上下文下限（512）
        {"context_tokens": 100},                           # 低于下限
        {"context_tokens": 999999},                        # 超上限
        {"input_tokens": "abc"},                           # 非整数
        {"input_tokens": True},                            # bool 不是整数
        {"unknown": 1},                                    # 未知字段
        {"context_tokens": None, "input_tokens": 2048},    # 显式 null 不是"缺省"，必须 422
    ],
)
def test_put_window_rejects_invalid(tmp_path, payload):
    client, app, _ = make_client(tmp_path)
    resp = client.put("/admin/jev/window", json=payload)
    assert resp.status_code == 422, resp.text
    assert app.state.publisher.published == []


# ── 发布前指纹复核（Task 10 FIX 4）────────────────────────────────────

class _Trees:
    """最小树仓库桩：按队列吐指纹，队列耗尽后停在最后一个。"""

    def __init__(self, fingerprints):
        self._fingerprints = list(fingerprints)

    def fingerprint(self):
        if len(self._fingerprints) > 1:
            return self._fingerprints.pop(0)
        return self._fingerprints[0]

    def active(self):
        return None


def _fresh_conn(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    return conn


def test_publish_rechecks_fingerprint_and_rebuilds(tmp_path):
    """提交后图被改写：用新指纹重建快照，不发布过期指纹。"""
    conn = _fresh_conn(tmp_path)
    try:
        publisher = Publisher()
        service = ConfigurationService(
            conn, _Trees([("tree-1", "sha-a"), ("tree-1", "sha-b")]), publisher)

        def write(_conn):
            prepared = service._prepared_snapshot()  # 指纹 sha-a
            return prepared

        service._write_and_publish(write)
        assert len(publisher.published) == 1
        assert publisher.published[0].fingerprint == ("tree-1", "sha-b")
    finally:
        conn.close()


def test_publish_refuses_when_fingerprint_keeps_changing(tmp_path):
    """复核期间指纹持续变化：抛与 GraphService 相同的冲突且不发布。"""
    conn = _fresh_conn(tmp_path)
    try:
        publisher = Publisher()
        service = ConfigurationService(
            conn,
            _Trees([("tree-1", "sha-a"), ("tree-1", "sha-b"), ("tree-1", "sha-c")]),
            publisher,
        )
        with pytest.raises(GraphChangedDuringLoad):
            service._write_and_publish(lambda _conn: service._prepared_snapshot())
        assert publisher.published == []
    finally:
        conn.close()


# ── 真实组合根：写事务回滚契约（与服务层 save_window 同型）──────────────


def direct_graph():
    """start → target(qwen3-8b)：无问题节点，不需要决策服务即可启用。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def _window_tree_app(tmp_path):
    """真实 create_app + 已启用图：enable 校验失败时 DB/审计/快照都不得变化。"""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(root=direct_graph())))
    repo.enable("main")
    app = create_app(settings)
    return app, TestClient(app)


def test_failed_window_save_changes_neither_db_audit_nor_snapshot(tmp_path):
    app, client = _window_tree_app(tmp_path)
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("DELETE FROM models WHERE id = 'qwen3-8b'")
        conn.commit()
    finally:
        conn.close()
    before_revision = app.state.snapshot.snapshot_revision
    before_window = (app.state.snapshot.jev_input_tokens, app.state.snapshot.jev_context_tokens)

    response = client.put("/admin/jev/window", json={"context_tokens": 8192, "input_tokens": 2048})

    assert response.status_code == 422, response.text
    issue_codes = {issue["code"] for issue in response.json()["detail"]}
    assert "unknown_model" in issue_codes
    conn = connect(app.state.settings.db_path)
    try:
        assert read_jev_window(conn) == before_window
        assert conn.execute(
            "SELECT COUNT(*) FROM changes WHERE kind = 'jev_window'").fetchone()[0] == 0
    finally:
        conn.close()
    assert app.state.snapshot.snapshot_revision == before_revision
    assert (app.state.snapshot.jev_input_tokens, app.state.snapshot.jev_context_tokens) == before_window


def test_put_window_publishes_candidate_snapshot_with_new_values(tmp_path):
    settings = test_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        before = app.state.snapshot.snapshot_revision

        response = client.put("/admin/jev/window", json={"context_tokens": 8192, "input_tokens": 2048})

        assert response.status_code == 200, response.text
        assert app.state.snapshot.jev_input_tokens == 2048
        assert app.state.snapshot.jev_context_tokens == 8192
        assert app.state.snapshot.snapshot_revision != before
        body = response.json()
        assert (body["input_tokens"], body["context_tokens"]) == (2048, 8192)


# ── 路由引擎：截断生效 + trace ─────────────────────────────────────────

class GraphJev:
    """Graph v3 决策服务替身：一次 choose_many 返回本波全部问题答案。"""

    def __init__(self):
        self.calls = []

    async def choose_many(self, settings, text, questions):
        self.calls.append({"text": text, "questions": list(questions)})
        return {
            node_id: ChoiceAnswer(
                {"easy": 0.3, "strong": 0.7}, ["strong", "easy"], 0.7, 0.4,
                {"input_tokens": 5}, 8.0,
            )
            for node_id in questions
        }


def _snapshot(*, input_tokens=MIN_INPUT_TOKENS, context_tokens=DEFAULT_CONTEXT_TOKENS):
    jev_settings = JevSettings("http://jev.test", "d1-2B", "", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE)
    tree = tree_from_dict(tree_dict(enabled=True))
    return build_snapshot(
        tree=tree,
        backends={"fast": Backend(url="http://fast.test", headers={}, upstream_name="fast")},
        time_periods=[],
        jev=jev_settings,
        capabilities={"fast": "简单请求"},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
        compiled=compile_graph(tree.root),
    )


def test_evaluator_truncates_jev_input_and_traces_it():
    snap = _snapshot(input_tokens=MIN_INPUT_TOKENS)
    jev = GraphJev()
    trace = []
    asyncio.run(GraphEvaluator(jev).evaluate(
        snap, RoutingRequest("国网营销数字化改造项目背景。" * 500, session_id="s1"),
        trace_sink=trace,
    ))

    sent = jev.calls[0]["text"]
    assert estimate_tokens(sent) <= MIN_INPUT_TOKENS
    window_events = [item for item in trace if item["event"] == "window"]
    assert len(window_events) == 1
    assert window_events[0]["truncated"] is True
    assert window_events[0]["tokens_before"] > MIN_INPUT_TOKENS


def test_evaluator_leaves_short_input_alone():
    snap = _snapshot()
    jev = GraphJev()
    trace = []
    asyncio.run(GraphEvaluator(jev).evaluate(
        snap, RoutingRequest("你好", session_id="s2"), trace_sink=trace,
    ))

    assert jev.calls[0]["text"] == "你好"
    # 冻结契约：window 事件总会发出，未截断时 truncated=False 且 token 前后一致。
    window_events = [item for item in trace if item["event"] == "window"]
    assert len(window_events) == 1
    assert window_events[0]["truncated"] is False
    assert window_events[0]["tokens_before"] == window_events[0]["tokens_after"]
