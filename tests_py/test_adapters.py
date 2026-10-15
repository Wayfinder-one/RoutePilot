"""Task 7 测试：双协议 IR + 流式互转 + fake-upstream。

简报 Step 1 的 5 个测试（test_openai_parse_extracts_last_user_text 至
test_anthropic_event_sequence）逐字采用未动；其后 6 个 ``test_fake_*`` 为本任务
自加的假上游验收测试——fake-upstream 是 Task 8/11 的测试底座（spec §14.4），
其双协议非流式 / 流式 / fail=500 / fail=midstream / latency_ms 行为在底层锁死。
"""

import json
import time

from fastapi.testclient import TestClient

from sr.adapters import openai, anthropic
import sr.fake_upstream as fake_upstream

OA_BODY = {"model": "auto", "stream": False,
           "messages": [{"role": "system", "content": "你是助手"},
                        {"role": "user", "content": "画一张架构图"}]}
AN_BODY = {"model": "auto", "stream": False, "max_tokens": 1024,
           "system": "你是助手",
           "messages": [{"role": "user", "content": "画一张架构图"}]}

def test_openai_parse_extracts_last_user_text():
    ir = openai.parse(OA_BODY)
    assert ir.system == "你是助手" and ir.params["max_tokens"] is None
    assert openai.main_text(ir) == "画一张架构图"

def test_anthropic_parse_system_field():
    ir = anthropic.parse(AN_BODY)
    assert ir.system == "你是助手" and anthropic.main_text(ir) == "画一张架构图"

def test_think_param_mapping():
    assert openai.to_upstream(openai.parse(OA_BODY), "m", "deep")["reasoning_effort"] == "high"
    body = anthropic.to_upstream(anthropic.parse(AN_BODY), "m", "light")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}

def test_response_model_rewrite():
    resp = {"model": "qwen3-32b-real", "choices": [{"message": {"content": "ok"}}]}
    out = openai.from_upstream(resp, model_asked="auto")
    assert out["model"] == "auto"

def test_anthropic_event_sequence():
    # OpenAI chunk → IR → Anthropic 事件序列（§7.6）
    state = anthropic.StreamState()
    evts = []
    for ch in [{"choices":[{"delta":{"content":"你"}}]},
               {"choices":[{"delta":{"content":"好"}}]},
               {"choices":[{"delta":{}}], "usage":{"total_tokens":2}}]:
        evts += anthropic.ir_to_events(openai.chunk_to_ir(ch), state)
    assert evts[0].startswith("event: message_start")
    assert sum(e.startswith("event: content_block_delta") for e in evts) == 2
    assert any(e.startswith("event: message_delta") for e in evts)  # usage
    assert evts[-1].startswith("event: message_stop")


# ─────────────────────────────────────────────────────────────────────────────
# 以下为本任务自加：fake-upstream 验收器具测试（spec §14.4 底座行为锁死）
# ─────────────────────────────────────────────────────────────────────────────

FAKE = TestClient(fake_upstream.create_app())
USER = {"role": "user", "content": "画一张架构图"}

def test_fake_nonstream_echoes_upstream_model():
    r = FAKE.post("/v1/chat/completions",
                  json={"model": "qwen3-32b-real", "messages": [USER]})
    assert r.status_code == 200
    assert r.json()["model"] == "qwen3-32b-real"       # model 回显请求的上游名
    assert r.json()["choices"][0]["message"]["content"]
    r = FAKE.post("/v1/messages",
                  json={"model": "claude-fake", "max_tokens": 16, "messages": [USER]})
    assert r.status_code == 200
    assert r.json()["model"] == "claude-fake"
    assert r.json()["content"][0]["text"]

def test_fake_openai_stream_three_deltas_then_done():
    r = FAKE.post("/v1/chat/completions",
                  json={"model": "m", "stream": True,
                        "stream_options": {"include_usage": True}, "messages": [USER]})
    packets = [p for p in r.text.split("\n\n") if p.strip()]
    assert packets[-1] == "data: [DONE]"                        # 正常终止（§7.3）
    chunks = [json.loads(p[len("data: "):]) for p in packets[:-1]]
    texts = [c["choices"][0]["delta"].get("content") for c in chunks if c.get("choices")]
    assert "".join(t for t in texts if t) == "".join(fake_upstream.DELTAS)   # 3 段 delta
    assert any(c["choices"][0].get("finish_reason") == "stop"
               for c in chunks if c.get("choices"))
    assert chunks[-1].get("usage") and chunks[-1]["choices"] == []   # include_usage 末 chunk

def test_fake_anthropic_stream_event_sequence():
    r = FAKE.post("/v1/messages",
                  json={"model": "m", "max_tokens": 16, "stream": True, "messages": [USER]})
    blocks = [b for b in r.text.split("\n\n") if b.strip()]
    names = [b.splitlines()[0][len("event: "):] for b in blocks]
    assert names == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]   # §7.4 顺序

def test_fake_fail_500_returns_protocol_envelope():
    r = FAKE.post("/v1/chat/completions?fail=500", json={"model": "m", "messages": [USER]})
    assert r.status_code == 500
    assert set(r.json()["error"]) >= {"message", "type"}        # §7.3 错误信封
    r = FAKE.post("/v1/messages?fail=500",
                  json={"model": "m", "max_tokens": 16, "messages": [USER]})
    assert r.status_code == 500
    assert r.json()["type"] == "error" and r.json()["error"]["type"]   # §7.4 错误信封
    # 非流式 + fail=midstream 等同 fail=500（裁定：midstream 仅对流式有意义）
    r = FAKE.post("/v1/chat/completions?fail=midstream", json={"model": "m", "messages": [USER]})
    assert r.status_code == 500

def test_fake_openai_midstream_cut_after_first_packet():
    r = FAKE.post("/v1/chat/completions?fail=midstream",
                  json={"model": "m", "stream": True, "messages": [USER]})
    assert r.text.startswith("data: ")                 # 首包已发出
    assert r.text.count("data: ") == 1                 # 恰好切断在首包之后
    assert "data: [DONE]" not in r.text                # 终止符未达——§7.5 红线器具

def test_fake_anthropic_midstream_cut_after_first_packet():
    r = FAKE.post("/v1/messages?fail=midstream",
                  json={"model": "m", "max_tokens": 16, "stream": True, "messages": [USER]})
    assert r.text.startswith("event: message_start")   # 首包已发出
    assert r.text.count("event: ") == 1                # 恰好切断在首包之后
    assert "message_stop" not in r.text                # 终止事件未达——§7.5 红线器具

def test_fake_latency_ms_delays_response():
    t0 = time.monotonic()
    FAKE.post("/v1/chat/completions?latency_ms=120", json={"model": "m", "messages": [USER]})
    assert time.monotonic() - t0 >= 0.12
