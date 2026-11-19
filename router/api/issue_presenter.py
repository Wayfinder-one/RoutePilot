"""Bilingual console copy for graph validation ``Issue`` values.

Validators are machine-only: an ``Issue`` carries ``code`` + locators +
``params`` and no human text.  This module is the single place that renders
that machine fact into the ``zh``/``en`` pair the console (and the 422 payload)
has always received, so validators and domain code never maintain UI copy.
"""
from __future__ import annotations

from router.routing.graph.validate import Issue


def _text(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _param(issue: Issue, name: str, default=""):
    return (issue.params or {}).get(name, default)


def _edge_tail(issue: Issue) -> str:
    """``source.port → target.port`` in the exact shape the validators used."""
    return (f"{_param(issue, 'source')}.{_param(issue, 'source_port')}"
            f" → {_param(issue, 'target')}.{_param(issue, 'target_port')}")


def present_issue(issue: Issue) -> dict[str, str]:
    """Render one machine ``Issue`` into the console's bilingual message."""
    code = issue.code
    kind = _param(issue, "kind")
    node_id = issue.node_id
    edge_id = issue.edge_id
    port = issue.port

    if code == "graph_parse_error":
        detail = _param(issue, "detail")
        if detail:
            return _text(
                f"图解析失败：root 不符合 sr-routing-graph v3 结构（{detail}）",
                f"Graph parse failed: root does not conform to the sr-routing-graph v3 structure ({detail})",
            )
        return _text(
            "图解析失败：root 不符合 sr-routing-graph v3 结构（未知字段或类型非法）",
            "Graph parse failed: root does not conform to the sr-routing-graph v3 structure (unknown fields or invalid types)",
        )
    if code == "invalid_timezone":
        timezone = _param(issue, "timezone")
        return _text(f"时区非法：{timezone} 不是可用的 IANA 时区",
                     f"Invalid timezone: {timezone} is not a usable IANA timezone")
    if code == "limit_exceeded":
        limit = _param(issue, "limit")
        count = _param(issue, "count")
        if kind == "nodes":
            return _text(f"节点数超过上限 {limit}（当前 {count} 个）",
                         f"Too many nodes: limit is {limit} (found {count})")
        if kind == "edges":
            return _text(f"边数超过上限 {limit}（当前 {count} 条）",
                         f"Too many edges: limit is {limit} (found {count})")
        if kind == "options":
            return _text(f"选项数量超过上限 {limit}（当前 {count} 个）：{node_id}",
                         f"Too many options: limit is {limit} (found {count}): {node_id}")
        if kind == "questions":
            return _text(f"问题节点超过上限 {limit}（当前 {count} 个）",
                         f"Too many question nodes: limit is {limit} (found {count})")
        if kind == "chain":
            return _text(f"最长问题链 {count} 波，超过上限 {limit} 波",
                         f"Longest question chain is {count} waves, limit is {limit}")
    if code == "duplicate_node_id":
        return _text(f"节点 id 重复：{node_id}", f"Duplicate node id: {node_id}")
    if code == "duplicate_edge_id":
        return _text(f"边 id 重复：{edge_id}", f"Duplicate edge id: {edge_id}")
    if code == "start_count":
        count = _param(issue, "count")
        return _text(f"start 节点数量必须为 1（当前 {count} 个）",
                     f"Exactly one start node is required (found {count})")
    if code == "logic_operator_invalid":
        operator = _param(issue, "operator")
        return _text(f"判断门算子非法：{operator}（{node_id}）",
                     f"Invalid logic operator: {operator} ({node_id})")
    if code == "condition_config_invalid":
        if kind == "text_missing_contains":
            return _text(
                f"条件节点配置非法：source=text 需要非空 contains_any（{node_id}）",
                f"Invalid condition node: source=text requires a non-empty contains_any ({node_id})",
            )
        if kind == "text_blank_contains":
            return _text(
                f"条件节点配置非法：contains_any 不能含空白关键词（{node_id}）",
                f"Invalid condition node: contains_any must not contain blank keywords ({node_id})",
            )
        if kind == "periods_missing":
            return _text(
                f"条件节点配置非法：source=periods 需要非空时段列表（{node_id}）",
                f"Invalid condition node: source=periods requires a non-empty period list ({node_id})",
            )
        if kind == "periods_blank":
            return _text(
                f"条件节点配置非法：periods 不能含空白时段名（{node_id}）",
                f"Invalid condition node: periods must not contain blank period names ({node_id})",
            )
    if code == "question_incomplete":
        if kind == "instructions":
            return _text(f"问题说明不能为空：{node_id}",
                         f"Question instructions must not be empty: {node_id}")
        if kind == "choice_min_options":
            count = _param(issue, "count")
            return _text(f"choice 选项至少需要 2 个（当前 {count} 个）：{node_id}",
                         f"A choice question needs at least 2 options (found {count}): {node_id}")
        if kind == "option_key_empty":
            return _text(f"选项 key 不能为空：{node_id}",
                         f"Option key must not be empty: {node_id}")
        if kind == "option_key_duplicate":
            key = _param(issue, "key")
            return _text(f"选项 key 重复：{key}（{node_id}）",
                         f"Duplicate option key: {key} ({node_id})")
        if kind == "option_label_empty":
            key = _param(issue, "key")
            return _text(f"选项 label 不能为空：{key}（{node_id}）",
                         f"Option label must not be empty: {key} ({node_id})")
        if kind == "option_label_duplicate":
            label = _param(issue, "label")
            return _text(f"选项 label 重复：{label}（{node_id}）",
                         f"Duplicate option label: {label} ({node_id})")
        if kind == "option_capability_empty":
            key = _param(issue, "key")
            return _text(f"选项 capability 不能为空：{key}（{node_id}）",
                         f"Option capability must not be empty: {key} ({node_id})")
    if code == "score_criteria_required":
        return _text(f"score 问题必须配置评分档 criteria：{node_id}",
                     f"A score question requires criteria levels: {node_id}")
    if code == "score_criteria_invalid":
        lower = _param(issue, "min")
        upper = _param(issue, "max")
        return _text(
            f"score 的 criteria 必须是 {lower}–{upper} 个有限数值：{node_id}",
            f"Score criteria must be {lower} to {upper} finite numbers: {node_id}",
        )
    if code == "target_priority_invalid":
        if kind == "not_integer":
            return _text(f"target priority 必须是整数：{node_id}",
                         f"Target priority must be an integer: {node_id}")
        if kind == "non_positive":
            return _text(f"target priority 必须是正整数：{node_id}",
                         f"Target priority must be a positive integer: {node_id}")
        if kind == "duplicate":
            return _text(f"target priority 必须唯一：{node_id}",
                         f"Target priority must be unique: {node_id}")
    if code == "fallback_missing":
        if kind == "unset":
            return _text(
                "兜底模型未配置：没有 target 激活或决策失败时需要兜底目标",
                "Fallback model is not configured: a fallback target is required when no target activates or a decision fails",
            )
        if kind == "missing_node":
            return _text(f"兜底模型指向的节点不存在：{node_id}",
                         f"Fallback model points to a missing node: {node_id}")
        if kind == "not_target":
            return _text(f"兜底模型必须指向 target 节点：{node_id}",
                         f"Fallback model must point to a target node: {node_id}")
    if code == "edge_endpoint_missing":
        if kind == "source":
            return _text(f"边的起点节点不存在：{node_id}（{edge_id}）",
                         f"Edge source node does not exist: {node_id} ({edge_id})")
        if kind == "target":
            return _text(f"边的目标节点不存在：{node_id}（{edge_id}）",
                         f"Edge target node does not exist: {node_id} ({edge_id})")
    if code == "edge_port_missing":
        if kind == "source":
            return _text(f"输出端口不存在：{node_id}.{port}（{edge_id}）",
                         f"Output port does not exist: {node_id}.{port} ({edge_id})")
        if kind == "target":
            return _text(f"输入槽位不存在：{node_id}.{port}（{edge_id}）",
                         f"Input slot does not exist: {node_id}.{port} ({edge_id})")
    if code == "numeric_edge_condition_missing":
        tail = _edge_tail(issue)
        return _text(f"数值输入必须配置比较条件：{tail}",
                     f"Numeric input requires a comparison condition: {tail}")
    if code == "numeric_edge_condition_invalid":
        tail = _edge_tail(issue)
        return _text(f"数值比较条件非法（算子非法、阈值非有限或量级超限）：{tail}",
                     f"Invalid numeric condition (bad operator, non-finite or out-of-range threshold): {tail}")
    if code == "boolean_edge_condition_invalid":
        tail = _edge_tail(issue)
        return _text(f"布尔边条件既不能为空（= true）也不能是非法 is 条件：{tail}",
                     f"Boolean edge condition must be null (=true) or a valid is condition: {tail}")
    if code == "slot_conflict":
        return _text(f"同一槽位存在多条入边：{node_id}.{port}",
                     f"Multiple incoming edges on the same slot: {node_id}.{port}")
    if code == "edge_cycle":
        return _text(f"图中有环：{node_id}", f"The graph contains a cycle at {node_id}")
    if code == "logic_slot_count":
        count = _param(issue, "count")
        if kind == "not":
            return _text(f"NOT 必须恰好有 1 个输入（当前 {count} 个）：{node_id}",
                         f"NOT needs exactly 1 input (found {count}): {node_id}")
        if kind == "and_or":
            return _text(f"AND / OR 至少需要 2 个输入（当前 {count} 个）：{node_id}",
                         f"AND / OR need at least 2 inputs (found {count}): {node_id}")
    if code == "question_unreachable":
        return _text(
            f"问题节点从 start 与第 0 阶段条件均不可达：{node_id}",
            f"Question node is reachable from neither the start node nor any phase-0 condition: {node_id}",
        )
    if code == "target_models_required":
        return _text(f"target 未配置任何模型：{node_id}",
                     f"Target has no models configured: {node_id}")
    if code == "unknown_model":
        model = _param(issue, "model")
        return _text(f"目标引用了未登记模型：{model}（{node_id}）",
                     f"Target references an unknown model: {model} ({node_id})")
    if code == "reasoning_unsupported":
        model = _param(issue, "model")
        efforts = _param(issue, "efforts")
        reasoning = _param(issue, "reasoning")
        return _text(
            f"模型不支持所选推理档位：{model} 支持 {efforts!r}，目标配置 {reasoning!r}（{node_id}）",
            f"Model does not support the selected reasoning tier: {model} supports {efforts!r}, target uses {reasoning!r} ({node_id})",
        )
    if code == "unknown_period":
        period = _param(issue, "period")
        return _text(f"条件引用未定义时段：{period}（{node_id}）",
                     f"Condition references an undefined period: {period} ({node_id})")
    if code == "decision_service_unavailable":
        return _text(
            "图包含问题节点，但决策服务（JEV）未配置或不可用",
            "The graph contains question nodes but the decision service (JEV) is not configured or unavailable",
        )
    # Defensive: an unmapped code must still render deterministically.
    return _text(code, code)


def issues_payload(issues) -> list[dict]:
    """Issue 列表 → spec §5 的 422 载荷（dict of 双语字段 + 机器定位）。

    Issue 本身只存机器字段；中文/英文在这里由 issue_presenter 统一生成，
    控制台收到的键集合与重构前一致。
    """
    payload = []
    for issue in issues:
        message = present_issue(issue)
        payload.append({
            "code": issue.code,
            "zh": message["zh"],
            "en": message["en"],
            "node_id": issue.node_id,
            "edge_id": issue.edge_id,
            "port": issue.port,
            "level": issue.level,
        })
    return payload
