"""PolicyEvaluator 调度测试共用替身（Task 3 与后续调度/试算测试）。

三类答案构造器只产出已通过 wire 校验的合法答案形态；评估/试算测试统一从
这里取答案结构，不在各自文件里自造。``run_policy`` 固定 ``now`` 与 1024
token 输入窗口，保证轨迹断言不受真实时间影响。
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from router.jev.client import ChoiceAnswer, NoulAnswer, ScoreAnswer
from router.jev.config import JevSettings
from router.routing.policy import policy_from_dict
from router.routing.policy.evaluator import PolicyEvaluator
from router.routing.types import RoutingRequest


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
