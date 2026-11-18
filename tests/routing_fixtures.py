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
