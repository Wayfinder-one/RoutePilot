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
from dataclasses import replace
from urllib.parse import quote

import httpx
import pytest

from router.api.request_presenter import present_record
from router.api.routes_v1 import decode_department
from router.decision_events import replay_payload, summary_payload
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.routing.conditions import TimePeriod
from router.routing.graph.compiled import compile_graph
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.tree import tree_from_dict
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import assemble_snapshot_set, build_snapshot
from router.store.db import connect, migrate
from router.upstream.breaker import CircuitBreaker
from router.upstream.catalog import Backend
from router.upstream.runtime_keys import runtime_model_key, tree_scope
from tests.routing_fixtures import (
    deliver_decision_events,
    kernel_state,
    recording_core_app,
    seed_execution_catalog,
    split_app,
    test_settings,
    tree_dict,
)


JEV_SETTINGS = JevSettings(
    "http://jev.test", "NeoHorse-Jev-4B", "secret", 5,
    DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE,
)
SELECTION_VALUES = {"target", "sticky", "otherwise", "error_fallback"}
MODELS = ("fast", "strong", "power", "strong-a", "strong-b", "safe")
CAPABILITIES = {model: f"{model} 能力" for model in MODELS}
#: ``make_app`` 默认图 tree.id = "main"；Auto 调度的作用域必须与之一致。
MAIN_SCOPE = tree_scope("main")


class RecordingBalancer(LoadBalancer):
    """记录 acquire 的 preferred，其余调度语义复用真实 LoadBalancer。"""

    def __init__(self, breaker=None):
        super().__init__(breaker)
        self.calls = []

    def acquire(self, models, *, preferred=None, dry_run=False, scope=''):
        self.calls.append({"models": list(models), "preferred": preferred,
                           "dry_run": dry_run, "scope": scope})
        return super().acquire(
            models, preferred=preferred, dry_run=dry_run, scope=scope)


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
    return {"id": "start", "type": "start"}


def _target(node_id, priority, models):
    return {"id": node_id, "type": "target", "priority": priority,
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
             "instructions": "选择能力档位",
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
             "periods": ["peak"]},
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
             "instructions": "选择能力档位",
             "options": [
                 {"key": "fast", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "q-domain", "type": "question", "question_type": "noul",
             "instructions": "是否需要电力业务知识？"},
            {"id": "and-power", "type": "logic", "operator": "and"},
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
             "instructions": "选择能力档位",
             "options": [
                 {"key": "fast", "label": "轻量模型", "capability": "简单请求"},
                 {"key": "strong", "label": "增强模型", "capability": "复杂请求"},
             ]},
            {"id": "q-clarify", "type": "question", "question_type": "noul",
             "instructions": "是否需要更深入处理？"},
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
             "instructions": "选择能力档位",
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
    """Peripheral app + in-process kernel running the real routing service.

    The kernel owns the route set and executes the decision; the peripheral only
    executes the model call (mock ``handler``) and receives the decision event.
    The test route set is built directly (like the pre-split harness) so these
    data-plane cases can keep their exact graphs; kernel-side enable validation is
    covered by the kernel tests.  ``app.test_kernel`` / ``app.test_sink`` expose
    the kernel state and the captured decisions.
    """
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    # 外围执行目录：Auto 执行按内核决策的逻辑模型解析 Provider Key 与上游 URL。
    seed_execution_catalog(
        conn, key="peripheral-secret",
        models=[(model, model, "{}") for model in MODELS])
    conn.close()

    def kernel_only_handler(request):
        raise AssertionError(
            f"kernel made an external request: {request.method} {request.url}")

    kernel, sink = recording_core_app(
        graph_dir=settings.graph_dir,
        transport=(httpx.MockTransport(handler) if handler is not None
                   else httpx.MockTransport(kernel_only_handler)),
    )
    kernel.state.balancer = RecordingBalancer(kernel.state.breaker)
    kernel.state.routing_service = RoutingService(
        GraphEvaluator(kernel.state.jev_client),
        ModelScheduler(kernel.state.affinity, kernel.state.balancer),
    )
    backends = {
        model: Backend(
            url=f"http://{model}.test/v1/chat/completions",
            headers={}, upstream_name=model, provider="local",
        )
        for model in MODELS
    }
    if active:
        tree = tree_from_dict(tree_dict(
            "main",
            root=root or switch_graph(),
            sticky=sticky or {"enabled": False, "ttl_seconds": 1800},
            enabled=True, default=True,
        ))
        snapshot = build_snapshot(
            tree=tree, backends=backends,
            time_periods=[] if time_periods is None else time_periods,
            jev=JEV_SETTINGS, capabilities=CAPABILITIES,
            compiled=compile_graph(tree.root),
        )
        kernel.state.snapshot_set = assemble_snapshot_set(
            [snapshot], fingerprint="test")
    else:
        kernel.state.snapshot_set = assemble_snapshot_set(
            [], fingerprint="empty")
    if transport is None:
        transport = httpx.MockTransport(handler)
    app = split_app(settings=settings, kernel=kernel, transport=transport)
    app.test_sink = sink
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


