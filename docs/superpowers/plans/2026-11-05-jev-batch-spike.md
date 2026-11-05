# JEV 多问题批量 Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现并执行一个可复现、可硬中止的 d1-2B 多问题批量 spike，判断单画布一次请求是否在答案、最终路由、容量和 prompt 预算上可替代当前波次路径。

**Architecture:** 新建一个标准库 HTTP 的 spike 脚本，以 `DecisionQuestion` 和现有 `GraphExecutionEngine` 为唯一 wire/图语义来源。H1 复用单问响应做当前波次本地重放；H2 使用真实 G3 激活条件做串联对照；H3 优先复用已有响应，缺口从预留请求额度补齐。所有阶段共享请求计数器、缓存和结构化产物。

**Tech Stack:** Python 3.11+、标准库 `urllib/json/asyncio/threading/concurrent.futures/dataclasses`、现有 `router.jev` 与 `router.routing.graph`、pytest；不新增第三方依赖。

**Source spec:** `docs/JEV-多问题批量-spike-2026-11-05.md`

## Global Constraints

- 不修改 `router/` 下的生产代码；只新增 spike 脚本、测试、被忽略的产物和执行报告。
- HTTP 只使用 urllib；不得引入 numpy、pandas、scipy、requests 或 tokenizer 依赖。
- 样本总数为 60–80，最大 80；所有阶段合计硬上限 2000 次，80 条预算为 1980 次，其中 smoke/H3 边界预留不超过 200 次。
- 小批 20 条计入全量；`--scenario all` 必须在同一进程中通过缓存复用小批 A/B 和重复基线，不得重新请求。
- 默认并发不超过 4；只有 H2 并发场景允许 `ThreadPoolExecutor(max_workers=8)`。连续两个 5xx 或达到请求上限立即中止。
- `ROUTER_JEV_API_KEY` 只能进入 Authorization header；不得写入日志、异常、响应文件或命令行参数。
- 不重启服务、不改服务配置、不写 DB；生产执行前必须再次获得人工确认，离线测试不得连接 76。
- G1/G2 是同波画布；G3 使用 `q1.prob.strong gt 0.65 → q2`、`q2.prob.strong gte 0.5 → q3`、`q3.yes gt 0.5 → q4`，以及 `q4.value gt 4 → t-review`、`q4.value lte 4 → t-normal`、`error_fallback=t-error`。
- A 路径任一请求答案缺失/非法都保守进入 `error_fallback`；C 路径只让实际激活问题的失败影响路由。
- 阈值结果按“样本 × 组 × 问题 × 算子 × 阈值”输出，禁止只给全局混合比例；0 翻转也必须给 95% Wilson 上界。
- `artifacts/jev-batch-spike/` 必须由 `.gitignore` 排除；不得修改或提交已有未跟踪目录 `configs/cases/` 的内容。
- 仓库默认忽略 `scripts/` 和 `docs/`；下列提交步骤仅对本计划明确列出的文件使用 `git add -f`，不得强制添加整个目录。

## File Structure

- Create `scripts/spike_jev_batch.py`：样本/图 fixture、传输、重放、三项实验、CLI 和产物写入。
- Create `tests/test_spike_jev_batch.py`：全部离线单元测试；使用 fake transport，不访问网络。
- Modify `.gitignore`：保留 `/artifacts/jev-batch-spike/` 排除规则。
- Create at execution time `artifacts/jev-batch-spike/{samples.jsonl,g3-graph.json,equivalence.json,capacity.json,budget.json,summary.json}`：敏感或大体量运行产物，不入 git。
- Create at execution time `docs/JEV-多问题批量-spike-报告-2026-11-05.md`：只写实际执行结果，不预写结论。

## Shared Interfaces

后续任务必须保持以下名称和形状一致：

```python
@dataclass(frozen=True)
class Sample:
    id: str
    text: str
    length_bucket: str
    boundary_tags: tuple[str, ...] = ()

@dataclass
class ResponseRecord:
    stage: str
    status: int | None
    body: dict | None
    error: str | None
    duration_ms: float
    input_tokens: int | None

@dataclass(frozen=True)
class RequestBudget:
    h1: int
    repeats: int
    scan: int
    concurrency: int
    reserve: int

    @property
    def total(self) -> int: ...

@dataclass(frozen=True)
class BudgetDecision:
    enabled: bool
    effective_context: int
    state_budget: int
    raw_overhead: int
```

函数契约（类型声明，不是独立代码块）：

```text
load_samples(path: Path, limit: int = 0) -> list[Sample]
build_question_groups() -> dict[str, dict[str, DecisionQuestion]]
build_g3_tree() -> RoutingTree
g3_snapshot(tree: RoutingTree) -> RoutingSnapshot
plan_request_budget(sample_count: int, scenario: str = "all") -> RequestBudget
replay_g3(tree: RoutingTree, text: str, records: dict[str, ResponseRecord], *, strict_all: bool) -> RoutingDecision
run_live_g3(tree: RoutingTree, text: str, transport: SpikeTransport) -> tuple[RoutingDecision, list[ResponseRecord]]
condition_passes(node: QuestionNode, port: str, op: str, threshold: float, value: float) -> bool
wilson_interval(failures: int, total: int) -> tuple[float, float]
threshold_records(sample_id: str, group: str, question_id: str, question: DecisionQuestion, a: JevAnswer, b: JevAnswer) -> list[dict]
run_equivalence(samples, groups, tree, transport, *, cache=None, group_names=("G1", "G2", "G3"), repeat_samples=20) -> dict
run_capacity(transport, groups, tree, *, scan_repeats=20, rounds=10, workers=8) -> dict
calibrate_budget(records: list[dict], safety: int = 128) -> dict
window_budget(context: int, input_tokens: int, raw_overhead: int, calibration: float, safety: int) -> BudgetDecision
```

---

### Task 1: 固定样本、G1/G2/G3 和请求预算

**Files:**
- Create: `scripts/spike_jev_batch.py`
- Create: `tests/test_spike_jev_batch.py`
- Modify: `.gitignore`
- Reference: `configs/default_routing_tree.json`, `configs/cases/case-logic.json`, `configs/cases/case-industry.json`

**Interfaces:**
- Produces: `Sample`、`RequestBudget`、`load_samples()`、`build_question_groups()`、`build_g3_tree()`、`plan_request_budget()`。
- Fixture 来源：G1/G3 的 choice 来自 default 的 fast/strong；noul/score 来自 case-logic；G2 的 choice 取 case-industry 前四个候选 medical/legal/finance/energy。

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path

