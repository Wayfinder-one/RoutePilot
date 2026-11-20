# 计算日志详情：数值与判断过程 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 日志详情按阶段逐事件展示 JEV 返回值、阈值比较、门运算、激活与最终优先级判断，打开日志即可一眼看清计算过程。

**Architecture:** 展示契约仍由后端 `present_trace_event()` 产出（事件自描述），为每类事件补 `kind_label` 与带数值的 `summary/facts`；前端 `logs.js` 不再把一个 `phase` 压成一行，而是渲染“阶段头 + 逐事件行”，默认全部展开。图回放、快照、API、数据库均不变。

**Tech Stack:** Python 3.11+、FastAPI、pytest；原生 ES Modules、Playwright Core、Node `node:test`。

**Design source:** `docs/superpowers/specs/2026-11-20-log-computation-detail-design.md`

## Global Constraints

- 保留工作区已有改动，不做 `git reset`、整文件覆盖或无关重构。
- 不新增 API、不改数据库/快照存储、不改图回放与试算页；`router/console/trace.js` 保持原样。
- 机器字段不得丢失；`present_trace()` 保持幂等（已带 `phase` 的事件原样返回）。
- 所有展示文案与数值格式化由 `trace_presenter.py` 产出；前端只做分组与投影。
- 数值格式：概率整数百分比（四舍五入，≥.5 进位）；槽位原值/阈值 3 位小数去尾零；
  布尔小写 `true/false`；缺失 `—`；耗时 <1000ms 取整 ms，≥1000ms 用秒（2 位小数）。
- 事件行默认全部渲染，不提供折叠按钮；`false/unknown` 槽位降低对比度但必须可见。
- 现有日志 QA 语义不变：7 个 `.lg-stage`、阶段首现顺序、旧记录空状态、移动端无横向滚动。
- 不引入新依赖；`scripts/` 被 `.gitignore` 排除，QA 脚本改动不入库，只随工作区生效。

---

## File Structure

| 文件 | 职责 | 变更 |
|---|---|---|
| `router/api/trace_presenter.py` | 机器事件 → 展示字段（唯一文案/格式化层） | 新增数值格式化助手；重写初始/JEV/最终/调度等分支的 `summary/facts`；新增 `kind_label` |
| `router/api/request_presenter.py` | 记录级展示字段 | `present_result_event()` 补 `kind_label` |
| `router/console/logs.js` | 日志页分组与 DOM 投影 | `stages()` 保留阶段内全部事件；`stageHtml()` 渲染阶段头 + 逐事件行 |
| `router/console/shared.css` | 日志详情样式 | 阶段改块级容器；新增 `.lg-stage-h` / `.lg-event` 及响应式规则 |
| `router/console/i18n.js` | 双语字典 | 新增 `logs.computation`（计算过程 / Computation） |
| `tests/test_trace_presenter.py` | 展示边界单测 | 更新 `_DISPLAY_FIELDS`；新增各事件类型断言 |
| `scripts/console_qa/logs.mjs` | 真实浏览器 QA | 新增“无需点击即可看到数值与比较”用例 |

---

### Task 1: 后端数值格式化、类型标签与初始/JEV 事件

**Files:**
- Modify: `router/api/trace_presenter.py`
- Modify: `tests/test_trace_presenter.py`

**Interfaces:**
- Produces: `_number(value) -> str`、`_percent(value) -> str`、`_ms(value) -> str`、
  `_condition(value) -> str`、`_status_text(status) -> str`、
  `_slot_expression(raw, condition, status, missing) -> str`、
  `_input_fact(input: dict) -> list`、`_probabilities(ports: dict) -> str`、
  `_candidate_label(candidate) -> str`、`_candidate_fact(candidate, winner) -> list`。
- Produces: `present_trace_event()` 返回的每个事件都带
  `kind_label: {"zh": str, "en": str}`。
- 说明：`_candidate_label` / `_candidate_fact` 在本任务定义，Task 2 使用。

- [ ] **Step 1: 写失败测试（辅助函数 + 初始/JEV 事件）**

在 `tests/test_trace_presenter.py` 顶部把展示字段集合改为：

```python
_DISPLAY_FIELDS = {"phase", "label", "cell", "summary", "facts", "tone", "kind_label"}
```

在 `test_present_trace_enriches_events_without_losing_machine_fields` 的循环里补：

```python
        assert event["kind_label"]["zh"] and event["kind_label"]["en"]
```

文件末尾追加：

