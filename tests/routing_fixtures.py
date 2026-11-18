from router.config import Settings
from router.routing.policy.validate import EnableContext


def test_settings(tmp_path, **overrides):
    values = {
        "db_path": str(tmp_path / "control.db"),
        "policy_dir": str(tmp_path / "routing-policy"),
        "user_auth": False,
    }
    values.update(overrides)
    return Settings(**values)


test_settings.__test__ = False


def enable_context():
    return EnableContext(
        known_models={"fast", "strong"},
        model_reasoning={"fast": None},
        known_periods={"peak", "normal", "offpeak"},
        jev_available=True,
    )


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
        "error_fallback": "safe",
    }


def tree_dict(tree_id="main", *, root=None, enabled=False, sticky=None):
    return {
        "id": tree_id, "name": "测试图", "enabled": enabled,
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": root or graph_dict(),
    }


def policy_dict():
    return {
        "kind": "sr-routing-policy",
        "version": 4,
        "name": "测试策略",
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "选择模型",
                "options": [
                    {"key": "fast", "label": "轻量", "capability": "简单请求"},
                    {"key": "strong", "label": "增强", "capability": "复杂请求"},
                ],
            },
            "risk": {"type": "noul", "instructions": "是否高风险"},
            "severity": {"type": "score", "instructions": "评分", "criteria": [1, 5, 10]},
        },
        "targets": {
            "fast": {"models": [{"model": "fast"}]},
            "strong": {"models": [{"model": "strong"}]},
            "safe": {"models": [{"model": "fast"}]},
        },
        "rules": [
            {"name": "高风险", "when": {"answer": "risk.yes", "gte": 0.8}, "target": "safe"},
            {
                "name": "复杂请求",
                "when": {"all": [
                    {"answer": "route.selected", "eq": "strong"},
                    {"answer": "severity.value", "gte": 5},
                ]},
                "target": "strong",
            },
            {"name": "轻量请求", "when": {"answer": "route.selected", "eq": "fast"}, "target": "fast"},
        ],
        "fallback": "safe",
    }


def huge_chain_graph(count=1200):
    """n0 → n1 → … 的 target 链（默认 1200 节点/1199 边），用于极限图限额回归。

    节点与边都超过 spec §2.7 限额，校验必须在任何 DFS 之前返回 limit_exceeded。
    """
    raw = {
        "id": "root", "type": "graph",
        "nodes": [{"id": "n0", "type": "start"}],
        "edges": [],
        "error_fallback": f"n{count - 1}",
    }
    for index in range(1, count):
        raw["nodes"].append({
            "id": f"n{index}", "type": "target", "priority": index,
            "models": [{"model": "fast", "reasoning": None}],
        })
        raw["edges"].append({
            "id": f"e{index}", "source": f"n{index - 1}", "source_port": "out",
            "target": f"n{index}", "target_port": "in_1",
        })
    return raw