import pytest

from router.routing.graph.validate import EnableContext, validate_graph_for_enable
from scripts.spike_jev_batch import (
    build_g3_tree,
    build_question_groups,
    load_samples,
    plan_request_budget,
)


def test_full_budget_is_1980_and_each_stage_is_explicit():
    budget = plan_request_budget(80)
    assert (budget.h1, budget.repeats, budget.scan, budget.concurrency, budget.reserve) == (960, 60, 120, 640, 200)
    assert budget.total == 1980


def test_budget_rejects_unknown_scenario_and_bad_sample_count():
    with pytest.raises(ValueError):
        plan_request_budget(0)
    with pytest.raises(ValueError):
        plan_request_budget(81)
    with pytest.raises(ValueError):
        plan_request_budget(20, "unknown")


def test_groups_and_g3_use_real_question_shapes():
    groups = build_question_groups()
    assert {key: len(value) for key, value in groups.items()} == {"G1": 2, "G2": 3, "G3": 4}
    assert [q.question_type for q in groups["G3"].values()] == ["choice", "choice", "noul", "score"]
    tree = build_g3_tree()
    issues = validate_graph_for_enable(tree, EnableContext(
        known_models={"mock"}, model_reasoning={"mock": None},
        known_periods=set(), jev_available=True,
    ))
    assert issues == []
    assert tree.root.error_fallback == "t-error"
    assert [(edge.source_port, edge.target, edge.condition.op, edge.condition.value)
            for edge in tree.root.edges[1:4]] == [
        ("prob.strong", "q2", "gt", 0.65),
        ("prob.strong", "q3", "gte", 0.5),
        ("yes", "q4", "gt", 0.5),
    ]


def test_load_samples_validates_and_applies_limit(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text("".join(
        f'{{"id":"s{i}","text":"文本{i}","length_bucket":"short","boundary_tags":[]}}\n'
        for i in range(60)
    ), encoding="utf-8")
    loaded = load_samples(path)
    assert len(loaded) == 60
    assert [item.id for item in loaded[:2]] == ["s0", "s1"]
    assert len(load_samples(path, 1)) == 1
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'scripts.spike_jev_batch'`。

- [ ] **Step 3: 实现 fixture、样本校验和预算**

在脚本中写入以下完整核心实现；JSON fixture 的 target 全部使用 `mock` 模型，位置字段省略：

```python
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

from router.jev.client import DecisionQuestion, JevCandidate
from router.routing.model import tree_from_dict

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {"all", "equivalence", "capacity", "budget"}


@dataclass(frozen=True)
class Sample:
    id: str
    text: str
    length_bucket: str
    boundary_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestBudget:
    h1: int
    repeats: int
    scan: int
    concurrency: int
    reserve: int

    @property
    def total(self) -> int:
        return self.h1 + self.repeats + self.scan + self.concurrency + self.reserve


def load_samples(path: Path, limit: int = 0) -> list[Sample]:
    result = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict) or set(raw) != {"id", "text", "length_bucket", "boundary_tags"}:
            raise ValueError(f"invalid sample at line {line_number}")
        if not all(isinstance(raw[key], str) and raw[key] for key in ("id", "text", "length_bucket")):
            raise ValueError(f"invalid sample strings at line {line_number}")
        if raw["length_bucket"] not in {"short", "medium", "long"}:
            raise ValueError(f"invalid length_bucket at line {line_number}")
        if not isinstance(raw["boundary_tags"], list) or not all(isinstance(x, str) for x in raw["boundary_tags"]):
            raise ValueError(f"invalid boundary_tags at line {line_number}")
        if raw["id"] in seen:
            raise ValueError(f"duplicate sample id {raw['id']!r}")
        seen.add(raw["id"])
        result.append(Sample(raw["id"], raw["text"], raw["length_bucket"], tuple(raw["boundary_tags"])))
    if not 60 <= len(result) <= 80:
        raise ValueError("sample count must be in 60..80")
    return result[:limit] if limit else result


def plan_request_budget(sample_count: int, scenario: str = "all") -> RequestBudget:
    if not 1 <= sample_count <= 80 or scenario not in SCENARIOS:
        raise ValueError("invalid sample count or scenario")
    do_h1 = scenario in {"all", "equivalence"}
    do_h2 = scenario in {"all", "capacity"}
    do_h3 = scenario in {"all", "budget"}
    return RequestBudget(
        h1=sample_count * 12 if do_h1 else 0,
        repeats=min(sample_count, 20) * 3 if do_h1 else 0,
        scan=120 if do_h2 else 0,
        concurrency=640 if do_h2 else 0,
        reserve=200 if do_h3 else 0,
    )


def _load_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _as_decision(node):
    if node.question_type == "choice":
        return DecisionQuestion("choice", node.instructions, options=tuple(
            JevCandidate(item.key, item.label, item.capability) for item in node.options
        ))
    if node.question_type == "noul":
        return DecisionQuestion("noul", node.instructions)
    return DecisionQuestion("score", node.instructions, criteria=node.criteria)


def _source_trees():
    default = tree_from_dict({
        "id": "default", "name": "", "enabled": True,
        "root": _load_json("configs/default_routing_tree.json")["root"],
    })
    logic_raw = _load_json("configs/cases/case-logic.json")
    industry_raw = _load_json("configs/cases/case-industry.json")
    logic = tree_from_dict({"id": "logic", "name": "", "enabled": True, "root": logic_raw["root"]})
    industry = tree_from_dict({"id": "industry", "name": "", "enabled": True, "root": industry_raw["root"]})
    return default, logic, industry


def build_question_groups():
    default, logic, industry = _source_trees()
    default_nodes = {node.id: node for node in default.root.nodes}
    logic_nodes = {node.id: node for node in logic.root.nodes}
    industry_nodes = {node.id: node for node in industry.root.nodes}
    capability = default_nodes["capability"]
    industry_choice = replace(
        industry_nodes["industry"], options=tuple(industry_nodes["industry"].options[:4])
    )
    return {
        "G1": {"capability": _as_decision(capability), "urgent": _as_decision(logic_nodes["q-urgent"])},
        "G2": {"industry": _as_decision(industry_choice), "severity": _as_decision(logic_nodes["q-severity"]),
               "urgent": _as_decision(logic_nodes["q-urgent"])},
        "G3": {"q1": _as_decision(capability), "q2": _as_decision(capability),
               "q3": _as_decision(logic_nodes["q-urgent"]), "q4": _as_decision(logic_nodes["q-severity"])},
    }


