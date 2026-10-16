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

def test_include_usage_double_terminates_once():
    # include_usage 真实线序：finish chunk → 纯 usage chunk（choices:[]）前后脚两包；
    # 终止段（message_delta + message_stop）只发一次（spec §7.4 单 message_stop）。
    state = anthropic.StreamState()
    evts = []
    for ch in [{"choices":[{"delta":{"content":"你"}}]},
               {"choices":[{"delta":{}, "finish_reason":"stop"}]},
               {"choices":[], "usage":{"prompt_tokens":1, "completion_tokens":2}}]:
        evts += anthropic.ir_to_events(openai.chunk_to_ir(ch), state)
    assert sum(e.startswith("event: message_stop") for e in evts) == 1
    assert sum(e.startswith("event: message_delta") for e in evts) == 1
    md = json.loads(next(e for e in evts
                         if e.startswith("event: message_delta")).split("data: ", 1)[1])
    assert md["usage"]["input_tokens"] == 1 and md["usage"]["output_tokens"] == 2
    assert md["delta"]["stop_reason"] == "end_turn"     # 取 finish chunk，不被 usage chunk 清掉


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


# ─────────────────────────────────────────────────────────────────────────────
# I4（终审 Important）：跨协议终止原因归一 + 互查表消重
# 实测：Anthropic 上游 → OpenAI 客户端 **流式** 时 finish_reason 直传
# （end_turn/max_tokens/tool_use/stop_sequence 原样吐给 OpenAI 客户端）；非流式
# 反而是对的。修法 = adapters 导出唯一表（STOP_TO_FINISH/FINISH_TO_STOP），
# chunk_to_ir 与 server 共用。
# ─────────────────────────────────────────────────────────────────────────────

from sr.adapters import STOP_TO_FINISH, FINISH_TO_STOP          # noqa: E402


def _an_event(kind: str, **data) -> dict:
    return {"type": kind, **data}


def test_anthropic_stop_reason_to_openai_finish_reason_stream():
    """Anthropic message_delta 的四种 stop_reason → OpenAI finish_reason（§7.6）。"""
    for stop, want in (("end_turn", "stop"), ("max_tokens", "length"),
                       ("tool_use", "tool_calls"), ("stop_sequence", "stop")):
        ic = anthropic.chunk_to_ir(_an_event(
            "message_delta", delta={"stop_reason": stop},
            usage={"input_tokens": 1, "output_tokens": 2}))
        assert ic.finish == want, (stop, ic.finish)
        out = openai.ir_to_chunk(ic, "auto")
        assert out["choices"][0]["finish_reason"] == want, stop


def test_anthropic_unknown_stop_reason_is_none():
    """枚举外取值不猜语义（回 None，而不是原样透传）。"""
    ic = anthropic.chunk_to_ir(_an_event(
        "message_delta", delta={"stop_reason": "pause_turn"}, usage={}))
    assert ic.finish is None


