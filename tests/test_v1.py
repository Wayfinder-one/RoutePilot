"""`/v1/chat/completions` 真链路：MockTransport 分流 JEV `/v1/systemone` 与假 OpenAI 上游。

覆盖 Policy v4 数据面契约（设计 §3/§5）：一次决策至多一次 JEV 调用、首条命中
规则与兜底目标（jev_error/no_rule_matched）、`x-sr-*` 响应头取值集合、粘性二次
请求仍然执行全部问题再做 preferred 调度、命中目标熔断返回 503 且不改选其他
目标、流式/非流式/取消的租约释放，以及 SQLite 账本只持久化白名单元数据
（policy_id/policy_revision/jev_calls），answers/trace 原样留在内存 ring。
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from router.api import routes_test
from router.api.routes_v1 import _decision_record, router
from router.config import Settings
from router.jev.client import ChoiceAnswer, JevClient
from router.jev.config import JevSettings
from router.request_logging import LOG_COLUMNS, RequestLogger, log_values
from router.request_outcome import OUTCOME_INCOMPLETE
from router.routing.affinity import SessionAffinity
from router.routing.conditions import TimePeriod
from router.routing.policy import (
    PolicyResource,
    policy_from_dict,
    policy_revision_of,
)
from router.routing.policy.evaluator import PolicyEvaluator
from router.routing.scheduler import LoadBalancer
from router.routing.scheduling import ModelScheduler
from router.routing.service import RoutingService
from router.routing.snapshot import build_snapshot
from router.routing.types import RoutingMiss
from router.store.db import connect, migrate
from router.upstream.breaker import CircuitBreaker
from router.upstream.catalog import Backend


JEV_SETTINGS = JevSettings("http://jev.test", "d1-2B")
SELECTION_VALUES = {"target", "fallback", "sticky", "direct"}
MODELS = ("fast", "strong-a", "strong-b", "power", "safe")
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


# ── policy fixtures ─────────────────────────────────────────────────────


def route_policy(*, sticky=None, rules=None, fallback="safe", targets=None):
    """标准测试策略：route(choice)/risk(noul)/severity(score) 三问题 + 有序规则。"""
    return {
        "kind": "sr-routing-policy", "version": 4, "name": "测试策略",
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "questions": {
            "route": {
                "type": "choice", "instructions": "选择模型",
                "options": [
                    {"key": "fast", "label": "轻量", "capability": "简单请求"},
                    {"key": "strong", "label": "增强", "capability": "复杂请求"},
                ],
            },
            "risk": {"type": "noul", "instructions": "是否高风险"},
            "severity": {"type": "score", "instructions": "评分", "criteria": [1, 5, 10]},
        },
        "targets": targets or {
            "fast": {"models": [{"model": "fast"}]},
            "strong": {"models": [{"model": "strong-a"}, {"model": "strong-b"}]},
            "power": {"models": [{"model": "power"}]},
            "safe": {"models": [{"model": "safe"}]},
        },
        "rules": rules if rules is not None else [
            {"name": "高风险", "when": {"answer": "risk.yes", "gte": 0.8}, "target": "safe"},
            {
                "name": "复杂请求",
                "when": {"all": [
                    {"answer": "route.selected", "eq": "strong"},
                    {"answer": "severity.value", "gte": 5},
                ]},
                "target": "strong",
            },
            {"name": "轻量请求", "when": {"answer": "route.selected", "eq": "fast"}, "target": "fast"},
        ],
        "fallback": fallback,
    }


def direct_policy(model="safe"):
    """无问题策略：文本条件直连目标，一次决策不触发 JEV。"""
    return {
        "kind": "sr-routing-policy", "version": 4, "name": "直连策略",
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "questions": {},
        "targets": {"direct": {"models": [{"model": model}]}},
        "rules": [{"name": "直连", "when": {"text_contains_any": ["请求"]}, "target": "direct"}],
        "fallback": "direct",
    }


def period_policy():
    """时段规则：peak 时段命中 power 目标。"""
    raw = route_policy()
    raw["rules"] = [{"name": "高峰", "when": {"period_in": ["peak"]}, "target": "power"}]
    return raw


def all_day_peak():
    # start == end 在 matching_periods 里表示全天生效（HH:MM 不允许 24:00）。
    return [TimePeriod("peak", (0, 1, 2, 3, 4, 5, 6), "00:00", "00:00")]


# ── harness ────────────────────────────────────────────────────────────


def make_app(tmp_path, handler, *, raw=None, active=True, transport=None,
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
        PolicyEvaluator(app.state.jev_client),
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
    resource = None
    if active:
        policy = policy_from_dict(raw or route_policy())
        resource = PolicyResource(
            id="main", policy=policy, enabled=True,
            policy_revision=policy_revision_of(policy),
        )
    app.state.snapshot = build_snapshot(
        resource=resource, backends=backends,
        time_periods=[] if time_periods is None else time_periods,
        jev=JEV_SETTINGS, capabilities=CAPABILITIES,
    )
    app.include_router(router, prefix="/v1")
    app.include_router(routes_test.router, prefix="/admin")
    return app


def payload(text="请求", *, stream=False):
    return {"model": "ignored", "messages": [{"role": "user", "content": text}], "stream": stream}


def choice(*, fast=0.2, strong=0.8):
    return {"type": "choice", "probabilities": {"轻量": fast, "增强": strong}}


def noul(yes):
    return {"type": "noul", "noul": yes}


def score(value):
    # wire 协议按档位索引选择，legend 回填实际值（criteria [1, 5, 10]）。
    index = {1: 0, 5: 1, 10: 2}[value]
    return {"type": "score", "score": index, "legend": {str(index): value}}


def standard_answers(*, fast=0.2, strong=0.8, risk=0.1, severity=1):
    return {
        "route": choice(fast=fast, strong=strong),
        "risk": noul(risk),
        "severity": score(severity),
    }


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


# ── brief Step 1: 新账本列与单次 JEV 调用的记录工厂 ───────────────────


def make_decision_record():
    answer = ChoiceAnswer(
        probabilities={"fast": 0.8, "strong": 0.2},
        ranking=["fast", "strong"], p_top=0.8, margin=0.6,
        usage={}, duration_ms=12.0,
    )
    decision = SimpleNamespace(
        model="fast", target_id="safe", policy_path="safe",
        selection_reason="target", fallback_reason=None,
        sticky_hit=False, session_hash=None, reasoning=None,
        matched_rule="高风险", jev_duration_ms=12.0,
        jev_questions=("route", "risk"), answers={"route": answer},
        trace=[
            {"event": "jev", "questions": ["route", "risk"], "duration_ms": 12.0},
            {"event": "rule", "index": 0, "name": "高风险", "matched": True},
            {"event": "final", "kind": "target", "target_id": "safe", "reason": None},
        ],
    )
    snapshot = SimpleNamespace(
        policy_id="main", policy_revision="p" * 64,
        snapshot_revision="s" * 64,
        jev=JevSettings("http://jev.test", "d1-2B"),
    )
    return _decision_record(
        decision, snapshot, "测试", None, 1.0, time.monotonic(),
    )


def test_log_values_use_policy_and_jev_call_columns():
    record = {
        "id": "r1", "policy_id": "main", "policy_revision": "p" * 64,
        "jev_calls": 1, "outcome_state": OUTCOME_INCOMPLETE,
    }
    values = log_values(record)
    assert values[LOG_COLUMNS.index("policy_id")] == "main"
    assert values[LOG_COLUMNS.index("policy_revision")] == "p" * 64
    assert values[LOG_COLUMNS.index("jev_calls")] == 1
    assert "tree_id" not in LOG_COLUMNS
    assert "waves" not in LOG_COLUMNS


def test_v1_decision_record_has_one_jev_call_and_raw_rule_trace():
    record = make_decision_record()
    assert record["jev_calls"] == 1
    assert record["matched_rule"] == "高风险"
    assert "waves" not in record
    assert "node_labels" not in record
    assert [e["event"] for e in record["trace"]].count("jev") == 1


# ── rule hit / single JEV call / fallback ───────────────────────────────


def test_selected_target_uses_real_systemone_chain(tmp_path):
    async def scenario():
        har = Harness(standard_answers(fast=0.9, strong=0.1))
        app = make_app(tmp_path, har)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "target"
        assert response.headers["x-sr-model"] == "fast"
        assert response.headers["x-sr-selection"] in SELECTION_VALUES
        # 一次决策一次 JEV 调用：全部问题一次构造、一个 choose_many。
        assert len(har.jev_requests) == 1
        assert list(har.jev_requests[0]["questions"]) == ["route", "risk", "severity"]
        assert har.upstream_models == ["fast"]
        record = recent(app)
        assert record["selection_reason"] == "target"
        assert record["matched_rule"] == "轻量请求"
        assert record["policy_id"] == "main"
        assert record["policy_revision"] == app.state.snapshot.policy_revision
        assert record["jev_model"] == "d1-2B"
        assert record["jev_calls"] == 1
        assert record["jev_choice"] == "fast"
        assert record["jev_top"] == pytest.approx(0.9)
        assert record["jev_margin"] == pytest.approx(0.8)
        assert record["jev_probabilities"] == {
            "fast": pytest.approx(0.9), "strong": pytest.approx(0.1),
        }
        assert record["answers"]["route"]["probabilities"]["fast"] == pytest.approx(0.9)
        assert record["route_label"] == {"zh": "目标命中", "en": "Target selected"}
        assert [pair[0]["en"] for pair in record["metrics"]] == [
            "Upstream", "Routing", "Total", "JEV calls"]
        assert record["metrics"][0][1] == 200
        assert record["metrics"][3][1] == 1
        assert record["fields"][0] == [{"zh": "请求 ID", "en": "Request ID"}, record["id"]]
        assert [pair[0]["en"] for pair in record["fields"][1:3]] == ["Policy", "Policy revision"]
        assert [event["event"] for event in record["trace"]].count("request_outcome") == 1
        assert record["trace"][-1]["phase"] == "outcome"
        assert record["trace"][-1]["tone"] == "hit"
        assert len(app.state.requestlog.recent_requests()) == 1
        assert app.state.balancer.inflight("fast") == 0

    asyncio.run(scenario())


def test_numeric_rule_combines_answers_in_one_systemone_call(tmp_path):
    async def scenario():
        har = Harness(standard_answers(fast=0.1, strong=0.9, severity=5))
        app = make_app(tmp_path, har)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "target"
        assert response.headers["x-sr-model"] == "strong-a"
        # 三个问题只收到一次 systemone 请求，问题按文档一次构造。
        assert len(har.jev_requests) == 1
        assert list(har.jev_requests[0]["questions"]) == ["route", "risk", "severity"]
        assert har.upstream_models == ["strong-a"]
        record = recent(app)
        assert record["matched_rule"] == "复杂请求"
        assert record["jev_calls"] == 1
        assert set(record["answers"]) == {"route", "risk", "severity"}
        rule_events = [event for event in record["trace"] if event["event"] == "rule"]
        # 首条命中即停止：高风险未命中、复杂请求命中，轻量请求不再求值。
        assert [(event["index"], event["name"], event["matched"]) for event in rule_events] == [
            (0, "高风险", False), (1, "复杂请求", True),
        ]
        # 摘要列取 answers 按问题 ID 排序后的首个 ChoiceAnswer。
        assert record["jev_choice"] == "strong"
        assert record["jev_top"] == pytest.approx(0.9)
        await flush(app)
        conn = connect(app.state.settings.db_path)
        row = dict(conn.execute("SELECT * FROM request_logs").fetchone())
        conn.close()
        assert row["jev_calls"] == 1
        assert row["policy_id"] == "main"
        assert row["jev_choice"] == "strong"
        assert set(LOG_COLUMNS) & {
            "answers", "trace", "text", "route_label", "metrics", "fields", "matched_rule",
        } == set()
        assert "answers" in recent(app) and "trace" in recent(app)
        assert app.state.balancer.inflight("strong-a") == 0

    asyncio.run(scenario())


def test_one_decision_never_makes_a_second_jev_call(tmp_path):
    """v4 去除 wave：无论问题多少，一次决策只有一个 jev 轨迹事件。"""
    async def scenario():
        har = Harness(standard_answers(fast=0.1, strong=0.9, severity=5))
        app = make_app(tmp_path, har)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-model"] == "strong-a"
        assert len(har.jev_requests) == 1
        record = recent(app)
        assert record["jev_calls"] == 1
        kinds = [event["event"] for event in record["trace"]]
        assert kinds.count("jev") == 1
        assert "wave" not in kinds
        jev_event = next(event for event in record["trace"] if event["event"] == "jev")
        assert jev_event["failed"] is False
        await flush(app)
        conn = connect(app.state.settings.db_path)
        row = dict(conn.execute("SELECT jev_calls FROM request_logs").fetchone())
        columns = {item[1] for item in conn.execute("PRAGMA table_info(request_logs)")}
        conn.close()
        assert row["jev_calls"] == 1
        assert "waves" not in columns
        assert "answers" not in columns and "trace" not in columns
        assert app.state.balancer.inflight("strong-a") == 0

    asyncio.run(scenario())


def test_no_rule_matched_uses_fallback_target(tmp_path):
    async def scenario():
        har = Harness(standard_answers(fast=0.9, strong=0.1))
        raw = route_policy(rules=[
            {"name": "高风险", "when": {"answer": "risk.yes", "gte": 0.8}, "target": "safe"},
        ])
        app = make_app(tmp_path, har, raw=raw)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]
        record = recent(app)
        assert record["selection_reason"] == "fallback"
        assert record["fallback_reason"] == "no_rule_matched"
        assert record["matched_rule"] is None
        assert record["route_label"] == {"zh": "默认目标兜底", "en": "Default target fallback"}
        assert record["jev_choice"] == "fast"
        await flush(app)
        conn = connect(app.state.settings.db_path)
        assert conn.execute("SELECT jev_calls FROM request_logs").fetchone()[0] == 1
        conn.close()

    asyncio.run(scenario())


# ── jev_error 兜底 ─────────────────────────────────────────────────────


def test_missing_answer_key_uses_jev_error_fallback(tmp_path):
    async def scenario():
        har = Harness({})
        app = make_app(tmp_path, har)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]
        record = recent(app)
        assert record["selection_reason"] == "fallback"
        assert record["fallback_reason"] == "jev_error"
        assert record["matched_rule"] is None
        assert record["jev_choice"] is None
        assert record["jev_top"] is None and record["jev_margin"] is None
        assert record["jev_calls"] == 1
        failed = next(event for event in record["trace"] if event["event"] == "jev")
        assert failed["failed"] is True and failed["error"]
        assert app.state.balancer.inflight("safe") == 0

    asyncio.run(scenario())


def test_invalid_probabilities_use_jev_error_fallback(tmp_path):
    async def scenario():
        har = Harness({
            "route": {"type": "choice", "probabilities": {"轻量": 0.9, "增强": 0.9}},
            "risk": noul(0.1), "severity": score(1),
        })
        app = make_app(tmp_path, har)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]

    asyncio.run(scenario())


def test_timeout_uses_jev_error_fallback(tmp_path):
    async def scenario():
        har = Harness(standard_answers(), jev_failure=httpx.ReadTimeout("timed out"))
        app = make_app(tmp_path, har)
        response = await post(app)
        assert response.status_code == 200
        assert response.headers["x-sr-selection"] == "fallback"
        assert response.headers["x-sr-model"] == "safe"
        assert har.upstream_models == ["safe"]
        assert recent(app)["fallback_reason"] == "jev_error"

    asyncio.run(scenario())


# ── sticky / circuit breaker ────────────────────────────────────────────


def test_sticky_second_request_reruns_questions_then_prefers_model(tmp_path):
    async def scenario():
        har = Harness(standard_answers(fast=0.1, strong=0.9, severity=5))
        app = make_app(
            tmp_path, har,
            raw=route_policy(sticky={"enabled": True, "ttl_seconds": 1800}),
        )
        headers = {"x-sr-session-id": "session-1"}
        first = await post(app, headers=headers)
        assert first.status_code == 200
        assert first.headers["x-sr-model"] == "strong-a"
        assert first.headers["x-sr-selection"] == "target"
        assert app.state.balancer.calls[-1]["preferred"] is None
        assert app.state.balancer.inflight("strong-a") == 0

        second = await post(app, headers=headers)
        assert second.status_code == 200
        assert second.headers["x-sr-model"] == "strong-a"
        assert second.headers["x-sr-selection"] == "sticky"
        # 第二次请求仍先执行全部问题，再做 preferred 调度。
        assert [list(request["questions"]) for request in har.jev_requests] == [
            ["route", "risk", "severity"], ["route", "risk", "severity"],
        ]
        assert app.state.balancer.calls[-1]["preferred"] == "strong-a"
        assert har.upstream_models == ["strong-a", "strong-a"]
        record = recent(app)
        assert record["sticky_hit"] == 1
        assert record["selection_reason"] == "sticky"
        assert record["answers"]["route"]["ranking"][0] == "strong"

    asyncio.run(scenario())


def test_matched_target_circuit_open_returns_503_without_fallback_target(tmp_path):
    async def scenario():
        # 规则只指向 power 目标；power 熔断后直接 503，不改选其他目标。
        har = Harness(standard_answers(fast=0.1, strong=0.9, severity=5))
        raw = route_policy(rules=[
            {"name": "复杂请求", "when": {"answer": "route.selected", "eq": "strong"},
             "target": "power"},
        ])
        app = make_app(tmp_path, har, raw=raw)
        for _ in range(app.state.breaker.threshold):
            app.state.breaker.failure("power")
        assert app.state.breaker.is_open("power") is True
        response = await post(app)
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_available_backend"
        assert har.upstream_models == []
        assert app.state.balancer.inflight("power") == 0
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert record["error_class"] == "no_available_backend"
        assert [event["event"] for event in record["trace"][-2:]] == ["schedule", "routing_error"]
        assert record["trace"][-2]["unavailable"] == {"power": "breaker_open"}
        assert record["trace"][-1]["stage"] == "schedule"
        assert record["routing_ms"] > 0
        assert record["total_ms"] >= record["routing_ms"]
        assert record["target_id"] == "power"
        assert record["selection_reason"] == "target"
        assert record["policy_path"] == "power"
        assert record["fallback_reason"] is None
        # 失败记录保持稀疏元数据：命中规则的证据在原始 rule 轨迹事件里。
        rule_events = [event for event in record["trace"] if event["event"] == "rule"]
        assert [(event["index"], event["name"], event["matched"]) for event in rule_events] == [
            (0, "复杂请求", True),
        ]
        # 调度失败前 JEV 已成功：账本仍记录这一次调用。
        assert record["jev_calls"] == 1

    asyncio.run(scenario())


def test_schedule_failure_on_fallback_target_keeps_fallback_reason(tmp_path):
    async def scenario():
        har = Harness(standard_answers(fast=0.9, strong=0.1))
        raw = route_policy(rules=[
            {"name": "高风险", "when": {"answer": "risk.yes", "gte": 0.8}, "target": "safe"},
        ])
        app = make_app(tmp_path, har, raw=raw)
        for _ in range(app.state.breaker.threshold):
            app.state.breaker.failure("safe")

        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_available_backend"
        assert har.upstream_models == []
        record = recent(app)
        final = next(event for event in record["trace"] if event["event"] == "final")
        assert final["kind"] == "fallback"
        assert final["reason"] == "no_rule_matched"
        assert record["outcome_state"] == "routing_error"
        assert record["target_id"] == "safe"
        assert record["selection_reason"] == "fallback"
        assert record["policy_path"] == "safe"
        assert record["fallback_reason"] == "no_rule_matched"

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
            "selection_reason": "fallback",
            "policy_path": "safe",
            "outcome_state": "routing_error",
        }

    asyncio.run(scenario())


# ── routing failure logs ───────────────────────────────────────────────


def test_no_active_policy_creates_a_routing_error_log(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), active=False)
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_active_policy"
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert record["error_class"] == "no_active_policy"
        failure = record["trace"][-1]
        assert {key: failure[key] for key in ("event", "stage", "error_class", "message")} == {
            "event": "routing_error", "stage": "policy",
            "error_class": "no_active_policy", "message": "no routing policy is enabled",
        }
        # 轨迹保持机器字段：phase/label/facts/tone 等展示文案不再进入数据面。
        assert "phase" not in failure and "label" not in failure
        assert record["route_label"] == {"zh": "路由中止", "en": "Routing stopped"}
        assert not any(event["event"] == "request_outcome" for event in record["trace"])
        assert len(app.state.requestlog.recent_requests()) == 1
        assert record["model"] is None
        assert record["target_id"] is None
        assert record["selection_reason"] is None
        assert record["policy_path"] is None
        assert record["fallback_reason"] is None
        assert record["jev_calls"] == 0

    asyncio.run(scenario())


def test_invalid_json_does_not_create_a_calculation_log(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500), raw=direct_policy())
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


def test_invariant_failure_keeps_partial_policy_trace(tmp_path):
    class BrokenEngine:
        async def route(self, snapshot, request, *, trace_sink=None):
            trace_sink.append({
                "event": "time", "active_periods": [], "timezone": "Asia/Shanghai",
            })
            trace_sink.append({
                "event": "window", "tokens_before": 1, "tokens_after": 1,
                "truncated": False,
            })
            raise RoutingMiss("rule target 'ghost' is not defined in the policy")

    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500))
        app.state.routing_service = BrokenEngine()
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "routing_invariant_violation"
        record = recent(app)
        assert record["outcome_state"] == "routing_error"
        assert [event["event"] for event in record["trace"][:2]] == ["time", "window"]
        assert record["trace"][-1]["error_class"] == "routing_invariant_violation"
        assert record["trace"][-1]["stage"] == "policy"
        assert record["target_id"] is None
        assert record["selection_reason"] is None
        assert record["policy_path"] is None
        assert record["fallback_reason"] is None

    asyncio.run(scenario())


def test_unknown_routing_failure_uses_a_safe_console_message(tmp_path):
    class BrokenEngine:
        async def route(self, snapshot, request, *, trace_sink=None):
            trace_sink.append({
                "event": "time", "active_periods": [], "timezone": "Asia/Shanghai",
            })
            raise ValueError("sensitive internal detail")

    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500))
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
        app = make_app(tmp_path, lambda request: httpx.Response(500))
        app.state.snapshot = None
        response = await post(app)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_valid_snapshot"
        record = recent(app)
        assert record["trace"][-1]["stage"] == "snapshot"
        assert record["trace"][-1]["error_class"] == "no_valid_snapshot"
        assert record["policy_id"] is None
        assert record["policy_revision"] is None
        assert record["jev_calls"] == 0

    asyncio.run(scenario())


# ── 时段规则走原始轨迹 ─────────────────────────────────────────────────


def test_period_condition_uses_active_time_periods(tmp_path):
    async def scenario():
        app = make_app(
            tmp_path, Harness(standard_answers()),
            raw=period_policy(), time_periods=all_day_peak(),
        )
        response = await post(app)

        assert response.status_code == 200
        assert response.headers["x-sr-model"] == "power"
        record = recent(app)
        time_event = record["trace"][0]
        assert time_event["event"] == "time"
        assert time_event["active_periods"] == ["peak"]
        assert time_event["timezone"] == "Asia/Shanghai"
        assert record["matched_rule"] == "高峰"
        assert app.state.balancer.inflight("power") == 0

    asyncio.run(scenario())


# ── admin test chat: direct model selection ────────────────────────────


def test_admin_fixed_model_skips_policy_and_jev(tmp_path):
    async def scenario():
        jev_calls = []

        def handler(request):
            if request.url.host == "jev.test":
                jev_calls.append(request)
                raise AssertionError("direct model test must skip JEV")
            return upstream_ok(request)

        app = make_app(tmp_path, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test",
        ) as client:
            response = await client.post("/admin/test/chat/completions", json={
                "model": "power",
                "messages": [{"role": "user", "content": "请求"}],
            })
        assert response.status_code == 200
        assert response.headers["x-sr-model"] == "power"
        assert response.headers["x-sr-selection"] == "direct"
        assert jev_calls == []
        record = recent(app)
        assert record["decision"] == "test"
        assert record["selection_reason"] == "direct"
        assert record["route_label"] == {"zh": "固定模型测试", "en": "Direct model test"}
        assert record["jev_calls"] == 0
        assert record["answers"] == {}

    asyncio.run(scenario())


def test_admin_fixed_model_unknown_is_404(tmp_path):
    async def scenario():
        app = make_app(tmp_path, lambda request: httpx.Response(500))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test",
        ) as client:
            response = await client.post("/admin/test/chat/completions", json={
                "model": "ghost",
                "messages": [{"role": "user", "content": "请求"}],
            })
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "unknown_model"

    asyncio.run(scenario())


# ── upstream / lease / stream paths ────────────────────────────────────


def test_no_active_policy_fails_before_any_http_call(tmp_path):
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(500)

        app = make_app(tmp_path, handler, active=False)
        response = await post(app)
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "no_active_policy"
        assert calls == []

    asyncio.run(scenario())


def test_stream_completion_releases_lease_and_records_usage(tmp_path):
    async def scenario():
        jev_calls = []

        def handler(request):
            if request.url.host == "jev.test":
                jev_calls.append(request)
                raise AssertionError("question-free policy must skip JEV")
            body = b'data: {"choices":[]}\n\ndata: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\ndata: [DONE]\n\n'

            class Chunks(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield body

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

        app = make_app(tmp_path, handler, raw=direct_policy())
        response = await post(app, payload(stream=True))
        assert response.status_code == 200
        assert b"[DONE]" in response.content
        assert jev_calls == []
        assert app.state.balancer.inflight("safe") == 0
        record = recent(app)
        assert record["outcome_state"] == "completed"
        assert record["total_tokens"] == 3
        assert record["answers"] == {}
        assert record["jev_calls"] == 0
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
        app = make_app(tmp_path, handler, raw=direct_policy())
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

        app = make_app(tmp_path, handler, raw=direct_policy())
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
    record = {"trace": [], "model": "safe", "routing_ms": 2.0, "jev_calls": 0}
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

        app = make_app(tmp_path, handler, raw=direct_policy())
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
        app = make_app(tmp_path, None, raw=direct_policy(), transport=transport)
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
        app = make_app(tmp_path, lambda request: httpx.Response(500), raw=direct_policy())
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