def build_g3_tree():
    default, logic, _ = _source_trees()
    default_raw = _load_json("configs/default_routing_tree.json")["root"]
    logic_raw = _load_json("configs/cases/case-logic.json")["root"]
    choice = next(node for node in default_raw["nodes"] if node.get("id") == "capability")
    urgent = next(node for node in logic_raw["nodes"] if node.get("id") == "q-urgent")
    severity = next(node for node in logic_raw["nodes"] if node.get("id") == "q-severity")
    nodes = [{"id": "start", "type": "start"}]
    for node_id, source in (("q1", choice), ("q2", choice), ("q3", urgent), ("q4", severity)):
        item = copy.deepcopy(source)
        item["id"] = node_id
        item.pop("position", None)
        nodes.append(item)
    for node_id, priority in (("t-review", 3), ("t-normal", 2), ("t-error", 1)):
        nodes.append({"id": node_id, "type": "target", "priority": priority,
                      "models": [{"model": "mock", "reasoning": None}]})
    edges = [
        {"id": "e0", "source": "start", "source_port": "out", "target": "q1", "target_port": "in_1", "condition": None},
        {"id": "e1", "source": "q1", "source_port": "prob.strong", "target": "q2", "target_port": "in_1", "condition": {"op": "gt", "value": 0.65}},
        {"id": "e2", "source": "q2", "source_port": "prob.strong", "target": "q3", "target_port": "in_1", "condition": {"op": "gte", "value": 0.5}},
        {"id": "e3", "source": "q3", "source_port": "yes", "target": "q4", "target_port": "in_1", "condition": {"op": "gt", "value": 0.5}},
        {"id": "e4", "source": "q4", "source_port": "value", "target": "t-review", "target_port": "in_1", "condition": {"op": "gt", "value": 4}},
        {"id": "e5", "source": "q4", "source_port": "value", "target": "t-normal", "target_port": "in_1", "condition": {"op": "lte", "value": 4}},
    ]
    return tree_from_dict({"id": "g3", "name": "G3", "enabled": True,
                           "root": {"id": "root", "type": "graph", "nodes": nodes, "edges": edges,
                                    "error_fallback": "t-error"}})
```

`load_samples()` 先校验完整文件包含 60–80 条，再应用 `--limit`，因此离线 dry-run 可以检查小规模切片而生产样本仍需满足总量约束。

- [ ] **Step 4: 运行测试并确认通过**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 4 passed。

- [ ] **Step 5: 提交本任务**

```bash
git add .gitignore tests/test_spike_jev_batch.py
git add -f scripts/spike_jev_batch.py docs/JEV-多问题批量-spike-2026-11-05.md docs/superpowers/plans/2026-11-05-jev-batch-spike.md
git commit -m "test(spike): add JEV batch fixtures and request budget"
```

---

### Task 2: 实现带硬上限和 5xx 熔断的 urllib 传输

**Files:**
- Modify: `scripts/spike_jev_batch.py`
- Modify: `tests/test_spike_jev_batch.py`

**Interfaces:**
- Produces: `ResponseRecord`、`BudgetExceeded`、`SpikeAbort`、`SpikeTransport.post(state, questions, stage) -> ResponseRecord`。
- `questions` 必须是 `Mapping[str, DecisionQuestion]`；wire 构造复用 `router.jev.client._wire_question`。

- [ ] **Step 1: 写传输失败测试**

```python
import json
import urllib.error
from io import BytesIO

import pytest

from scripts.spike_jev_batch import BudgetExceeded, ResponseRecord, SpikeAbort, SpikeTransport, build_question_groups


class FakeHTTPResponse:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode()
        self.status = status

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.body


def success_body():
    return {"answers": {"urgent": {"type": "noul", "noul": 0.6}},
            "usage": {"input_tokens": 123, "output_tokens": 0}}


def test_transport_sends_wire_and_never_needs_to_log_key(monkeypatch):
    captured = {}
    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data)
        return FakeHTTPResponse(success_body())
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    transport = SpikeTransport("http://jev.test", "d1-2B", api_key="secret", sleep_seconds=0)
    record = transport.post("state", {"urgent": build_question_groups()["G1"]["urgent"]}, "test")
    assert isinstance(record, ResponseRecord) and record.input_tokens == 123
    assert captured["authorization"] == "Bearer secret"
    assert captured["payload"]["questions"]["urgent"] == {"type": "noul", "instructions": "这条用户请求描述的告警或故障是否紧急，是否需要立即处置？"}


def test_transport_stops_on_second_consecutive_5xx(monkeypatch):
    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", None, BytesIO(b"{}"))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    transport = SpikeTransport("http://jev.test", "d1-2B", sleep_seconds=0)
    question = {"urgent": build_question_groups()["G1"]["urgent"]}
    transport.post("state", question, "test")
    with pytest.raises(SpikeAbort):
        transport.post("state", question, "test")
    assert transport.count == 2


def test_transport_rejects_request_before_io_at_cap(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args: (_ for _ in ()).throw(AssertionError("network used")))
    transport = SpikeTransport("http://jev.test", "d1-2B", max_requests=0, sleep_seconds=0)
    with pytest.raises(BudgetExceeded):
        transport.post("state", {"urgent": build_question_groups()["G1"]["urgent"]}, "test")
    assert transport.count == 0
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 新测试 FAIL，提示缺少 `SpikeTransport`。

- [ ] **Step 3: 实现传输**