def test_openai_finish_reason_to_anthropic_stop_reason_reverse():
    """反向（OpenAI 上游 → Anthropic 客户端）同样归一到 §7.4 枚举。"""
    for finish, want in (("stop", "end_turn"), ("length", "max_tokens"),
                         ("tool_calls", "tool_use"), ("function_call", "tool_use")):
        state = anthropic.StreamState()
        evts = []
        evts += anthropic.ir_to_events(openai.chunk_to_ir(
            {"choices": [{"delta": {"content": "x"}}]}), state)
        evts += anthropic.ir_to_events(openai.chunk_to_ir(
            {"choices": [{"delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}), state)
        md = json.loads(next(e for e in evts
                             if e.startswith("event: message_delta")).split("data: ", 1)[1])
        assert md["delta"]["stop_reason"] == want, (finish, md)


def test_stop_finish_tables_are_the_single_shared_source():
    """消重（rollup #24 真问题是「漏用」）：唯一表在 sr/adapters，
    server 与 anthropic 适配器都取同一对象，不再各持私有副本。"""
    from sr import server
    assert server.STOP_TO_FINISH is STOP_TO_FINISH
    assert server.FINISH_TO_STOP is FINISH_TO_STOP
    assert anthropic._STOP_REASON is FINISH_TO_STOP
    # 互逆：Anthropic → OpenAI 后再回 Anthropic 稳定（stop_sequence 按语义声明
    # 折叠到 "stop"，不参与可逆性断言）
    for stop, finish in STOP_TO_FINISH.items():
        assert FINISH_TO_STOP[finish] == ("end_turn" if stop == "stop_sequence"
                                          else stop), stop


# ─────────────────────────────────────────────────────────────────────────────
# 收尾微修（本 commit）备注 2：同协议 anthropic→anthropic 流式 stop_reason 原样
# 回环。旧实现 ir_to_events 只查 _STOP_REASON.get(ic.finish)，而 ic.finish 已被
# chunk_to_ir 归一为 OpenAI 语义（stop_sequence → "stop"）→ 回程再映射就把
# stop_sequence 误报成 end_turn、refusal/pause_turn 丢成 null。
# 修法：chunk_to_ir 把原始 Anthropic stop_reason 存进 IRChunk.raw_finish，
# ir_to_events 优先用它；raw_finish 为 None（来源是 OpenAI chunk）时仍走共享表。
# ─────────────────────────────────────────────────────────────────────────────


def _an_message_delta(stop: str):
    return anthropic.chunk_to_ir(_an_event(
        "message_delta", delta={"stop_reason": stop},
        usage={"input_tokens": 1, "output_tokens": 2}))


def _message_delta(evts: list[str]) -> dict:
    return json.loads(next(e for e in evts
                           if e.startswith("event: message_delta")).split("data: ", 1)[1])


def test_same_protocol_anthropic_stop_reason_roundtrip():
    """同协议回环：四个 stop_reason 原样（旧实现 stop_sequence → end_turn）。"""
    for stop in ("end_turn", "max_tokens", "tool_use", "stop_sequence"):
        state = anthropic.StreamState()
        evts = anthropic.ir_to_events(anthropic.chunk_to_ir(_an_event(
            "content_block_delta", index=0,
            delta={"type": "text_delta", "text": "好"})), state)
        evts += anthropic.ir_to_events(_an_message_delta(stop), state)
        md = _message_delta(evts)
        assert md["delta"]["stop_reason"] == stop, (stop, md)
        assert md["usage"]["output_tokens"] == 2, md          # usage 不被丢
        assert evts[-1].startswith("event: message_stop")


def test_anthropic_chunk_to_ir_keeps_raw_stop_reason():
    """chunk_to_ir 双写：finish 保持 OpenAI 口径（跨协议用），raw_finish 存原值。"""
    ic = _an_message_delta("stop_sequence")
    assert ic.finish == "stop" and ic.raw_finish == "stop_sequence"
    # OpenAI chunk 无 Anthropic 原值 → raw_finish 默认 None（跨协议走共享表）
    assert openai.chunk_to_ir(
        {"choices": [{"delta": {}, "finish_reason": "stop"}]}).raw_finish is None
    assert anthropic.chunk_to_ir(
        _an_event("content_block_delta", index=0,
                  delta={"type": "text_delta", "text": "x"})).raw_finish is None


def test_same_protocol_unknown_stop_reason_passes_through_raw():
    """同协议不做映射：枚举外的 Anthropic 原值（pause_turn/refusal）也原样转发
    （旧实现丢成 null）。跨协议路径仍按共享表回 null，见
    test_anthropic_unknown_stop_reason_is_none——raw_finish 仅 Anthropic 来源有值。"""
    for stop in ("pause_turn", "refusal"):
        ic = _an_message_delta(stop)
        assert ic.finish is None and ic.raw_finish == stop, stop
        md = _message_delta(anthropic.ir_to_events(ic, anthropic.StreamState()))
        assert md["delta"]["stop_reason"] == stop, (stop, md)


def test_cross_protocol_finish_to_stop_still_uses_shared_table():
    """跨协议（OpenAI chunk → Anthropic 客户端）行为不变：raw_finish 恒 None 时
    仍由 FINISH_TO_STOP 决定，四种 finish_reason 映射与既有测试一致。"""
    for finish, want in (("stop", "end_turn"), ("length", "max_tokens"),
                         ("tool_calls", "tool_use"), ("function_call", "tool_use"),
                         ("content_filter", None)):        # 枚举外 → None（不猜）
        ic = openai.chunk_to_ir({"choices": [{"delta": {}, "finish_reason": finish}],
                                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        assert ic.raw_finish is None
        md = _message_delta(anthropic.ir_to_events(ic, anthropic.StreamState()))
        assert md["delta"]["stop_reason"] == want, (finish, md)