async def post(app, body=None, *, headers=None, path="/v1/chat/completions"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
    ) as client:
        return await client.post(path, json=body or payload(), headers=headers)


def recent(app):
    """Presented summary of the last kernel decision event.

    The peripheral keeps no request log after the split: every decision fact is
    read from the kernel's complete event (same projection the console list
    endpoint uses).
    """
    return present_record(summary_payload(app.test_sink.events[-1]))


def last_event(app):
    return app.test_sink.events[-1]


async def flush(app):
    """Kept as a no-op: execution results are discarded, there is no local queue."""
    return None


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
        assert [pair[0]["en"] for pair in record["metrics"]] == [
            "Decision time", "Decision model time", "Waves"]
        assert record["metrics"][0][1].endswith("ms")
        assert record["metrics"][2][1] == 1
        assert record["fields"][0] == [{"zh": "请求 ID", "en": "Request ID"}, record["id"]]
        # 外围不再记录执行结果：决策事件只描述路由决策本身。
        assert record["outcome_state"] == "completed"
        assert "request_outcome" not in [
            event["event"] for event in record["trace"]]
        assert len(app.test_sink.events) == 1
        assert kernel_state(app).balancer.inflight("fast", scope=MAIN_SCOPE) == 0

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
        # 决策事件自包含：answers/trace 只在事件里，不再有 SQLite 请求账本列。
        assert set(last_event(app)["answers"]) == {"q-cap", "q-domain"}
        assert "answers" in recent(app) and "trace" in recent(app)
        assert kernel_state(app).balancer.inflight("power", scope=MAIN_SCOPE) == 0

    asyncio.run(scenario())