```python
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

from router.jev.client import _wire_question


@dataclass
class ResponseRecord:
    stage: str
    status: int | None
    body: dict | None
    error: str | None
    duration_ms: float
    input_tokens: int | None


class BudgetExceeded(RuntimeError): pass
class SpikeAbort(RuntimeError): pass


class SpikeTransport:
    def __init__(self, base_url, model, *, api_key="", timeout=20.0,
                 max_requests=2000, sleep_seconds=0.02):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_requests = max_requests
        self.sleep_seconds = sleep_seconds
        self.count = 0
        self.stage_counts = {}
        self._consecutive_5xx = 0
        self._lock = threading.Lock()

    def post(self, state: str, questions: Mapping[str, DecisionQuestion], stage: str) -> ResponseRecord:
        if not questions:
            raise ValueError("questions must not be empty")
        with self._lock:
            if self.count >= self.max_requests:
                raise BudgetExceeded(f"request cap {self.max_requests} reached")
            self.count += 1
            self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1
        wire = {key: _wire_question(question) for key, question in questions.items()}
        payload = {"model": self.model, "state": state, "questions": wire}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(
            self.base_url + "/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        started = time.monotonic()
        status = None
        body = None
        error = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = json.loads(response.read())
                if not isinstance(body, dict):
                    raise ValueError("response body must be an object")
        except urllib.error.HTTPError as exc:
            status = exc.code
            error = f"HTTP {exc.code}"
            try:
                parsed = json.loads(exc.read())
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            error = type(exc).__name__
        duration = (time.monotonic() - started) * 1000
        with self._lock:
            self._consecutive_5xx = self._consecutive_5xx + 1 if status is not None and 500 <= status < 600 else 0
            should_abort = self._consecutive_5xx >= 2
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        if should_abort:
            raise SpikeAbort("two consecutive 5xx responses")
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
            input_tokens = None
        return ResponseRecord(stage, status, body, error, duration, input_tokens)
```

- [ ] **Step 4: 运行传输测试和既有 JEV 测试**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py tests/test_graph_jev_client.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add tests/test_spike_jev_batch.py
git add -f scripts/spike_jev_batch.py
git commit -m "test(spike): add capped SystemOne transport"
```

---

### Task 3: 用真实 GraphExecutionEngine 重放 G3 并实现统计原语

**Files:**
- Modify: `scripts/spike_jev_batch.py`
- Modify: `tests/test_spike_jev_batch.py`

**Interfaces:**
- Consumes: Task 1 的 `build_g3_tree()`，Task 2 的 `ResponseRecord`。
- Produces: `typed_answer()`、`replay_g3()`、`route_outcome()`、`condition_passes()`、`wilson_interval()`。
- `route_outcome(decision, http_requests)` 返回 `target/candidates/activated/failed/http_requests`。

- [ ] **Step 1: 写失败语义、gt/gte 和 Wilson 测试**

```python
import pytest

from scripts.spike_jev_batch import (
    ResponseRecord, build_g3_tree, build_question_groups, condition_passes,
    replay_g3, route_outcome, wilson_interval,
)


def test_wilson_zero_flip_still_has_nonzero_upper_bound():
    low, high = wilson_interval(0, 100)
    assert low == 0.0
    assert high == pytest.approx(0.036994807476, abs=1e-12)
    with pytest.raises(ValueError):
        wilson_interval(1, 0)


def test_condition_gt_and_gte_are_not_merged():
    tree = build_g3_tree()
    q3 = next(node for node in tree.root.nodes if node.id == "q3")
    assert condition_passes(q3, "yes", "gt", 0.5, 0.5) is False
    assert condition_passes(q3, "yes", "gte", 0.5, 0.5) is True


def test_strict_a_validates_all_but_c_ignores_inactive_failures():
    question = build_question_groups()["G3"]["q1"]
    probabilities = {
        candidate.label: 0.1 if candidate.key == "strong" else 0.9
        for candidate in question.options
    }
    q1 = ResponseRecord("test", 200, {
        "answers": {"q1": {"type": "choice", "probabilities": probabilities}},
        "usage": {"input_tokens": 10},
    }, None, 1.0, 10)
    failed = ResponseRecord("test", 500, None, "HTTP 500", 1.0, None)
    records = {"q1": q1, "q2": failed, "q3": failed, "q4": failed}
    tree = build_g3_tree()
    c_decision = replay_g3(tree, "文本", records, strict_all=False)
    a_decision = replay_g3(tree, "文本", records, strict_all=True)
    assert route_outcome(c_decision, len(c_decision.waves))["activated"] == ("q1",)
    assert route_outcome(c_decision, len(c_decision.waves))["failed"] is False
    assert route_outcome(a_decision, 1)["failed"] is True
    assert route_outcome(a_decision, 1)["target"] == "t-error"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 新测试因缺少重放/统计函数而 FAIL。

- [ ] **Step 3: 实现重放和统计原语**

```python
import asyncio
from datetime import datetime

from router.api.upstream import Backend
from router.jev.client import JevError, parse_answer
from router.jev.config import JevSettings
from router.routing.affinity import SessionAffinity
from router.routing.graph.conditions import evaluate_condition
from router.routing.graph.engine import GraphExecutionEngine
from router.routing.graph.model import Condition, Edge, QuestionNode
from router.routing.scheduler import LoadBalancer
from router.routing.snapshot import build_snapshot
from router.routing.types import RoutingRequest


def typed_answer(key, question, record):
    if record.status != 200 or record.body is None:
        raise JevError(record.error or f"HTTP {record.status}")
    return parse_answer(key, question, record.body, record.duration_ms)


def g3_snapshot(tree):
    backend = Backend(url="http://mock.test", headers={}, upstream_name="mock", provider="mock")
    return build_snapshot(
        tree=tree, backends={"mock": backend}, time_periods=[],
        jev=JevSettings("http://jev.test", "d1-2B"), capabilities={},
        jev_input_tokens=2048, jev_context_tokens=4096,
    )


class ReplayJev:
    def __init__(self, records, all_questions, strict_all):
        self.records = records
        self.all_questions = all_questions
        self.strict_all = strict_all

    async def choose_many(self, settings, text, questions):
        if self.strict_all:
            if set(self.records) != set(self.all_questions):
                raise JevError("packed record keys do not match G3")
            parsed = {
                key: typed_answer(key, question, self.records[key])
                for key, question in self.all_questions.items()
            }
            return {key: parsed[key] for key in questions}
        return {
            key: typed_answer(key, question, self.records[key])
            for key, question in questions.items()
        }


def replay_g3(tree, text, records, *, strict_all):
    all_questions = {
        node.id: _as_decision(node) for node in tree.root.nodes
        if isinstance(node, QuestionNode)
    }
    engine = GraphExecutionEngine(
        ReplayJev(records, all_questions, strict_all), SessionAffinity(), LoadBalancer()
    )
    decision = asyncio.run(engine.route(
        g3_snapshot(tree), RoutingRequest(text),
        now=datetime.fromisoformat("2026-11-05T12:00:00+08:00"), dry_run=True,
    ))
    decision.release()
    return decision


def route_outcome(decision, http_requests):
    return {
        "target": decision.target_id,
        "candidates": [item.target_id for item in decision.candidates],
        "activated": tuple(decision.answers),
        "failed": any(wave.failed for wave in decision.waves),
        "http_requests": http_requests,
    }


def condition_passes(node: QuestionNode, port, op, threshold, value):
    edge = Edge("metric", node.id, port, "metric", "in_1", Condition(op, threshold))
    result = evaluate_condition(edge, node, value)
    if result.status not in {"true", "false"}:
        raise ValueError(f"invalid metric condition: {result.reason or result.status}")
    return result.value


def wilson_interval(failures, total):
    if total <= 0 or not 0 <= failures <= total:
        raise ValueError("require total > 0 and 0 <= failures <= total")
    z = 1.96
    p = failures / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - half), min(1.0, center + half)
```

