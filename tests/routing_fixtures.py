from router.config import Settings
from router.jev.config import DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE
from router.jev.window import DEFAULT_CONTEXT_TOKENS, DEFAULT_INPUT_TOKENS
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

#: 默认 fixture 的运行画布：provider/model/时段都随 root 的 target 引用自动生成，
#: 保证 ``tree_dict()`` 返回的文档严格自包含（四个运行字段齐全）。
DEFAULT_PROVIDER = "local"
DEFAULT_PROVIDER_BASE_URL = "http://upstream.test/v1"
DEFAULT_PERIODS = [
    {"name": "peak", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"},
    {"name": "normal", "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:00"},
    {"name": "offpeak", "days": [0, 1, 2, 3, 4, 5, 6], "start": "18:00", "end": "09:00"},
]


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


def _target_model_ids(root) -> list[str]:
    """root 里所有 target 引用的模型 id（保持首次出现顺序、去重）。"""
    ids: list[str] = []
    for node in (root or {}).get("nodes") or ():
        if not isinstance(node, dict) or node.get("type") != "target":
            continue
        for slot in node.get("models") or ():
            model = slot.get("model") if isinstance(slot, dict) else slot
            if isinstance(model, str) and model not in ids:
                ids.append(model)
    return ids


def default_jev_dict() -> dict:
    """默认 JEV 服务（非密钥字段）：与历史数据库种子同端点/模型/id。"""
    return {
        "id": "default",
        "base_url": "http://jev.test",
        "model": "NeoHorse-Jev-4B",
        "key_env": "ROUTER_JEV_API_KEY",
        "timeout_seconds": 10.0,
        "instructions": DEFAULT_INSTRUCTIONS,
        "state_template": DEFAULT_STATE_TEMPLATE,
    }


def runtime_for_models(model_ids, *, base_url=DEFAULT_PROVIDER_BASE_URL,
                       provider=DEFAULT_PROVIDER, jev=None, periods=None) -> dict:
    """按模型 id 生成一组自包含运行字段（默认 provider 与 JEV 非密钥配置）。"""
    return {
        "backends": {
            "providers": [{
                "name": provider, "base_url": base_url, "key_env": "",
                "reasoning_format": "openai",
            }],
            "models": [
                {"id": model_id, "upstream": model_id, "provider": provider,
                 "profile": {}}
                for model_id in model_ids
            ],
        },
        "jev": default_jev_dict() if jev is None else jev,
        "jev_window": {
            "input_tokens": DEFAULT_INPUT_TOKENS,
            "context_tokens": DEFAULT_CONTEXT_TOKENS,
        },
        "time_periods": DEFAULT_PERIODS if periods is None else periods,
    }


def tree_dict(tree_id="main", *, root=None, enabled=False, default=False,
              departments=None, sticky=None, canvas=None, runtime=None):
    """自包含决策组文档：root 的 target 模型自动进入运行目录。

    严格契约下不存在「无运行字段」的树文档；需要测试缺字段拒绝时显式删除字段。
    """
    default_root = root is None
    if default_root:
        root = graph_dict()
    raw = {
        "id": tree_id, "name": "测试图", "enabled": enabled,
        "default": default,
        "departments": list(departments or []),
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": root,
    }
    if canvas is None and default_root:
        canvas = {node_id: dict(coords) for node_id, coords in GRAPH_CANVAS.items()}
    if canvas:
        raw["canvas"] = canvas
    raw.update(runtime_for_models(_target_model_ids(root)) if runtime is None else runtime)
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
            "id": "json-jev-service",
            "base_url": "http://jev-json.test",
            "model": "json-jev",
            "key_env": "ROUTER_JEV_API_KEY",
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


def default_tree(repo):
    """测试辅助：扫描仓储得到默认启用图（路由集默认图）。"""
    from router.routing.route_set import route_set_indices
    trees = repo.enabled()
    default_id, _ = route_set_indices(trees)
    if default_id is None:
        return None
    return next(tree for tree in trees if tree.id == default_id)


def runtime_from_catalog(conn) -> dict:
    """从数据库目录构造一组四字段运行配置（测试辅助，生产已无此回退）。

    仅用于历史用例把「当前目录」显式固化进画布，模拟控制台显式刷新快照的动作。
    """
    from router.reasoning import ReasoningError, validate_model_profile
    from router.store.jev_services import JevServiceRepo
    from router.store.repositories import ModelRepo, ProviderRepo, TimePeriodRepo
    from router.upstream.catalog import parse_profile_text

    providers = [
        {key: value for key, value in row.items() if key != "api_key"}
        for row in ProviderRepo(conn).list()
    ]
    formats = {
        row["name"]: row.get("reasoning_format") or "openai"
        for row in providers
    }
    models = []
    for row in ModelRepo(conn).list():
        profile = parse_profile_text(row.get("profile"))
        try:
            profile = validate_model_profile(
                profile, formats.get(row["provider"], "openai"))
        except ReasoningError:
            profile = dict(profile)
            profile.pop("reasoning", None)
        models.append({
            "id": row["id"], "upstream": row["upstream"],
            "provider": row["provider"], "profile": profile,
        })
    services = JevServiceRepo(conn).list()
    service = services[0] if services else None
    return {
        "backends": {"providers": providers, "models": models},
        "jev": service.snapshot_dict() if service is not None else None,
        "jev_window": (service.window_dict() if service is not None else {
            "input_tokens": DEFAULT_INPUT_TOKENS,
            "context_tokens": DEFAULT_CONTEXT_TOKENS,
        }),
        "time_periods": TimePeriodRepo(conn).load(),
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
