def target_dict(node_id="target", models=("fast",), *, capability="", reasoning=None):
    return {
        "id": node_id,
        "type": "target",
        "strategy": "least_inflight",
        "capability": capability,
        "models": [{"model": model, "reasoning": reasoning} for model in models],
    }


def jev_dict(node_id="choose", *, options=None, fallback=None):
    return {
        "id": node_id,
        "type": "jev",
        "instructions": "选择最合适的模型。",
        "min_prob": 0.0,
        "min_margin": 0.0,
        "options": options if options is not None else [
            {"key": "fast", "label": "轻量模型", "target": target_dict("fast-target", ("fast",))},
            {"key": "strong", "label": "增强模型", "target": target_dict("strong-target", ("strong-a", "strong-b"), capability="复杂推理、代码和长文档。")},
        ],
        "fallback": fallback if fallback is not None else target_dict("safe", ("standard",)),
    }


def tree_dict(tree_id="main", *, root=None, enabled=False, sticky=None):
    return {
        "id": tree_id,
        "name": "主分流树",
        "enabled": enabled,
        "timezone": "Asia/Shanghai",
        "sticky": sticky if sticky is not None else {"enabled": True, "ttl_seconds": 1800},
        "root": root if root is not None else jev_dict(),
    }
