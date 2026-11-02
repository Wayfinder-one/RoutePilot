"""多问题 JevClient（choose_many）与三类答案的 wire 契约测试。

MockTransport 响应严格按 2026-11-02 M0 冻结的实测 wire 形状构造：
- noul 答案：{"type": "noul", "noul": <P(yes)>}，客户端派生 no = 1 - yes；
- score 答案：{"type": "score", "score": <档位索引>, "legend": {"<索引>": <档位值>}}，
  客户端映射 value = legend[str(int(round(score)))]；
- choice 答案的 probabilities 以 label 为 key，客户端映射回 option key。
"""

import asyncio
import json

import httpx
import pytest

from router.jev.client import (
    ChoiceAnswer,
    DecisionQuestion,
    JevCandidate,
    JevClient,
    JevError,
    NoulAnswer,
    ScoreAnswer,
)
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings


def settings(**overrides):
    values = {
        "base_url": "http://jev.test/",
        "model": "NeoHorse-Jev-4B",
        "api_key": "secret",
        "timeout_seconds": 5.0,
        "instructions": DEFAULT_INSTRUCTIONS,
        "state_template": DEFAULT_STATE_TEMPLATE,
    }
    values.update(overrides)
    return JevSettings(**values)


CHOICE = DecisionQuestion("choice", "请为该请求选择模型能力档位", (
    JevCandidate("light", "轻量", "简单"),
    JevCandidate("strong", "增强", "复杂"),
))
NOUL = DecisionQuestion("noul", "请判断该请求是否属于电力领域问题")
SCORE = DecisionQuestion("score", "请评估该请求的复杂度档位", (), (1.0, 10.0))
QUESTIONS = {"capability": CHOICE, "domain": NOUL, "complexity": SCORE}

USAGE = {"input_tokens": 321, "output_tokens": 7}


def multi_body():
    return {
        "answers": {
            "capability": {
                "type": "choice",
                "probabilities": {"轻量": 0.2, "增强": 0.8},
                "confidence": 0.8,
            },
            "domain": {"type": "noul", "noul": 0.25},
            "complexity": {
                "type": "score",
                "score": 2.0,
                "legend": {"2": 7.5},
                "probabilities": {"2": 1.0},
                "confidence": 1.0,
            },
        },
        "usage": dict(USAGE),
    }


def run(client, questions=QUESTIONS, text="110kV线路故障如何处理"):
    return asyncio.run(client.choose_many(settings(), text, questions))


def test_batch_sends_three_questions_in_one_call_and_parses_three_answers():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=multi_body())

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    answers = run(client)

    assert list(answers) == ["capability", "domain", "complexity"]
    assert isinstance(answers["capability"], ChoiceAnswer)
    assert isinstance(answers["domain"], NoulAnswer)
    assert isinstance(answers["complexity"], ScoreAnswer)

    assert len(requests) == 1
    request = requests[0]
    assert request.url == "http://jev.test/v1/systemone"
    assert request.headers["authorization"] == "Bearer secret"
    payload = json.loads(request.content)
    assert payload["model"] == "NeoHorse-Jev-4B"
    assert payload["state"] == "【待判断的用户请求】\n110kV线路故障如何处理\n\n【任务】请选择最合适处理这条请求的模型。"
    assert list(payload["questions"]) == ["capability", "domain", "complexity"]
    assert payload["questions"]["capability"] == {
        "type": "choice",
        "instructions": "请为该请求选择模型能力档位",
        "criteria": {"轻量": "简单", "增强": "复杂"},
    }
    assert payload["questions"]["domain"] == {
        "type": "noul",
        "instructions": "请判断该请求是否属于电力领域问题",
    }
    assert payload["questions"]["complexity"] == {
        "type": "score",
        "instructions": "请评估该请求的复杂度档位",
        "criteria": [1.0, 10.0],
    }

    assert answers["capability"].probabilities == {"light": 0.2, "strong": 0.8}
    assert answers["capability"].ranking == ["strong", "light"]
    assert answers["capability"].selected_key == "strong"
    assert answers["capability"].p_top == 0.8
    assert answers["capability"].margin == pytest.approx(0.6)
    assert answers["domain"].yes == 0.25 and answers["domain"].no == 0.75
    assert answers["complexity"].value == 7.5

    usage = answers["capability"].usage
    assert usage == USAGE
    assert usage is answers["domain"].usage is answers["complexity"].usage
    duration = answers["capability"].duration_ms
    assert answers["domain"].duration_ms == answers["complexity"].duration_ms == duration
    assert duration >= 0.0


