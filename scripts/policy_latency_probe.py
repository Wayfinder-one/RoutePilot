#!/usr/bin/env python3
"""Routing Policy v4 延迟探测：测量而不预设方向（brief Step 6）。

对目标服务的 ``POST /admin/try`` 发送完整 v4 策略文档（默认取
``configs/default_routing_policy.json``，即设计 §4.1 文档），每条请求文本
固定。逐条记录 wall latency、HTTP 状态、trace 内 JEV 事件数与 answers
数量；汇总只使用真实观测（p50/p95/max/吞吐）。任何响应包含多个 JEV
事件即退出 2——v4 一次决策至多一次 JEV 是结构不变式，不是性能偏好。

目标服务需可匿名访问 ``/admin/try``（例如 ROUTER_USER_AUTH=0 的本地
实例）。脚本不读取任何 API key，输出也不包含任何凭据。

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
POLICY_PATH = ROOT / "configs" / "default_routing_policy.json"
PROBE_TEXT = "10kV 线路跳闸，请安排抢修工单并做故障定位。"
MAX_REQUESTS = 200
MAX_CONCURRENCY = 8
REQUEST_TIMEOUT = httpx.Timeout(30.0)


def summarize(latencies_ms: list[float], wall_seconds: float, question_count: int) -> dict:
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


async def probe_one(client: httpx.AsyncClient, url: str, policy: dict) -> dict:
    started = time.perf_counter()
    try:
        response = await client.post(url, json={"policy": policy, "text": PROBE_TEXT},
                                     timeout=REQUEST_TIMEOUT)
        latency_ms = (time.perf_counter() - started) * 1000.0
        body = response.json() if response.status_code == 200 else {}
    except httpx.HTTPError as exc:
        return {
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "status": 0,
            "jev_events": 0,
            "answers": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    trace = body.get("trace") if isinstance(body, dict) else None
    jev_events = sum(1 for event in trace or [] if event.get("event") == "jev")
    answers = body.get("answers") if isinstance(body, dict) else None
    return {
        "latency_ms": latency_ms,
        "status": response.status_code,
        "jev_events": jev_events,
        "answers": len(answers) if isinstance(answers, dict) else 0,
        "error": None if response.status_code == 200 else response.text[:300],
    }


async def run(url: str, policy: dict, requests: int, concurrency: int) -> tuple[list[dict], float]:
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        async def worker():
            async with semaphore:
                results.append(await probe_one(client, url, policy))

        wall_started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(requests)))
        wall_seconds = time.perf_counter() - wall_started
    return results, wall_seconds


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure /admin/try latency for the full v4 routing policy")
    parser.add_argument("--url", default="http://127.0.0.1:8400")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", default="artifacts/policy-v4-latency.json")
    args = parser.parse_args()

    if args.requests > MAX_REQUESTS:
        print(f"requests {args.requests} exceeds the hard cap {MAX_REQUESTS}", file=sys.stderr)
        return 2
    if args.concurrency > MAX_CONCURRENCY:
        print(f"concurrency {args.concurrency} exceeds the hard cap {MAX_CONCURRENCY}", file=sys.stderr)
        return 2

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    question_count = len(policy.get("questions", {}))
    try_url = args.url.rstrip("/") + "/admin/try"
    print(f"planned_requests = {args.requests}")

    results, wall_seconds = asyncio.run(
        run(try_url, policy, args.requests, args.concurrency))
    latencies_ms = [row["latency_ms"] for row in results]

    multiple_jev = [index for index, row in enumerate(results) if row["jev_events"] > 1]
    http_failures = [row for row in results if row["status"] != 200]

    artifact = {
        "url": try_url,
        "concurrency": args.concurrency,
        "question_count": question_count,
        "latencies_ms": latencies_ms,
        "summary": (summarize(latencies_ms, wall_seconds, question_count)
                    if latencies_ms else None),
        "requests": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    if multiple_jev:
        print(f"multiple JEV events in responses: {multiple_jev[:5]}", file=sys.stderr)
        return 2
    if http_failures:
        first = http_failures[0]
        print(f"{len(http_failures)}/{len(results)} requests failed; "
              f"first status={first['status']} error={first.get('error')}", file=sys.stderr)
        return 1
    print(json.dumps(artifact["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
