#!/usr/bin/env python3
"""Routing Policy v4 延迟探测：测量而不预设方向（Task 10 Step 6）。

对目标服务的 ``POST /admin/try`` 发送完整 v4 策略文档（``--policy``，默认
``configs/default_routing_policy.json``，即设计 §4.1 文档）与固定文本
（``--text``，默认 ``95598 抢修工单如何处理？``），所有请求共用
``now: "2026-11-18T10:00:00+08:00"``。策略文档在任何请求之前先经
``policy_from_dict`` 完整解析校验：缺失、坏 JSON、非对象或 kind/version
不符都以清晰错误信息退出 2。

逐条观测 ``latency_ms/status/jev_calls/answer_count``；失败样本绝不进入
成功观测——HTTP 非 200、任一 JEV 事件 ``failed=true``、一次决策多次 JEV
或正常 JEV 后 ``answer_count != question_count`` 都是样本失败，明细写入
独立的 ``failures`` 数组，且使整体退出 2。汇总只使用成功观测
（p50/p95/max/吞吐），并有空观测 / 非正 wall_seconds 输入保护。

目标服务需可匿名访问 ``/admin/try``（例如 ROUTER_USER_AUTH=0 的本地
实例）。脚本不读取任何 API key，输出也不包含任何凭据或答案内容。

只在用户提供可测试的本地服务后执行；本脚本不自带服务。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 直接按路径执行时让 router 包可导入

from router.routing.policy.model import policy_from_dict  # noqa: E402
DEFAULT_POLICY_PATH = "configs/default_routing_policy.json"
DEFAULT_TEXT = "95598 抢修工单如何处理？"
FIXED_NOW = "2026-11-18T10:00:00+08:00"
MIN_REQUESTS = 1
MAX_REQUESTS = 200
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 8
REQUEST_TIMEOUT = httpx.Timeout(30.0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure /admin/try latency for the full v4 routing policy")
    parser.add_argument("--url", default="http://127.0.0.1:8400")
    parser.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", default="artifacts/policy-v4-latency.json")
    return parser.parse_args(argv)


def summarize(latencies_ms: list[float], wall_seconds: float, question_count: int) -> dict:
    if not latencies_ms:
        raise ValueError("no successful latency observations")
    if wall_seconds <= 0:
        raise ValueError("wall_seconds must be positive")
    ordered = sorted(latencies_ms)
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999999) - 1))
    return {
        "n": len(ordered),
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[p95_index],
        "max_ms": ordered[-1],
        "wall_seconds": wall_seconds,
        "requests_per_second": len(ordered) / wall_seconds,
        "questions_per_second": len(ordered) * question_count / wall_seconds,
    }


def load_policy_document(path: Path) -> dict:
    """读取并完整解析 v4 策略文档；任何非法形状都抛 OSError/ValueError。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    policy_from_dict(raw)  # kind/version/结构校验：坏文档在这里抛 ValueError
    return raw


def _observation(latency_ms: float, status: int, jev_calls: int, answer_count: int) -> dict:
    return {
        "latency_ms": latency_ms,
        "status": status,
        "jev_calls": jev_calls,
        "answer_count": answer_count,
    }


async def probe_one(
    client: httpx.AsyncClient, url: str, payload: dict, question_count: int,
) -> tuple[dict | None, dict | None]:
    """一条 /admin/try 观测；返回 (成功观测, 失败明细)，二者互斥。"""
    started = time.perf_counter()
    try:
        response = await client.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        failure = _observation((time.perf_counter() - started) * 1000.0, 0, 0, 0)
        failure["error"] = f"{type(exc).__name__}: {exc}"
        failure["reason"] = f"transport error: {failure['error']}"
        return None, failure
    latency_ms = (time.perf_counter() - started) * 1000.0

    body: object = {}
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    trace = body.get("trace")
    trace = trace if isinstance(trace, list) else []
    jev_events = [
        event for event in trace
        if isinstance(event, dict) and event.get("event") == "jev"
    ]
    jev_failed = any(event.get("failed") is True for event in jev_events)
    answers = body.get("answers")
    answer_count = len(answers) if isinstance(answers, dict) else 0

    observation = _observation(latency_ms, response.status_code,
                               len(jev_events), answer_count)
    reasons: list[str] = []
    if response.status_code != 200:
        observation["error"] = response.text[:300]
        reasons.append(f"http status {response.status_code}")
    if jev_failed:
        reasons.append("jev event reported failed=true")
    if len(jev_events) > 1:
        # v4 一次决策至多一次 JEV 是结构不变式，不是性能偏好。
        reasons.append("multiple jev calls in one response")
    if not jev_failed and answer_count != question_count:
        reasons.append(
            f"answer count {answer_count} != question count {question_count}")
    if reasons:
        observation["reason"] = "; ".join(reasons)
        return None, observation
    return observation, None