注意：`ReplayJev(strict_all=True)` 会在第一波先解析全部 A 答案，因此未激活问题的失败也会使整波失败；`strict_all=False` 只解析当前 queue，符合 C 语义。

- [ ] **Step 4: 运行重放测试和图引擎回归**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py tests/test_graph_engine.py -q`
Expected: 全部 PASS，且未修改 `tests/test_graph_engine.py`。

- [ ] **Step 5: 提交本任务**

```bash
git add tests/test_spike_jev_batch.py
git add -f scripts/spike_jev_batch.py
git commit -m "test(spike): replay G3 with production graph semantics"
```

---

### Task 4: 实现 H1 A/B、重复基线、边界指标和端到端对照

**Files:**
- Modify: `scripts/spike_jev_batch.py`
- Modify: `tests/test_spike_jev_batch.py`

**Interfaces:**
- Produces: `run_equivalence()`、`threshold_records()`、`call_document()`；可选 `cache` 是调用方持有的 `dict`，键为 `(sample_id, group, path, question_id)`，值为元数据加 `response: ResponseRecord`。
- 输出字典固定包含 `calls/comparisons/thresholds/routes/invalid_comparisons/summary`；`calls` 由 `call_document()` 生成，每项带 `sample_id/group/path/question_id/question_count/state_est_tokens/wire_est_tokens` 以及 ResponseRecord 字段。

- [ ] **Step 1: 写缓存复用和算子边界测试**

```python
from router.jev.client import NoulAnswer
from scripts.spike_jev_batch import Sample, run_equivalence, threshold_records


class FakeH1Transport:
    def __init__(self):
        self.count = 0

    def post(self, state, questions, stage):
        self.count += 1
        answers = {}
        for key, question in questions.items():
            if question.question_type == "choice":
                labels = [item.label for item in question.options]
                probabilities = {label: 0.7 / 1 if i == 0 else 0.3 / (len(labels) - 1)
                                 for i, label in enumerate(labels)}
                answers[key] = {"type": "choice", "probabilities": probabilities}
            elif question.question_type == "noul":
                answers[key] = {"type": "noul", "noul": 0.6}
            else:
                answers[key] = {"type": "score", "score": 0,
                                "legend": {str(i): value for i, value in enumerate(question.criteria)}}
        return ResponseRecord(stage, 200, {"answers": answers, "usage": {"input_tokens": 100}},
                              None, 1.0, 100)


def test_small_and_full_share_cache_without_repeating_requests():
    samples = [Sample(f"s{i}", f"文本{i}", "short") for i in range(60)]
    groups = build_question_groups()
    tree = build_g3_tree()
    fake = FakeH1Transport()
    cache = {}
    run_equivalence(samples[:20], groups, tree, fake, cache=cache,
                    group_names=("G1", "G2"), repeat_samples=20)
    assert fake.count == 20 * 7 + 20 * 2
    second = run_equivalence(samples, groups, tree, fake, cache=cache,
                             group_names=("G1", "G2", "G3"), repeat_samples=20)
    assert fake.count == 60 * 12 + 20 * 3
    assert len(second["routes"]) == 60
    assert all(row["a_http_requests"] == 1 for row in second["routes"])


def test_threshold_records_keep_gt_and_gte_separate():
    question = build_question_groups()["G1"]["urgent"]
    a = NoulAnswer(0.5, 0.5, {}, 0.0)
    b = NoulAnswer(0.5001, 0.4999, {}, 0.0)
    rows = threshold_records("s1", "G1", "urgent", question, a, b)
    gt = next(row for row in rows if row["op"] == "gt" and row["threshold"] == 0.5)
    gte = next(row for row in rows if row["op"] == "gte" and row["threshold"] == 0.5)
    assert gt["flipped"] is True
    assert gte["flipped"] is False
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 新测试因缺少 `run_equivalence()` 而 FAIL。

- [ ] **Step 3: 实现 H1**

实现必须按以下确定顺序执行，不能引入随机调度：

```python
from router.jev.window import estimate_tokens
from router.jev.config import DEFAULT_STATE_TEMPLATE

PROB_THRESHOLDS = (0.5, 0.6, 0.65, 0.7)
SCORE_THRESHOLDS = (4.0, 8.0)
OPS = ("gt", "gte", "lt", "lte", "eq", "neq")


def render_state(text):
    return DEFAULT_STATE_TEMPLATE.format(request=text)


def canonical_questions(questions):
    return json.dumps({key: _wire_question(value) for key, value in questions.items()},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _call(transport, cache, key, *, stage, state, questions, sample_id, group, path, question_id):
    if key not in cache:
        record = transport.post(state, questions, stage)
        cache[key] = {
            "sample_id": sample_id, "group": group, "path": path, "question_id": question_id,
            "question_count": len(questions), "state_est_tokens": estimate_tokens(state),
            "wire_est_tokens": estimate_tokens(state + "\n" + canonical_questions(questions)),
            "response": record,
        }
    return cache[key]


def call_document(cached):
    metadata = {key: value for key, value in cached.items() if key != "response"}
    return {**metadata, **cached["response"].__dict__}


def _question_node(key, question):
    from router.routing.graph.model import QuestionOption
    return QuestionNode(key, question.question_type, question.instructions,
                        options=tuple(QuestionOption(x.key, x.label, x.capability) for x in question.options),
                        criteria=question.criteria)
```

`run_equivalence()` 的每条样本/组必须实现以下完整数据流：

