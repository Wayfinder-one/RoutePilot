"""Task 6 测试：会话沿用 + 反馈解锁 + 阶梯 + 熔断（简报 Step 1 测试逐字采用）。

与简报的出入（记任务报告）：
- test_ladder 简报原文 `def test_ladder():` 无 tmp_path 形参却在函数体里调
  `_conn(tmp_path)`（照抄必 NameError），补上形参——最小修正，断言逐字未动；
- 文件头多一个 autouse fixture：清空 sr.models._FAIL_COUNTS——熔断计数是模块级
  内存状态（spec §8.3），不隔离会在套件扩到多条熔断用例时前后污染；
- 简报 6 个测试的函数体与断言逐字未动；另有 3 个补充测试（简报 Produces 接口里
  resolve_backend / record_success 无测试覆盖，TDD 补齐）：test_resolve_backend、
  test_resolve_backend_missing_env、test_circuit_recover。
"""

import pytest

import sr.db as db
from sr.engine import resolve_session, extract_session
from sr.models import (ladder_up, ladder_next, record_failure, record_success,
                       is_circuit_open, resolve_backend)


@pytest.fixture(autouse=True)
def _reset_fail_counts():
    """熔断计数是 sr.models 模块级内存 dict，用例间清零防串扰（spec §8.3）。"""
    import sr.models as models

    models.clear_circuit_state()
    yield
    models.clear_circuit_state()


def _conn(tmp_path):
    return db.init_db(str(tmp_path / "t.db"))

def test_extract_session_priority():
    assert extract_session("openai", {"user": "u1"}, {"x-session-id": "s9"}, "1.1.1.1") == "s9"
    assert extract_session("openai", {"user": "u1"}, {}, "1.1.1.1") == "u1"
    assert extract_session("anthropic", {"metadata": {"user_id": "u2"}}, {}, "1.1.1.1") == "u2"
    fp = extract_session("openai", {"messages": [{"role": "user", "content": "hi"}]},
                         {"authorization": "Bearer k"}, "1.1.1.1")
    assert fp and len(fp) == 40                                # sha1 指纹

def test_sticky_reuse(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions(id, current_model, last_active, turns, unlocks)"
                 " VALUES('s1','qwen3-32b', datetime('now'), 1, 0)")
    d = resolve_session(conn, "s1", None, {"session_sticky": "1", "session_idle": "1800"})
    assert d and d.model == "qwen3-32b" and d.source == "sticky"

def test_idle_expiry_is_new_session(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions(id, current_model, last_active, turns, unlocks)"
                 " VALUES('s1','qwen3-32b', datetime('now','-2 hours'), 1, 0)")
    assert resolve_session(conn, "s1", None, {"session_sticky": "1", "session_idle": "1800"}) is None

def test_unlock_ladder_up(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions(id, current_model, last_active, turns, unlocks)"
                 " VALUES('s1','qwen3-8b', datetime('now'), 2, 0)")
    d = resolve_session(conn, "s1", None,
                        {"session_sticky": "1", "session_idle": "1800"},
                        feedback="wrong_answer")
    assert d and d.model == "qwen3-32b" and d.source == "unlocked"
    assert conn.execute("SELECT unlocks FROM sessions WHERE id='s1'").fetchone()[0] == 1

def test_ladder(tmp_path):                                     # 简报缺 tmp_path 形参，见文件头
    conn = _conn(tmp_path)
    assert ladder_up(conn, "qwen3-8b") == "qwen3-32b"
    assert ladder_up(conn, "claude-sonnet") is None
    assert ladder_next(conn, "claude-sonnet") == "qwen3-8b"    # 到顶回第一格（§8.2）

def test_circuit_breaker(tmp_path):
    conn = _conn(tmp_path)
    m = "qwen3-8b"
    assert not is_circuit_open(m)
    record_failure(conn, m); record_failure(conn, m); record_failure(conn, m)
    assert is_circuit_open(m)
    assert conn.execute("SELECT ok, break_reason FROM models WHERE id=?", (m,)).fetchone()["ok"] == 0


# ── 补充测试（简报 Produces 接口无覆盖项，TDD 补齐；记任务报告） ──────────────

def test_resolve_backend(tmp_path, monkeypatch):
    """§8.1：openai/anthropic 两形态的 URL、认证头、proto、上游名。"""
    conn = _conn(tmp_path)
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "sk-local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    b = resolve_backend(conn, "qwen3-8b")
    assert b.url == "http://127.0.0.1:8123/chat/completions"
    assert b.headers == {"Authorization": "Bearer sk-local"}
    assert b.proto == "openai" and b.upstream_name == "qwen3-8b"
    b2 = resolve_backend(conn, "claude-sonnet")
    assert b2.url == "http://127.0.0.1:8123/v1/messages"
    assert b2.headers == {"x-api-key": "sk-anthropic",
                          "anthropic-version": "2023-06-01"}
    assert b2.proto == "anthropic" and b2.upstream_name == "claude-sonnet"

def test_resolve_backend_missing_env(tmp_path, monkeypatch):
    """§8.1：key_env 缺失抛 RuntimeError(f"env {key_env} not set")（裁定逐字）。"""
    conn = _conn(tmp_path)
    monkeypatch.delenv("LOCAL_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match=r"^env LOCAL_OPENAI_API_KEY not set$"):
        resolve_backend(conn, "qwen3-8b")

def test_circuit_recover(tmp_path):
    """§8.3：record_success 清零内存计数并恢复 models.ok=1、break_reason 清空。"""
    conn = _conn(tmp_path)
    m = "qwen3-8b"
    for _ in range(3):
        record_failure(conn, m)
    assert is_circuit_open(m)
    record_success(conn, m)
    assert not is_circuit_open(m)
    row = conn.execute("SELECT ok, break_reason FROM models WHERE id=?", (m,)).fetchone()
    assert row["ok"] == 1 and row["break_reason"] is None


def test_circuit_half_open_after_window(tmp_path, monkeypatch):
    """M3：熔断打开后经过 HALF_OPEN_AFTER_S 放行一次探测（is_circuit_open 回 False）；
    未过窗仍打开；成功清零恢复。"""
    import sr.models as models
    clock = {"t": 1000.0}
    monkeypatch.setattr(models.time, "monotonic", lambda: clock["t"])
    conn = _conn(tmp_path)
    m = "qwen3-8b"
    for _ in range(3):
        record_failure(conn, m)
    assert is_circuit_open(m)                       # 刚打开
    clock["t"] += 1.0
    assert is_circuit_open(m)                       # 未过窗仍打开
    clock["t"] += models.HALF_OPEN_AFTER_S          # 过半开窗口
    assert not is_circuit_open(m)                   # 半开：放行一次探测
    record_success(conn, m)
    assert not is_circuit_open(m)                   # 探测成功 → 清零恢复
