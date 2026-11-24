import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.service import JevService
from router.request_logging import RECENT_LIMIT, RequestLogger
from router.routing.tree import tree_from_dict
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
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


def test_time_periods_roundtrip_is_catalog_only(client):
    """时段目录写只改数据库；零启用图的空路由集不投影目录。"""
    response = client.put("/admin/time-periods", json={"periods": [{"name": "值班", "days": [0, 1], "start": "22:00", "end": "06:00"}]})
    assert response.status_code == 200, response.text
    body = client.get("/admin/time-periods").json()
    assert body["periods"][0]["name"] == "值班"
    assert "now" in body
    assert client.app.state.snapshot_set.snapshots == {}


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


def _snapshot_tree_root():
    return _direct_root()


def _question_root():
    """start → q(choice) → direct：真实 /v1 请求会同时经过 JEV 与上游。"""
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "q", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位",
             "options": [
                 {"key": "light", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "direct", "type": "target", "priority": 1,
             "models": [{"model": "qwen3-8b", "reasoning": None}]},
        ],
        "edges": [
            {"id": "e-start-q", "source": "start", "source_port": "out",
             "target": "q", "target_port": "in_1", "condition": None},
            {"id": "e-q-direct", "source": "q", "source_port": "selected.light",
             "target": "direct", "target_port": "in_1", "condition": None},
        ],
        "error_fallback": "direct",
    }


