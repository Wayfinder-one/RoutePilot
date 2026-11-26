from pathlib import Path

import httpx

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


# --------------------------------------------------------------------------- #
# 跨服务测试：进程内内核（Task 7 起目录写需要内核图批次）
# --------------------------------------------------------------------------- #
class _KernelTransport(httpx.AsyncBaseTransport):
    """把内核 ASGI app 暴露成 ``CoreClient`` 可用的 in-process transport。

    关闭时顺带关掉内核自建的 ``AsyncClient``：测试不启动内核 lifespan，避免
    每个测试泄漏一个未关闭的 HTTP 客户端。
    """

    def __init__(self, app):
        self.app = app
        self._inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request):
        return await self._inner.handle_async_request(request)

    async def aclose(self):
        await self._inner.aclose()
        http = getattr(self.app.state, "http", None)
        if http is not None:
            await http.aclose()


def default_graph_dir(base) -> str:
    """``test_settings(base)`` 的图目录（与 ``create_app`` 共享同一目录）。"""
    return str(Path(base) / "routing-graph")


def core_app(base=None, *, graph_dir=None, transport=None):
    """A real ``create_core_app`` over the peripheral's graph directory."""
    from router.api.core_app import create_core_app
    from router.core_config import CoreSettings, LogSinkSettings

    directory = str(graph_dir) if graph_dir is not None else default_graph_dir(base)
    return create_core_app(
        CoreSettings(graph_dir=directory, logging=LogSinkSettings(type="disabled")),
        transport=transport)


class RecordingSink:
    """Synchronous in-memory decision-event sink for split-app tests.

    The kernel emits through ``app.state.decision_sink``; tests substitute this
    object to observe the exact event that a real peripheral would receive.
    """

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    async def start(self):
        pass

    async def close(self):
        pass


def recording_core_app(base=None, *, graph_dir=None, transport=None):
    """Kernel app + substituted ``RecordingSink`` (``(app, sink)``)."""
    kernel = core_app(base, graph_dir=graph_dir, transport=transport)
    sink = RecordingSink()
    kernel.state.decision_sink = sink
    return kernel, sink


def split_app(base=None, *, kernel=None, settings=None, transport=None,
              graph_dir=None, core_transport=None):
    """Peripheral app wired to an in-process kernel (``core_transport``).

    ``base`` is the directory passed to :func:`test_settings`; pass ``settings``
    for a custom peripheral layout and ``graph_dir`` when the kernel graph
    directory is not ``<base>/routing-graph``.  ``core_transport`` overrides the
    kernel transport entirely (unreachable-kernel / forbidden-call tests); the
    kernel app itself stays reachable from tests as ``app.test_kernel`` (never
    read by the application itself).
    """
    from router.api.internal_app import create_service_apps

    settings = settings if settings is not None else test_settings(base)
    if core_transport is None:
        core_transport = core_app_for(
            graph_dir=settings.graph_dir, app=kernel)
    # Mirror the production wiring: the public console app and the internal
    # event receiver are two apps sharing one decision-log store.
    app, internal = create_service_apps(
        settings, transport=transport, core_transport=core_transport)
    app.test_kernel = kernel
    app.test_internal = internal
    return app


def split_app_for(base=None, *, settings=None, transport=None,
                  kernel_transport=None):
    """Peripheral app backed by a fresh in-process kernel over the same directory.

    ``transport`` serves the peripheral's model-upstream client; ``kernel_transport``
    serves the kernel's own HTTP client (JEV calls).  Graph files already written
    to ``<settings.graph_dir>`` are loaded by the kernel at construction.
    """
    settings = settings if settings is not None else test_settings(base)
    kernel = core_app(graph_dir=settings.graph_dir, transport=kernel_transport)
    return split_app(settings=settings, kernel=kernel, transport=transport)


def kernel_state(app):
    """State of the in-process kernel behind a split-test peripheral app.

    Graph/routing assertions belong to the kernel after the split: tests keep
    using the peripheral ``app`` for HTTP and SQLite, and reach the kernel app
    explicitly (``app.test_kernel`` / ``app.state.kernel_app``) for the route
    set, graph repository and graph publisher.
    """
    kernel = getattr(app, "test_kernel", None)
    if kernel is None:
        kernel = app.state.kernel_app
    return kernel.state