def test_two_wave_clarify_calls_systemone_once_per_wave_and_logs_wave_count(tmp_path):
    async def scenario():
        har = Harness({"q-cap": choice(fast=0.1, strong=0.9), "q-clarify": noul(0.9)})
        app = make_app(tmp_path, har, root=two_wave_graph())
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "target"
        # strong-target 有两个模型：选择是随机的，只断言落在该目标内。
        model = response.headers["x-sr-model"]
        assert model in ("strong-a", "strong-b")
        assert [list(request["questions"]) for request in har.jev_requests] == [
            ["q-cap"], ["q-clarify"],
        ]
        assert har.upstream_models == [model]
        record = recent(app)
        assert record["waves"] == 2
        assert record["jev_choice"] == "strong"
        assert record["jev_ms"] == pytest.approx(record["answers"]["q-cap"]["duration_ms"])
        wave_events = [event for event in record["trace"] if event["event"] == "wave"]
        assert [event["wave"] for event in wave_events] == [1, 2]
        assert set(record["answers"]) == {"q-cap", "q-clarify"}
        assert kernel_state(app).balancer.inflight(model, scope=MAIN_SCOPE) == 0

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
        # 决策事件自包含：波次计数只来自事件本身，不再写 SQLite 请求账本。
        assert last_event(app)["decision"]["waves"] == 1

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
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0

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
        model = first.headers["x-sr-model"]
        assert model in ("strong-a", "strong-b")
        assert first.headers["x-sr-selection"] == "target"
        assert kernel_state(app).balancer.calls[-1]["preferred"] is None
        assert kernel_state(app).balancer.inflight(model, scope=MAIN_SCOPE) == 0
        first_revision = kernel_state(app).snapshot_set.default_snapshot.tree_revision

        # 仅移动节点（canvas 属于纯布局字段）：behavior revision 不变。
        moved = copy.deepcopy(switch_graph())
        moved_canvas = {
            node["id"]: {"x": 40 + index * 100, "y": 60 + index * 80}
            for index, node in enumerate(moved["nodes"])
        }
        moved_tree = tree_from_dict(tree_dict(
            "main", root=moved, canvas=moved_canvas,
            sticky={"enabled": True, "ttl_seconds": 1800},
            enabled=True, default=True,
        ))
        moved_snapshot = build_snapshot(
            tree=moved_tree,
            backends=kernel_state(app).snapshot_set.default_snapshot.backends, time_periods=[],
            jev=JEV_SETTINGS, capabilities=CAPABILITIES,
            compiled=compile_graph(moved_tree.root),
        )
        assert moved_snapshot.tree_revision == first_revision
        kernel_state(app).snapshot_set = assemble_snapshot_set(
            [moved_snapshot], fingerprint="moved")

        second = await post(app, headers=headers)
        assert second.status_code == 200
        assert second.headers["x-sr-model"] == model
        assert second.headers["x-sr-selection"] == "sticky"
        # 第二次请求仍先执行被激活的问题，再做 preferred 调度。
        assert [list(request["questions"]) for request in har.jev_requests] == [["q"], ["q"]]
        assert kernel_state(app).balancer.calls[-1]["preferred"] == model
        assert har.upstream_models == [model, model]
        record = recent(app)
        assert record["sticky_hit"] == 1
        assert record["selection_reason"] == "sticky"
        assert record["answers"]["q"]["ranking"][0] == "strong"

    asyncio.run(scenario())


def test_preferred_target_circuit_open_returns_503_without_calling_next_upstream(tmp_path):
    async def scenario():
        har = Harness({"q": choice(fast=0.1, strong=0.9)})
        app = make_app(tmp_path, har, root=priority_graph())
        for _ in range(kernel_state(app).breaker.threshold):
            kernel_state(app).breaker.failure(runtime_model_key(MAIN_SCOPE, "power"))
        assert kernel_state(app).breaker.is_open(
            runtime_model_key(MAIN_SCOPE, "power")) is True
        assert kernel_state(app).breaker.is_open("power") is False
        response = await post(app)
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_available_backend"
        # p1 power-target 熔断后直接 503，不静默改选 p2 strong-target 上游。
        assert har.upstream_models == []
        assert kernel_state(app).balancer.inflight("power", scope=MAIN_SCOPE) == 0
        assert kernel_state(app).balancer.inflight("strong-a", scope=MAIN_SCOPE) == 0
        # 决策事件（内核产生）完整保留调度失败 trace；执行结果本就不在事件里。
        event = last_event(app)
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert [item["event"] for item in event["trace"][-2:]] == [
            "schedule", "routing_error"]
        assert event["trace"][-2]["unavailable"] == {"power": "breaker_open"}
        assert event["trace"][-1]["stage"] == "schedule"
        assert event["trace"][-1]["error_class"] == "no_available_backend"
        assert record["routing_ms"] > 0
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


