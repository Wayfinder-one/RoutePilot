from router.config import Settings
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.routing.graph.validate import EnableContext


def test_settings(tmp_path, **overrides):
    values = {
        "db_path": str(tmp_path / "control.db"),
        "graph_dir": str(tmp_path / "routing-graph"),
        "user_auth": False,
    }
    values.update(overrides)
    return Settings(**values)


test_settings.__test__ = False


def enable_context():
    return EnableContext(
        known_models={"fast"},
        model_reasoning={"fast": None},
        known_periods={"peak", "normal", "offpeak"},
        jev_available=True,
    )


#: 默认 fixture 的布局坐标；画布坐标属于 envelope 顶层 ``canvas``，不属于节点。
GRAPH_CANVAS = {
    "start": {"x": 0, "y": 0},
    "q": {"x": 200, "y": 0},
    "safe": {"x": 400, "y": 0},
}


def graph_dict():
    return {
        "id": "root", "type": "graph",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "q", "type": "question", "question_type": "choice", "instructions": "选择", "options": [
                {"key": "easy", "label": "轻量", "capability": "简单请求"},
                {"key": "strong", "label": "增强", "capability": "复杂请求"},
            ]},
            {"id": "safe", "type": "target", "priority": 1, "models": [{"model": "fast", "reasoning": None}]},
        ],
        "edges": [
            {"id": "e-start-q", "source": "start", "source_port": "out", "target": "q", "target_port": "in_1", "condition": None},
            {"id": "e-q-safe", "source": "q", "source_port": "selected.easy", "target": "safe", "target_port": "in_1", "condition": None},
        ],
        "error_fallback": "safe",
    }


def tree_dict(tree_id="main", *, root=None, enabled=False, sticky=None, canvas=None):
    raw = {
        "id": tree_id, "name": "测试图", "enabled": enabled,
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": root or graph_dict(),
    }
    if canvas is None and root is None:
        canvas = {node_id: dict(coords) for node_id, coords in GRAPH_CANVAS.items()}
    if canvas:
        raw["canvas"] = canvas
    return raw


def runtime_dict(**overrides):
    raw = {
        "backends": {
            "providers": [{
                "name": "local",
                "base_url": "http://json-only.test/v1",
                "key_env": "JSON_PROVIDER_API_KEY",
                "reasoning_format": "openai",
            }],
            "models": [{
                "id": "fast",
                "upstream": "json-upstream",
                "provider": "local",
                "profile": {
                    "capability": "来自 JSON 的能力",
                    "domains": ["json"],
                    "reasoning": {
                        "efforts": {"off": None, "high": "high"},
                        "default": "off",
                    },
                },
            }],
        },
        "jev": {
            "base_url": "http://jev-json.test",
            "model": "json-jev",
            "timeout_seconds": 5.0,
            "instructions": DEFAULT_INSTRUCTIONS,
            "state_template": DEFAULT_STATE_TEMPLATE,
        },
        "jev_window": {"input_tokens": 1024, "context_tokens": 4096},
        "time_periods": [
            {"name": "peak", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"},
            {"name": "normal", "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:00"},
            {"name": "offpeak", "days": [0, 1, 2, 3, 4, 5, 6], "start": "18:00", "end": "09:00"},
        ],
    }
    raw.update(overrides)
    return raw


def portable_tree_dict(tree_id="portable", *, root=None):
    """自包含决策组文档：四组运行字段齐全，root 引用 runtime_dict 里的 fast 模型。"""
    if root is None:
        root = {
            "id": "root", "type": "graph",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "direct", "type": "target", "priority": 1,
                 "models": [{"model": "fast", "reasoning": None}]},
            ],
            "edges": [{
                "id": "e-start-direct", "source": "start", "source_port": "out",
                "target": "direct", "target_port": "in_1", "condition": None,
            }],
            "error_fallback": "direct",
        }
    raw = tree_dict(tree_id, root=root)
    raw.update(runtime_dict())
    return raw


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
