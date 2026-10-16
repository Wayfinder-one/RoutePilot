"""audit 快修批：B3（Anthropic 流式 input_tokens 进计价）+ 预算窗口时区归一。

对应审核意见：
- B3：Anthropic 上游流式的 input_tokens 只出现在 `message_start`，而流式 usage sink
  旧实现只取 `ic.usage`（message_delta 只有 output_tokens）→ 输入侧被丢成 0，
  计价系统性低估；修法：sink 并入 `ic.input_tokens`。
- 预算窗口：`requests.ts` 是本地带偏移 ISO 串，旧查询用
  `ts >= date('now','start of day')`（UTC 日期串）比较，在非 UTC 机器上本地
  00:00–08:00 区间会把昨天的行算进「今天」；修法：两侧过 `datetime()` + 本地起点。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import sr.db as db
from sr.routes._deps import _cost_of, _daily_spend


# ── B3：Anthropic 流式 usage sink ────────────────────────────────────────────

def _relay_anthropic(frames: list[str], sink: dict) -> str:
    from sr.server import _relay_stream

    class _Dummy:
        async def aclose(self):
            return None

    async def gen():
        for f in frames:
            yield f.encode()

    async def run():
        return [b async for b in _relay_stream(
            "anthropic", "anthropic", "m", _Dummy(), _Dummy(), gen(), b"",
            usage_sink=sink)]

    # 3.14 无隐式事件环；用**局部**环，避免影响同线程其它测试（asyncio.run 会清环）
    loop = asyncio.new_event_loop()
    try:
        return b"".join(loop.run_until_complete(run())).decode()
    finally:
        loop.close()


def test_anthropic_stream_sink_keeps_input_tokens():
    """B3：message_start 的 input_tokens 必须并入 sink（否则流式计价丢输入侧）。"""
    NL2 = chr(10) * 2
    frames = [
        'event: message_start' + NL2 +
        'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}' + NL2,
        'event: content_block_delta' + NL2 +
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}' + NL2,
        'event: message_delta' + NL2 +
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":7}}' + NL2,
    ]
    sink: dict = {}
    out = _relay_anthropic(frames, sink)
    assert "message_start" in out                       # 客户端拿到 Anthropic 事件
    usage = sink.get("usage") or {}
    assert usage.get("output_tokens") == 7, usage
    assert usage.get("input_tokens") == 11, usage       # B3 的修复点


def test_streaming_cost_uses_both_sides(tmp_path):
    """B3 的后果：sink 补齐后 `_cost_of` 才算得出含输入侧的成本。"""
    conn = db.init_db(str(tmp_path / "t.db"))
    conn.execute("UPDATE models SET price_in = 1.0, price_out = 2.0 "
                 "WHERE id = 'qwen3-8b'")
    conn.commit()
    usage = {"input_tokens": 11, "output_tokens": 7}     # 修后 sink 的形状
    assert _cost_of(conn, "qwen3-8b", usage) == (11 * 1.0 + 7 * 2.0) / 1_000_000
    # 旧形状（只有 output_tokens）会低估：算出的成本更小
    assert _cost_of(conn, "qwen3-8b", {"output_tokens": 7}) == (7 * 2.0) / 1_000_000


# ── 预算窗口：时区归一 ──────────────────────────────────────────────────────

def _add_request(conn, rid: str, ts: str, cost: float) -> None:
    conn.execute(
        "INSERT INTO requests (id, ts, proto, outcome, cost) VALUES (?,?,?,?,?)",
        (rid, ts, "openai", "upstream", cost))
    conn.commit()


def test_daily_spend_window_is_timezone_normalized(tmp_path):
    """本地凌晨 02:00（UTC 仍是前一天）时，昨天的行**不得**算进今天。"""
    conn = db.init_db(str(tmp_path / "t.db"))
    _add_request(conn, "today", "2026-10-17T01:00:00+08:00", 10.0)
    _add_request(conn, "yesterday", "2026-10-16T23:00:00+08:00", 10.0)
    _add_request(conn, "older", "2026-10-15T12:00:00+08:00", 10.0)
    tz8 = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 17, 2, 0, tzinfo=tz8)      # 本地凌晨（UTC 16 日 18:00）
    assert _daily_spend(conn, now=now) == 10.0          # 旧实现会算成 20（或 30）
    # 恰好跨到本地 00:00 时，仍只算「今天」
    assert _daily_spend(conn, now=datetime(2026, 10, 17, 0, 0, tzinfo=tz8)) == 10.0
    # 本地 10-17 前一秒仍是 10-16 → 只算 10-16 的两行
    assert _daily_spend(conn, now=datetime(2026, 10, 16, 23, 59, 59,
                                           tzinfo=tz8)) == 20.0
