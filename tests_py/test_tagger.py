"""A2 测试（审核报告 §3-A2）：打标器从「被 import 的函数」升级为「注入的
async 协议对象」。

- parity：同一输入下 HeuristicTagger.tag 的 signals 与直接调 tags.tag_all
  完全相等（last_vec 传入也被 heuristic 忽略，vec 恒 None）；
- injection：create_app 收 Tagger 实例——假打标器（name="fake"）接入
  /v1 主链：x-sr-tagger 响应头取 tagger.name，且请求确实经过注入对象
  （调用计数 + 捕获的 TagRequest），不是模块级函数；
- async 签名：inspect.iscoroutinefunction + 运行时 inspect.iscoroutine
  双断言（M2 torch 前向必须 await 化，签名现在就必须到位）；
- now 注入：假打标器捕获 TagRequest，断言 now 非 None（time_window 用）；
- vec 回传（M2 接线）：回传 vec 的假打标器 → 树模式成功后 sessions.last_vec
  落库（JSON bytes）；vec=None（heuristic 常态）→ 不写，列保持 NULL。

模式照抄 test_server_v1/test_e2e_loop：进程内假上游 + init_db + UPDATE
base_url；Python 3.14 无隐式事件循环，模块级显式建环。
"""

import asyncio
import inspect
import json
import threading
import time
from datetime import datetime

import httpx
import pytest
import uvicorn

import sr.db as db
import sr.tags as tags
from sr.fake_upstream import create_app as _fake_upstream_app
from sr.server import create_app
from sr.tagger import HeuristicTagger, TagRequest, TagResult