1. 根据 `sha256(f"{sample.id}:{group}")` 的奇偶决定 A→B 或 B→A，交错顺序只影响请求，不改变缓存键。
2. A 调 `_call(... path="A", question_id="*")` 一次；B 对每个问题调用一次。将同一个 A ResponseRecord 映射到每个问题 id 供 `typed_answer()` 使用。
3. 前 `repeat_samples` 条每组选择首个 noul（无 noul 则首个问题）再请求一次，`path="repeat"`；不得复用 B 的响应充当重复测量。
4. 只有 A/B 两侧都能解析时，才写入概率/value delta 和阈值判断；解析失败保留在 calls/routes，不得从失败样本的分母中静默消失，另写 `invalid_comparisons`。
5. choice 比较每个 option key 的 `probabilities`、selected；noul 比较 `yes`；score 比较 `value`。阈值端口分别为 `prob.<key>`、`yes`、`value`。
6. 每条阈值记录含 `sample_id/group/question_id/port/op/threshold/a_value/b_value/flipped`；聚合按 `group/question_id/port/op/threshold` 输出 `flips,total,rate,wilson_low,wilson_high`。
7. G3 用 `replay_g3(..., strict_all=True)` 重放 A，用 `strict_all=False` 重放 B；写 `a/b/target_same/candidates_same/activated_same/a_http_requests=1/c_http_requests=len(b_decision.waves)/new_error_fallback`。
8. `summary` 计算每组 max/mean 绝对概率差、argmax 一致率、重复基线 max、边界子集翻转率、G3 最终路由一致率和新增兜底数。`noise_ok` 固定为 `max_repeat <= 0.02 and max_ab <= 0.02`；不用样本量不足的显著性检验冒充“不显著”。

H1 pass 必须同时满足：argmax/selected=100%、max_ab≤0.02、max_repeat≤0.02、每个分层及边界子集翻转率≤0.005、G3 target/candidates=100%、新增兜底=0。任一类边界样本为空时 `status="conditional"`；任何失败记录或无效比较不得被当成等价证据。

- [ ] **Step 4: 运行 H1 测试**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 全部 PASS；缓存测试的最终 fake 请求数严格为 780。

- [ ] **Step 5: 提交本任务**

```bash
git add tests/test_spike_jev_batch.py
git add -f scripts/spike_jev_batch.py
git commit -m "test(spike): measure answer and route equivalence"
```

---

### Task 5: 实现 H2 N 扫描和真实串联并发基线

**Files:**
- Modify: `scripts/spike_jev_batch.py`
- Modify: `tests/test_spike_jev_batch.py`

**Interfaces:**
- Produces: `percentile(values, q)`、`expand_questions(questions, n)`、`run_capacity()`。
- `run_capacity()` 返回 `scan/concurrency`；并发项必须含 `wall_seconds/request_count/question_count/questions_per_second/requests_per_second/canvas_p50_ms/canvas_p95_ms/error_count`。

- [ ] **Step 1: 写百分位、唯一 id 和提前停止测试**

```python
from scripts.spike_jev_batch import expand_questions, percentile, run_live_g3


def test_percentile_uses_nearest_rank():
    assert percentile([10, 20, 30, 40], 0.5) == 20
    assert percentile([10, 20, 30, 40], 0.95) == 40


def test_expand_questions_has_unique_ids_and_stable_shapes():
    source = build_question_groups()["G2"]
    expanded = expand_questions(source, 8)
    assert list(expanded) == [f"q{i}" for i in range(1, 9)]
    assert [q.question_type for q in expanded.values()] == [
        "choice", "score", "noul", "choice", "score", "noul", "choice", "score"
    ]


class EarlyStopTransport:
    def __init__(self):
        self.count = 0

    def post(self, state, questions, stage):
        self.count += 1
        assert list(questions) == ["q1"]
        question = questions["q1"]
        probabilities = {
            item.label: 0.1 if item.key == "strong" else 0.9
            for item in question.options
        }
        return ResponseRecord(stage, 200, {
            "answers": {"q1": {"type": "choice", "probabilities": probabilities}},
            "usage": {"input_tokens": 100},
        }, None, 1.0, 100)


def test_live_current_path_stops_after_failed_activation_condition():
    fake = EarlyStopTransport()
    decision, records = run_live_g3(build_g3_tree(), "文本", fake)
    assert fake.count == 1
    assert len(records) == 1
    assert tuple(decision.answers) == ("q1",)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 新测试因缺少容量函数而 FAIL。

- [ ] **Step 3: 实现容量实验**

```python
from concurrent.futures import ThreadPoolExecutor