def test_request_recent_links_snapshot_and_endpoint_serves_tree_and_trace(tmp_path):
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    canvas = {"start": {"x": 12, "y": 34}, "direct": {"x": 260, "y": 34}}
    repo.create(tree_from_dict(tree_dict(
        root=_snapshot_tree_root(), canvas=canvas, enabled=True, default=True,
        departments=["财务部"])))
    app = create_app(settings)
    snapshot = app.state.snapshot_set.default_snapshot
    trace = [
        {"event": "final", "target_id": "direct", "kind": "target", "priority": 1,
         "candidates": []},
        {"event": "scheduled", "model": "qwen3-8b", "reason": "target"},
        {"event": "request_outcome", "outcome_state": "completed"},
    ]
    record = {
        "id": "snapshot-request",
        "ts": "2026-11-20T10:00:00+08:00",
        "text": "秘密请求文本",
        "decision": "route",
        "model": "qwen3-8b",
        "target_id": "direct",
        "tree_id": "main",
        "tree_revision": snapshot.tree_revision,
        "snapshot_revision": snapshot.snapshot_revision,
        "selection_reason": "target",
        "outcome_state": "completed",
        "waves": 1,
        "routing_ms": 5.0,
        "total_ms": 9.0,
        "trace": trace,
    }
    app.state.requestlog.enqueue(record, snapshot=snapshot)
    with TestClient(app) as client:
        listed = client.get("/admin/requests/recent").json()["requests"][0]
        assert listed["snapshot_url"] == "/console/routing.html?request=snapshot-request"
        assert listed["graph_snapshot_id"]
        assert "_graph_snapshot" not in listed
        response = client.get("/admin/requests/snapshot-request/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["request_id"] == "snapshot-request"
        assert body["graph_snapshot_id"] == listed["graph_snapshot_id"]
        assert body["tree"]["canvas"]["start"] == {"x": 12, "y": 34}
        assert body["trace"] == trace
        assert body["request"]["text"] == "秘密请求文本"
        assert body["request"]["id"] == "snapshot-request"

        # 同一张图的第二份请求：行为/布局相同，只有 envelope 元数据不同。
        # 内容寻址必须共享同一份 canonical 图与 ID，但 revision 各归各请求。
        first_live = client.get("/admin/requests/snapshot-request/snapshot").json()
        # 生命周期 default 是易变 envelope 元数据：改成非默认也不得拆分图去重。
        changed_tree = replace(
            snapshot.tree,
            default=False,
            tree_revision="second-tree-revision",
            updated_at="2026-11-21T01:00:00+08:00",
            document_sha256="second-document-hash",
        )
        changed_snapshot = SimpleNamespace(
            tree=changed_tree,
            snapshot_revision="second-snapshot-revision",
        )
        second = dict(
            record,
            id="snapshot-request-2",
            ts="2026-11-20T09:00:00+08:00",
            text="第二份正文",
            tree_revision="second-tree-revision",
            snapshot_revision="second-snapshot-revision",
            trace=list(trace),
        )
        app.state.requestlog.enqueue(second, snapshot=changed_snapshot)
        second_live = client.get("/admin/requests/snapshot-request-2/snapshot").json()
        assert second_live["graph_snapshot_id"] == first_live["graph_snapshot_id"]
        assert second_live["tree"] == first_live["tree"]
        assert not {
            "enabled", "default", "tree_revision", "updated_at", "document_sha256"
        } & set(second_live["tree"])
        # 部门是行为字段：必须保留在 canonical 图里，不参与元数据过滤。
        assert first_live["tree"]["departments"] == ["财务部"]
        assert second_live["request"]["tree_revision"] == "second-tree-revision"
        assert second_live["request"]["snapshot_revision"] == "second-snapshot-revision"

    # 重启后账本行仍在、快照可从 SQLite 重放；正文/原始 trace 不在账本里。
    restarted = create_app(settings)
    with TestClient(restarted) as client:
        listed = client.get("/admin/requests/recent").json()["requests"][0]
        assert listed["id"] == "snapshot-request"
        assert listed["snapshot_url"] == "/console/routing.html?request=snapshot-request"
        assert listed["graph_snapshot_id"] == record["graph_snapshot_id"]
        assert "text" not in listed
        assert "_graph_snapshot" not in listed
        response = client.get("/admin/requests/snapshot-request/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["tree"]["canvas"]["start"] == {"x": 12, "y": 34}
        assert body["trace"] == trace
        assert "text" not in body["request"]

        # 第二份请求跨重启后仍是自己的 revision 与同一份 canonical 图。
        response = client.get("/admin/requests/snapshot-request-2/snapshot")
        assert response.status_code == 200
        restored = response.json()
        assert restored["graph_snapshot_id"] == second_live["graph_snapshot_id"]
        assert restored["tree"] == second_live["tree"]
        assert restored["trace"] == second_live["trace"]
        assert restored["request"] == {
            key: value for key, value in second_live["request"].items() if key != "text"
        }


def test_request_snapshot_persistence_excludes_live_request_secrets(tmp_path):
    """Upstream Authorization / JEV key / raw session / body never enter the tables."""
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        JevServiceRepo(conn).save(
            JevService("default", "http://jev.test", "NeoHorse-Jev-4B",
                       timeout_seconds=5.0),
            api_key="jev-secret-value")
    finally:
        conn.close()
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(
        root=_question_root(), enabled=True, default=True)))

    upstream_secret = "upstream-secret-value"
    jev_secret = "jev-secret-value"
    session_secret = "raw-session-secret-value"
    caller_secret = "caller-secret-value"
    body_text = "正文机密-value"
    seen = {}

    def handler(request):
        if request.url.host == "jev.test":
            return httpx.Response(200, json={
                "answers": {"q": {"type": "choice", "probabilities": {
                    "轻量模型": 0.9, "增强模型": 0.1,
                }}},
                "usage": {"input_tokens": 10},
            })
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "model": "qwen3-8b",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

    app = create_app(settings, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        provider_payload = {
            "base_url": "http://upstream.test/v1", "key_env": "",
            "api_key": upstream_secret,
        }
        preview = client.put("/admin/providers/local", json=provider_payload)
        assert preview.status_code == 409, preview.text
        confirmation = {"trees": [
            {"id": item["id"], "document_sha256": item["document_sha256"]}
            for item in preview.json()["detail"]["affected_trees"]
        ]}
        assert client.put("/admin/providers/local", json={
            **provider_payload, "impact_confirmation": confirmation,
        }).status_code == 200
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": body_text}]},
            headers={"x-sr-session-id": session_secret,
                     "authorization": f"Bearer {caller_secret}"},
        )
        assert response.status_code == 200, response.text
        # 真实密钥确实在运行态 Backend 上使用过，只是不该被快照持久化。
        assert seen.get("authorization") == f"Bearer {upstream_secret}"
        request_id = app.state.requestlog.recent_requests(1)[0]["id"]
        client.portal.call(app.state.requestlog.flush)

    conn = connect(settings.db_path)
    try:
        graph = conn.execute("SELECT graph FROM graph_snapshots").fetchone()[0]
        trace = conn.execute(
            "SELECT trace FROM request_snapshots WHERE request_id = ?", (request_id,)
        ).fetchone()[0]
        ledger_columns = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
    finally:
        conn.close()
    persisted = graph + "\n" + trace
    for secret in (upstream_secret, jev_secret, session_secret, caller_secret, body_text):
        assert secret not in persisted, f"snapshot persistence leaked {secret}"
    assert not {"text", "trace", "session_id", "api_key"} & ledger_columns

    # 重启后只读快照回落到 SQLite：正文不再存在，图与 trace 仍可回放。
    restarted = create_app(settings, transport=httpx.MockTransport(handler))
    with TestClient(restarted) as client:
        response = client.get(f"/admin/requests/{request_id}/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["request"]["id"] == request_id
        assert "text" not in body["request"]
        assert body["tree"]["root"]["type"] == "graph"
        assert [event["event"] for event in body["trace"]][-1] == "request_outcome"
        for secret in (upstream_secret, jev_secret, session_secret, caller_secret, body_text):
            assert secret not in response.text, f"snapshot endpoint leaked {secret}"


def test_request_snapshot_pending_beyond_the_ring_is_served(tmp_path):
    """A queued snapshot that fell out of the display ring is not an unknown id."""
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(
        root=_snapshot_tree_root(), enabled=True, default=True)))
    app = create_app(settings)
    for index in range(RECENT_LIMIT + 1):
        app.state.requestlog.enqueue({
            "id": f"pending-{index}",
            "ts": "2026-11-20T10:00:00+08:00",
            "text": f"待写正文 {index}",
            "decision": "route",
            "model": "qwen3-8b",
            "outcome_state": "incomplete",
        }, snapshot=app.state.snapshot_set.default_snapshot)
    with TestClient(app) as client:
        response = client.get("/admin/requests/pending-0/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["request_id"] == "pending-0"
        assert body["request"]["text"] == "待写正文 0"
        assert body["tree"]["root"]["type"] == "graph"
        assert client.get("/admin/requests/missing/snapshot").status_code == 404


def test_request_snapshot_unknown_id_is_404_and_old_rows_have_no_link(tmp_path):
    settings = test_settings(tmp_path)
    app = create_app(settings)
    app.state.requestlog.enqueue({
        "id": "old-row",
        "ts": "2026-11-01T10:00:00+08:00",
        "decision": "route",
        "model": "qwen3-8b",
        "outcome_state": "completed",
    })
    with TestClient(app) as client:
        listed = client.get("/admin/requests/recent").json()["requests"][0]
        assert "snapshot_url" not in listed
        assert "graph_snapshot_id" not in listed
        missing = client.get("/admin/requests/unknown/snapshot")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "unknown request"}