asyncio.set_event_loop(asyncio.new_event_loop())


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def _post(app, path, body, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(path, json=body, headers=headers or {})


class FakeTagger:
    """注入用假打标器：name="fake"，tag 返回固定 signals/vec，调用留痕。"""

    name = "fake"

    def __init__(self, signals=None, vec=None):
        self.calls: list[TagRequest] = []
        self._signals = signals if signals is not None else {}
        self._vec = vec

    async def tag(self, req: TagRequest) -> TagResult:
        self.calls.append(req)
        return TagResult(signals=self._signals, vec=self._vec)


# ── 进程内假上游 :8127（与 test_server_v1 的 8123/8124、test_e2e_loop 的
#    8125 错开；success 路径的会话写库依赖它）────────────────────────────────

@pytest.fixture(scope="module")
def _fake_upstream():
    server = uvicorn.Server(uvicorn.Config(
        _fake_upstream_app(), host="127.0.0.1", port=8127, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "fake-upstream :8127 未就绪"
    yield server
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _api_keys(monkeypatch):
    """进程内 app 走 resolve_backend 读 key_env（§8.1），统一补齐。"""
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "sk-test-local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")


@pytest.fixture(autouse=True)
def _reset_fail_counts():
    """熔断计数是 sr.models 模块级内存 dict（§8.3），用例前后清零防串扰
    （同 test_session / test_server_v1 / test_e2e_loop 口径）。"""
    import sr.models as models

    models._FAIL_COUNTS.clear()
    yield
    models._FAIL_COUNTS.clear()


@pytest.fixture
def make_app(tmp_path, _fake_upstream):
    """app 工厂：注入指定 tagger（缺省 FakeTagger），库指到 :8127 假上游。"""
    def _make(tagger=None):
        conn = db.init_db(str(tmp_path / "t.db"))
        conn.execute("UPDATE providers SET base_url='http://127.0.0.1:8127'")
        conn.commit()
        conn.close()
        return create_app(str(tmp_path / "t.db"),
                          tagger if tagger is not None else FakeTagger())
    return _make


# ── 1) parity：HeuristicTagger 与 tags.tag_all 同产物 ────────────────────────

def test_heuristic_tagger_parity():
    """同一输入下 await HeuristicTagger().tag(req) 的 signals 与直接调
    tags.tag_all(...) 完全相等；last_vec 传入被忽略；vec 恒 None。"""
    now = datetime(2026, 10, 16, 10, 30)
    headers = {"x-sr-tenant": "acme"}
    text = "帮我设计一个支持千万并发的订单系统，要考虑分库分表"
    result = None
    for last_msg, ctx in [(None, None), ("帮我设计订单系统", [10, 50, 200])]:
        req = TagRequest(text=text, headers=headers, n_messages=3, now=now,
                         last_msg=last_msg, context_thresholds=ctx,
                         last_vec=[0.1, 0.2])          # heuristic 忽略 last_vec
        result = _run(HeuristicTagger().tag(req))
        assert result.signals == tags.tag_all(
            text, headers, 3, now, last_msg=last_msg, context_thresholds=ctx)
    assert result.vec is None


# ── 2) async 签名（M2 前置：torch 前向必须 await 化）─────────────────────────

def test_tagger_async_signature():
    """tag 必须是 coroutine function：静态（inspect）+ 运行时（iscoroutine）
    双断言，防「同步函数包 async 壳」的签名漂移。"""
    assert inspect.iscoroutinefunction(HeuristicTagger.tag)
    assert inspect.iscoroutinefunction(FakeTagger.tag)
    req = TagRequest(text="hi", headers={}, n_messages=1,
                     now=datetime(2026, 10, 16, 9, 0), last_msg=None)
    coro = HeuristicTagger().tag(req)
    assert inspect.iscoroutine(coro)
    _run(coro)


# ── 3) injection：路由主链真的走注入对象 ────────────────────────────────────

def test_tagger_is_injected(make_app):
    """create_app 注入假 Tagger：/v1 响应头 x-sr-tagger == "fake"（取
    tagger.name），且假 tagger 被调用恰好一次、捕获到同一文本——证明主链
    走注入对象而不是模块级 tag_all。"""
    fake = FakeTagger()
    app = make_app(fake)
    r = _run(_post(app, "/v1/chat/completions", {
        "model": "auto",
        "messages": [{"role": "user", "content": "帮我设计订单系统"}]},
        headers={"x-session-id": "inj-s1"}))
    assert r.status_code == 200
    assert r.headers["x-sr-tagger"] == "fake"
    assert len(fake.calls) == 1
    assert fake.calls[0].text == "帮我设计订单系统"


def test_tag_request_now_is_injected(make_app):
    """now 由 server 注入（time_window 规则标签用，§4.5「只用传入的 now」）：
    假 tagger 捕获 TagRequest，断言 now 非 None 且是 datetime。"""
    fake = FakeTagger()
    app = make_app(fake)
    _run(_post(app, "/v1/chat/completions", {
        "model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-session-id": "now-s1"}))
    assert len(fake.calls) == 1
    assert isinstance(fake.calls[0].now, datetime)


# ── 4) vec 回传通道（M2 接线：CheckpointTagger 落地即生效的写库路径）────────

def test_tagger_vec_written_to_sessions(make_app):
    """打标器回传 vec（checkpoint 嵌入头）→ 树模式成功后 sessions.last_vec
    落库，JSON bytes 序列化；heuristic 恒 None，此用例用回传 vec 的假打标器
    验证写路径已接通（M2 接线）。"""
    vec = [0.25, 0.5, 0.75]
    app = make_app(FakeTagger(vec=vec))
    r = _run(_post(app, "/v1/chat/completions", {
        "model": "auto", "messages": [{"role": "user", "content": "画一张海报"}]},
        headers={"x-session-id": "vec-s1"}))
    assert r.status_code == 200
    conn = db.get(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT last_vec FROM sessions WHERE id = 'vec-s1'").fetchone()
    finally:
        conn.close()
    assert row is not None and row["last_vec"] is not None
    assert json.loads(bytes(row["last_vec"]).decode("utf-8")) == vec


def test_tagger_vec_none_not_written(make_app):
    """vec=None（heuristic 常态）→ sessions.last_vec 保持 NULL，零写。"""
    app = make_app(FakeTagger())
    r = _run(_post(app, "/v1/chat/completions", {
        "model": "auto", "messages": [{"role": "user", "content": "随便聊聊"}]},
        headers={"x-session-id": "vecn-s1"}))
    assert r.status_code == 200
    conn = db.get(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT last_vec FROM sessions WHERE id = 'vecn-s1'").fetchone()
    finally:
        conn.close()
    assert row is not None and row["last_vec"] is None