def install_graph(kernel, document):
    """Create or replace one graph document in the kernel and republish it."""
    from router.routing.tree import tree_from_dict

    repo = kernel.state.graph_repo
    tree = tree_from_dict(document)
    current = repo.get(tree.id)
    if current is None:
        repo.create(tree)
    else:
        repo.update(tree, base_document_sha256=current.document_sha256)
    kernel.state.publisher.refresh()
    return tree


def remove_graph(kernel, tree_id: str) -> None:
    """Delete one graph file from the kernel store and republish."""
    path = Path(kernel.state.settings.graph_dir) / "graphs" / f"{tree_id}.json"
    path.unlink(missing_ok=True)
    kernel.state.publisher.refresh()


def seed_execution_catalog(conn, *, provider="local",
                           base_url=DEFAULT_PROVIDER_BASE_URL,
                           key="peripheral-secret",
                           reasoning_format="openai",
                           models=None) -> None:
    """Insert one provider + its models into the peripheral SQLite catalog.

    ``models`` is a list of ``(model_id, upstream, profile_json)`` triples; the
    default mirrors :data:`DEFAULT_PROVIDER`/graph fixture model ids with
    ``upstream == model_id`` and an empty profile.
    """
    rows = models if models is not None else [
        (model, model, "{}") for model in ("fast", "strong", "power", "safe")
    ]
    conn.execute(
        "INSERT INTO providers (name, base_url, key_env, reasoning_format, api_key) "
        "VALUES (?, ?, '', ?, ?)",
        (provider, base_url, reasoning_format, key))
    for model_id, upstream, profile in rows:
        conn.execute(
            "INSERT INTO models (id, upstream, provider, profile) "
            "VALUES (?, ?, ?, ?)",
            (model_id, upstream, provider, profile))
    conn.commit()


def deliver_decision_events(peripheral, sink) -> list[dict]:
    """Feed captured kernel events into the peripheral's internal receiver.

    Equivalent to the kernel sink's real POST to the internal listener (which the
    dedicated end-to-end test exercises with live lifespans); the public console
    app itself no longer mounts the receiver path.
    """
    from fastapi.testclient import TestClient

    from router.api.internal_app import create_internal_app

    internal = getattr(peripheral, "test_internal", None)
    receiver = internal or create_internal_app(peripheral.state.decision_logs)
    received = []
    with TestClient(receiver) as client:
        for event in sink.events:
            response = client.post("/internal/v1/decision-logs", json=event)
            assert response.status_code == 200, response.text
            received.append(event)
    return received


def core_app_for(base=None, *, graph_dir=None, app=None,
                 transport=None) -> httpx.AsyncBaseTransport:
    """In-process kernel transport for ``create_app(..., core_transport=...)``.

    ``base`` is the directory passed to :func:`test_settings` (graph directory =
    ``<base>/routing-graph``); pass ``graph_dir``/``app`` for custom layouts and
    for tests that need to inject kernel-side faults.  ``transport`` is the
    kernel's own HTTP transport (JEV / decision-event calls), which lets a test
    observe requests the kernel — not the peripheral — makes.
    """
    kernel = app if app is not None else core_app(
        base, graph_dir=graph_dir, transport=transport)
    return _KernelTransport(kernel)


def wire_catalog_sync(app, transport) -> None:
    """Production catalog-wiring for hand-built peripheral test apps.

    ``create_app()`` sets ``app.state.core``/``catalog_sync_factory`` itself; a
    test app mounting only some routers needs the same two attributes for the
    migrated catalog routes (models/JEV/time-periods).
    """
    from router.control.remote_catalog_sync import RemoteCatalogSyncService
    from router.core_client import CoreClient

    app.state.core = CoreClient(
        "http://core", http=httpx.AsyncClient(transport=transport))
    app.state.catalog_sync_factory = lambda conn: RemoteCatalogSyncService(
        conn, app.state.core)
