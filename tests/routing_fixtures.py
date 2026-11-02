def graph_dict():
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
            {"id": "q", "type": "question", "question_type": "choice", "instructions": "选择", "position": {"x": 200, "y": 0}, "options": [
                {"key": "easy", "label": "轻量", "capability": "简单请求"},
                {"key": "strong", "label": "增强", "capability": "复杂请求"},
            ]},
            {"id": "safe", "type": "target", "priority": 1, "position": {"x": 400, "y": 0}, "models": [{"model": "fast", "reasoning": None}]},
        ],
        "edges": [
            {"id": "e-start-q", "source": "start", "source_port": "out", "target": "q", "target_port": "in_1", "condition": None},
            {"id": "e-q-safe", "source": "q", "source_port": "selected.easy", "target": "safe", "target_port": "in_1", "condition": None},
        ],
        "otherwise_target": "safe", "error_fallback": "safe",
    }


def tree_dict(tree_id="main", *, root=None, enabled=False, sticky=None):
    return {
        "id": tree_id, "name": "测试图", "enabled": enabled,
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": root or graph_dict(),
    }