```python
def test_present_trace_formats_time_window_and_condition_values():
    events = [
        {"event": "time", "active_periods": ["peak", "normal"], "timezone": "Asia/Shanghai"},
        {"event": "window", "tokens_before": 18, "tokens_after": 18, "truncated": False},
        {"event": "condition", "node_id": "cond-text", "source": "text",
         "matched": ["10kV", "跳闸"], "value": True},
    ]
    time_event, window_event, condition_event = present_trace(events)
    assert time_event["kind_label"] == {"zh": "时段", "en": "Time"}
    assert time_event["summary"]["zh"] == "时段：Asia/Shanghai，生效 peak、normal"
    assert time_event["facts"][1] == [
        {"zh": "生效时段", "en": "Active periods"}, {"zh": "peak、normal", "en": "peak, normal"}]
    assert window_event["kind_label"] == {"zh": "窗口", "en": "Window"}
    assert window_event["summary"]["zh"] == "输入窗口：18 → 18 tok"
    assert window_event["facts"][0] == [{"zh": "Token", "en": "Tokens"}, "18 → 18"]
    assert condition_event["kind_label"] == {"zh": "条件", "en": "Condition"}
    assert condition_event["summary"]["zh"] == "条件 cond-text：命中，匹配 10kV、跳闸"
    assert condition_event["facts"][1] == [
        {"zh": "匹配", "en": "Matched"}, {"zh": "10kV、跳闸", "en": "10kV, 跳闸"}]


def test_present_trace_formats_question_values_with_percentages():
    event = {
        "event": "question", "wave": 1, "node_id": "capability",
        "selected": "strong", "p_top": 0.84, "margin": 0.72, "duration_ms": 9.0,
        "ports": {
            "prob.strong": 0.84, "prob.easy": 0.16,
            "selected.strong": True, "selected.easy": False,
            "p_top": 0.84, "margin": 0.72,
        },
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["kind_label"] == {"zh": "问题", "en": "Question"}
    assert presented["summary"]["zh"] == "问题 capability：strong（p_top 84%，margin 72%）"
    assert presented["summary"]["en"] == "Question capability: strong (p_top 84%, margin 72%)"
    assert presented["facts"][0] == [
        {"zh": "概率", "en": "Probabilities"}, "strong 84% · easy 16%"]
    assert presented["facts"][1] == [{"zh": "耗时", "en": "Duration"}, "9 ms"]


def test_present_trace_formats_noul_and_score_questions():
    noul = present_trace([{
        "event": "question", "wave": 1, "node_id": "domain", "duration_ms": 7.0,
        "ports": {"yes": 0.92, "no": 0.08},
    }], has_time_period=False)[0]
    assert noul["summary"]["zh"] == "问题 domain：P(yes) 92%，P(no) 8%"
    assert noul["facts"][0][1] == "yes 92% · no 8%"

    score = present_trace([{
        "event": "question", "wave": 1, "node_id": "complexity", "duration_ms": 4.0,
        "ports": {"value": 0.7},
    }], has_time_period=False)[0]
    assert score["summary"]["zh"] == "问题 complexity：value 0.7"
    assert score["facts"][0] == [{"zh": "值", "en": "Value"}, "0.7"]


def test_present_trace_formats_slot_comparison_and_boolean_passthrough():
    numeric = present_trace([{
        "event": "slot", "wave": 1, "edge_id": "e1", "node_id": "power-target",
        "port": "in_1", "source": "capability", "source_port": "selected.strong",
        "raw": 0.84, "condition": {"op": "gt", "value": 0.65}, "status": "true",
    }], has_time_period=False)[0]
    assert numeric["kind_label"] == {"zh": "槽位", "en": "Slot"}
    assert numeric["summary"]["zh"] == "capability.selected.strong 0.84 > 0.65 → true"
    assert numeric["facts"] == [[{"zh": "目标", "en": "Target"}, "power-target.in_1"]]

    boolean = present_trace([{
        "event": "slot", "wave": 0, "edge_id": "e2", "node_id": "q", "port": "in_1",
        "source": "start", "source_port": "out", "raw": True, "condition": None,
        "status": "true",
    }], has_time_period=False)[0]
    assert boolean["summary"]["zh"] == "start.out true → true"

    invalid = present_trace([{
        "event": "slot", "wave": 1, "edge_id": "e3", "node_id": "t", "port": "in_1",
        "source": "q", "source_port": "value", "raw": 0.5, "condition": None,
        "status": "invalid",
    }], has_time_period=False)[0]
    assert invalid["summary"]["zh"] == "q.value 0.5 （未配置阈值） → invalid"
    assert invalid["summary"]["en"] == "q.value 0.5 (no threshold) → invalid"


def test_present_trace_formats_gate_operator_and_inputs():
    event = {
        "event": "gate", "wave": 1, "node_id": "and-power", "operator": "and",
        "value": True,
        "inputs": [
            {"port": "in_1", "raw": 0.84, "condition": {"op": "gt", "value": 0.65},
             "status": "true"},
            {"port": "in_2", "raw": 0.92, "condition": {"op": "gt", "value": 0.7},
             "status": "true"},
        ],
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["kind_label"] == {"zh": "门", "en": "Gate"}
    assert presented["summary"]["zh"] == "门 and-power：true AND true → true"
    assert presented["facts"] == [
        [{"zh": "in_1", "en": "in_1"}, "0.84 > 0.65 → true"],
        [{"zh": "in_2", "en": "in_2"}, "0.92 > 0.7 → true"],
    ]


def test_present_trace_formats_wave_duration_and_activation():
    wave = present_trace([{
        "event": "wave", "wave": 1, "questions": ["capability", "domain"],
        "duration_ms": 9.0, "failed": False, "error": None,
    }], has_time_period=False)[0]
    assert wave["kind_label"] == {"zh": "轮次", "en": "Wave"}
    assert wave["summary"]["zh"] == "第 1 轮 JEV：2 个问题，耗时 9 ms"

    activate = present_trace([{
        "event": "activate", "wave": 1, "node_id": "power-target", "via_port": "in_1",
    }], has_time_period=False)[0]
    assert activate["kind_label"] == {"zh": "激活", "en": "Activation"}
    assert activate["summary"]["zh"] == "激活 power-target（via in_1）"
    assert activate["facts"] == [
        [{"zh": "节点", "en": "Node"}, "power-target"],
        [{"zh": "入口", "en": "Input"}, "in_1"],
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_trace_presenter.py -q`
Expected: FAIL，`KeyError: 'kind_label'` 或断言不匹配。

- [ ] **Step 3: 实现辅助函数**

在 `router/api/trace_presenter.py` 顶部补 `import math`（放在 `from __future__` 之后），
并在 `_fact` 之后插入：

