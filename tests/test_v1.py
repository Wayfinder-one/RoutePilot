"""`/v1/chat/completions` 真链路：MockTransport 分流 `/v1/systemone` 与假 OpenAI 上游。

覆盖 M1 验收矩阵（spec §7.2）：selected 直连、数值 AND、两波追问、统一兜底模型、
决策失败（缺答案 key / 非法概率 / timeout）、同波合并一次调用、粘性二次请求
仍然执行被激活的问题再 preferred 调度、移动节点不改变 revision、首选 target 熔断
返回 503 且不调用次优上游、流式/非流式/取消的 lease 释放、SQLite waves 与内存
answers 的边界，以及 `x-sr-selection` 取值集合。
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from fastapi import FastAPI

from router.api.routes_v1 import router
from router.config import Settings
from router.jev.client import JevClient
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.request_logging import LOG_COLUMNS, RequestLogger
from router.routing.affinity import SessionAffinity
from router.routing.conditions import TimePeriod
from router.routing.graph.compiled import compile_graph
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.tree import tree_from_dict
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import build_snapshot
from router.store.db import connect, migrate
from router.upstream.breaker import CircuitBreaker
from router.upstream.catalog import Backend
from tests.routing_fixtures import tree_dict


JEV_SETTINGS = JevSettings(
    "http://jev.test", "NeoHorse-Jev-4B", "secret", 5,
    DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE,
)
SELECTION_VALUES = {"target", "sticky", "otherwise", "error_fallback"}
MODELS = ("fast", "strong", "power", "strong-a", "strong-b", "safe")
CAPABILITIES = {model: f"{model} 能力" for model in MODELS}


class RecordingBalancer(LoadBalancer):
    """记录 acquire 的 preferred，其余调度语义复用真实 LoadBalancer。"""

    def __init__(self, breaker=None):
        super().__init__(breaker)
        self.calls = []

    def acquire(self, models, *, preferred=None, dry_run=False):
        self.calls.append({"models": list(models), "preferred": preferred, "dry_run": dry_run})
        return super().acquire(models, preferred=preferred, dry_run=dry_run)


class BlockingTransport(httpx.AsyncBaseTransport):
    """所有请求都挂起，用于客户端取消路径。"""

    def __init__(self):
        self.started = asyncio.Event()

    async def handle_async_request(self, request):
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


# ── graph fixtures ──────────────────────────────────────────────────────


def _start():
    return {"id": "start", "type": "start", "position": {"x": 0, "y": 0}}


def _target(node_id, priority, models):
    return {"id": node_id, "type": "target", "priority": priority,
            "position": {"x": 400, "y": priority * 100},
            "models": [{"model": model, "reasoning": None} for model in models]}


def _edge(edge_id, source, source_port, target, target_port, condition=None):
    return {"id": edge_id, "source": source, "source_port": source_port,
            "target": target, "target_port": target_port, "condition": condition}


def _graph(nodes, edges, *, error="safe"):
    return {"id": "root", "type": "graph", "nodes": nodes, "edges": edges,
            "error_fallback": error}


def switch_graph():
    """start → choice(fast/strong) → 两个 target；safe 兜底。"""
    return _graph(
        [
            _start(),
            {"id": "q", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位", "position": {"x": 200, "y": 0},
             "options": [
                 {"key": "fast", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            _target("fast-target", 1, ("fast",)),
            _target("strong-target", 2, ("strong-a", "strong-b")),
            _target("safe", 3, ("safe",)),
        ],
        [
            _edge("e-start-q", "start", "out", "q", "in_1"),
            _edge("e-q-fast", "q", "selected.fast", "fast-target", "in_1"),
            _edge("e-q-strong", "q", "selected.strong", "strong-target", "in_1"),
        ],
    )


def no_target_graph():
    """只有 selected.fast 连到 target；选 strong 时没有任何 target 激活。"""
    graph = switch_graph()
    graph["edges"] = [
        _edge("e-start-q", "start", "out", "q", "in_1"),
        _edge("e-q-fast", "q", "selected.fast", "fast-target", "in_1"),
    ]
    return graph


def direct_graph():
    """start → target，无问题节点（不触碰决策服务）。"""
    return _graph(
        [_start(), _target("direct", 1, ("safe",))],
        [_edge("e-start-direct", "start", "out", "direct", "in_1")],
        error="direct",
    )


def period_condition_graph():
    """direct_graph 再加一个真实时段条件（condition/source=periods）。"""
    return _graph(
        [
            _start(),
            {"id": "cond-peak", "type": "condition", "source": "periods",
             "periods": ["peak"], "position": {"x": 200, "y": 0}},
            _target("direct", 1, ("safe",)),
        ],
        [
            _edge("e-start-direct", "start", "out", "direct", "in_1"),
            _edge("e-cond-direct", "cond-peak", "out", "direct", "in_2"),
        ],
        error="direct",
    )


def all_day_peak():
    # start == end 在 matching_periods 里表示全天生效（HH:MM 不允许 24:00）。
    return [TimePeriod("peak", (0, 1, 2, 3, 4, 5, 6), "00:00", "00:00")]


def two_question_graph():
    """同一波两个问题：choice + noul，数值 AND 组合到 power-target。"""
    return _graph(
        [
            _start(),
            {"id": "q-cap", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位", "position": {"x": 180, "y": -100},
             "options": [
                 {"key": "fast", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "q-domain", "type": "question", "question_type": "noul",
             "instructions": "是否需要电力业务知识？", "position": {"x": 180, "y": 40}},
            {"id": "and-power", "type": "logic", "operator": "and",
             "position": {"x": 400, "y": -40}},
            _target("power-target", 4, ("power",)),
            _target("strong-target", 2, ("strong-a", "strong-b")),
            _target("fast-target", 2, ("fast",)),
            _target("safe", 1, ("safe",)),
        ],
        [
            _edge("e-start-cap", "start", "out", "q-cap", "in_1"),
            _edge("e-start-domain", "start", "out", "q-domain", "in_1"),
            _edge("e-cap-and", "q-cap", "prob.strong", "and-power", "in_1",
                  {"op": "gt", "value": 0.65}),
            _edge("e-domain-and", "q-domain", "yes", "and-power", "in_2",
                  {"op": "gt", "value": 0.7}),
            _edge("e-and-power", "and-power", "out", "power-target", "in_1"),
            _edge("e-cap-strong", "q-cap", "selected.strong", "strong-target", "in_1"),
            _edge("e-cap-fast", "q-cap", "selected.fast", "fast-target", "in_1"),
        ],
    )


def two_wave_graph():
    """第一波 choice；高置信 strong 再激活第二波 noul 追问。"""
    return _graph(
        [
            _start(),
            {"id": "q-cap", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位", "position": {"x": 180, "y": 0},
             "options": [
                 {"key": "fast", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "q-clarify", "type": "question", "question_type": "noul",
             "instructions": "是否需要更深入处理？", "position": {"x": 400, "y": 80}},
            _target("strong-target", 1, ("strong-a", "strong-b")),
            _target("fast-target", 2, ("fast",)),
            _target("safe", 3, ("safe",)),
        ],
        [
            _edge("e-start-cap", "start", "out", "q-cap", "in_1"),
            _edge("e-cap-strong", "q-cap", "selected.strong", "strong-target", "in_1"),
            _edge("e-cap-fast", "q-cap", "selected.fast", "fast-target", "in_1"),
            _edge("e-cap-clarify", "q-cap", "prob.strong", "q-clarify", "in_1",
                  {"op": "gte", "value": 0.5}),
            _edge("e-clarify-strong", "q-clarify", "yes", "strong-target", "in_2",
                  {"op": "gt", "value": 0.5}),
        ],
    )


def priority_graph():
    """selected.strong 同时激活 p4 power-target 与 p3 strong-target（数字越大越优先）。"""
    return _graph(
        [
            _start(),
            {"id": "q", "type": "question", "question_type": "choice",
             "instructions": "选择能力档位", "position": {"x": 200, "y": 0},
             "options": [
                 {"key": "fast", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            _target("power-target", 4, ("power",)),
            _target("strong-target", 3, ("strong-a",)),
            _target("fast-target", 2, ("fast",)),
            _target("safe", 1, ("safe",)),
        ],
        [
            _edge("e-start-q", "start", "out", "q", "in_1"),
            _edge("e-q-power", "q", "selected.strong", "power-target", "in_1"),
            _edge("e-q-strong", "q", "selected.strong", "strong-target", "in_1"),
            _edge("e-q-fast", "q", "selected.fast", "fast-target", "in_1"),
        ],
    )


# ── harness ────────────────────────────────────────────────────────────


def make_app(tmp_path, handler, *, root=None, active=True, sticky=None, transport=None,
             time_periods=None):
    path = str(tmp_path / "v1.db")
    conn = connect(path)
    migrate(conn)
    conn.close()
    app = FastAPI()
    app.state.settings = Settings(db_path=path)
    app.state.http = httpx.AsyncClient(transport=transport or httpx.MockTransport(handler))
    app.state.breaker = CircuitBreaker()
    app.state.balancer = RecordingBalancer(app.state.breaker)
    app.state.affinity = SessionAffinity()
    app.state.jev_client = JevClient(app.state.http)
    app.state.routing_service = RoutingService(
        GraphEvaluator(app.state.jev_client),
        ModelScheduler(app.state.affinity, app.state.balancer),
    )
    app.state.requestlog = RequestLogger(app.state.settings.db_path)
    backends = {
        model: Backend(
            url=f"http://{model}.test/v1/chat/completions",
            headers={}, upstream_name=model, provider="local",
        )
        for model in MODELS
    }
    tree = None
    if active:
        tree = tree_from_dict(tree_dict(
            "main",
            root=root or switch_graph(),
            sticky=sticky or {"enabled": False, "ttl_seconds": 1800},
        ))
    app.state.snapshot = build_snapshot(
        tree=tree, backends=backends,
        time_periods=[] if time_periods is None else time_periods,
        jev=JEV_SETTINGS, capabilities=CAPABILITIES,
        compiled=compile_graph(tree.root) if tree is not None else None,
    )
    app.include_router(router, prefix="/v1")
    return app


def payload(text="请求", *, stream=False):
    return {"model": "ignored", "messages": [{"role": "user", "content": text}], "stream": stream}


def choice(*, fast=0.2, strong=0.8):
    return {"type": "choice", "probabilities": {"轻量模型": fast, "增强模型": strong}}


def noul(yes):
    return {"type": "noul", "noul": yes}


def upstream_ok(request):
    model = json.loads(request.content)["model"]
    return httpx.Response(200, json={
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    })


class Harness:
    """按 host 分流：jev.test → 决策服务应答；其余 → 假 OpenAI 上游。"""

    def __init__(self, answers, *, jev_failure=None):
        self.answers = answers
        self.jev_failure = jev_failure
        self.jev_requests = []
        self.upstream_models = []

    def __call__(self, request):
        if request.url.host == "jev.test":
            self.jev_requests.append(json.loads(request.content))
            if self.jev_failure is not None:
                raise self.jev_failure
            return httpx.Response(200, json={"answers": self.answers, "usage": {"input_tokens": 10}})
        self.upstream_models.append(json.loads(request.content)["model"])
        return upstream_ok(request)


async def post(app, body=None, *, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        return await client.post("/v1/chat/completions", json=body or payload(), headers=headers)


def recent(app):
    return app.state.requestlog.recent_requests(1)[0]


async def flush(app):
    await app.state.requestlog.flush()


# ── selected direct / numeric AND / waves ──────────────────────────────


def test_selected_target_uses_real_systemone_chain(tmp_path):
    async def scenario():
        har = Harness({"q": choice(fast=0.9, strong=0.1)})
        app = make_app(tmp_path, har, root=switch_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "target"
        assert response.headers["x-sr-model"] == "fast"
        assert response.headers["x-sr-selection"] in SELECTION_VALUES
        assert len(har.jev_requests) == 1
        assert list(har.jev_requests[0]["questions"]) == ["q"]
        assert har.upstream_models == ["fast"]
        record = recent(app)
        assert record["selection_reason"] == "target"
        assert record["jev_model"] == "NeoHorse-Jev-4B"
        assert record["jev_choice"] == "fast"
        assert record["jev_top"] == pytest.approx(0.9)
        assert record["jev_margin"] == pytest.approx(0.8)
        assert record["jev_probabilities"] == {
            "fast": pytest.approx(0.9), "strong": pytest.approx(0.1),
        }
        assert record["waves"] == 1
        assert record["answers"]["q"]["probabilities"]["fast"] == pytest.approx(0.9)
        assert record["route_label"] == {"zh": "目标命中", "en": "Target selected"}
        assert [pair[0]["en"] for pair in record["metrics"]] == ["Upstream", "Routing", "Total", "Waves"]
        assert record["metrics"][0][1] == 200
        assert record["metrics"][3][1] == 1
        assert record["fields"][0] == [{"zh": "请求 ID", "en": "Request ID"}, record["id"]]
        assert [event["event"] for event in record["trace"]].count("request_outcome") == 1
        assert record["trace"][-1]["phase"] == "outcome"
        assert record["trace"][-1]["tone"] == "hit"
        assert len(app.state.requestlog.recent_requests()) == 1
        assert app.state.balancer.inflight("fast") == 0

    asyncio.run(scenario())


def test_numeric_and_merges_two_questions_into_one_systemone_call(tmp_path):
    async def scenario():
        har = Harness({"q-cap": choice(fast=0.1, strong=0.9), "q-domain": noul(0.95)})
        app = make_app(tmp_path, har, root=two_question_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "target"
        assert response.headers["x-sr-model"] == "power"
        # 同波两个问题只收到一次 systemone 请求，问题按 node_id 排序合并。
        assert len(har.jev_requests) == 1
        assert list(har.jev_requests[0]["questions"]) == ["q-cap", "q-domain"]
        assert har.upstream_models == ["power"]
        record = recent(app)
        assert record["waves"] == 1
        assert set(record["answers"]) == {"q-cap", "q-domain"}
        question_events = [event for event in record["trace"] if event["event"] == "question"]
        assert [event["node_id"] for event in question_events] == ["q-cap", "q-domain"]
        # 兼容列取第一波按 node_id 排序后的首个 ChoiceAnswer。
        assert record["jev_choice"] == "strong"
        assert record["jev_top"] == pytest.approx(0.9)
        assert record["jev_ms"] == pytest.approx(record["answers"]["q-cap"]["duration_ms"])
        await flush(app)
        conn = connect(app.state.settings.db_path)
        row = dict(conn.execute("SELECT * FROM request_logs").fetchone())
        conn.close()
        assert row["waves"] == 1
        assert row["jev_choice"] == "strong"
        assert set(LOG_COLUMNS) & {"answers", "trace", "text", "route_label", "metrics", "fields"} == set()
        assert "answers" in recent(app) and "trace" in recent(app)
        assert app.state.balancer.inflight("power") == 0

    asyncio.run(scenario())


def test_two_wave_clarify_calls_systemone_once_per_wave_and_logs_wave_count(tmp_path):
    async def scenario():
        har = Harness({"q-cap": choice(fast=0.1, strong=0.9), "q-clarify": noul(0.9)})
        app = make_app(tmp_path, har, root=two_wave_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "target"
        assert response.headers["x-sr-model"] == "strong-a"
        assert [list(request["questions"]) for request in har.jev_requests] == [
            ["q-cap"], ["q-clarify"],
        ]
        assert har.upstream_models == ["strong-a"]
        record = recent(app)
        assert record["waves"] == 2
        assert record["jev_choice"] == "strong"
        assert record["jev_ms"] == pytest.approx(record["answers"]["q-cap"]["duration_ms"])
        wave_events = [event for event in record["trace"] if event["event"] == "wave"]
        assert [event["wave"] for event in wave_events] == [1, 2]
        assert set(record["answers"]) == {"q-cap", "q-clarify"}
        await flush(app)
        conn = connect(app.state.settings.db_path)
        row = dict(conn.execute("SELECT * FROM request_logs").fetchone())
        columns = {item[1] for item in conn.execute("PRAGMA table_info(request_logs)")}
        conn.close()
        assert row["waves"] == 2
        assert "answers" not in columns and "trace" not in columns
        assert app.state.balancer.inflight("strong-a") == 0

    asyncio.run(scenario())


def test_fallback_model_when_no_target_activates(tmp_path):
    async def scenario():
        har = Harness({"q": choice(fast=0.1, strong=0.9)})
        app = make_app(tmp_path, har, root=no_target_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "error_fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]
        record = recent(app)
        assert record["selection_reason"] == "error_fallback"
        assert record["fallback_reason"] == "error_fallback"
        assert record["route_label"] == {"zh": "默认目标兜底", "en": "Default target fallback"}
        assert record["jev_choice"] == "strong"
        await flush(app)
        conn = connect(app.state.settings.db_path)
        assert conn.execute("SELECT waves FROM request_logs").fetchone()[0] == 1
        conn.close()

    asyncio.run(scenario())


# ── error_fallback 三态 ────────────────────────────────────────────────


def test_missing_answer_key_uses_error_fallback(tmp_path):
    async def scenario():
        har = Harness({})
        app = make_app(tmp_path, har, root=switch_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "error_fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]
        record = recent(app)
        assert record["selection_reason"] == "error_fallback"
        assert record["fallback_reason"] == "error_fallback"
        assert record["jev_choice"] is None
        assert record["jev_top"] is None and record["jev_margin"] is None
        assert record["waves"] == 1
        failed = [event for event in record["trace"] if event["event"] == "wave"]
        assert failed[0]["failed"] is True and failed[0]["error"]
        assert app.state.balancer.inflight("safe") == 0

    asyncio.run(scenario())


def test_invalid_probabilities_use_error_fallback(tmp_path):
    async def scenario():
        har = Harness({"q": {"type": "choice", "probabilities": {"轻量模型": 0.9, "增强模型": 0.9}}})
        app = make_app(tmp_path, har, root=switch_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "error_fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]

    asyncio.run(scenario())


def test_timeout_uses_error_fallback(tmp_path):
    async def scenario():
        har = Harness({}, jev_failure=httpx.ReadTimeout("timed out"))
        app = make_app(tmp_path, har, root=switch_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "error_fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]
        assert recent(app)["fallback_reason"] == "error_fallback"

    asyncio.run(scenario())


# ── sticky / revision / circuit breaker ────────────────────────────────


def test_sticky_second_request_reruns_question_then_prefers_model(tmp_path):
    async def scenario():
        har = Harness({"q": choice(fast=0.1, strong=0.9)})
        app = make_app(
            tmp_path, har, root=switch_graph(),
            sticky={"enabled": True, "ttl_seconds": 1800},
        )
        headers = {"x-sr-session-id": "session-1"}
        first = await post(app, headers=headers)
        assert first.status_code == 200
        assert first.headers["x-sr-model"] == "strong-a"
        assert first.headers["x-sr-selection"] == "target"
        assert app.state.balancer.calls[-1]["preferred"] is None
        assert app.state.balancer.inflight("strong-a") == 0
        first_revision = app.state.snapshot.tree_revision

        # 仅移动节点（position 属于纯布局字段）：behavior revision 不变。
        moved = copy.deepcopy(switch_graph())
        for node in moved["nodes"]:
            node["position"] = {
                "x": node["position"]["x"] + 40,
                "y": node["position"]["y"] + 40,
            }
        moved_tree = tree_from_dict(tree_dict(
            "main", root=moved,
            sticky={"enabled": True, "ttl_seconds": 1800},
        ))
        moved_snapshot = build_snapshot(
            tree=moved_tree,
            backends=app.state.snapshot.backends, time_periods=[],
            jev=JEV_SETTINGS, capabilities=CAPABILITIES,
            compiled=compile_graph(moved_tree.root),
        )
        assert moved_snapshot.tree_revision == first_revision
        app.state.snapshot = moved_snapshot

        second = await post(app, headers=headers)
        assert second.status_code == 200
        assert second.headers["x-sr-model"] == "strong-a"
        assert second.headers["x-sr-selection"] == "sticky"
        # 第二次请求仍先执行被激活的问题，再做 preferred 调度。
        assert [list(request["questions"]) for request in har.jev_requests] == [["q"], ["q"]]
        assert app.state.balancer.calls[-1]["preferred"] == "strong-a"
        assert har.upstream_models == ["strong-a", "strong-a"]
        record = recent(app)
        assert record["sticky_hit"] == 1
        assert record["selection_reason"] == "sticky"
        assert record["answers"]["q"]["ranking"][0] == "strong"

    asyncio.run(scenario())


def test_preferred_target_circuit_open_returns_503_without_calling_next_upstream(tmp_path):
    async def scenario():
        har = Harness({"q": choice(fast=0.1, strong=0.9)})
        app = make_app(tmp_path, har, root=priority_graph())
        for _ in range(app.state.breaker.threshold):
            app.state.breaker.failure("power")
        assert app.state.breaker.is_open("power") is True
        response = await post(app)
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_available_backend"
        # p1 power-target 熔断后直接 503，不静默改选 p2 strong-target 上游。
        assert har.upstream_models == []
        assert app.state.balancer.inflight("power") == 0
        assert app.state.balancer.inflight("strong-a") == 0
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert record["error_class"] == "no_available_backend"
        assert [event["event"] for event in record["trace"][-2:]] == ["schedule", "routing_error"]
        assert record["trace"][-2]["unavailable"] == {"power": "breaker_open"}
        assert record["trace"][-1]["stage"] == "schedule"
        assert record["routing_ms"] > 0
        assert record["total_ms"] >= record["routing_ms"]
        assert record["target_id"] == "power-target"
        assert record["selection_reason"] == "target"
        assert record["policy_path"] == "power-target"
        assert record["fallback_reason"] is None

    asyncio.run(scenario())


# ── node labels / routing failure logs ────────────────────────────────


def test_live_log_uses_node_titles_from_the_serving_snapshot(tmp_path):
    async def scenario():
        root = switch_graph()
        next(item for item in root["nodes"] if item["id"] == "q")["title"] = "能力选择"
        app = make_app(tmp_path, Harness({"q": choice()}), root=root)
        response = await post(app)

        assert response.status_code == 200
        record = recent(app)
        assert record["node_labels"]["q"] == {"title": "能力选择", "type": "question"}
        assert record["node_labels"]["fast-target"]["type"] == "target"
        assert any(event["event"] == "schedule" for event in record["trace"])

    asyncio.run(scenario())


def test_no_active_tree_creates_a_routing_error_log(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), active=False)
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_active_tree"
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert record["error_class"] == "no_active_tree"
        failure = record["trace"][-1]
        assert {key: failure[key] for key in ("event", "stage", "error_class", "message")} == {
            "event": "routing_error", "stage": "graph",
            "error_class": "no_active_tree", "message": "no routing tree is enabled",
        }
        assert failure["phase"] == "routing_error"
        assert failure["label"] == {"zh": "路由中止", "en": "Routing stopped"}
        assert failure["facts"] == [[{"zh": "原因", "en": "Reason"}, "no_active_tree"]]
        assert failure["tone"] == "bad"
        assert record["route_label"] == {"zh": "路由中止", "en": "Routing stopped"}
        assert not any(event["event"] == "request_outcome" for event in record["trace"])
        assert len(app.state.requestlog.recent_requests()) == 1
        assert record["node_labels"] == {}
        assert record["model"] is None
        assert record["target_id"] is None
        assert record["selection_reason"] is None
        assert record["policy_path"] is None
        assert record["fallback_reason"] is None

    asyncio.run(scenario())


def test_invalid_json_does_not_create_a_calculation_log(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), root=direct_graph())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions", content="{",
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 400
        assert app.state.requestlog.recent_requests() == []

    asyncio.run(scenario())


def test_invariant_failure_keeps_partial_graph_trace(tmp_path):
    async def scenario():
        root = _graph([_start()], [], error="ghost")
        app = make_app(tmp_path, lambda request: httpx.Response(500), root=root)
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "routing_invariant_violation"
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert [event["event"] for event in record["trace"][:2]] == ["time", "window"]
        assert record["trace"][-1]["error_class"] == "routing_invariant_violation"
        assert record["trace"][-1]["stage"] == "graph"
        assert record["target_id"] is None
        assert record["selection_reason"] is None
        assert record["policy_path"] is None
        assert record["fallback_reason"] is None

    asyncio.run(scenario())


def test_no_candidate_schedule_failure_preserves_fallback_target(tmp_path):
    async def scenario():
        har = Harness({"q": choice(fast=0.1, strong=0.9)})
        app = make_app(tmp_path, har, root=no_target_graph())
        for _ in range(app.state.breaker.threshold):
            app.state.breaker.failure("safe")

        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_available_backend"
        assert har.upstream_models == []
        record = recent(app)
        final = next(event for event in record["trace"] if event["event"] == "final")
        assert final["kind"] == "otherwise"
        assert record["outcome_state"] == "routing_error"
        assert record["target_id"] == "safe"
        assert record["selection_reason"] == "error_fallback"
        assert record["policy_path"] == "safe"
        assert record["fallback_reason"] == "error_fallback"

        await flush(app)
        conn = connect(app.state.settings.db_path)
        try:
            row = dict(conn.execute(
                "SELECT target_id, selection_reason, policy_path, outcome_state FROM request_logs"
            ).fetchone())
        finally:
            conn.close()
        assert row == {
            "target_id": "safe",
            "selection_reason": "error_fallback",
            "policy_path": "safe",
            "outcome_state": "routing_error",
        }

    asyncio.run(scenario())


def test_unknown_routing_failure_uses_a_safe_console_message(tmp_path):
    class BrokenEngine:
        async def route(self, snapshot, request, *, trace_sink=None):
            trace_sink.append({
                "event": "time", "active_periods": [], "timezone": "Asia/Shanghai",
            })
            raise ValueError("sensitive internal detail")

    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), root=direct_graph())
        app.state.routing_service = BrokenEngine()
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "routing_failed"
        record = recent(app)
        assert record["error_class"] == "routing_failed"
        assert record["trace"][-1]["message"] == "routing failed"
        assert "sensitive internal detail" not in str(record)

    asyncio.run(scenario())


def test_missing_snapshot_creates_a_snapshot_stage_failure(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), root=direct_graph())
        app.state.snapshot = None
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_valid_snapshot"
        record = recent(app)
        assert record["node_labels"] == {}
        assert record["trace"][-1]["stage"] == "snapshot"
        assert record["trace"][-1]["error_class"] == "no_valid_snapshot"

    asyncio.run(scenario())


# ── trace presentation: time phase follows the graph, not the deployment ─


def test_time_event_stays_initial_when_no_period_condition_ran(tmp_path):
    """部署配置了时段，但本图没有时段条件：time 事件仍属于「初始计算」。"""

    async def scenario():
        app = make_app(tmp_path, Harness({}), root=direct_graph(),
                       time_periods=all_day_peak())
        response = await post(app)

        assert response.status_code == 200
        trace = recent(app)["trace"]
        time_event = trace[0]
        assert time_event["event"] == "time"
        assert time_event["phase"] == "initial"
        assert time_event["label"] == {"zh": "初始计算", "en": "Initial evaluation"}
        assert app.state.balancer.inflight("safe") == 0

    asyncio.run(scenario())


def test_time_event_is_period_when_a_real_period_condition_ran(tmp_path):
    async def scenario():
        app = make_app(tmp_path, Harness({}), root=period_condition_graph(),
                       time_periods=all_day_peak())
        response = await post(app)

        assert response.status_code == 200
        trace = recent(app)["trace"]
        time_event = trace[0]
        assert time_event["event"] == "time"
        assert time_event["phase"] == "period"
        assert time_event["label"] == {"zh": "时段判断", "en": "Time period"}
        condition = next(event for event in trace if event["event"] == "condition")
        assert condition["source"] == "periods"
        assert condition["value"] is True
        assert condition["phase"] == "period"
        assert app.state.balancer.inflight("safe") == 0

    asyncio.run(scenario())


# ── upstream / lease / stream paths ────────────────────────────────────


def test_no_active_tree_fails_before_any_http_call(tmp_path):
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(500)

        app = make_app(tmp_path, handler, active=False)
        response = await post(app)
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_active_tree"
        assert calls == []

    asyncio.run(scenario())


def test_stream_completion_releases_lease_and_records_usage(tmp_path):
    async def scenario():
        jev_calls = []

        def handler(request):
            if request.url.host == "jev.test":
                jev_calls.append(request)
                raise AssertionError("direct target must skip JEV")
            body = b'data: {"choices":[]}\n\ndata: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\ndata: [DONE]\n\n'

            class Chunks(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield body

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

        app = make_app(tmp_path, handler, root=direct_graph())
        response = await post(app, payload(stream=True))
        assert response.status_code == 200
        assert b"[DONE]" in response.content
        assert jev_calls == []
        assert app.state.balancer.inflight("safe") == 0
        record = recent(app)
        assert record["outcome_state"] == "completed"
        assert record["total_tokens"] == 3
        assert record["answers"] == {}
        assert record["trace"][-1]["event"] == "request_outcome"
        assert record["fields"][-1][1] == 3

    asyncio.run(scenario())


def test_stream_upstream_error_closes_response_and_releases_lease_once(tmp_path, monkeypatch):
    """stream=true + 上游 5xx：JSON 502 返回、上游响应关闭一次、租约释放一次。"""
    from router.routing import scheduler as scheduler_module

    released = []
    lease_release = scheduler_module.Lease.release

    def counting_release(self):
        released.append(self.model)
        lease_release(self)

    monkeypatch.setattr(scheduler_module.Lease, "release", counting_release)
    closes = []

    class ErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":"down"}'

        async def aclose(self):
            closes.append(True)

    def handler(request):
        return httpx.Response(
            503, headers={"content-type": "application/json"}, stream=ErrorStream(),
        )

    async def scenario():
        app = make_app(tmp_path, handler, root=direct_graph())
        response = await post(app, payload(stream=True))

        # 客户端可见的响应仍是现有 handler 的 upstream_error 映射。
        assert response.status_code == 502
        assert response.headers["content-type"] == "application/json"
        assert response.headers["x-sr-model"] == "safe"
        assert response.headers["x-sr-selection"] == "target"
        assert response.json() == {"error": {"message": "upstream_error"}}
        # 上游响应恰好关闭一次，租约恰好释放一次（未移交 _relay，也没有 double release）。
        assert closes == [True]
        assert released == ["safe"]
        assert app.state.balancer.inflight("safe") == 0
        record = recent(app)
        assert record["outcome_state"] == "upstream_error"
        assert record["error_class"] == "upstream_error"
        assert record["metrics"][0][1] == 503

    asyncio.run(scenario())


def test_stream_lease_is_held_while_the_body_is_iterated(tmp_path, monkeypatch):
    """hand-off 后租约归 _relay 所有：第二个分片未到之前 inflight 必须仍为 1。"""
    from router.routing import scheduler as scheduler_module

    released = []
    lease_release = scheduler_module.Lease.release

    def counting_release(self):
        released.append(self.model)
        lease_release(self)

    monkeypatch.setattr(scheduler_module.Lease, "release", counting_release)
    closes = []
    gate = None

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\n\n"
            await gate.wait()
            yield b"data: second\n\n"

        async def aclose(self):
            closes.append(True)

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()

        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=GatedStream(),
            )

        app = make_app(tmp_path, handler, root=direct_graph())
        request_body = json.dumps(payload(stream=True)).encode()
        request_started = False
        messages = asyncio.Queue()

        async def receive():
            nonlocal request_started
            if not request_started:
                request_started = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            await asyncio.Event().wait()  # 客户端在测试期间保持连接。

        async def send(message):
            await messages.put(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"router.test"),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("router.test", 80),
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            while True:
                message = await asyncio.wait_for(messages.get(), 2)
                if message["type"] == "http.response.start":
                    assert message["status"] == 200
                elif message["type"] == "http.response.body" and message.get("body"):
                    first = message["body"]
                    break
            # 第一个 SSE 分片已发出，第二个仍被上游 gate 挡住：租约必须被持有。
            assert first == b"data: first\n\n"
            assert app.state.balancer.inflight("safe") == 1
            assert released == []
            gate.set()
            chunks = [first]
            while True:
                message = await asyncio.wait_for(messages.get(), 2)
                if message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body", False):
                        break
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert b"".join(chunks) == b"data: first\n\ndata: second\n\n"
        assert closes == [True]
        assert released == ["safe"]
        assert app.state.balancer.inflight("safe") == 0
        assert recent(app)["outcome_state"] == "completed"

    asyncio.run(scenario())


def test_terminal_result_is_appended_only_once():
    import time
    from router.api.request_presenter import OutcomeRecorder

    class Logger:
        def __init__(self):
            self.results = []

        def enqueue_result(self, record):
            self.results.append(record)

    logger = Logger()
    record = {"trace": [], "model": "safe", "routing_ms": 2.0, "waves": 0}
    outcome = OutcomeRecorder(logger, record, time.monotonic())
    assert outcome.finish(outcome_state="completed", upstream_status=200) is True
    assert outcome.finish(outcome_state="upstream_error", upstream_status=503) is False
    assert len(logger.results) == 1
    assert [event["event"] for event in record["trace"]] == ["request_outcome"]
    assert record["trace"][0]["outcome_state"] == "completed"


def test_upstream_5xx_maps_to_502_and_releases_lease(tmp_path):
    async def scenario():
        def handler(request):
            return httpx.Response(503, json={"error": "down"})

        app = make_app(tmp_path, handler, root=direct_graph())
        response = await post(app)
        assert response.status_code == 502
        assert app.state.balancer.inflight("safe") == 0
        record = recent(app)
        assert record["outcome_state"] == "upstream_error"
        assert record["trace"][-1]["event"] == "request_outcome"
        assert record["trace"][-1]["tone"] == "bad"

    asyncio.run(scenario())


def test_cancellation_during_upstream_records_terminal_state_and_releases(tmp_path):
    async def scenario():
        transport = BlockingTransport()
        app = make_app(tmp_path, None, root=direct_graph(), transport=transport)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test",
        ) as client:
            task = asyncio.create_task(client.post("/v1/chat/completions", json=payload()))
            await asyncio.wait_for(transport.started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert app.state.balancer.inflight("safe") == 0
        assert recent(app)["outcome_state"] == "cancelled"

    asyncio.run(scenario())


def test_stream_disconnect_does_not_count_as_upstream_failure(tmp_path):
    from router.upstream.proxy import UpstreamProxy

    class Response:
        status_code = 200

        def __init__(self):
            self.closed = False

        async def aiter_raw(self):
            yield b"data: first\n\n"
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    class Breaker:
        """Records breaker calls; a disconnect must not reach failure()."""

        def __init__(self):
            self.failures = []
            self.successes = []

        def failure(self, model):
            self.failures.append(model)

        def success(self, model):
            self.successes.append(model)

    class Outcome:
        def finish(self, **kwargs):
            self.kwargs = kwargs

    async def scenario():
        response = Response()
        outcome = Outcome()
        released = []
        backend = Backend(url="", headers={}, upstream_name="safe")
        proxy = UpstreamProxy(client=None, breaker=Breaker())
        generator = proxy._relay(response, model="safe", outcome=outcome, is_disconnected=None,
                                 backend=backend, upstream_started=0.0, upstream_ms=1.0,
                                 release=lambda: released.append(True))
        assert await generator.__anext__() == b"data: first\n\n"
        await generator.aclose()
        assert response.closed is True
        assert released == [True]
        assert outcome.kwargs["outcome_state"] == "client_disconnected"
        assert proxy.breaker.failures == []  # client disconnect is not an upstream fault

    asyncio.run(scenario())


def test_mid_stream_upstream_error_counts_in_breaker(tmp_path):
    from router.upstream.proxy import UpstreamProxy

    class Response:
        status_code = 200

        def __init__(self):
            self.closes = 0

        async def aiter_raw(self):
            yield b"data: first\n\n"
            raise httpx.ReadError("upstream stream died")

        async def aclose(self):
            self.closes += 1

    class Outcome:
        def finish(self, **kwargs):
            self.kwargs = kwargs

    async def scenario():
        response = Response()
        outcome = Outcome()
        released = []
        backend = Backend(url="", headers={}, upstream_name="safe")
        proxy = UpstreamProxy(client=None, breaker=CircuitBreaker(threshold=1))
        generator = proxy._relay(response, model="safe", outcome=outcome, is_disconnected=None,
                                 backend=backend, upstream_started=0.0, upstream_ms=1.0,
                                 release=lambda: released.append(True))
        assert await generator.__anext__() == b"data: first\n\n"
        with pytest.raises(httpx.ReadError):
            await generator.__anext__()
        assert proxy.breaker.is_open("safe") is True
        assert outcome.kwargs["outcome_state"] == "upstream_error"
        assert response.closes == 1
        assert released == [True]

    asyncio.run(scenario())


def test_stream_generator_closes_response_and_releases_lease_once(tmp_path):
    from router.upstream.proxy import UpstreamProxy

    class Response:
        status_code = 200

        def __init__(self):
            self.closes = 0

        async def aiter_raw(self):
            yield b"data: a\n\n"
            yield b"data: b\n\n"

        async def aclose(self):
            self.closes += 1

    class Breaker:
        def failure(self, model):
            raise AssertionError("a clean stream must not trip the breaker")

    class Outcome:
        def finish(self, **kwargs):
            self.kwargs = kwargs

    async def scenario():
        response = Response()
        outcome = Outcome()
        released = []
        backend = Backend(url="", headers={}, upstream_name="safe")
        proxy = UpstreamProxy(client=None, breaker=Breaker())
        generator = proxy._relay(response, model="safe", outcome=outcome, is_disconnected=None,
                                 backend=backend, upstream_started=0.0, upstream_ms=1.0,
                                 release=lambda: released.append(True))
        chunks = [chunk async for chunk in generator]
        assert chunks == [b"data: a\n\n", b"data: b\n\n"]
        assert response.closes == 1
        assert released == [True]
        assert outcome.kwargs["outcome_state"] == "completed"

    asyncio.run(scenario())


def test_invalid_json_is_a_client_error(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), root=direct_graph())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions", content="{",
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "invalid_json"

    asyncio.run(scenario())