def test_no_active_tree_is_rejected_before_any_replayable_event(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), active=False)
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_active_tree"
        # 内核在选图之前拒绝：不产生可回放事件，外围内存日志保持为空（设计 §7.1）。
        assert app.test_sink.events == []
        assert app.state.decision_logs.recent(20) == []

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
        assert app.test_sink.events == []
        assert app.state.decision_logs.recent(20) == []

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
        for _ in range(kernel_state(app).breaker.threshold):
            kernel_state(app).breaker.failure(runtime_model_key(MAIN_SCOPE, "safe"))
        # 熔断只作用于 Auto 图的运行键；裸键属于固定模型直连，必须保持关闭。
        assert kernel_state(app).breaker.is_open("safe") is False

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

        # 决策事件自包含：失败决策的目标/原因都在事件本身（外围不再写请求账本）。
        assert last_event(app)["decision"]["status"] == "routing_error"
        assert last_event(app)["decision"]["target_id"] == "safe"

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
        kernel_state(app).routing_service = BrokenEngine()
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "routing_failed"
        record = recent(app)
        failure = record["trace"][-1]
        assert failure["event"] == "routing_error"
        assert failure["error_class"] == "routing_failed"
        assert failure["message"] == "routing failed"
        assert "sensitive internal detail" not in json.dumps(
            last_event(app), ensure_ascii=False)

    asyncio.run(scenario())


def test_missing_snapshot_creates_a_snapshot_stage_failure(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), root=direct_graph())
        kernel_state(app).snapshot_set = None
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_valid_snapshot"
        # 选图前失败：内核不产生可回放事件。
        assert app.test_sink.events == []

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
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0

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
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0

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
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0
        record = recent(app)
        assert record["outcome_state"] == "completed"
        assert record["answers"] == {}
        # 外围不再记录执行结果（token/outcome），事件只描述决策。
        assert "total_tokens" not in record
        assert "request_outcome" not in [item["event"] for item in record["trace"]]

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
        # 上游响应恰好关闭一次；内核决策租约在内核返回决策时已释放。
        assert closes == [True]
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0

        # 执行失败不再回写内核事件：事件仍是决策成功；上游熔断键属于外围执行层，
        # 带图作用域（裸模型键只属于固定模型直连），达到阈值后打开。
        for _ in range(2):
            assert (await post(app, payload(stream=True))).status_code == 502
        assert app.state.breaker.is_open(
            runtime_model_key(tree_scope("main"), "safe")) is True
        assert app.state.breaker.is_open("safe") is False
        assert closes == [True, True, True]
        assert recent(app)["outcome_state"] == "completed"

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
            # 第一个 SSE 分片已发出，第二个仍被上游 gate 挡住：内核决策租约早已
            # 释放（决策与执行分离），上游响应由外围 relay 持有。
            assert first == b"data: first\n\n"
            assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0
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
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0
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
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0
        # 上游 5xx 只影响外围执行（不写日志）；决策事件仍是成功决策。
        record = recent(app)
        assert record["outcome_state"] == "completed"
        assert "request_outcome" not in [item["event"] for item in record["trace"]]

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
        # 客户端取消只中止外围上游执行；内核决策已完成且租约已释放，事件是决策。
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0
        assert recent(app)["outcome_state"] == "completed"

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
        generator = proxy._relay(
            response, model="safe", breaker_key="safe", outcome=outcome,
            is_disconnected=None, backend=backend, upstream_started=0.0,
            upstream_ms=1.0, release=lambda: released.append(True))
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
        generator = proxy._relay(
            response, model="safe", breaker_key="safe", outcome=outcome,
            is_disconnected=None, backend=backend, upstream_started=0.0,
            upstream_ms=1.0, release=lambda: released.append(True))
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
        generator = proxy._relay(
            response, model="safe", breaker_key="safe", outcome=outcome,
            is_disconnected=None, backend=backend, upstream_started=0.0,
            upstream_ms=1.0, release=lambda: released.append(True))
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