async def gather_samples(
    url: str, payload: dict, question_count: int, requests: int, concurrency: int,
    transport: httpx.AsyncTransport | None = None,
) -> tuple[list[dict], list[dict], float]:
    """发完全部请求；成功观测与失败明细分列收集。"""
    semaphore = asyncio.Semaphore(concurrency)
    successes: list[dict] = []
    failures: list[dict] = []

    async with httpx.AsyncClient(transport=transport) as client:
        async def worker():
            async with semaphore:
                observation, failure = await probe_one(
                    client, url, payload, question_count)
                if observation is not None:
                    successes.append(observation)
                else:
                    failures.append(failure)

        wall_started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(requests)))
        wall_seconds = time.perf_counter() - wall_started
    return successes, failures, wall_seconds


def build_artifact(
    *, url: str, concurrency: int, question_count: int,
    successes: list[dict], failures: list[dict], wall_seconds: float,
) -> dict:
    latencies_ms = [row["latency_ms"] for row in successes]
    return {
        "url": url,
        "concurrency": concurrency,
        "question_count": question_count,
        "latencies_ms": latencies_ms,
        "summary": (summarize(latencies_ms, wall_seconds, question_count)
                    if latencies_ms else None),
        "requests": successes,
        "failures": failures,
    }


def artifact_exit_code(artifact: dict) -> int:
    """任何失败样本都使整体探测失败（HTTP/JEV 契约，不是性能偏好）。"""
    return 2 if artifact.get("failures") else 0


async def run_probe(
    *, url: str, policy: dict, text: str, requests: int, concurrency: int,
    transport: httpx.AsyncTransport | None = None,
) -> dict:
    """一次完整探测，返回 artifact；``transport`` 缝供离线测试注入。"""
    payload = {"policy": policy, "text": text, "now": FIXED_NOW}
    question_count = len(policy.get("questions", {}))
    successes, failures, wall_seconds = await gather_samples(
        url, payload, question_count, requests, concurrency, transport)
    return build_artifact(
        url=url, concurrency=concurrency, question_count=question_count,
        successes=successes, failures=failures, wall_seconds=wall_seconds,
    )


def write_artifact(output: Path, artifact: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")


def main() -> int:
    args = parse_args()
    if not MIN_REQUESTS <= args.requests <= MAX_REQUESTS:
        print(f"requests {args.requests} is outside the allowed range "
              f"[{MIN_REQUESTS},{MAX_REQUESTS}]", file=sys.stderr)
        return 2
    if not MIN_CONCURRENCY <= args.concurrency <= MAX_CONCURRENCY:
        print(f"concurrency {args.concurrency} is outside the allowed range "
              f"[{MIN_CONCURRENCY},{MAX_CONCURRENCY}]", file=sys.stderr)
        return 2

    # 任何网络请求之前：完整解析 + 校验策略文档（缺失/坏 JSON/非法 kind 或
    # version 都以清晰信息退出 2）。
    try:
        policy = load_policy_document(Path(args.policy))
    except (OSError, ValueError) as exc:
        print(f"policy document {args.policy!r} is not a valid Routing Policy v4: {exc}",
              file=sys.stderr)
        return 2

    try_url = args.url.rstrip("/") + "/admin/try"
    print(f"planned_requests = {args.requests}")

    artifact = asyncio.run(run_probe(
        url=try_url, policy=policy, text=args.text,
        requests=args.requests, concurrency=args.concurrency,
    ))
    artifact["policy_path"] = str(Path(args.policy))
    write_artifact(Path(args.output), artifact)

    if artifact["failures"]:
        first = artifact["failures"][0]
        print(
            f"{len(artifact['failures'])}/{args.requests} samples failed; "
            f"first: status={first.get('status')} "
            f"reason={first.get('reason') or first.get('error')}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(artifact["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
