"""/v1/chat/completions 的推理档位注入回归（Policy v4 组合根）。

策略用无问题、无规则的静态 v4 文档表达「直连」语义：fallback 即目标，
一次决策不触发 JEV；目标模型的 ``reasoning`` 档位决定注入行为。其余
断言与推理方言映射保持原契约：九种 format 注入、provider 继承与模型
覆盖、过期档位降级为透传并携带 warning、流式请求同样注入且释放租约。
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from router.api.app import create_app
from router.routing.conditions import TimePeriod
from router.routing.snapshot import build_snapshot, model_capability, read_jev_window
from router.store.db import connect
from router.store.jev_config import JevConfigRepo
from router.store.policies import PolicyStorageError
from router.store.repositories import ModelRepo, TimePeriodRepo
from router.upstream.catalog import load_backends
from tests.routing_fixtures import test_settings

TEXT = "今天天气怎么样"
POLICY_ID = "reason-policy"


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: hi\n\n"


class StreamingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append({"body": json.loads(await request.aread())})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())


def harness(tmp_path, *, streaming=False):
    requests = []

    def handler(request):
        requests.append({"body": json.loads(request.content)})
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    transport = StreamingTransport() if streaming else httpx.MockTransport(handler)
    app = create_app(test_settings(tmp_path), transport=transport)
    return SimpleNamespace(app=app, client=TestClient(app), requests=transport.requests if streaming else requests)


def direct_policy(reasoning=None):
    """无问题、无规则的静态策略：fallback 即目标，不触发 JEV。"""
    return {
        "kind": "sr-routing-policy",
        "version": 4,
        "name": "推理直连",
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "questions": {},
        "targets": {
            "direct": {"models": [{"model": "qwen3-8b", "reasoning": reasoning}]},
        },
        "rules": [],
        "fallback": "direct",
    }


def declare(client, reasoning, *, provider_format=None):
    if provider_format:
        response = client.put("/admin/providers/local", json={"base_url": "http://upstream.test/v1", "reasoning_format": provider_format})
        assert response.status_code == 200, response.text
    if any(model["id"] == "qwen3-8b" for model in client.get("/admin/models").json()["models"]):
        response = client.put("/admin/models/qwen3-8b", json={"profile": {"reasoning": reasoning}})
        assert response.status_code == 200, response.text
    else:
        response = client.post("/admin/models", json={
            "id": "qwen3-8b", "upstream": "qwen3-8b", "provider": "local",
            "profile": {"reasoning": reasoning},
        })
        assert response.status_code == 201, response.text


def activate(client, level=None):
    policy = direct_policy(level)
    existing = client.get(f"/admin/policies/{POLICY_ID}").json().get("policy_resource")
    if existing is None:
        response = client.post("/admin/policies", json={"id": POLICY_ID, "policy": policy})
        assert response.status_code == 201, response.text
    else:
        response = client.put(f"/admin/policies/{POLICY_ID}", json=policy,
                              headers={"if-match": existing["document_sha256"]})
        assert response.status_code == 200, response.text
    if not response.json()["policy_resource"]["enabled"]:
        assert client.post(f"/admin/policies/{POLICY_ID}/enable").status_code == 200


def chat(client, **body):
    return client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": TEXT}], **body})


def test_null_reasoning_is_full_passthrough(tmp_path):
    h = harness(tmp_path)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    activate(h.client)
    fields = {"reasoning_effort": "client", "reasoning": {"effort": "client"}, "thinking": {"type": "enabled"}, "enable_thinking": True}
    response = chat(h.client, **fields)
    assert response.status_code == 200
    assert "x-sr-reasoning" not in response.headers
    sent = h.requests[-1]["body"]
    for key, value in fields.items():
        assert sent[key] == value


FORMAT_CASES = [
    ("openai", {"off": None, "high": "high"}, "high", {"reasoning_effort": "high"}),
    ("deepseek", {"off": None, "high": "high"}, "high", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("qwen", {"off": None, "on": None}, "on", {"enable_thinking": True}),
    ("qwen-chat-template", {"off": None, "on": None}, "on", {"chat_template_kwargs": {"keep": 1, "enable_thinking": True}}),
    ("openrouter", {"off": None, "high": "high"}, "high", {"reasoning": {"effort": "high"}}),
    ("zai", {"off": None, "high": "high"}, "high", {"thinking": {"type": "enabled", "clear_thinking": False}, "reasoning_effort": "high"}),
    ("together", {"off": None, "high": "high"}, "high", {"reasoning": {"enabled": True}, "reasoning_effort": "high"}),
    ("string-thinking", {"off": None, "high": "high"}, "high", {"thinking": "high"}),
    ("ant-ling", {"off": None, "high": "high"}, "high", {"reasoning": {"effort": "high"}}),
]


@pytest.mark.parametrize("fmt,efforts,level,expected", FORMAT_CASES)
def test_reasoning_injection_per_format(tmp_path, fmt, efforts, level, expected):
    h = harness(tmp_path)
    declare(h.client, {"format": fmt, "efforts": efforts})
    activate(h.client, level)
    response = chat(h.client, reasoning_effort="client", thinking="client", enable_thinking=False, chat_template_kwargs={"keep": 1})
    assert response.status_code == 200, response.text
    assert response.headers["x-sr-reasoning"] == level
    sent = h.requests[-1]["body"]
    for key, value in expected.items():
        assert sent[key] == value, (fmt, key)
    record = h.client.get("/admin/requests/recent").json()["requests"][0]
    assert record["reasoning"] == level
    assert record["policy_id"] == POLICY_ID


def test_provider_format_inheritance_and_model_override(tmp_path):
    h = harness(tmp_path)
    declare(h.client, {"format": "qwen", "efforts": {"off": None, "on": None}}, provider_format="deepseek")
    activate(h.client, "on")
    assert chat(h.client).status_code == 200
    assert h.requests[-1]["body"]["enable_thinking"] is True


def test_stale_reasoning_level_degrades_to_passthrough_with_warning(tmp_path):
    """快照先于模型降级构建：请求期档位不再受支持 → 透传 + warning。

    模型 profile 降级后，对启用策略的重载会因启用级校验失败而拒绝
    （PolicyStorageError），旧快照继续服务；手工按「旧策略 + 新目录」
    重建快照，模拟发布时点早于降级的真实状态。
    """
    h = harness(tmp_path)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    activate(h.client, "high")
    conn = connect(h.app.state.settings.db_path)
    with conn:
        conn.execute("UPDATE models SET profile = ? WHERE id = 'qwen3-8b'", (json.dumps({"reasoning": {"efforts": {"off": None, "low": "low"}}}),))
    stale_snapshot = h.app.state.snapshot
    with pytest.raises(PolicyStorageError):
        h.app.state.publisher.refresh(conn)
    assert h.app.state.snapshot is stale_snapshot
    # 旧策略资源 + 降级后的模型目录：请求期推理注入必须降级而不是崩溃。
    stale_resource = h.app.state.policy_repo.get(POLICY_ID)
    input_tokens, context_tokens = read_jev_window(conn)
    h.app.state.snapshot = build_snapshot(
        resource=stale_resource,
        backends=load_backends(conn),
        time_periods=[TimePeriod.from_dict(row) for row in TimePeriodRepo(conn).load()],
        jev=JevConfigRepo(conn).load(),
        capabilities={row["id"]: model_capability(row) for row in ModelRepo(conn).list()},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
    )
    conn.close()
    response = chat(h.client, reasoning_effort="client")
    assert response.status_code == 200
    assert response.headers["x-sr-reasoning-warning"] == "unsupported"
    assert h.requests[-1]["body"]["reasoning_effort"] == "client"


def test_streaming_request_injects_reasoning_and_releases(tmp_path):
    h = harness(tmp_path, streaming=True)
    declare(h.client, {"efforts": {"off": None, "high": "high"}})
    activate(h.client, "high")
    response = chat(h.client, stream=True)
    assert response.status_code == 200
    assert response.headers["x-sr-reasoning"] == "high"
    assert response.text == "data: hi\n\n"
    assert h.requests[-1]["body"]["reasoning_effort"] == "high"
    assert h.app.state.balancer.inflight("qwen3-8b") == 0
