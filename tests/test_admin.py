import asyncio

import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.request_logging import RequestLogger
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.routing_trees import RoutingTreeRepo
from tests.routing_fixtures import test_settings, tree_dict


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(test_settings(tmp_path)))


def _direct_root(model="qwen3-8b"):
    """start → target(model) 的最小启用树：活动图校验只需要模型存在。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": model, "reasoning": None}]},
        ],
        "edges": [{
            "id": "e-start-direct", "source": "start", "source_port": "out",
            "target": "direct", "target_port": "in_1", "condition": None,
        }],
        "error_fallback": "direct",
    }


def _app_with_active_tree(tmp_path):
    """真实 create_app + 已启用图（graph 目录预置），用于写用例失败路径断言。"""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(root=_direct_root())))
    repo.enable("main")
    return create_app(settings)


def test_overview_and_request_stats_use_current_log_contract(client):
    overview = client.get("/admin/overview").json()
    assert {"daily", "routes", "total", "sticky", "confidence", "confidence_since"} <= set(overview)
    assert "evals" not in overview
    stats = client.get("/admin/requests/stats").json()
    assert stats["ring_size"] == 0
    assert stats["queue_limit"] > 0


@pytest.mark.parametrize("path", [
    "/admin/signals", "/admin/signal-questions", "/admin/tagger",
    "/admin/keyword-groups", "/admin/eval", "/admin/evals",
    "/admin/settings",
])
def test_legacy_signal_and_eval_apis_are_gone(client, path):
    assert client.get(path).status_code == 404


def test_time_periods_roundtrip_and_snapshot_refresh(client):
    response = client.put("/admin/time-periods", json={"periods": [{"name": "值班", "days": [0, 1], "start": "22:00", "end": "06:00"}]})
    assert response.status_code == 200, response.text
    body = client.get("/admin/time-periods").json()
    assert body["periods"][0]["name"] == "值班"
    assert "now" in body
    assert client.app.state.snapshot.time_periods[0].name == "值班"


def test_time_period_write_failure_returns_issue_array_and_rolls_back(tmp_path):
    """Task 2：时段写用例的活动图校验失败必须是 422 + Issue 数组。

    ``ValidationFailed`` 继承 ``ValueError``，而 ``TimePeriodRepo.save`` 的结构错误
    也走 ``ValueError``；路由若用无差别的 ``except ValueError`` 收敛，就会把 Issue
    数组降级成字符串 detail（破坏 spec §5 契约），所以这里同时断言回滚与载荷。
    """
    app = _app_with_active_tree(tmp_path)
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute("DELETE FROM models WHERE id = 'qwen3-8b'")
        conn.commit()
    finally:
        conn.close()
    new_periods = [{"name": "值班", "days": [0, 1], "start": "22:00", "end": "06:00"}]

    with TestClient(app) as client:
        before_periods = client.get("/admin/time-periods").json()["periods"]
        before_revision = app.state.snapshot.snapshot_revision

        response = client.put("/admin/time-periods", json={"periods": new_periods})

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, list), detail
        assert "unknown_model" in {issue["code"] for issue in detail}
        assert client.get("/admin/time-periods").json()["periods"] == before_periods
        assert before_periods != new_periods
    assert app.state.snapshot.snapshot_revision == before_revision
    conn = connect(app.state.settings.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM changes WHERE kind = 'time-periods'").fetchone()[0] == 0
    finally:
        conn.close()


def test_model_and_provider_writes_still_publish(client):
    response = client.put("/admin/models/qwen3-8b", json={"upstream": "real-model", "profile": {"capability": "通用请求"}})
    assert response.status_code == 200, response.text
    assert client.app.state.snapshot.backends["qwen3-8b"].upstream_name == "real-model"
    assert client.app.state.snapshot.capabilities["qwen3-8b"] == "通用请求"
    response = client.put("/admin/providers/local", json={"base_url": "http://example.internal:9/v1", "key_env": ""})
    assert response.status_code == 200, response.text
    assert client.app.state.snapshot.backends["qwen3-8b"].url == "http://example.internal:9/v1/chat/completions"
    kinds = {row["kind"] for row in client.get("/admin/changes").json()["changes"]}
    assert {"model", "provider"} <= kinds


def test_request_recent_returns_memory_ring(client):
    body = client.get("/admin/requests/recent").json()
    assert body == {"requests": []}


def test_startup_hydrates_recent_request_logs_from_sqlite(tmp_path):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    log = RequestLogger(settings.db_path)
    log.enqueue({
        "id": "historical-1",
        "ts": "2026-11-03T18:59:00+08:00",
        "decision": "route",
        "model": "fast",
        "routing_ms": 12.0,
    })
    asyncio.run(log.flush())

    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/admin/requests/recent").json()
        stats = client.get("/admin/requests/stats").json()

    assert [item["id"] for item in body["requests"]] == ["historical-1"]
    assert body["requests"][0]["latency_ms"] == 12.0
    assert "text" not in body["requests"][0]
    assert "trace" not in body["requests"][0]
    assert stats["ring_size"] == 1
