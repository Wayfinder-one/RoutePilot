"""Human-readable facts attached to graph trace events at emission time.

Machine fields remain the routing/replay contract.  These optional fields let
the console render new graph events without interpreting routing internals.
"""
from __future__ import annotations


def _text(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _fact(zh: str, en: str, value) -> list:
    return [_text(zh, en), value]


def _phase(wave: int) -> tuple[str, dict[str, str]]:
    if wave == 0:
        return "initial", _text("初始计算", "Initial evaluation")
    return f"jev-wave-{wave}", _text(f"JEV 第 {wave} 轮", f"JEV wave {wave}")


def describe_graph_event(event: dict, *, has_time_period: bool) -> dict:
    """Return one enriched event; never mutate its machine fields."""
    kind = event["event"]
    phase = "initial"
    label = _text("初始计算", "Initial evaluation")
    cell = _text("已处理", "Processed")
    summary = _text("完成输入准备。", "Input preparation completed.")
    facts: list[list] = []
    tone = ""

    if kind == "time":
        if has_time_period:
            phase, label = "period", _text("时段判断", "Time period")
            names = event["active_periods"]
            cell = _text("、".join(names) if names else "未命中", ", ".join(names) if names else "No match")
            summary = _text("已计算当前生效时段。", "Evaluated active time periods.")
            facts = [
                _fact("时区", "Timezone", event["timezone"]),
                _fact("生效时段", "Active periods", ", ".join(names) if names else "—"),
            ]
            tone = "hit" if names else ""
    elif kind == "window":
        if event["truncated"]:
            cell = _text("输入已截断", "Input truncated")
            summary = _text("输入窗口已按配置截断。", "The input window was truncated as configured.")
            facts = [_fact("窗口处理", "Window", _text("已截断", "Truncated"))]
            tone = "warn"
        else:
            cell = _text("输入就绪", "Input ready")
            summary = _text("输入窗口已准备。", "The input window is ready.")
    elif kind == "condition":
        source = event["source"]
        if source == "periods" and has_time_period:
            phase, label = "period", _text("时段判断", "Time period")
        else:
            phase, label = "condition", _text("条件判断", "Conditions")
        matched = event["matched"]
        cell = _text("命中" if event["value"] else "未命中", "Matched" if event["value"] else "No match")
        summary = _text(
            f"条件 {event['node_id']} {'命中' if event['value'] else '未命中'}。",
            f"Condition {event['node_id']} {'matched' if event['value'] else 'did not match'}.",
        )
        facts = [
            _fact("节点", "Node", event["node_id"]),
            _fact("依据", "Matched values", ", ".join(matched) if matched else "—"),
        ]
        tone = "hit" if event["value"] else ""
    elif kind in {"gate", "slot", "activate", "wave", "question"}:
        phase, label = _phase(event["wave"])
        facts = [_fact("轮次", "Wave", str(event["wave"]))]
        if kind == "wave":
            failed = event["failed"]
            cell = _text("调用失败" if failed else "调用完成", "Call failed" if failed else "Call complete")
            summary = _text(
                f"JEV 第 {event['wave']} 轮{'失败' if failed else '完成'}。",
                f"JEV wave {event['wave']} {'failed' if failed else 'completed'}.",
            )
            facts += [
                _fact("问题数", "Questions", str(len(event["questions"]))),
                _fact("耗时", "Duration", f"{event['duration_ms']:.1f} ms"),
            ]
            tone = "bad" if failed else "hit"
        elif kind == "question":
            cell = _text("已回答", "Answered")
            summary = _text(f"问题 {event['node_id']} 已回答。", f"Question {event['node_id']} was answered.")
            facts += [_fact("问题", "Question", event["node_id"])]
            if "selected" in event:
                facts += [_fact("选项", "Choice", event["selected"])]
            tone = "hit"
        elif kind == "gate":
            value = event["value"]
            cell = _text("通过" if value else "未通过", "True" if value else "False")
            summary = _text(f"逻辑门 {event['node_id']} 已计算。", f"Gate {event['node_id']} was evaluated.")
            facts += [_fact("节点", "Node", event["node_id"]), _fact("运算", "Operator", event["operator"])]
            tone = "hit" if value else ""
        elif kind == "slot":
            status = event["status"]
            cell = _text("通过" if status == "true" else "未通过", "True" if status == "true" else status)
            summary = _text(f"连接 {event['edge_id']} 已判断。", f"Edge {event['edge_id']} was evaluated.")
            facts += [_fact("连接", "Edge", event["edge_id"]), _fact("状态", "Status", status)]
            tone = "hit" if status == "true" else ""
        else:  # activate
            cell = _text("已激活", "Activated")
            summary = _text(f"节点 {event['node_id']} 已激活。", f"Node {event['node_id']} was activated.")
            facts += [_fact("节点", "Node", event["node_id"]), _fact("入口", "Input", event["via_port"])]
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

    return {
        **event,
        "phase": phase,
        "label": label,
        "cell": cell,
        "summary": summary,
        "facts": facts,
        "tone": tone,
    }
