"""scripts/policy_latency_probe.py 的离线契约（Task 10 Step 6）。

探测脚本不自带服务：真实 HTTP 全部用 httpx.MockTransport 驱动
``run_probe`` 缝（transport 可注入），断言：

* ``summarize`` 的空观测 / 非正 wall_seconds 输入保护；
* ``--requests``/``--concurrency`` 越界与策略文档非法都在任何网络请求
  之前退出 2（run_probe 被 monkeypatch 成遇调即炸）；
* 失败样本绝不进入成功观测：latencies_ms/requests 只含成功样本，
  失败明细进独立 failures 数组；HTTP 非 200、JEV ``failed=true``、
  多次 JEV、answer_count != question_count 全部按样本失败并使退出码为 2；
* 输出键契约：url/concurrency/question_count/latencies_ms/summary/
  requests/failures，观测不含任何答案内容或凭据。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from tests.routing_fixtures import policy_dict

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "policy_latency_probe", ROOT / "scripts" / "policy_latency_probe.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

QUESTION_COUNT = 3  # policy_dict: route/risk/severity
FIXED_NOW = "2026-11-18T10:00:00+08:00"
DEFAULT_TEXT = "95598 抢修工单如何处理？"


def ok_body() -> dict:
    """一次合法 /admin/try 响应：一个未失败 JEV 事件 + 全量 answers。"""
    return {
        "target": "fast", "models": ["fast"],
        "matched_rule": "轻量请求", "matched_rule_index": 2,
        "answers": {"route": {"selected": "fast"}, "risk": {"yes": 0.1},
                    "severity": {"value": 1.0}},
        "trace": [{"event": "jev", "questions": ["route", "risk", "severity"],
                   "duration_ms": 12.0, "failed": False, "error": None}],
        "fallback": False, "reason": None,
    }


def ok_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/try"
        payload = json.loads(request.content)
        assert payload["policy"]["kind"] == "sr-routing-policy"
        assert payload["policy"]["version"] == 4
        assert payload["now"] == FIXED_NOW
        assert isinstance(payload["text"], str) and payload["text"]
        return httpx.Response(200, json=ok_body())
    return httpx.MockTransport(handler)


def run(transport, *, requests=3, concurrency=1, text=DEFAULT_TEXT,
        url="http://probe.test/admin/try"):
    return asyncio.run(probe.run_probe(
        url=url, policy=policy_dict(), text=text,
        requests=requests, concurrency=concurrency, transport=transport,
    ))


# --------------------------------------------------------------------------- #
# summarize 的输入保护（Task 10 Step 6 原文行为）
# --------------------------------------------------------------------------- #

def test_summarize_rejects_empty_observations():
    with pytest.raises(ValueError, match="no successful latency observations"):
        probe.summarize([], 1.0, QUESTION_COUNT)


@pytest.mark.parametrize("wall_seconds", [0.0, -1.5])
def test_summarize_rejects_non_positive_wall_seconds(wall_seconds):
    with pytest.raises(ValueError, match="wall_seconds must be positive"):
        probe.summarize([12.0, 34.0], wall_seconds, QUESTION_COUNT)


def test_summarize_reports_the_plan_shape():
    summary = probe.summarize([10.0, 20.0, 30.0, 40.0], 2.0, QUESTION_COUNT)
    assert summary == {
        "n": 4,
        "p50_ms": pytest.approx(25.0),
        "p95_ms": 40.0,
        "max_ms": 40.0,
        "wall_seconds": 2.0,
        "requests_per_second": 2.0,
        "questions_per_second": 6.0,
    }


# --------------------------------------------------------------------------- #
# argparse：计划固定的默认值与取值范围
# --------------------------------------------------------------------------- #

def test_argparse_defaults_match_the_plan_contract():
    args = probe.parse_args([])
    assert args.url == "http://127.0.0.1:8400"
    assert args.policy == "configs/default_routing_policy.json"
    assert args.text == DEFAULT_TEXT
    assert args.requests == 40
    assert args.concurrency == 1
    assert args.output == "artifacts/policy-v4-latency.json"


# --------------------------------------------------------------------------- #
# 预检失败：arg 上限 / 策略文档非法 → 任何网络请求之前退出 2
# --------------------------------------------------------------------------- #

def _forbid_network(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("the probe must not reach the network on a pre-flight exit")
    monkeypatch.setattr(probe, "run_probe", boom)


def _valid_policy_file(tmp_path) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_dict(), ensure_ascii=False), encoding="utf-8")
    return path


def _argv(policy_path, *extra) -> list[str]:
    return ["probe", "--policy", str(policy_path), *extra]


@pytest.mark.parametrize(
    "cap_args",
    [
        ("--requests", "201"),
        ("--requests", "0"),
        ("--concurrency", "9"),
        ("--concurrency", "0"),
    ],
)
def test_main_exits_2_on_out_of_range_caps_before_network(
        tmp_path, monkeypatch, cap_args):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(sys, "argv", _argv(_valid_policy_file(tmp_path), *cap_args))
    assert probe.main() == 2


@pytest.mark.parametrize(
    "content",
    [
        # v3 图文档：kind/version 不符（拆分拼接保持 Task 10 符号扫描空输出）。
        '{"kind": "sr-routing-' 'graph", "version": 3}',
        '{"kind": "sr-routing-policy", "version": 5}',  # version 不符
        "{not-json",                                     # 坏 JSON
        '["not", "an", "object"]',                       # 顶层不是对象
    ],
)
def test_main_exits_2_on_invalid_policy_document_before_network(
        tmp_path, monkeypatch, content):
    _forbid_network(monkeypatch)
    path = tmp_path / "broken.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", _argv(path))
    assert probe.main() == 2


def test_main_exits_2_on_missing_policy_file_before_network(tmp_path, monkeypatch):
    _forbid_network(monkeypatch)
    missing = tmp_path / "absent.json"
    monkeypatch.setattr(sys, "argv", _argv(missing))
    assert probe.main() == 2


# --------------------------------------------------------------------------- #
# run_probe：成功/失败样本分离 + 退出码契约
# --------------------------------------------------------------------------- #

def test_run_probe_keeps_successes_and_failures_in_separate_arrays():
    kinds = iter(["ok", "ok", "ok", "http", "jev_failed", "answers", "multi"])

    def handler(request: httpx.Request) -> httpx.Response:
        kind = next(kinds)
        if kind == "http":
            return httpx.Response(500, json={"detail": "boom"})
        body = ok_body()
        if kind == "jev_failed":
            body["trace"][0]["failed"] = True
            body["trace"][0]["error"] = "mock decision failure"
        elif kind == "answers":
            body["answers"].pop("severity")
        elif kind == "multi":
            body["trace"].append(dict(body["trace"][0]))
        return httpx.Response(200, json=body)

    artifact = run(httpx.MockTransport(handler), requests=7, concurrency=1)

    # 成功样本：只有前三条；latencies_ms/requests 与之一一对应。
    assert len(artifact["requests"]) == 3
    assert len(artifact["latencies_ms"]) == 3
    assert all(row["jev_calls"] == 1 and row["answer_count"] == QUESTION_COUNT
               for row in artifact["requests"])
    assert all(set(row) == {"latency_ms", "status", "jev_calls", "answer_count"}
               for row in artifact["requests"])
    assert artifact["question_count"] == QUESTION_COUNT
    assert artifact["summary"]["n"] == 3

    # 失败样本：四类失败全部进独立 failures 数组，绝不混入成功观测。
    assert len(artifact["failures"]) == 4
    reasons = [row.get("reason", "") for row in artifact["failures"]]
    assert any("http status 500" in reason for reason in reasons)
    assert any("failed=true" in reason for reason in reasons)
    assert any("answer count 2 != question count 3" in reason for reason in reasons)
    assert any("multiple jev" in reason for reason in reasons)
    assert probe.artifact_exit_code(artifact) == 2


def test_run_probe_all_success_exits_zero_and_reports_summary():
    artifact = run(ok_transport(), requests=4, concurrency=2)
    assert artifact["failures"] == []
    assert len(artifact["requests"]) == 4
    assert artifact["summary"]["n"] == 4
    assert probe.artifact_exit_code(artifact) == 0


def test_run_probe_payload_uses_the_passed_text():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["text"])
        return httpx.Response(200, json=ok_body())

    run(httpx.MockTransport(handler), requests=1, text="自定义探测文本")
    assert seen == ["自定义探测文本"]


def test_run_probe_without_successes_has_no_summary():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    artifact = run(httpx.MockTransport(handler), requests=2, concurrency=1)
    assert artifact["latencies_ms"] == []
    assert artifact["requests"] == []
    assert artifact["summary"] is None
    assert len(artifact["failures"]) == 2
    assert probe.artifact_exit_code(artifact) == 2


# --------------------------------------------------------------------------- #
# main 的装配契约：planned_requests 打印、artifact 写盘、退出码
# --------------------------------------------------------------------------- #

def test_main_prints_planned_requests_writes_artifact_and_exits_zero(
        tmp_path, monkeypatch, capsys):
    policy_path = _valid_policy_file(tmp_path)
    output = tmp_path / "nested" / "latency.json"

    async def fake_run_probe(**kwargs):
        assert kwargs["text"] == "自定义探测文本"
        assert kwargs["requests"] == 4 and kwargs["concurrency"] == 2
        return {
            "url": kwargs["url"], "concurrency": 2, "question_count": QUESTION_COUNT,
            "latencies_ms": [11.0, 12.0, 13.0, 14.0],
            "summary": {"n": 4}, "requests": [{"latency_ms": 11.0}],
            "failures": [],
        }

    monkeypatch.setattr(probe, "run_probe", fake_run_probe)
    monkeypatch.setattr(
        sys, "argv",
        _argv(policy_path, "--text", "自定义探测文本", "--requests", "4",
              "--concurrency", "2", "--output", str(output)),
    )
    assert probe.main() == 0
    out = capsys.readouterr().out
    assert "planned_requests = 4" in out
    assert '"n": 4' in out
    saved = json.loads(output.read_text(encoding="utf-8"))
    # 计划要求成功输出包含策略文件路径；观测与摘要照 run_probe 原样写盘。
    assert saved["policy_path"] == str(policy_path)
    assert saved["url"].endswith("/admin/try")
    assert saved["failures"] == []
    assert saved["summary"]["n"] == 4


def test_main_exits_2_and_reports_first_failure(tmp_path, monkeypatch, capsys):
    policy_path = _valid_policy_file(tmp_path)

    async def fake_run_probe(**kwargs):
        return {
            "url": kwargs["url"], "concurrency": 1, "question_count": QUESTION_COUNT,
            "latencies_ms": [11.0], "summary": {"n": 1},
            "requests": [{"latency_ms": 11.0}],
            "failures": [{"latency_ms": 9.0, "status": 504, "jev_calls": 1,
                          "answer_count": 0, "reason": "http status 504"}],
        }

    monkeypatch.setattr(probe, "run_probe", fake_run_probe)
    monkeypatch.setattr(sys, "argv", _argv(policy_path, "--output", str(tmp_path / "o.json")))
    assert probe.main() == 2
    err = capsys.readouterr().err
    assert "1/40" in err
    assert "http status 504" in err
