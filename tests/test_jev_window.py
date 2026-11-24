"""JEV 输入窗口：token 估算/截断、发布骨架指纹复核、路由引擎截断。

窗口现在是 JEV 服务目录行的一部分（``input_tokens``/``context_tokens``），旧独立
窗口端点与旧图/无活动图的数据库回退读取都已删除；目录语义（多服务各自窗口、
校验边界）由 ``tests/test_jev_admin.py`` 与 ``tests/test_jev_services.py`` 覆盖。
本文件保留纯窗口逻辑与发布骨架（``_write_and_publish`` 指纹复核）的回归。
"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from router.control.config import ConfigurationService, _ConfigWriteResult
from router.jev.client import ChoiceAnswer
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings
from router.jev.window import (
    DEFAULT_CONTEXT_TOKENS,
    MIN_INPUT_TOKENS,
    apply_window,
    estimate_tokens,
)
from router.routing.graph.compiled import compile_graph
from router.routing.graph.evaluator import GraphEvaluator
from router.routing.snapshot import build_snapshot
from router.routing.tree import tree_from_dict
from router.routing.types import RoutingRequest
from router.store.db import connect, migrate
from router.store.routing_trees import GraphChangedDuringLoad
from router.upstream.catalog import Backend
from tests.routing_fixtures import tree_dict


# ── window 单元测试 ────────────────────────────────────────────────────


def test_estimate_tokens_counts_cjk_as_one_and_ascii_as_quarter():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("你好abcd") == 3          # 2 + ceil(4/4)
    assert estimate_tokens("你好abcd") < estimate_tokens("你好你好")


def test_apply_window_keeps_short_text_untouched():
    text = "帮我写一段关于秋天的短文案"
    result = apply_window(text, 1024)
    assert result.truncated is False
    assert result.text == text
    assert result.tokens_before == result.tokens_after == estimate_tokens(text)


@pytest.mark.parametrize("budget", [MIN_INPUT_TOKENS, 512, 1024])
def test_apply_window_truncates_to_budget_and_keeps_head_and_tail(budget):
    head = "开头背景：国网营销数字化改造项目。"
    tail = "结尾诉求：请给出分阶段纠偏措施。"
    body = "计量采集、电价电费核算、供电服务工单、新能源并网四个子系统。" * 300
    result = apply_window(head + body + tail, budget)
    assert result.truncated is True
    assert result.tokens_before > budget
    assert result.tokens_after <= budget
    assert result.text.startswith(head[:6])
    assert result.text.endswith(tail[-6:])
    assert "已截断" in result.text


def test_apply_window_floors_below_minimum_budget():
    text = "字" * 5000
    result = apply_window(text, 10)
    assert result.truncated is True
    assert result.tokens_after <= MIN_INPUT_TOKENS


# ── 发布前指纹复核（共享骨架，目录写不再涉及图批次）────────────────────

class Publisher:
    """SnapshotPublisher 桩：验证 locked/prepare/publish 次序与指纹复核契约。"""

    def __init__(self):
        self.published = []

    @contextmanager
    def locked(self):
        yield self

    def prepare(self, conn, trees, fingerprint):
        return SimpleNamespace(snapshot_set=trees, fingerprint=fingerprint)

    def publish(self, prepared):
        self.published.append(prepared)


def _default_tree(tree_id="tree-1"):
    return SimpleNamespace(id=tree_id, default=True, departments=())


class _Trees:
    """最小树仓库桩：按队列吐路由集指纹，队列耗尽后停在最后一个。"""

    def __init__(self, fingerprints):
        self._fingerprints = list(fingerprints)

    def route_fingerprint(self):
        if len(self._fingerprints) > 1:
            return self._fingerprints.pop(0)
        return self._fingerprints[0]

    def enabled(self):
        return [_default_tree()]


def _fresh_conn(tmp_path):
    conn = connect(str(tmp_path / "v3.db"))
    migrate(conn)
    return conn


def test_publish_rechecks_fingerprint_and_rebuilds(tmp_path):
    """提交后图被改写：用新指纹重建快照，不发布过期指纹。"""
    conn = _fresh_conn(tmp_path)
    try:
        publisher = Publisher()
        service = ConfigurationService(
            conn, _Trees(["sha-a", "sha-b"]), publisher)

        def write(_conn):
            prepared = service._prepared_snapshot_set()  # 指纹 sha-a
            return _ConfigWriteResult(prepared, None)

        service._write_and_publish(write)
        assert len(publisher.published) == 1
        assert publisher.published[0].fingerprint == "sha-b"
    finally:
        conn.close()


def test_publish_refuses_when_fingerprint_keeps_changing(tmp_path):
    """复核期间指纹持续变化：抛与 GraphService 相同的冲突且不发布。"""
    conn = _fresh_conn(tmp_path)
    try:
        publisher = Publisher()
        service = ConfigurationService(
            conn,
            _Trees(["sha-a", "sha-b", "sha-c"]),
            publisher,
        )
        with pytest.raises(GraphChangedDuringLoad):
            service._write_and_publish(
                lambda _conn: _ConfigWriteResult(service._prepared_snapshot_set(), None))
        assert publisher.published == []
    finally:
        conn.close()


class _SwappingTrees:
    """最小树仓库桩：底层文档在两次读取之间被改写，模拟磁盘竞态。"""

    def __init__(self, before, after, *, swap_after=1):
        self._document = before              # (route_fingerprint, enabled trees)
        self._after = after
        self._reads = 0
        self._swap_after = swap_after
        self.calls = []

    def _read(self, name):
        fingerprint, trees = self._document
        self.calls.append(name)
        self._reads += 1
        if self._reads >= self._swap_after:
            self._document = self._after
        return fingerprint, trees

    def route_fingerprint(self):
        return self._read("route_fingerprint")[0]

    def enabled(self):
        return self._read("enabled")[1]


def test_prepared_snapshot_reads_fingerprint_before_tree(tmp_path):
    """指纹必须先于树读取：两次读取之间图变了，绝不能把过期树配当前指纹发布。"""
    doc_a = ("sha-a", [_default_tree("doc-a")])
    doc_b = ("sha-b", [_default_tree("doc-b")])
    conn = _fresh_conn(tmp_path)
    try:
        publisher = Publisher()
        trees = _SwappingTrees(doc_a, doc_b)
        service = ConfigurationService(conn, trees, publisher)

        service._write_and_publish(
            lambda _conn: _ConfigWriteResult(service._prepared_snapshot_set(), None))

        assert len(publisher.published) == 1
        published = publisher.published[0]
        # 重考发现「树过期、指纹是当前」，用当前文档重建一次；绝不发布
        # (doc-a 的树, doc-b 的指纹) 这种自相矛盾的快照。
        assert published.snapshot_set is doc_b[1]
        assert published.snapshot_set[0] is doc_b[1][0]
        assert published.fingerprint == doc_b[0]
        assert trees.calls[:2] == ["route_fingerprint", "enabled"]
    finally:
        conn.close()


def test_recheck_rebuilds_complete_enabled_set(tmp_path):
    """指纹不符时的重考必须用完整启用集合重建，绝不回退到默认图/单图。"""
    doc_a = ("sha-a", [_default_tree("tree-a1"), _default_tree("tree-a2")])
    doc_b = ("sha-b", [_default_tree("tree-b1"), _default_tree("tree-b2")])
    conn = _fresh_conn(tmp_path)
    try:
        publisher = Publisher()
        # 前两次读取（初建候选快照）看到 doc-a，之后磁盘变成 doc-b。
        trees = _SwappingTrees(doc_a, doc_b, swap_after=2)
        service = ConfigurationService(conn, trees, publisher)

        service._write_and_publish(
            lambda _conn: _ConfigWriteResult(
                service._prepared_snapshot_set(), None))

        assert len(publisher.published) == 1
        published = publisher.published[0]
        # 重考重建的是当前完整启用集合（两张图都在），不是单图回退。
        assert published.snapshot_set is doc_b[1]
        assert [tree.id for tree in published.snapshot_set] == [
            "tree-b1", "tree-b2"]
        assert published.fingerprint == "sha-b"
        assert trees.calls.count("enabled") == 2
    finally:
        conn.close()


# ── 路由引擎：截断生效 + trace ─────────────────────────────────────────

class GraphJev:
    """Graph v3 决策服务替身：一次 choose_many 返回本波全部问题答案。"""

    def __init__(self):
        self.calls = []

    async def choose_many(self, settings, text, questions):
        self.calls.append({"text": text, "questions": list(questions)})
        return {
            node_id: ChoiceAnswer(
                {"easy": 0.3, "strong": 0.7}, ["strong", "easy"], 0.7, 0.4,
                {"input_tokens": 5}, 8.0,
            )
            for node_id in questions
        }


def _snapshot(*, input_tokens=MIN_INPUT_TOKENS, context_tokens=DEFAULT_CONTEXT_TOKENS):
    jev_settings = JevSettings("http://jev.test", "d1-2B", "", 5, DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE)
    tree = tree_from_dict(tree_dict(enabled=True))
    return build_snapshot(
        tree=tree,
        backends={"fast": Backend(url="http://fast.test", headers={}, upstream_name="fast")},
        time_periods=[],
        jev=jev_settings,
        capabilities={"fast": "简单请求"},
        jev_input_tokens=input_tokens,
        jev_context_tokens=context_tokens,
        compiled=compile_graph(tree.root),
    )


def test_evaluator_truncates_jev_input_and_traces_it():
    snap = _snapshot(input_tokens=MIN_INPUT_TOKENS)
    jev = GraphJev()
    trace = []
    asyncio.run(GraphEvaluator(jev).evaluate(
        snap, RoutingRequest("国网营销数字化改造项目背景。" * 500, session_id="s1"),
        trace_sink=trace,
    ))

    sent = jev.calls[0]["text"]
    assert estimate_tokens(sent) <= MIN_INPUT_TOKENS
    window_events = [item for item in trace if item["event"] == "window"]
    assert len(window_events) == 1
    assert window_events[0]["truncated"] is True
    assert window_events[0]["tokens_before"] > MIN_INPUT_TOKENS


def test_evaluator_leaves_short_input_alone():
    snap = _snapshot()
    jev = GraphJev()
    trace = []
    asyncio.run(GraphEvaluator(jev).evaluate(
        snap, RoutingRequest("你好", session_id="s2"), trace_sink=trace,
    ))

    assert jev.calls[0]["text"] == "你好"
    # 冻结契约：window 事件总会发出，未截断时 truncated=False 且 token 前后一致。
    window_events = [item for item in trace if item["event"] == "window"]
    assert len(window_events) == 1
    assert window_events[0]["truncated"] is False
    assert window_events[0]["tokens_before"] == window_events[0]["tokens_after"]
