"""PolicyEvaluator 调度测试共用替身（Task 3 与后续调度/试算测试）。

三类答案构造器只产出已通过 wire 校验的合法答案形态；评估/试算测试统一从
这里取答案结构，不在各自文件里自造。``run_policy`` 固定 ``now`` 与 1024
token 输入窗口，保证轨迹断言不受真实时间影响。

``make_policy_client``（Task 6）是管理/试算 API 的真实应用工厂：临时
SQLite + MockTransport，JEV 与上游都不出进程，试算/路由请求全部在
``app.state.test_jev_requests`` / ``test_upstream_requests`` 留证。
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.jev.client import ChoiceAnswer, NoulAnswer, ScoreAnswer
from router.jev.config import JevSettings
from router.routing.policy import policy_from_dict
from router.routing.policy.evaluator import PolicyEvaluator
from router.routing.types import RoutingRequest
from router.store.db import connect, migrate
from router.store.jev_config import JevConfigRepo
from tests.routing_fixtures import test_settings


class FakeJev:
    def __init__(self, answers=None, error=None):
        self.answers = answers or {}
        self.error = error
        self.calls = []

    async def choose_many(self, settings, text, questions):
        self.calls.append({"settings": settings, "text": text, "questions": questions})
        if self.error is not None:
            raise self.error
        return dict(self.answers)


def choice(selected: str, top: float) -> ChoiceAnswer:
    if not 0.5 <= top <= 1.0:
        raise ValueError("test choice top must be in [0.5, 1]")
    other = "strong" if selected == "fast" else "fast"
    return ChoiceAnswer(
        probabilities={selected: top, other: 1.0 - top},
        ranking=[selected, other], p_top=top, margin=2.0 * top - 1.0,
        usage={"input_tokens": 20}, duration_ms=12.0,
    )


def noul(yes: float) -> NoulAnswer:
    return NoulAnswer(yes, 1.0 - yes, {"input_tokens": 20}, 12.0)


def score(value: float) -> ScoreAnswer:
    return ScoreAnswer(value, {"input_tokens": 20}, 12.0)


def run_policy(raw, *, answers=None, error=None, text="测试请求"):
    jev = FakeJev(answers, error)
    snapshot = SimpleNamespace(
        policy=policy_from_dict(raw),
        jev=JevSettings("http://jev.test", "d1-2B"),
        time_periods=[], jev_input_tokens=1024,
    )
    trace = []
    outcome = asyncio.run(PolicyEvaluator(jev).evaluate(
        snapshot, RoutingRequest(text),
        now=datetime.fromisoformat("2026-11-18T10:00:00+08:00"),
        trace_sink=trace,
    ))
    return outcome, jev


def make_policy_client(tmp_path):
    settings = test_settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    conn.execute("INSERT INTO providers(name, base_url) VALUES ('local', 'http://local.test')")
    for model in ("fast", "strong"):
        conn.execute(
            "INSERT INTO models(id, upstream, provider, profile) VALUES (?, ?, 'local', '{}')",
            (model, model),
        )
    JevConfigRepo(conn).save(JevSettings("http://jev.test", "d1-2B"))
    conn.commit()
    conn.close()
    jev_requests, upstream_requests = [], []

    def handler(request):
        if request.url.path == "/v1/systemone":
            jev_requests.append(request)
            state = json.loads(request.content).get("state", "")
            risk = 0.9 if "忽略之前" in state else 0.1
            return httpx.Response(200, json={
                "answers": {
                    "route": {"type": "choice", "probabilities": {"轻量": 0.8, "增强": 0.2}},
                    "risk": {"type": "noul", "noul": risk},
                    "severity": {"type": "score", "score": 2, "legend": {"2": 5}},
                },
                "usage": {"input_tokens": 100, "output_tokens": 0},
            })
        upstream_requests.append(request)
        return httpx.Response(200, json={
            "id": "chat-test", "object": "chat.completion", "created": 0,
            "model": "fast", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    app = create_app(settings, transport=httpx.MockTransport(handler))
    app.state.test_jev_requests = jev_requests
    app.state.test_upstream_requests = upstream_requests
    return TestClient(app), app


make_policy_client.__test__ = False