```python
_DASH = "—"
_OP_SYMBOLS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "=", "neq": "≠", "is": "="}
def _number(value) -> str:
    """Trim a scalar for an expression line (3 decimals, no trailing zeros)."""
    if value is None or value == "":
        return _DASH
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        rounded = round(float(value), 3)
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _percent(value) -> str:
    """0.84 -> '84%'; rounds halves up, never shows 'true'/'false'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DASH
    if not math.isfinite(float(value)):
        return _DASH
    return f"{int(math.floor(value * 100 + 0.5))}%"


def _ms(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DASH
    if not math.isfinite(float(value)):
        return _DASH
    if value >= 1000:
        return f"{value / 1000:.2f} s"
    return f"{int(math.floor(value + 0.5))} ms"


def _condition(condition) -> str:
    """'gt', 0.65 -> '> 0.65'; unknown/missing operators render as ''."""
    if not isinstance(condition, dict):
        return ""
    symbol = _OP_SYMBOLS.get(condition.get("op"))
    if symbol is None:
        return ""
    return f"{symbol} {_number(condition.get('value'))}"


def _status_text(status) -> str:
    text = str(status if status is not None else "unknown")
    return text


def _slot_expression(raw, condition, status, missing: str) -> str:
    """'0.84 > 0.65 -> true'; booleans pass through; numbers need a threshold."""
    left = _number(raw)
    if isinstance(raw, bool):
        body = left if not condition else f"{left} {_condition(condition)}"
    elif condition:
        body = f"{left} {_condition(condition)}"
    else:
        body = f"{left} {missing}"
    return f"{body} → {_status_text(status)}"


def _input_fact(input: dict) -> list:
    """One gate-input fact: label is the slot port; prose stays bilingual."""
    port = str(input.get("port") or _DASH)
    raw, condition, status = input.get("raw"), input.get("condition"), input.get("status")
    zh = _slot_expression(raw, condition, status, "（未配置阈值）")
    en = _slot_expression(raw, condition, status, "(no threshold)")
    return _fact(port, port, zh if zh == en else _text(zh, en))


def _probabilities(ports: dict) -> str:
    entries = [
        (str(key)[5:], value) for key, value in ports.items()
        if str(key).startswith("prob.") and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ]
    entries.sort(key=lambda item: (-float(item[1]), item[0]))
    return " · ".join(f"{name} {_percent(value)}" for name, value in entries) or _DASH


def _candidate_label(candidate) -> str:
    if not isinstance(candidate, dict):
        return str(candidate)
    return f"{candidate.get('target_id')} p{_number(candidate.get('priority'))}"


def _candidate_fact(candidate, winner: str) -> list:
    if not isinstance(candidate, dict):
        name = str(candidate)
        return _fact(name, name, name)
    target = str(candidate.get("target_id"))
    via = f"{candidate.get('via_node')}.{candidate.get('via_port')}"
    priority = _number(candidate.get("priority"))
    wave = _number(candidate.get("wave"))
    mark = " ✓" if target == winner else ""
    return _fact(target, target, _text(
        f"p{priority} · via {via} · 第 {wave} 轮{mark}",
        f"p{priority} · via {via} · wave {wave}{mark}",
    ))
```

- [ ] **Step 4: 重写初始/JEV 分支**

在 `present_trace_event()` 的默认值块里加一行：

```python
    event_kind = _text("事件", "Event")
```

把从 `if kind == "time":` 到 `else:  # activate` 结束的整段（到
`elif kind == "fallback":` 之前）替换为：

```python
    if kind == "time":
        event_kind = _text("时段", "Time")
        if has_time_period:
            phase, label = "period", _text("时段判断", "Time period")
        names = [str(name) for name in event.get("active_periods") or []]
        timezone = str(event.get("timezone") or _DASH)
        joined = "、".join(names)
        cell = _text(joined or "未命中", ", ".join(names) or "No match")
        if names:
            summary = _text(f"时段：{timezone}，生效 {joined}",
                            f"Time: {timezone}, active {', '.join(names)}")
            tone = "hit"
        else:
            summary = _text(f"时段：{timezone}，未命中",
                            f"Time: {timezone}, no active period")
        facts = [
            _fact("时区", "Timezone", timezone),
            _fact("生效时段", "Active periods",
                  _text(joined, ", ".join(names)) if names else _DASH),
        ]
    elif kind == "window":
        event_kind = _text("窗口", "Window")
        window = f"{_number(event.get('tokens_before'))} → {_number(event.get('tokens_after'))}"
        truncated = bool(event.get("truncated"))
        cell = _text("输入已截断", "Input truncated") if truncated \
            else _text("输入就绪", "Input ready")
        summary = (
            _text(f"输入窗口：{window} tok（已截断）",
                  f"Input window: {window} tok (truncated)")
            if truncated else
            _text(f"输入窗口：{window} tok", f"Input window: {window} tok")
        )
        facts = [
            _fact("Token", "Tokens", window),
            _fact("截断", "Truncated", _text("是", "Yes") if truncated else _text("否", "No")),
        ]
        tone = "warn" if truncated else ""
    elif kind == "condition":
        event_kind = _text("条件", "Condition")
        if event.get("source") == "periods" and has_time_period:
            phase, label = "period", _text("时段判断", "Time period")
        else:
            phase, label = "condition", _text("条件判断", "Conditions")
        node = str(event.get("node_id"))
        matched = [str(value) for value in event.get("matched") or []]
        hit = bool(event.get("value"))
        cell = _text("命中" if hit else "未命中", "Matched" if hit else "No match")
        if hit and matched:
            summary = _text(f"条件 {node}：命中，匹配 {'、'.join(matched)}",
                            f"Condition {node}: matched {', '.join(matched)}")
        elif hit:
            summary = _text(f"条件 {node}：命中", f"Condition {node}: matched")
        else:
            summary = _text(f"条件 {node}：未命中", f"Condition {node}: no match")
        facts = [
            _fact("节点", "Node", node),
            _fact("匹配", "Matched",
                  _text("、".join(matched), ", ".join(matched)) if matched else _DASH),
            _fact("结果", "Result", "true" if hit else "false"),
        ]
        tone = "hit" if hit else ""
    elif kind in {"gate", "slot", "activate", "wave", "question"}:
        phase, label = _phase(event["wave"])
        wave = event["wave"]
        if kind == "wave":
            event_kind = _text("轮次", "Wave")
            failed = event["failed"]
            questions = event.get("questions") or []
            duration = _ms(event.get("duration_ms"))
            cell = _text("调用失败" if failed else "调用完成",
                         "Call failed" if failed else "Call complete")
            if failed:
                summary = _text(f"第 {wave} 轮 JEV 失败：{event.get('error') or _DASH}",
                                f"JEV wave {wave} failed: {event.get('error') or _DASH}")
            else:
                summary = _text(
                    f"第 {wave} 轮 JEV：{len(questions)} 个问题，耗时 {duration}",
                    f"JEV wave {wave}: {len(questions)} questions, {duration}")
            facts = [
                _fact("轮次", "Wave", str(wave)),
                _fact("问题数", "Questions", str(len(questions))),
                _fact("耗时", "Duration", duration),
            ]
            tone = "bad" if failed else "hit"
        elif kind == "question":
            event_kind = _text("问题", "Question")
            node = str(event.get("node_id"))
            ports = event.get("ports") if isinstance(event.get("ports"), dict) else {}
            selected = event.get("selected")
            if selected is None:
                selected = next((
                    str(key)[9:] for key, value in ports.items()
                    if str(key).startswith("selected.") and value is True
                ), None)
            p_top = event.get("p_top", ports.get("p_top"))
            margin = event.get("margin", ports.get("margin"))
            duration = _ms(event.get("duration_ms"))
            if selected is not None:
                summary = _text(
                    f"问题 {node}：{selected}（p_top {_percent(p_top)}，margin {_percent(margin)}）",
                    f"Question {node}: {selected} (p_top {_percent(p_top)}, "
                    f"margin {_percent(margin)})",
                )
                facts = [_fact("概率", "Probabilities", _probabilities(ports))]
            elif "yes" in ports or "no" in ports:
                summary = _text(
                    f"问题 {node}：P(yes) {_percent(ports.get('yes'))}，"
                    f"P(no) {_percent(ports.get('no'))}",
                    f"Question {node}: P(yes) {_percent(ports.get('yes'))}, "
                    f"P(no) {_percent(ports.get('no'))}",
                )
                facts = [_fact("概率", "Probabilities",
                               f"yes {_percent(ports.get('yes'))} · "
                               f"no {_percent(ports.get('no'))}")]
            else:
                value = _number(ports.get("value"))
                summary = _text(f"问题 {node}：value {value}",
                                f"Question {node}: value {value}")
                facts = [_fact("值", "Value", value)]
            facts += [_fact("耗时", "Duration", duration)]
            cell = _text("已回答", "Answered")
            tone = "hit"
        elif kind == "gate":
            event_kind = _text("门", "Gate")
            node = str(event.get("node_id"))
            operator = str(event.get("operator") or "").upper() or _DASH
            inputs = event.get("inputs") if isinstance(event.get("inputs"), list) else []
            statuses = [_status_text(input.get("status")) for input in inputs]
            value = "true" if event.get("value") else "false"
            joined = f" {operator} ".join(statuses)
            summary = _text(
                f"门 {node}：{joined} → {value}" if joined
                else f"门 {node}：{operator} → {value}",
                f"Gate {node}: {joined} → {value}" if joined
                else f"Gate {node}: {operator} → {value}",
            )
            facts = [_input_fact(input) for input in inputs] or [
                _fact("运算", "Operator", operator)]
            cell = _text("通过" if event.get("value") else "未通过",
                         "True" if event.get("value") else "False")
            tone = "hit" if event.get("value") else ""
        elif kind == "slot":
            event_kind = _text("槽位", "Slot")
            source = f"{event.get('source')}.{event.get('source_port')}"
            raw, condition, status = (
                event.get("raw"), event.get("condition"), event.get("status"))
            summary = _text(
                f"{source} {_slot_expression(raw, condition, status, '（未配置阈值）')}",
                f"{source} {_slot_expression(raw, condition, status, '(no threshold)')}",
            )
            facts = [_fact("目标", "Target",
                           f"{event.get('node_id')}.{event.get('port')}")]
            cell = _text("通过" if status == "true" else "未通过",
                         "True" if status == "true" else _status_text(status))
            tone = "hit" if status == "true" else ""
        else:  # activate
            event_kind = _text("激活", "Activation")
            node = str(event.get("node_id"))
            port = str(event.get("via_port"))
            summary = _text(f"激活 {node}（via {port}）", f"Activated {node} (via {port})")
            facts = [_fact("节点", "Node", node), _fact("入口", "Input", port)]
            cell = _text("已激活", "Activated")
            tone = "hit"
```

