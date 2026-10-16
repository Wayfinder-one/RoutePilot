"""M3：评测闭环 + 预估复盘（sr/eval.py）单测。"""

import json
from datetime import datetime, timedelta

import pytest

import asyncio
import sr.db as db
from sr import eval as ev
from sr.tagger import HeuristicTagger

# Python 3.14 不再隐式建环；同 test_server_v1 口径的 import 时垫片
asyncio.set_event_loop(asyncio.new_event_loop())


def _conn(tmp_path):
    return db.init_db(str(tmp_path / "t.db"))


def _seed_request(conn, rid, hit_branch, routed_to, tags, ts=None,
                  session_tag="new", cost=None):
    conn.execute(
        "INSERT INTO requests (id, ts, proto, model_asked, hit_branch, routed_to, "
        "session_id, session_tag, outcome, status, answer_ms, upstream_ms, cost, "
        "tree_version) VALUES (?, ?, 'openai', 'auto', ?, ?, NULL, ?, 'upstream', "
        "200, 1.0, 5.0, ?, 1)",
        (rid, ts or db._now(), hit_branch, routed_to, session_tag, cost))
    for tag, value, conf, is_rule in tags:
        conn.execute("INSERT INTO tags (request_id, tag, value, confidence, "
                     "is_rule) VALUES (?, ?, ?, ?, ?)",
                     (rid, tag, value, conf, 1 if is_rule else 0))


CS_TAGS = [("domain", "computer_science", 0.75, 0), ("complexity", "1", 0.75, 0)]


def test_replay_eval_matches_and_mismatches(tmp_path):
    """回放评测：重建标签跑当前树，与当时记录的去向比对（§11.4）。"""
    conn = _conn(tmp_path)
    _seed_request(conn, "r1", "3.0", "qwen3-32b", CS_TAGS)     # 一致
    _seed_request(conn, "r2", "3.0", "qwen3-8b", CS_TAGS)      # 记录去向不符
    _seed_request(conn, "r3", "9.0", "qwen3-8b", CS_TAGS,
                  session_tag="sticky")                        # 非新会话：不参与
    conn.commit()
    rep = ev.replay_eval(conn, str(tmp_path / "t.db"), days=7)
    assert rep.total == 2 and rep.matched == 1
    assert rep.rate == pytest.approx(0.5)
    assert len(rep.mismatches) == 1 and rep.mismatches[0]["request_id"] == "r2"
    # 混淆矩阵按当时候选分支（hit_branch）归组
    assert rep.branches["3.0"] == {"total": 2, "matched": 1}


def test_labels_eval_uses_tagger(tmp_path):
    """标注集评测：经注入打标器打标 → 当前树求值 → 与期望比对。"""
    conn = _conn(tmp_path)
    tagger = HeuristicTagger()
    txt = str(tmp_path / "t.db")
    run = asyncio.get_event_loop().run_until_complete
    # 期望一个不可能的去向 → 不一致
    rep = run(ev.labels_eval(conn, txt,
                             [{"text": "帮我写个函数", "expect_model": "__none__"}],
                             tagger))
    assert rep.total == 1 and rep.matched == 0 and rep.rate == 0.0
    assert rep.mismatches and rep.mismatches[0]["now_model"] is not None
    # 无期望（只求值）→ 计为一致（无可比对）
    rep2 = run(ev.labels_eval(conn, txt, [{"text": "hi"}], tagger))
    assert rep2.total == 1 and rep2.matched == 1


def test_log_and_recent_evals(tmp_path):
    conn = _conn(tmp_path)
    rep = ev.EvalReport(source="replay", days=7, total=3, matched=2,
                        ts=db._now())
    ev.log_eval(conn, rep, actor="cli")
    conn.commit()
    recent = ev.recent_evals(conn, 3)
    assert len(recent) == 1 and recent[0]["matched"] == 2 and recent[0]["rate"] == \
        pytest.approx(2 / 3)


def test_backfill_estimates_and_error(tmp_path):
    """预估复盘：满 7 天后按真实流量回填 actual.daily + 偏差 → gauge 值（§11.4）。"""
    conn = _conn(tmp_path)
    now = datetime.now().astimezone()
    est_ts = (now - timedelta(days=10)).isoformat(timespec="seconds")
    traffic_ts = (now - timedelta(days=8)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO estimates (id, ts, node, params, est, actual) "
        "VALUES ('e1', ?, '3', '{}', ?, NULL)",
        (est_ts, db._dump({"daily": 100, "cost_delta": 0})))
    for i in range(7):                                       # 窗口内 7 条进节点 3
        _seed_request(conn, f"r{i}", "3.0", "qwen3-32b", [], ts=traffic_ts, cost=0.01)
    conn.commit()
    assert ev.backfill_estimates(conn, days=7) == 1
    actual = json.loads(conn.execute(
        "SELECT actual FROM estimates WHERE id='e1'").fetchone()[0])
    assert actual["daily"] == pytest.approx(1.0)             # 7 条 / 7 天
    assert actual["cost_delta"] == pytest.approx(0.01)
    assert ev.estimate_error(conn) == pytest.approx(abs(1.0 - 100) / 100)
    devs = ev.estimate_deviations(conn, threshold=0.5)
    assert devs and devs[0]["node"] == "3"
    # 幂等：再跑不重复回填
    assert ev.backfill_estimates(conn, days=7) == 0
