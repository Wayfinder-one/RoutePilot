"""Human-readable facts attached to trace events at the API boundary.

Machine fields remain the routing/replay contract.  These optional fields let
the console render graph events without the evaluator knowing about UI text.

``has_time_period`` decides whether the always-emitted ``time`` event is shown
as the ``period`` stage.  It is a fact about *this graph*: a run that never
evaluated a period condition (``condition``/``source == "periods"``) keeps the
``time`` event in the ``initial`` stage, preserving the pre-refactor display.
``present_trace`` derives it from the events; accepting a deployment-wide
``bool(snapshot.time_periods)`` at the call sites would promote every graph,
including graphs with no period condition at all.
"""
from __future__ import annotations

import math


def _text(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _fact(zh: str, en: str, value) -> list:
    return [_text(zh, en), value]


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


def _phase(wave: int) -> tuple[str, dict[str, str]]:
    if wave == 0:
        return "initial", _text("初始计算", "Initial evaluation")
    return f"jev-wave-{wave}", _text(f"JEV 第 {wave} 轮", f"JEV wave {wave}")


def present_trace_event(event: dict, *, has_time_period: bool) -> dict:
    """Return one enriched event; never mutate its machine fields."""
    kind = event["event"]
    phase = "initial"
    label = _text("初始计算", "Initial evaluation")
    cell = _text("已处理", "Processed")
    summary = _text("完成输入准备。", "Input preparation completed.")
    facts: list[list] = []
    tone = ""
    event_kind = _text("事件", "Event")

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
    elif kind == "fallback":
        phase, label = "fallback", _text("兜底路径", "Fallback")
        cell = _text("已启用", "Used")
        summary = _text("JEV 失败后启用错误兜底目标。", "The error fallback target was used after a JEV failure.")
        facts = [_fact("目标", "Target", event["target_id"])]
        tone = "warn"
    elif kind == "final":
        phase, label = "final", _text("目标判定", "Target selected")
        fallback = event["kind"] != "target"
        cell = _text("兜底目标" if fallback else "目标命中", "Fallback target" if fallback else "Target matched")
        summary = _text(
            f"最终选择目标 {event['target_id']}。",
            f"Selected target {event['target_id']}.",
        )
        facts = [
            _fact("目标", "Target", event["target_id"]),
            _fact("优先级", "Priority", str(event["priority"])),
            _fact("候选数", "Candidates", str(len(event["candidates"]))),
        ]
        tone = "warn" if fallback else "hit"
    elif kind == "schedule":
        phase, label = "schedule", _text("模型调度", "Model scheduling")
        count = len(event["available_models"])
        cell = _text("有可用模型" if count else "无可用模型", "Available" if count else "Unavailable")
        summary = _text(
            f"目标 {event['target_id']} {'有' if count else '没有'}可用模型。",
            f"Target {event['target_id']} has {'available' if count else 'no available'} models.",
        )
        facts = [
            _fact("目标", "Target", event["target_id"]),
            _fact("策略", "Strategy", event["strategy"]),
            _fact("可用模型数", "Available models", str(count)),
        ]
        tone = "" if count else "bad"
    elif kind == "sticky_hit":
        phase, label = "sticky", _text("会话粘性", "Session affinity")
        cell = _text("命中", "Hit")
        summary = _text(f"复用会话模型 {event['model']}。", f"Reused session model {event['model']}.")
        facts = [_fact("模型", "Model", event["model"])]
        tone = "hit"
    elif kind == "scheduled":
        phase, label = "schedule", _text("模型调度", "Model scheduling")
        cell = event["model"]
        summary = _text(f"最终调度模型 {event['model']}。", f"Scheduled model {event['model']}.")
        facts = [
            _fact("模型", "Model", event["model"]),
            _fact("原因", "Reason", event["reason"]),
        ]
        tone = "hit"
    elif kind == "routing_error":
        phase, label = "routing_error", _text("路由中止", "Routing stopped")
        cell = summary = _text("路由失败", "Routing failed")
        facts = [_fact("原因", "Reason", event["error_class"])]
        tone = "bad"

    return {
        **event,
        "phase": phase,
        "label": label,
        "cell": cell,
        "summary": summary,
        "facts": facts,
        "tone": tone,
        "kind_label": event_kind,
    }


def present_trace(events: list[dict], *, has_time_period: bool | None = None) -> list[dict]:
    """Present raw machine events, leaving already-presented events untouched.

    ``has_time_period`` defaults to ``None``, meaning "derive from this graph's
    events": a ``condition`` event with ``source == "periods"``.  API callers
    must leave it unset -- a deployment-wide ``bool(snapshot.time_periods)`` is
    not the same fact and would move the ``time`` event of unrelated graphs to
    the ``period`` stage.  The parameter remains as an explicit override for
    callers that already hold the graph-level answer.
    """
    if has_time_period is None:
        has_time_period = any(
            event.get("event") == "condition" and event.get("source") == "periods"
            for event in events
        )
    return [
        event if "phase" in event else present_trace_event(
            event, has_time_period=has_time_period,
        )
        for event in events
    ]