在返回字典里补：

```python
        "kind_label": event_kind,
```

- [ ] **Step 5: 运行本任务测试**

Run: `.venv/bin/python -m pytest tests/test_trace_presenter.py -q`
Expected: PASS（旧 `final`/`scheduled` 用例仍走旧分支，`kind_label` 为默认 `事件/Event`）。

- [ ] **Step 6: 提交（仅在用户要求提交时执行）**

```bash
git add router/api/trace_presenter.py tests/test_trace_presenter.py
git commit -m "feat(logs): format initial and JEV trace values"
```

---

### Task 2: 后端最终/调度/失败事件与结果标签

**Files:**
- Modify: `router/api/trace_presenter.py`
- Modify: `router/api/request_presenter.py`
- Modify: `tests/test_trace_presenter.py`

**Interfaces:**
- Consumes: Task 1 的 `_number/_percent/_ms/_candidate_label/_candidate_fact/_DASH/_text/_fact`。
- Produces: `fallback/final/schedule/sticky_hit/scheduled/routing_error` 分支的
  `kind_label/summary/facts`；`present_result_event()` 的 `kind_label={"zh":"结果","en":"Outcome"}`。

- [ ] **Step 1: 写失败测试（最终/调度/失败/结果）**

在 `tests/test_trace_presenter.py` 末尾追加：