def test_real_request_produces_a_self_contained_decision_event(tmp_path):
    """One real Auto request: the kernel emits one replayable, self-contained event.

    The split removed SQLite decision-log snapshots; replay reads the graph/trace
    embedded in the same event (design §7).
    """
    async def scenario():
        har = Harness({"q": choice(fast=0.9, strong=0.1)})
        app = make_app(tmp_path, har, root=direct_graph())
        response = await post(app)
        assert response.status_code == 200

        event = last_event(app)
        replay = replay_payload(event)
        assert replay["tree"]["root"]["type"] == "graph"
        assert replay["trace"] == event["trace"]
        assert event["request"]["text"] == "请求"
        assert "graph_snapshot_id" not in event
        assert "snapshot_revision" not in event["request"]

        # 内存日志接收口：投递后可回放，且回放与当前图无关。
        deliver_decision_events(app, app.test_sink)
        assert app.state.decision_logs.get(event["id"]) is not None
        assert app.state.decision_logs.recent(1)[0]["graph"] == event["graph"]

    asyncio.run(scenario())


# ── standalone data plane: default graph + department header ───────────


def encode_department(value):
    return quote(value, safe='')


def add_department_graph(app, *, department='财务部', tree_id='finance'):
    """把带部门的启用图装配进 app 的路由集（默认图保持 main）。"""
    base = kernel_state(app).snapshot_set.default_snapshot
    tree = tree_from_dict(tree_dict(
        tree_id, root=switch_graph(), departments=[department],
        enabled=True, default=False,
        sticky={'enabled': False, 'ttl_seconds': 1800},
    ))
    snapshot = build_snapshot(
        tree=tree, backends=base.backends, time_periods=[],
        jev=JEV_SETTINGS, capabilities=CAPABILITIES,
        compiled=compile_graph(tree.root),
    )
    kernel_state(app).snapshot_set = assemble_snapshot_set(
        [base, snapshot], fingerprint='departments')
    return app


def make_admin_chat_app(tmp_path, handler, *, root=None):
    """真实组合根：``/admin/test/chat/completions`` 由 create_app 挂载。

    管理端测试对话与 ``/v1`` 共用同一外围执行器：Auto 走内核决策、固定模型只查
    外围 SQLite 目录（拆分后不再需要额外路由）。
    """
    return make_app(tmp_path, handler, root=root)


def test_public_v1_rejects_client_department_header(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500),
                       root=direct_graph())
        response = await post(
            app,
            {'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]},
            headers={'x-sr-department': encode_department('财务部')},
        )

        assert response.status_code == 400
        assert response.json() == {'error': {'message': 'department_header_not_allowed'}}

    asyncio.run(scenario())


def test_public_v1_rejects_empty_department_header_even_for_a_named_model(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500),
                       root=direct_graph())
        response = await post(
            app,
            {'model': 'fast', 'messages': [{'role': 'user', 'content': '你好'}]},
            headers={'x-sr-department': ''},
        )

        # 公开数据面不读取请求模型：只要出现 Header 就拒绝，与 model 无关。
        assert response.status_code == 400
        assert response.json() == {'error': {'message': 'department_header_not_allowed'}}

    asyncio.run(scenario())