def percentile(values, q):
    if not values or not 0 < q <= 1:
        raise ValueError("values must be nonempty and q in (0,1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def expand_questions(questions, n):
    if not 1 <= n <= 32:
        raise ValueError("n must be in 1..32")
    source = list(questions.values())
    return {f"q{i}": source[(i - 1) % len(source)] for i in range(1, n + 1)}


class LiveJev:
    def __init__(self, transport, state, stage="H2-B"):
        self.transport = transport
        self.state = state
        self.stage = stage
        self.records = []

    async def choose_many(self, settings, text, questions):
        record = self.transport.post(self.state, questions, self.stage)
        self.records.append(record)
        return {key: typed_answer(key, question, record) for key, question in questions.items()}


def run_live_g3(tree, text, transport):
    live = LiveJev(transport, render_state(text))
    engine = GraphExecutionEngine(live, SessionAffinity(), LoadBalancer())
    decision = asyncio.run(engine.route(
        g3_snapshot(tree), RoutingRequest(text),
        now=datetime.fromisoformat("2026-11-05T12:00:00+08:00"), dry_run=True,
    ))
    decision.release()
    return decision, live.records
```

`run_capacity()` 必须按以下流程实现：

1. 固定 `state=render_state` 后的 300–500 估算 token 文本；对 N=(1,2,4,8,16,32) 和 `expand_questions(G2,N)` 串行执行 `scan_repeats=20`。每个 N 输出 p50/p95/max、usage、HTTP/transport/timeout 数。
2. N≤8 的容量通过条件为错误率 0 且 p95≤2×N=1 p95；N>8 只记录，不把“记录行为”写成 pass。
3. 并发 A 对 N=(2,4) 各跑 `rounds=10`：每轮用 8 workers，每画布一次 N 问请求，记录每画布延迟和实际答案数。
4. 并发 B 使用同样的 workers/rounds，并通过 `run_live_g3()` 运行真实引擎。N=2 前缀图保留 start/q1/q2/t-error、e0/e1 和 `error_fallback=t-error`，不得构造缺少兜底 target 的非法图；N=4 使用完整 G3。
5. B 的每画布延迟从调用 `run_live_g3()` 前到返回后（内部已 release）；问题数取实际成功 HTTP 响应中的答案数，失败画布计入 error，不得按计划 N 充数。每轮墙钟从提交 8 个画布前到全部结束。
6. 吞吐分母为 A/B 各自总墙钟；问题/秒使用实际问题数，请求/秒只记录。A 通过要求 `questions_per_second >= 0.7 * B` 且 `canvas_p95_ms <= B`。
7. 任何 `BudgetExceeded/SpikeAbort` 都停止提交新画布并向上抛出；不得捕获后继续下一轮。

- [ ] **Step 4: 运行容量测试**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 全部 PASS；fake-live 测试证明 B 提前停止，未访问网络。

- [ ] **Step 5: 提交本任务**

```bash
git add tests/test_spike_jev_batch.py
git add -f scripts/spike_jev_batch.py
git commit -m "test(spike): compare packed and serial canvas capacity"
```

---

### Task 6: 实现 H3 校准、窗口公式、CLI 和产物

**Files:**
- Modify: `scripts/spike_jev_batch.py`
- Modify: `tests/test_spike_jev_batch.py`
- Reference: `router/jev/window.py`, `router/jev/config.py`

**Interfaces:**
- Produces: `calibrate_budget()`、`window_budget()`、`write_json()`、`main(argv=None) -> int`。
- CLI 支持 `--url --model --samples --scenario {all,equivalence,capacity,budget} --repeats 2 --limit 0 --max-requests 2000 --out-dir --timeout 20 --dry-run`。

- [ ] **Step 1: 写校准、边界和 dry-run 测试**

```python
import json

from scripts.spike_jev_batch import calibrate_budget, main, window_budget


def test_window_budget_includes_calibration_safety_and_minimum_input():
    enabled = window_budget(4096, 2048, raw_overhead=512, calibration=1.25, safety=128)
    assert enabled.effective_context == 3174
    assert enabled.enabled is True
    assert enabled.state_budget == 2048
    disabled = window_budget(4096, 2048, raw_overhead=2919, calibration=1.25, safety=128)
    assert disabled.enabled is False
    assert disabled.state_budget < 256


def test_calibration_is_an_upper_bound_not_mean_fit():
    result = calibrate_budget([
        {"status": 200, "wire_est_tokens": 100, "input_tokens": 110},
        {"status": 200, "wire_est_tokens": 200, "input_tokens": 180},
    ])
    assert result["calibration"] == 1.1
    assert result["safety"] == 128
    assert all(item["input_tokens"] <= item["wire_est_tokens"] * result["calibration"] + result["safety"]
               for item in result["records"])


def test_dry_run_prints_budget_without_network(tmp_path, capsys):
    samples = tmp_path / "samples.jsonl"
    rows = [{"id": f"s{i}", "text": "文本", "length_bucket": "short", "boundary_tags": []} for i in range(60)]
    samples.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    code = main(["--samples", str(samples), "--scenario", "all", "--dry-run", "--out-dir", str(tmp_path / "out")])
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["planned_requests"] == 1740
    assert not (tmp_path / "out").exists()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py -q`
Expected: 新测试因缺少预算/CLI 函数而 FAIL。

- [ ] **Step 3: 实现 H3 与 CLI**

```python
import argparse
import sys


@dataclass(frozen=True)
class BudgetDecision:
    enabled: bool
    effective_context: int
    state_budget: int
    raw_overhead: int


def window_budget(context, input_tokens, raw_overhead, calibration, safety):
    if min(context, input_tokens, raw_overhead) < 0 or calibration < 1 or safety < 0:
        raise ValueError("invalid budget arguments")
    effective = math.floor((context - safety) / calibration)
    return BudgetDecision(
        enabled=raw_overhead + 256 <= effective,
        effective_context=effective,
        state_budget=min(input_tokens, effective - raw_overhead),
        raw_overhead=raw_overhead,
    )


def calibrate_budget(records, safety=128):
    usable = [dict(item) for item in records
              if item.get("status") == 200 and isinstance(item.get("input_tokens"), int)
              and isinstance(item.get("wire_est_tokens"), int) and item["wire_est_tokens"] > 0]
    if not usable:
        raise ValueError("no usable token observations")
    calibration = max(1.0, max(item["input_tokens"] / item["wire_est_tokens"] for item in usable))
    return {"calibration": calibration, "safety": safety, "records": usable,
            "max_est_over_actual": max(item["wire_est_tokens"] / item["input_tokens"] for item in usable)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
```

H3 执行必须满足：

1. 从 H1/H2 `calls` 提取 token 观测，检查 `(256,1024,2048) × (1,2,4,8) × (G1,G2)` 覆盖矩阵。已有响应优先复用；缺失组合用非敏感合成文本补齐，并计入最多 200 的预留额度。
2. 每个新 H3 请求都带完整 questions mapping 的 `wire_est_tokens`；`raw_overhead` 使用 `estimate_tokens(canonical_questions(questions) + "\n" + DEFAULT_STATE_TEMPLATE.format(request=""))`。
3. 用 `calibrate_budget()` 得出 `CALIBRATION/SAFETY`，用 `window_budget()` 检查所有观测的校准上界；任何 `actual > est*CALIBRATION+SAFETY` 都使 H3 fail。
4. 在预留额度内构造接近、等于、超过 4096 的三类边界。对每个正常请求计算 `post_window_est = raw_overhead + min(state_est_tokens, decision.state_budget)`；若 `decision.enabled=false` 或 `post_window_est>decision.effective_context`，正常路径必须拦截且 H3 fail。为验证服务错误体而主动发送的超限 probe 单独标 `intentional_over_limit=true`，只核对预测与错误响应，不得计入路由可用样本。
5. 写 `budget.json`，包含覆盖矩阵、校准参数、每条观测、边界结果和 pass/conditional/fail。

CLI `main()` 必须按以下顺序：

1. 解析参数并加载样本；`--limit 0` 表示全部，`--repeats` 只接受 2（保留该参数是为了与方案命令兼容，不翻倍完整 A/B）。
2. 用 `plan_request_budget()` 打印 `planned_requests/by_stage/max_requests`；超过上限返回 2，`--dry-run` 在预算合法时返回 0。dry-run 不读取 API key、不创建 transport、不写文件。
3. 创建输出目录和共享 transport/cache。先写 `g3-graph.json`；`all` 先跑 20 条 G1/G2，再用相同 cache 跑全量 G1/G2/G3，随后运行 H2 和 H3。
4. 分别写 `equivalence.json/capacity.json/budget.json`；最后 `summary.json` 写三项状态、`planned_requests`、`actual_requests=transport.count`、`stage_counts` 和参数（不含 key）。
5. 任一 conditional/fail、请求预算错误或执行异常返回 2；全部 pass 才返回 0。异常信息不得包含 header 或 API key。

- [ ] **Step 4: 运行 CLI、预算和现有窗口回归**

Run: `.venv/bin/python -m pytest tests/test_spike_jev_batch.py tests/test_jev_window.py -q`
Expected: 全部 PASS；dry-run 测试无网络、无产物目录。

- [ ] **Step 5: 运行全量离线测试并提交**

Run: `.venv/bin/python -m pytest -q`
Expected: 全部 PASS；已有失败若非本计划引入，记录原样，不修改无关代码。

```bash
git add tests/test_spike_jev_batch.py
git add -f scripts/spike_jev_batch.py
git commit -m "test(spike): add calibrated budget and spike CLI"
```

---

### Task 7: 人工确认后执行生产 spike 并出报告

**Files:**
- Create: `artifacts/jev-batch-spike/samples.jsonl`（忽略）
- Create: `artifacts/jev-batch-spike/{g3-graph.json,equivalence.json,capacity.json,budget.json,summary.json}`（忽略）
- Create: `docs/JEV-多问题批量-spike-报告-2026-11-05.md`

**Interfaces:**
- Consumes: 前六个任务的 CLI 和全部离线测试。
- Produces: 可审计的实际请求数、三项 pass/conditional/fail、阈值重标清单、路由差异清单和改造建议。

- [ ] **Step 1: 准备并审计样本**

从五个 `docs/案例演示-2026-11-03/evidence/case-*.json` 提取 21 条唯一文本，人工补足到 60–80 条；长度各层 15–20 条，至少 20 条带边界标签。逐条检查无真实敏感数据、id 唯一、schema 符合 Task 1。运行：

```bash
git check-ignore -v artifacts/jev-batch-spike/samples.jsonl
.venv/bin/python scripts/spike_jev_batch.py \
  --samples artifacts/jev-batch-spike/samples.jsonl --scenario all --dry-run
```

Expected: 第一行命中 `.gitignore`；第二行 `planned_requests <= 1980`（60 条为 1740，80 条为 1980），exit 0。

- [ ] **Step 2: 获得生产执行确认并做只读 smoke**

未获得明确确认前停止。确认后检查 `/health`、现行模型和 4096/2048 窗口，运行既有 `scripts/spike_systemone_graph.py`。本阶段所有 HTTP 请求合计最多 20 次，人工记录实际数为 `smoke_requests`。不得重启容器或写 DB；连续两个 5xx 时停止。

- [ ] **Step 3: 执行完整 spike**

在 76 本地或确认可直连后运行。脚本上限 1980 与 smoke 上限 20 共用全局 2000 额度：

```bash
ROUTER_JEV_API_KEY="$ROUTER_JEV_API_KEY" python3 scripts/spike_jev_batch.py \
  --url http://127.0.0.1:30104 --model d1-2B \
  --samples artifacts/jev-batch-spike/samples.jsonl \
  --scenario all --repeats 2 --limit 0 --max-requests 1980 \
  --out-dir artifacts/jev-batch-spike --timeout 20
```

Expected: 进程结束；`summary.json.actual_requests <= 1980` 且 `actual_requests + smoke_requests <= 2000`，各阶段计数与预算可解释。退出码 2 时保留产物并报告失败项，不得通过提高上限掩盖。

- [ ] **Step 4: 核验产物并撰写报告**

```bash
python3 - <<'PY'
import json
from pathlib import Path
root = Path('artifacts/jev-batch-spike')
summary = json.loads((root / 'summary.json').read_text())
assert summary['actual_requests'] <= 1980
assert summary['actual_requests'] + 20 <= 2000  # smoke 的最坏上限
assert summary['actual_requests'] == sum(summary['stage_counts'].values())
for name in ('g3-graph.json', 'equivalence.json', 'capacity.json', 'budget.json', 'summary.json'):
    assert (root / name).is_file(), name
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
```

报告必须逐项引用实际数字：max/mean Δp、每个分层翻转数/判断数及 Wilson 区间、G3 target/candidates/新增兜底、N 扫描 p95、A/B 问题每秒与完整画布 p95、CALIBRATION/SAFETY、计划请求数、脚本实际请求数、smoke 实际数及两者合计。证据不足写 conditional，不得写 pass。

- [ ] **Step 5: 最终回归并提交报告**

```bash
.venv/bin/python -m pytest -q
git status --short
git add -f docs/JEV-多问题批量-spike-报告-2026-11-05.md
git commit -m "docs(spike): record JEV batch verification results"
```

Expected: 测试无本计划引入的失败；`git status` 不含 `artifacts/`，不含对 `configs/cases/` 的新增修改或提交。

---

## Plan Self-Review Checklist

- Spec coverage：样本/图（Task 1）、传输安全（Task 2）、A/C 图语义和失败语义（Task 3）、H1 指标/边界/重复基线（Task 4）、H2 真实基线（Task 5）、H3 和 CLI（Task 6）、生产执行与报告（Task 7）。
- 请求预算：Task 4 的缓存测试验证小批不重复；Task 6 dry-run 验证 60 条 1740、80 条 1980；Task 7 验证实际计数。
- 类型一致性：缓存值固定为 `{metadata..., "response": ResponseRecord}`；`typed_answer()` 和 `replay_g3()` 只接收 `cached["response"]`，产物通过 `call_document()` 展平为 JSON 可序列化 dict。
- 图语义：只允许 `error_fallback`，不使用当前引擎已不支持的独立 `otherwise_target`。
- 无网络测试：Tasks 1–6 均使用 fake/monkeypatch；只有 Task 7 在人工确认后访问生产。