```python
def test_present_trace_formats_final_priority_decision():
    event = {
        "event": "final", "target_id": "power-target", "kind": "target", "priority": 5,
        "candidates": [
            {"target_id": "power-target", "priority": 5, "via_node": "and-power",
             "via_port": "in_1", "wave": 1},
            {"target_id": "strong-target", "priority": 3, "via_node": "capability",
             "via_port": "in_1", "wave": 1},
        ],
    }
    presented = present_trace([event], has_time_period=False)[0]
    assert presented["kind_label"] == {"zh": "候选", "en": "Candidates"}
    assert presented["summary"]["zh"] == (
        "候选：power-target p5、strong-target p3 → power-target 胜出")
    assert presented["facts"] == [
        [{"zh": "power-target", "en": "power-target"},
         {"zh": "p5 · via and-power.in_1 · 第 1 轮 ✓",
          "en": "p5 · via and-power.in_1 · wave 1 ✓"}],
        [{"zh": "strong-target", "en": "strong-target"},
         {"zh": "p3 · via capability.in_1 · 第 1 轮",
          "en": "p3 · via capability.in_1 · wave 1"}],
    ]


def test_present_trace_formats_fallback_and_no_candidate_final():
    fallback = present_trace([{
        "event": "fallback", "target_id": "safe-target", "reason": "timeout",
    }], has_time_period=False)[0]
    assert fallback["summary"]["zh"] == "JEV 失败，启用错误兜底 safe-target（timeout）"
    assert fallback["facts"] == [
        [{"zh": "目标", "en": "Target"}, "safe-target"],
        [{"zh": "原因", "en": "Reason"}, "timeout"],
    ]

    final = present_trace([{
        "event": "final", "target_id": "safe-target", "kind": "otherwise",
        "priority": 1, "candidates": [],
    }], has_time_period=False)[0]
    assert final["summary"]["zh"] == "无候选，使用兜底目标 safe-target"
    assert final["facts"][0] == [{"zh": "目标", "en": "Target"}, "safe-target"]


def test_present_trace_formats_schedule_and_scheduled_values():
    schedule = present_trace([{
        "event": "schedule", "target_id": "power-target", "strategy": "least_inflight",
        "configured_models": ["qwen3-8b", "qwen3-32b"],
        "registered_models": ["qwen3-8b", "qwen3-32b"],
        "available_models": ["qwen3-8b"],
        "unavailable": {"qwen3-32b": "breaker_open"},
        "inflight": {"qwen3-8b": 1},
        "sticky": {"status": "miss", "preferred_model": "qwen3-8b"},
    }], has_time_period=False)[0]
    assert schedule["kind_label"] == {"zh": "调度", "en": "Schedule"}
    assert schedule["summary"]["zh"] == "目标 power-target：1 个可用模型，策略 least_inflight"
    assert schedule["facts"][0] == [
        {"zh": "可用模型", "en": "Available models"}, "qwen3-8b"]
    assert schedule["facts"][2] == [{"zh": "在途", "en": "In flight"}, "qwen3-8b: 1"]
    assert schedule["facts"][3] == [
        {"zh": "会话粘性", "en": "Session affinity"}, "miss → qwen3-8b"]

    scheduled = present_trace([{
        "event": "scheduled", "model": "qwen3-8b", "reason": "target", "dry_run": False,
    }], has_time_period=False)[0]
    assert scheduled["summary"]["zh"] == "最终调度 qwen3-8b（target）"
    assert scheduled["facts"] == [
        [{"zh": "模型", "en": "Model"}, "qwen3-8b"],
        [{"zh": "原因", "en": "Reason"}, "target"],
    ]


def test_present_result_event_has_outcome_kind_label():
    outcome = present_result_event({
        "outcome_state": "completed", "model": "qwen3-8b",
        "upstream_status": 200, "total_ms": 12.0,
    })
    assert outcome["kind_label"] == {"zh": "结果", "en": "Outcome"}
```

同时把原有 `test_present_trace_renders_routing_failure_event` 的 facts 断言替换为：

```python
    assert presented["facts"] == [
        [{"zh": "阶段", "en": "Stage"}, "schedule"],
        [{"zh": "原因", "en": "Reason"}, "no_available_backend"],
        [{"zh": "消息", "en": "Message"}, "none"],
    ]
    assert presented["kind_label"] == {"zh": "错误", "en": "Error"}
```

把同文件顶部的 import 改为：

```python
from router.api.request_presenter import present_record, present_result_event
from router.api.trace_presenter import present_trace
```

并把旧用例 `test_present_trace_enriches_events_without_losing_machine_fields`
里的 `final` 测试事件改成真实候选结构：

```python
        {"event": "final", "target_id": "safe", "kind": "target",
         "priority": 1,
         "candidates": [{"target_id": "safe", "priority": 1,
                         "via_node": "start", "via_port": "in_1", "wave": 0}]},
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_trace_presenter.py -q`
Expected: FAIL，`final/schedule/routing_error` 的 summary/facts 与 `kind_label` 仍为旧值。

- [ ] **Step 3: 实现最终/调度/失败分支**

把 `present_trace_event()` 中从 `elif kind == "fallback":` 到
`elif kind == "routing_error":` 分支结束（返回字典之前）的整段替换为：