def test_public_v1_uses_default_graph_and_emits_selection_headers(tmp_path):
    async def scenario():
        har = Harness({'q': choice(fast=0.9, strong=0.1)})
        app = make_app(tmp_path, har, root=switch_graph())
        response = await post(
            app, {'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]})

        assert response.status_code == 200, response.text
        assert response.headers['x-sr-graph-selection'] == 'default'
        assert response.headers['x-sr-tree-id'] == 'main'

    asyncio.run(scenario())


def test_public_v1_never_selects_a_department_graph(tmp_path):
    async def scenario():
        har = Harness({'q': choice(fast=0.9, strong=0.1)})
        app = add_department_graph(make_app(tmp_path, har, root=switch_graph()))
        response = await post(
            app, {'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]})

        assert response.status_code == 200, response.text
        assert response.headers['x-sr-graph-selection'] == 'default'
        assert response.headers['x-sr-tree-id'] == 'main'
        assert recent(app)['tree_id'] == 'main'

    asyncio.run(scenario())


def test_auto_stream_emits_default_graph_selection_headers(tmp_path):
    async def scenario():
        body = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body

        def handler(request):
            if request.url.host == 'jev.test':
                raise AssertionError('direct target must skip JEV')
            return httpx.Response(
                200, headers={'content-type': 'text/event-stream'}, stream=Chunks())

        app = make_app(tmp_path, handler, root=direct_graph())
        response = await post(app, payload(stream=True))

        assert response.status_code == 200
        assert response.headers['x-sr-graph-selection'] == 'default'
        assert response.headers['x-sr-tree-id'] == 'main'

    asyncio.run(scenario())


def test_missing_default_graph_is_a_no_default_tree_failure(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500),
                       root=direct_graph())
        kernel_state(app).snapshot_set = replace(
            kernel_state(app).snapshot_set, default_tree_id=None)
        response = await post(app)

        assert response.status_code == 503
        assert response.json() == {'error': {'message': 'no_default_tree'}}
        # 选图前失败：内核不产生可回放事件。
        assert app.test_sink.events == []

    asyncio.run(scenario())


def test_admin_test_chat_accepts_encoded_department_header(tmp_path):
    async def scenario():
        har = Harness({'q': choice(fast=0.9, strong=0.1)})
        app = add_department_graph(make_admin_chat_app(tmp_path, har))
        response = await post(
            app,
            {'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]},
            headers={'x-sr-department': encode_department('财务部')},
            path='/admin/test/chat/completions',
        )

        assert response.status_code == 200, response.text
        assert response.headers['x-sr-tree-id'] == 'finance'
        assert response.headers['x-sr-graph-selection'] == 'department'
        assert response.headers['x-sr-selection'] == 'target'
        record = recent(app)
        assert record['tree_id'] == 'finance'

    asyncio.run(scenario())


def test_admin_test_chat_rejects_malformed_department_header(tmp_path):
    async def scenario():
        app = make_admin_chat_app(tmp_path, Harness({}))
        body = {'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]}
        for raw in ('%zz', '%E4'):
            response = await post(
                app, body, headers={'x-sr-department': raw},
                path='/admin/test/chat/completions')
            assert response.status_code == 400
            assert response.json() == {'error': {'message': 'invalid_department'}}

    asyncio.run(scenario())


def test_kernel_breaker_scope_isolates_main_from_finance(tmp_path):
    """内核调度熔断按图作用域隔离：main 的键不阻断 finance 图。

    拆分后内核不再接收上游执行结果（无回写接口），因此这里直接构造调度熔断
    状态来验证作用域隔离——这正是内核选路阶段的行为。
    """
    async def scenario():
        app = make_admin_chat_app(
            tmp_path, lambda request: httpx.Response(200, json={'choices': []}))
        base = kernel_state(app).snapshot_set.default_snapshot
        # 部门图与默认图使用同一直连结构、同一 safe 逻辑模型和同一运行目录。
        tree = tree_from_dict(tree_dict(
            'finance', root=direct_graph(), departments=['财务部'],
            enabled=True, default=False))
        finance = build_snapshot(
            tree=tree, backends=base.backends,
            time_periods=[], jev=JEV_SETTINGS, capabilities=CAPABILITIES,
            compiled=compile_graph(tree.root))
        kernel_state(app).snapshot_set = assemble_snapshot_set(
            [base, finance], fingerprint='same-model')

        # 只打开 main 图的运行键：finance 图的同名模型不受影响。
        main_key = runtime_model_key(MAIN_SCOPE, 'safe')
        for _ in range(kernel_state(app).breaker.threshold):
            kernel_state(app).breaker.failure(main_key)
        assert kernel_state(app).breaker.is_open(main_key) is True
        # 裸键属于固定模型直连，不能被 Auto 污染。
        assert kernel_state(app).breaker.is_open('safe') is False

        default = await post(app, payload())
        assert default.status_code == 503
        assert default.json()['error']['message'] == 'no_available_backend'
        # main 的熔断在调度阶段拒绝请求：没有任何上游执行。
        assert kernel_state(app).balancer.inflight("safe", scope=MAIN_SCOPE) == 0

        finance_response = await post(
            app, {**payload(), 'model': 'auto'},
            headers={'x-sr-department': encode_department('财务部')},
            path='/admin/test/chat/completions')
        assert finance_response.status_code == 200, finance_response.text
        assert finance_response.headers['x-sr-tree-id'] == 'finance'

    asyncio.run(scenario())


def test_fixed_model_direct_does_not_require_a_route_set(tmp_path):
    async def scenario():
        app = make_admin_chat_app(
            tmp_path, lambda request: httpx.Response(200, json={'choices': []}),
            root=direct_graph())
        body = {'model': 'qwen3-8b', 'messages': [{'role': 'user', 'content': '直连'}]}
        # 空路由集 / 未初始化路由集都不影响固定模型直连（它只查外围 SQLite 目录）。
        for route_set in (assemble_snapshot_set([], fingerprint='empty'), None):
            kernel_state(app).snapshot_set = route_set
            response = await post(app, body, path='/admin/test/chat/completions')
            assert response.status_code == 200, response.text
            assert response.headers['x-sr-model'] == 'qwen3-8b'
            assert response.headers['x-sr-selection'] == 'direct'
            assert 'x-sr-graph-selection' not in response.headers
            assert 'x-sr-tree-id' not in response.headers
        # 固定模型直连不调用内核，也不产生决策事件。
        assert app.test_sink.events == []
        auto = await post(
            app, {'model': 'auto', 'messages': [{'role': 'user', 'content': 'auto'}]},
            path='/admin/test/chat/completions')
        assert auto.status_code == 503
        assert auto.json()['error']['message'] == 'no_valid_snapshot'

    asyncio.run(scenario())


def test_admin_fixed_model_department_header_is_ignored(tmp_path):
    async def scenario():
        app = add_department_graph(make_admin_chat_app(
            tmp_path, lambda request: httpx.Response(200, json={'choices': []})))
        response = await post(
            app,
            {'model': 'qwen3-8b', 'messages': [{'role': 'user', 'content': '直连'}]},
            headers={'x-sr-department': encode_department('财务部')},
            path='/admin/test/chat/completions',
        )

        # 固定模型直连是目录测试：可信 Header 允许出现，但不参与选图/响应头。
        assert response.status_code == 200, response.text
        assert response.headers['x-sr-selection'] == 'direct'
        assert 'x-sr-graph-selection' not in response.headers
        assert 'x-sr-tree-id' not in response.headers

    asyncio.run(scenario())


def test_decode_department_accepts_encoded_unicode():
    from router.api.routes_v1 import decode_department

    assert decode_department(None) == ''
    assert decode_department('') == ''
    assert decode_department(encode_department(' 财务部 ')) == '财务部'
    # 先 strip 再查控制字符（brief 的实现顺序）：纯首尾换行/制表符等价于未声明。
    assert decode_department('%0A') == ''
    assert decode_department('%20%09%20') == ''


@pytest.mark.parametrize('raw', [
    '%', '%zz', '%E4', 'a' * 769, 'x' * 65, 'a%00b', '%7F', 'a%01b',
])
def test_decode_department_rejects_malformed_values(raw):
    from router.api.routes_v1 import decode_department

    with pytest.raises(ValueError, match='invalid_department'):
        decode_department(raw)