def test_request_snapshot_url_encodes_request_id(tmp_path):
    settings = test_settings(tmp_path)
    repo = RoutingTreeRepo(settings.graph_dir)
    repo.initialize()
    repo.create(tree_from_dict(tree_dict(
        root=_snapshot_tree_root(), enabled=True, default=True)))
    app = create_app(settings)
    app.state.requestlog.enqueue({
        "id": "id with/space?",
        "ts": "2026-11-20T10:00:00+08:00",
        "decision": "route",
        "model": "qwen3-8b",
        "outcome_state": "completed",
    }, snapshot=app.state.snapshot_set.default_snapshot)
    with TestClient(app) as client:
        listed = client.get("/admin/requests/recent").json()["requests"][0]
        assert listed["snapshot_url"] == "/console/routing.html?request=id%20with%2Fspace%3F"
        assert client.get("/admin/requests/id%20with%2Fspace%3F/snapshot").status_code == 200


def test_request_snapshot_corrupt_graph_is_500_not_an_empty_graph(tmp_path):
    settings = test_settings(tmp_path)
    app = create_app(settings)
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO graph_snapshots (snapshot_id, tree_id, snapshot_revision, graph, created_at) "
            "VALUES ('bad-graph', 'main', NULL, '{not json', '2026-11-20T10:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO request_snapshots (request_id, graph_snapshot_id, trace, created_at) "
            "VALUES ('bad-request', 'bad-graph', '[]', '2026-11-20T10:00:00+08:00')"
        )
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as client:
        response = client.get("/admin/requests/bad-request/snapshot")
        assert response.status_code == 500
        assert response.json() == {"detail": "request snapshot is corrupt"}


def test_request_snapshot_normalizes_legacy_v5_display_metadata(tmp_path):
    """Rows written before canonical storage keep their ID but hide note metadata."""
    app = create_app(test_settings(tmp_path))
    document = dict(
        tree_dict(departments=["财务部"]),
        enabled=True,
        tree_revision="legacy-tree-revision",
        updated_at="2026-11-20T00:00:00+08:00",
        document_sha256="legacy-document-hash",
    )
    conn = connect(app.state.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO graph_snapshots "
            "(snapshot_id, tree_id, snapshot_revision, graph, created_at) "
            "VALUES ('legacy-v5', 'main', 'legacy-snapshot-revision', ?, ?)",
            (json.dumps(document), "2026-11-20T10:00:00+08:00"),
        )
        conn.execute(
            "INSERT INTO request_snapshots "
            "(request_id, graph_snapshot_id, trace, created_at) "
            "VALUES ('legacy-request', 'legacy-v5', '[]', ?)",
            ("2026-11-20T10:00:00+08:00",),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        response = client.get("/admin/requests/legacy-request/snapshot")
        assert response.status_code == 200
        body = response.json()
        # 历史行 ID 不重算：旧 v5 快照仍按自己写入时的哈希寻址。
        assert body["graph_snapshot_id"] == "legacy-v5"
        assert body["tree"]["canvas"] == document["canvas"]
        assert not {
            "enabled", "default", "tree_revision", "updated_at", "document_sha256"
        } & set(body["tree"])
        assert body["tree"]["departments"] == ["财务部"]