```python
    elif kind == "fallback":
        event_kind = _text("兜底", "Fallback")
        phase, label = "fallback", _text("兜底路径", "Fallback")
        target = str(event.get("target_id"))
        reason = event.get("reason")
        cell = _text("已启用", "Used")
        summary = _text(f"JEV 失败，启用错误兜底 {target}（{reason or _DASH}）",
                        f"JEV failed; using error fallback {target} ({reason or _DASH})")
        facts = [
            _fact("目标", "Target", target),
            _fact("原因", "Reason", reason or _DASH),
        ]
        tone = "warn"
    elif kind == "final":
        event_kind = _text("候选", "Candidates")
        phase, label = "final", _text("目标判定", "Target selected")
        target = str(event.get("target_id"))
        candidates = event.get("candidates") if isinstance(event.get("candidates"), list) else []
        fallback = event.get("kind") != "target"
        cell = _text("兜底目标" if fallback else "目标命中",
                     "Fallback target" if fallback else "Target matched")
        if candidates:
            parts = "、".join(_candidate_label(candidate) for candidate in candidates)
            summary = _text(f"候选：{parts} → {target} 胜出",
                            f"Candidates: {', '.join(_candidate_label(c) for c in candidates)}"
                            f" → {target} wins")
            facts = [_candidate_fact(candidate, target) for candidate in candidates]
        else:
            summary = _text(f"无候选，使用兜底目标 {target}",
                            f"No candidates; using fallback target {target}")
            facts = [
                _fact("目标", "Target", target),
                _fact("优先级", "Priority", _number(event.get("priority"))),
                _fact("候选数", "Candidates", "0"),
            ]
        tone = "warn" if fallback else "hit"
    elif kind == "schedule":
        event_kind = _text("调度", "Schedule")
        phase, label = "schedule", _text("模型调度", "Model scheduling")
        target = str(event.get("target_id"))
        available = [str(model) for model in event.get("available_models") or []]
        configured = [str(model) for model in event.get("configured_models") or []]
        strategy = str(event.get("strategy") or _DASH)
        cell = _text("有可用模型" if available else "无可用模型",
                     "Available" if available else "Unavailable")
        summary = _text(
            f"目标 {target}：{len(available)} 个可用模型，策略 {strategy}",
            f"Target {target}: {len(available)} available models, strategy {strategy}",
        )
        facts = [_fact("可用模型", "Available models", ", ".join(available) or _DASH)]
        if configured:
            facts.append(_fact("配置模型", "Configured models", ", ".join(configured)))
        inflight = event.get("inflight") if isinstance(event.get("inflight"), dict) else {}
        if inflight:
            facts.append(_fact("在途", "In flight",
                               ", ".join(f"{model}: {count}"
                                         for model, count in inflight.items())))
        sticky = event.get("sticky") if isinstance(event.get("sticky"), dict) else {}
        status = sticky.get("status")
        if status not in (None, "disabled", "no_session", "not_checked"):
            preferred = sticky.get("preferred_model")
            facts.append(_fact("会话粘性", "Session affinity",
                               f"{status} → {preferred}" if preferred else str(status)))
        tone = "" if available else "bad"
    elif kind == "sticky_hit":
        event_kind = _text("粘性", "Affinity")
        phase, label = "sticky", _text("会话粘性", "Session affinity")
        model = str(event.get("model"))
        cell = _text("命中", "Hit")
        summary = _text(f"命中会话粘性：复用 {model}",
                        f"Session affinity hit: reusing {model}")
        facts = [_fact("模型", "Model", model)]
        tone = "hit"
    elif kind == "scheduled":
        event_kind = _text("调度", "Schedule")
        phase, label = "schedule", _text("模型调度", "Model scheduling")
        model = str(event.get("model"))
        reason = event.get("reason")
        cell = model
        summary = _text(f"最终调度 {model}（{reason or _DASH}）",
                        f"Scheduled {model} ({reason or _DASH})")
        facts = [_fact("模型", "Model", model), _fact("原因", "Reason", reason or _DASH)]
        tone = "hit"
    elif kind == "routing_error":
        event_kind = _text("错误", "Error")
        phase, label = "routing_error", _text("路由中止", "Routing stopped")
        stage = str(event.get("stage") or _DASH)
        error_class = str(event.get("error_class") or _DASH)
        message = str(event.get("message") or _DASH)
        cell = _text("路由失败", "Routing failed")
        summary = _text(f"路由失败（{stage}）：{error_class}",
                        f"Routing failed ({stage}): {error_class}")
        facts = [
            _fact("阶段", "Stage", stage),
            _fact("原因", "Reason", error_class),
            _fact("消息", "Message", message),
        ]
        tone = "bad"
```

在 `router/api/request_presenter.py` 的 `present_result_event()` 返回字典里补：

```python
        "kind_label": _label("结果", "Outcome"),
```

- [ ] **Step 4: 运行本任务测试**

Run: `.venv/bin/python -m pytest tests/test_trace_presenter.py -q`
Expected: PASS。

- [ ] **Step 5: 跑相关后端回归**

Run: `.venv/bin/python -m pytest tests/test_trace_presenter.py tests/test_v1.py tests/test_admin.py tests/test_routes_v1_reasoning.py -q`
Expected: PASS（所有使用 `facts` 的旧断言已随本任务更新）。

- [ ] **Step 6: 提交（仅在用户要求提交时执行）**

```bash
git add router/api/trace_presenter.py router/api/request_presenter.py tests/test_trace_presenter.py
git commit -m "feat(logs): format final and schedule trace values"
```

---

### Task 3: 日志页逐事件渲染与样式

**Files:**
- Modify: `router/console/logs.js`
- Modify: `router/console/shared.css`
- Modify: `router/console/i18n.js`
- Test: `scripts/console_qa/logs.mjs`

**Interfaces:**
- Consumes: `trace_presenter` 产出的 `event.kind_label`、`event.summary`、`event.facts`、`event.tone`。
- Produces: `.lg-stage` 内新增 `.lg-stage-h` 与 `.lg-event`；表头 `logs.computation`。

- [ ] **Step 1: 写失败的真实浏览器用例**

在 `scripts/console_qa/logs.mjs` 的 `calculation-log mobile keeps the main conclusion within the viewport`
之前插入：

```javascript
test('calculation-log shows JEV values and comparisons without clicking a stage', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
  });
  await page.goto(routerBase + '/console/logs.html');
  await page.locator('.lg-row').first().click();
  await page.waitForSelector('.lg-item.open .lg-rail');

  const open = page.locator('.lg-item.open');
  const rail = open.locator('.lg-rail');
  const text = (await rail.innerText()).replace(/\s+/g, ' ');
  /* 不点任何阶段：事件行必须已经在 DOM 里。 */
  assert.ok(await open.locator('.lg-event').count() > 7,
    'the detail still collapses events into one row per phase');
  assertIncludes(text, '0.84 > 0.65 → true');
  assertIncludes(text, '0.92 > 0.7 → true');
  assertIncludes(text, 'p_top 84%');
  assertIncludes(text, 'strong 84%');
  assertIncludes(text, 'AND');
  assertIncludes(text, 'power-target p5');
  assertIncludes(text, 'strong-target p3');
  assertIncludes(text, 'power-target 胜出');
  assertIncludes(text, '→ false');

  const kinds = await open.locator('.lg-event-kind').allInnerTexts();
  assert.ok(kinds.includes('槽位'), 'event rows have no type label');
  assert.ok(kinds.includes('问题'), 'question rows are missing');
  assert.ok(kinds.includes('门'), 'gate rows are missing');
});
```

- [ ] **Step 2: 运行用例确认失败**

Run: `QA_ONLY='without clicking a stage' node scripts/console_qa/logs.mjs`
Expected: FAIL，`.lg-event` 数量为 0。

- [ ] **Step 3: 修改 `logs.js`**

`stages()` 改为（保留原有最后一条聚合，新增 `events`）：

```javascript
function stages(record) {
  const groups = new Map();
  events(record).forEach(event => {
    /* The API presenter attaches phase/label/cell/summary/facts/kind_label to
       every trace event; a raw machine event here means "no displayed stage". */
    if (!event.phase) return;
    const id = String(event.phase);
    if (!groups.has(id)) groups.set(id, { id, label: event.label ?? id, cell: '—',
      summary: '—', facts: [], tone: '', count: 0, events: [] });
    const group = groups.get(id);
    group.count += 1;
    group.events.push(event);
    if (event.label != null) group.label = event.label;
    if (event.cell != null && event.cell !== '') group.cell = event.cell;
    if (event.summary != null && event.summary !== '') group.summary = event.summary;
    if (Array.isArray(event.facts) && event.facts.length) group.facts = event.facts.slice(0, 3);
    const tone = Object.prototype.hasOwnProperty.call(TONES, event.tone) ? event.tone : '';
    if (TONES[tone] > TONES[group.tone]) group.tone = tone;
  });
  return [...groups.values()];
}
```