def test_question_mapping_order_is_preserved_in_request_payload():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=multi_body())

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run(client, questions={"complexity": SCORE, "domain": NOUL, "capability": CHOICE})
    payload = json.loads(requests[0].content)
    assert list(payload["questions"]) == ["complexity", "domain", "capability"]


def test_score_value_is_mapped_through_legend_not_the_raw_index():
    body = multi_body()
    body["answers"]["complexity"] = {"type": "score", "score": 0.0, "legend": {"0": 7.5, "1": 10.0}}
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    answers = run(client)
    assert isinstance(answers["complexity"], ScoreAnswer)
    assert answers["complexity"].value == 7.5


def test_choice_tie_is_resolved_by_option_config_order():
    body = multi_body()
    # wire 键序（增强, 轻量）与 option 配置序（light, strong）相反：只有按配置序打破平局
    # 才会选中 light，按 JSON dict 序实现则会错误地选中 strong。
    body["answers"]["capability"] = {"type": "choice", "probabilities": {"增强": 0.5, "轻量": 0.5}}
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    answers = run(client)
    assert answers["capability"].ranking == ["light", "strong"]
    assert answers["capability"].selected_key == "light"
    assert answers["capability"].margin == 0.0


def test_choice_probabilities_are_normalized():
    body = multi_body()
    # 契约：|sum - 1| <= 1e-3 视为有效并归一化；超差必须抛 JevError（见参数化失败用例）。
    body["answers"]["capability"] = {"type": "choice", "probabilities": {"轻量": 0.2, "增强": 0.8002}}
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    answers = run(client)
    probabilities = answers["capability"].probabilities
    assert probabilities == pytest.approx({"light": 0.2 / 1.0002, "strong": 0.8002 / 1.0002})
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert set(probabilities) == {"light", "strong"}
    assert answers["capability"].ranking == ["strong", "light"]
    assert answers["capability"].selected_key == "strong"
    assert answers["capability"].p_top == pytest.approx(0.8002 / 1.0002)


def test_non_dict_usage_falls_back_to_empty_dict_on_every_answer():
    body = multi_body()
    body["usage"] = "n/a"
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    answers = run(client)
    assert answers["capability"].usage == {}
    assert answers["domain"].usage == {}
    assert answers["complexity"].usage == {}


# 任一问题的解析失败都必须抛 JevError、消息含该问题 key（node id）、且绝不返回部分 dict。
def wire_body(body):
    """按真实 wire 字节构造响应：NaN/inf 用例无法走 httpx.Response(json=...) 的默认序列化。"""
    return httpx.Response(
        200,
        content=json.dumps(body, allow_nan=True),
        headers={"content-type": "application/json"},
    )


