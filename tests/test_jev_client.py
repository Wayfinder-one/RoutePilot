import asyncio
import json

import httpx
import pytest

from router.jev.client import JevCandidate, JevClient, JevError
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings


CANDIDATES = (
    JevCandidate("fast", "轻量模型", "简单问答和短翻译。"),
    JevCandidate("strong", "增强模型", "复杂推理和长文档。"),
)


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


def response(probabilities=None):
    return httpx.Response(200, json={
        "answers": {"route": {
            "choice": "增强模型",
            "probabilities": probabilities if probabilities is not None else {"轻量模型": 0.2, "增强模型": 0.8},
            "confidence": 0.8,
        }},
        "usage": {"input_tokens": 123, "output_tokens": 0},
    })


def test_choice_uses_neohorse_systemone_contract_and_probability_ranking():
    requests = []

    def handler(request):
        requests.append(request)
        return response()

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    answer = asyncio.run(client.choose(settings(), "110kV线路故障如何处理", CANDIDATES))
    assert answer.probabilities == {"fast": 0.2, "strong": 0.8}
    assert answer.ranking == ["strong", "fast"]
    assert answer.selected_key == "strong"
    assert answer.p_top == 0.8
    assert answer.margin == pytest.approx(0.6)
    assert answer.usage["input_tokens"] == 123
    request = requests[0]
    assert request.url == "http://jev.test/v1/systemone"
    assert request.headers["authorization"] == "Bearer secret"
    payload = json.loads(request.content)
    assert payload["model"] == "NeoHorse-Jev-4B"
    assert payload["state"] == "【待判断的用户请求】\n110kV线路故障如何处理\n\n【任务】请选择最合适处理这条请求的模型。"
    question = payload["questions"]["route"]
    assert question["type"] == "choice"
    assert question["instructions"] == DEFAULT_INSTRUCTIONS
    assert list(question["criteria"]) == ["轻量模型", "增强模型"]


def test_tie_is_resolved_by_configuration_order_not_returned_choice():
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response({"轻量模型": 0.5, "增强模型": 0.5}))))
    answer = asyncio.run(client.choose(settings(), "请求", CANDIDATES))
    assert answer.selected_key == "fast"
    assert answer.margin == 0.0


@pytest.mark.parametrize("probabilities", [
    {"轻量模型": 1.0},
    {"轻量模型": -0.1, "增强模型": 1.1},
    {"轻量模型": 0.2, "增强模型": 0.2},
    {"轻量模型": True, "增强模型": 0.0},
    {"轻量模型": 0.2, "增强模型": "0.8"},
])
def test_invalid_probability_distributions_raise_jev_error(probabilities):
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(probabilities))))
    with pytest.raises(JevError):
        asyncio.run(client.choose(settings(), "请求", CANDIDATES))


def test_http_and_transport_failures_raise_jev_error():
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503, text="down"))))
    with pytest.raises(JevError, match="503"):
        asyncio.run(client.choose(settings(), "请求", CANDIDATES))

    def timeout(request):
        raise httpx.TimeoutException("slow")

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(timeout)))
    with pytest.raises(JevError):
        asyncio.run(client.choose(settings(), "请求", CANDIDATES))


def test_client_reuses_stable_distribution_after_float_normalization():
    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response({"轻量模型": 0.3333, "增强模型": 0.6667}))))
    answer = asyncio.run(client.choose(settings(), "请求", CANDIDATES))
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)


def test_environment_key_is_used_when_stored_key_is_empty(monkeypatch):
    monkeypatch.setenv("ROUTER_JEV_API_KEY", "environment-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return response()

    client = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(client.choose(settings(api_key=""), "请求", CANDIDATES))
    assert requests[0].headers["authorization"] == "Bearer environment-secret"