在 `stageHtml` 之前新增 `eventHtml`，并把 `stageHtml` 替换为：

```javascript
function eventHtml(event) {
  const tone = Object.prototype.hasOwnProperty.call(TONES, event.tone) ? event.tone : '';
  const status = String(event.status ?? '');
  const muted = event.event === 'slot' && (status === 'false' || status === 'unknown');
  const kind = event.kind_label != null ? safe(event.kind_label) : '';
  const facts = Array.isArray(event.facts) ? event.facts : [];
  return '<div class="lg-event ' + tone + (muted ? ' is-muted' : '') + '">' +
    '<div class="lg-rail-cell"></div><div class="lg-event-spacer"></div>' +
    '<div class="lg-event-main">' +
      (kind ? '<span class="lg-event-kind">' + kind + '</span>' : '') +
      safe(event.summary) + '</div>' +
    '<div class="lg-evidence">' + (pairsHtml(facts, 'lg-evidence-pair') || '—') + '</div>' +
    '<div class="lg-event-spacer"></div></div>';
}

function stageHtml(stage, index) {
  const branch = stage.tone === 'warn' || stage.tone === 'bad' ? ' branch' : '';
  const rows = (stage.events || []).map(eventHtml).join('');
  return '<div class="lg-stage ' + stage.tone + branch + '">' +
    '<div class="lg-stage-h">' +
      '<div class="lg-rail-cell"><span class="lg-pin">' + (index + 1) + '</span></div>' +
      '<div class="lg-stage-name"><b>' + safe(stage.label) + '</b><small>' + safe(stage.id) +
      ' · ' + esc(tf('logs.eventCount', {count: stage.count})) + '</small></div>' +
      '<div class="lg-stage-spacer"></div><div class="lg-stage-spacer"></div>' +
      '<div class="lg-stage-count">' + stage.count + '</div>' +
    '</div>' + rows + '</div>';
}
```

`detailHtml()` 表头把 `logs.summary` 改为 `logs.computation`：

```javascript
    '<div class="lg-rail-head" aria-hidden="true"><span></span><span>' + esc(t('logs.stage')) +
    '</span><span>' + esc(t('logs.computation')) + '</span><span>' + esc(t('logs.evidence')) +
    '</span><span>' + esc(t('logs.events')) + '</span></div>' +
```

- [ ] **Step 4: 修改 `i18n.js`**

在 zh 的 `'logs.stage'` 一行后加：

```javascript
    'logs.computation': '计算过程',
```

在 en 的 `'logs.stage'` 一行后加：

```javascript
    'logs.computation': 'Computation',
```

- [ ] **Step 5: 修改 `shared.css`**

把下面这行：

```css
.lg-rail-head,.lg-stage{display:grid;grid-template-columns:42px 150px minmax(130px,1fr) minmax(180px,1.6fr) 54px}
```

替换为：

```css
.lg-rail-head,.lg-stage-h,.lg-event{display:grid;grid-template-columns:42px 150px minmax(130px,1fr) minmax(180px,1.6fr) 54px}
```

把下面这段：

```css
.lg-stage{min-height:42px;border-bottom:1px solid var(--line)}
.lg-stage:last-child{border-bottom:0}
.lg-stage>div{padding:6px 9px;min-width:0;border-right:1px solid var(--line)}
.lg-stage>div:last-child{border-right:0}
.lg-rail-cell{position:relative;padding:0!important}
.lg-rail-cell::before{content:"";position:absolute;left:17px;top:0;bottom:0;width:2px;background:var(--indigo-line)}
.lg-stage:first-of-type .lg-rail-cell::before{top:19px}
.lg-stage:last-child .lg-rail-cell::before{bottom:calc(100% - 19px)}
```

替换为：

```css
.lg-stage{position:relative;border-bottom:1px solid var(--line)}
.lg-stage:last-child{border-bottom:0}
.lg-stage::before{content:"";position:absolute;left:17px;top:0;bottom:0;width:2px;background:var(--indigo-line)}
.lg-rail-head + .lg-stage::before{top:19px}
.lg-stage-h{min-height:42px}
.lg-stage-h>div,.lg-event>div{padding:6px 9px;min-width:0;border-right:1px solid var(--line)}
.lg-stage-h>div:last-child,.lg-event>div:last-child{border-right:0}
.lg-event{border-top:1px dashed var(--line)}
.lg-event>div{padding:4px 9px}
.lg-rail-cell{position:relative;padding:0!important}
.lg-stage .lg-rail-cell::before{display:none}
.lg-event-main{font:500 10.5px var(--mono);line-height:1.55;overflow-wrap:anywhere;color:var(--text-2)}
.lg-event-kind{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;
  background:var(--panel-2);color:var(--text-3);font:700 8.5px var(--sans);vertical-align:1px}
.lg-event.warn .lg-event-main{color:var(--amber)}
.lg-event.bad .lg-event-main{color:var(--red)}
.lg-event.is-muted{opacity:.62}
.lg-event .lg-evidence{white-space:normal;overflow:visible;overflow-wrap:anywhere}
.lg-event .lg-evidence-pair{max-width:none;margin-right:10px;white-space:normal;overflow-wrap:anywhere}
```

把 `@media(max-width:1100px)` 里的：

```css
  .lg-rail-head,.lg-stage{grid-template-columns:38px 120px minmax(100px,1fr) 44px}
  .lg-rail-head span:nth-child(4){display:none}
  .lg-rail-cell{grid-row:1 / span 2}
  .lg-stage-name{grid-column:2;grid-row:1}
  .lg-judge{grid-column:3;grid-row:1}
  .lg-stage-count{grid-column:4;grid-row:1}
  .lg-evidence{display:block;grid-column:2 / -1;grid-row:2;white-space:normal;overflow:visible}
```