FAILURE_CASES = [
    ("missing-answer", lambda b: b["answers"].pop("domain"), "domain"),
    ("inexact-probability-keys", lambda b: b["answers"]["capability"].update(probabilities={"轻量": 0.8}), "capability"),
    ("negative-probability", lambda b: b["answers"]["capability"]["probabilities"].update({"轻量": -0.2, "增强": 1.2}), "capability"),
    ("probability-sum-out-of-tolerance", lambda b: b["answers"]["capability"].update(probabilities={"轻量": 0.4, "增强": 0.4}), "capability"),
    ("nan-probability", lambda b: b["answers"]["capability"]["probabilities"].update({"轻量": float("nan")}), "capability"),
    # 判别性：bool 与 0.0 求和恰为 1，若去掉 bool 检查会通过 —— 失败只能来自 bool 拒绝。
    ("bool-probability", lambda b: b["answers"]["capability"].update(probabilities={"轻量": True, "增强": 0.0}), "capability"),
    ("string-probability", lambda b: b["answers"]["capability"]["probabilities"].update({"轻量": "0.2"}), "capability"),
    ("noul-out-of-range", lambda b: b["answers"]["domain"].update(noul=1.5), "domain"),
    ("noul-bool", lambda b: b["answers"]["domain"].update(noul=True), "domain"),
    ("noul-missing", lambda b: b["answers"]["domain"].pop("noul"), "domain"),
    ("answer-type-mismatch", lambda b: b["answers"]["domain"].update(type="score"), "domain"),
    ("score-non-numeric", lambda b: b["answers"]["complexity"].update(score="high"), "complexity"),
    # 判别性：legend 含 "1" 时，若 True 被当数字会解析成 value=5.0 而不报错；
    # 失败只能来自 bool 拒绝（而不是 legend 未命中）。
    ("score-bool", lambda b: b["answers"]["complexity"].update(score=True, legend={"0": 2.0, "1": 5.0}), "complexity"),
    ("score-legend-miss", lambda b: b["answers"]["complexity"].update(score=3.0), "complexity"),
    ("score-legend-missing", lambda b: b["answers"]["complexity"].pop("legend"), "complexity"),
    ("score-legend-value-bool", lambda b: b["answers"]["complexity"]["legend"].update({"2": True}), "complexity"),
    ("score-legend-value-nan", lambda b: b["answers"]["complexity"]["legend"].update({"2": float("nan")}), "complexity"),
    ("answers-not-an-object", lambda b: b.update(answers=[]), "capability"),
]


@pytest.mark.parametrize(
    ["case", "mutate", "key"],
    FAILURE_CASES,
    ids=[case[0] for case in FAILURE_CASES],
)
def test_invalid_wire_answers_raise_jev_error_with_question_key(case, mutate, key):
    body = multi_body()
    mutate(body)
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: wire_body(body))))
    with pytest.raises(JevError) as excinfo:
        run(client)
    assert key in str(excinfo.value)


def test_http_500_raises_jev_error():
    client = JevClient(httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="down")),
    ))
    with pytest.raises(JevError, match="HTTP 500"):
        run(client)


def test_transport_timeout_raises_jev_error():
    def timeout(request):
        raise httpx.TimeoutException("slow upstream")

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(timeout)))
    with pytest.raises(JevError) as excinfo:
        run(client)
    assert "JEV transport failed" in str(excinfo.value)
    assert "TimeoutException" in str(excinfo.value)


def test_environment_key_is_used_when_stored_key_is_empty(monkeypatch):
    monkeypatch.setenv("ROUTER_JEV_API_KEY", "environment-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=multi_body())

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(client.choose_many(settings(api_key=""), "请求", QUESTIONS))
    assert requests[0].headers["authorization"] == "Bearer environment-secret"


def test_choose_many_requires_a_nonempty_question_mapping():
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=multi_body()))))
    with pytest.raises(JevError):
        asyncio.run(client.choose_many(settings(), "请求", {}))


def test_decision_question_validates_type_options_and_criteria():
    assert CHOICE.criteria is None and NOUL.criteria is None
    assert SCORE.criteria == (1.0, 10.0)
    with pytest.raises(ValueError):
        DecisionQuestion("unknown", "instructions")
    with pytest.raises(ValueError):
        DecisionQuestion("score", "评分", (), (1.0,))
    with pytest.raises(ValueError):
        DecisionQuestion("score", "评分", (), (1.0, True))
    with pytest.raises(ValueError):
        DecisionQuestion("score", "评分", (JevCandidate("a", "A", "x"),), (1.0, 10.0))
    with pytest.raises(ValueError):
        DecisionQuestion("choice", "选择", (JevCandidate("a", "A", "x"),))
    with pytest.raises(ValueError):
        DecisionQuestion("choice", "选择", CHOICE.options, (1.0, 10.0))
    with pytest.raises(ValueError):
        DecisionQuestion("noul", "noul", (), (1.0, 2.0))
    with pytest.raises(ValueError):
        DecisionQuestion("choice", "  ")