替换为：

```css
  .lg-rail-head,.lg-stage-h,.lg-event{grid-template-columns:38px 120px minmax(0,1fr) 44px}
  .lg-rail-head span:nth-child(4){display:none}
  .lg-stage-h>.lg-stage-name{grid-column:2}
  .lg-stage-h>.lg-stage-spacer{display:none}
  .lg-stage-h>.lg-stage-count{grid-column:4}
  .lg-event>.lg-event-spacer{display:none}
  .lg-event>.lg-rail-cell{grid-column:1;grid-row:1 / span 2}
  .lg-event>.lg-event-main{grid-column:2 / -1;grid-row:1;border-right:0}
  .lg-event>.lg-evidence{display:block;grid-column:2 / -1;grid-row:2;white-space:normal;overflow:visible}
```

把 `@media(max-width:700px)` 里的：

```css
  .lg-rail-head,.lg-stage{grid-template-columns:34px 105px minmax(0,1fr)}
  .lg-rail-head span:last-child,.lg-stage-count{display:none}
```

替换为：

```css
  .lg-rail-head,.lg-stage-h,.lg-event{grid-template-columns:34px minmax(0,1fr)}
  .lg-rail-head span:nth-child(3),.lg-rail-head span:nth-child(4),
  .lg-rail-head span:last-child,.lg-stage-count{display:none}
  .lg-stage-h>.lg-stage-spacer{display:none}
  .lg-stage-h>.lg-stage-name{grid-column:2}
  .lg-event>.lg-event-spacer{display:none}
  .lg-event>.lg-rail-cell{grid-column:1;grid-row:1 / span 2}
  .lg-event>.lg-event-main,.lg-event>.lg-evidence{grid-column:2 / -1;border-right:0}
```

再把下面三条现在已经没有 DOM 输出的规则：

```css
.lg-judge{font:700 11.5px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lg-stage.warn .lg-judge{color:var(--amber)}
.lg-stage.bad .lg-judge,.lg-stage.bad .lg-stage-name b{color:var(--red)}
```

替换为：

```css
.lg-stage.bad .lg-stage-name b{color:var(--red)}
```

- [ ] **Step 6: 运行目标 QA**

Run: `QA_ONLY='without clicking a stage' node scripts/console_qa/logs.mjs`
Expected: PASS。

- [ ] **Step 7: 跑日志页完整 QA**

Run: `node scripts/console_qa/logs.mjs`
Expected: ALL PASS，含阶段顺序、7 个 `.lg-stage`、旧记录空状态与移动端无横滚用例。

- [ ] **Step 8: 提交（仅在用户要求提交时执行；`scripts/` 被 .gitignore 排除，不提交）**

```bash
git add router/console/logs.js router/console/shared.css router/console/i18n.js
git commit -m "feat(console): list trace events with values in the log detail"
```

---

### Task 4: 全量验收与真实端到端核对

**Files:**
- Review: 本计划涉及的全部文件。
- No new production files.

**Interfaces:**
- Verifies: 数值表达式、逐事件默认展开、旧记录降级、移动端布局、图回放不受影响。

- [ ] **Step 1: 检查过时断言与遗留引用**

Run:

```bash
rg 'lg-judge' router/console || echo "no lg-judge rules"
rg 'kind_label' router/api router/console scripts/console_qa/logs.mjs
```

Expected: 第一条输出 `no lg-judge rules`（阶段摘要列已由逐事件行取代）；
第二条覆盖 `trace_presenter`（产出）、`request_presenter`（结果事件）与
`logs.js`（读取 `event.kind_label`）。

- [ ] **Step 2: 跑展示边界单测**

Run: `.venv/bin/python -m pytest tests/test_trace_presenter.py -q`
Expected: PASS。

- [ ] **Step 3: 跑完整后端测试**

Run: `.venv/bin/python -m pytest -q`
Expected: 全部通过。

- [ ] **Step 4: 跑项目标准控制台检查**

Run: `npm run check:console`
Expected: Node 契约测试与完整 Playwright QA 全部通过，退出码 0。

- [ ] **Step 5: 做一次真实端到端核对**

在本地 `npm run check:console` 的真实请求链路中确认：

- 请求完成 → 打开日志行 → **不点击任何阶段**即可看到 `0.84 > 0.65 → true`、概率百分比、
  门运算、激活、`power-target p5` / `strong-target p3` 与胜者；
- `false` 槽位在列表中可见但灰显；`invalid` 显示为 `invalid` 而不是 false；
- 旧记录（无 trace）仍显示空状态；快照链接与图回放按钮不受影响；
- 1440px 与 390px 均无横向滚动。

- [ ] **Step 6: 检查工作区**

Run: `git status --short`
Expected: 只有本次涉及的已跟踪文件（`router/api/*`、`router/console/*`、`tests/*`、`docs/*`）
和原有的工作区改动；没有 QA 截图、临时 DB 或缓存等意外新文件。

---

## Self-Review

**Spec coverage:**

| Spec 章节 | 覆盖任务 |
|---|---|
| 后端 `kind_label` 与各事件 summary/facts | Task 1（初始/JEV）、Task 2（最终/调度/失败/结果） |
| 数值格式规则 | Task 1 的 `_number/_percent/_ms/_condition` |
| 前端阶段头 + 逐事件行、默认展开 | Task 3 Step 3 |
| 表头“计算过程” | Task 3 Step 4 |
| 事件行配色/换行/响应式 | Task 3 Step 5 |
| 测试与验收 | Task 1/2 单测、Task 3 QA、Task 4 全量 |

**Placeholder scan:** 无 TBD/TODO；每个代码步骤都给出完整代码与精确命令。

**Type consistency:** `kind_label` 在 Task 1 定义并由 Task 2/3 使用；
`_candidate_label/_candidate_fact` 在 Task 1 定义、Task 2 调用；
`_DASH/_text/_fact` 沿用现有定义；前端只读取 `event.kind_label/summary/facts/tone`。

**已知取舍：** 折叠行的阶段链仍使用 `stage.cell/summary/tone` 的“最后一条”聚合，
不在本计划范围内改变；事件行默认全部渲染，不提供折叠开关。
