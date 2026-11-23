# 严格自包含画布、多 JEV 与引用同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有画布和案例 JSON 严格自包含，支持多 JEV 选择，并在模型、provider、JEV 变更或删除时经影响确认同步相关画布。

**Architecture:** 已保存画布是唯一运行快照，SQLite 只承担管理目录、下拉框和密钥存储。多 JEV 以稳定 ID 管理，选择或同步服务时同时复制连接配置和窗口；目录变更采用“409 影响清单 → 基线哈希确认 → GraphBatch 原子同步”的两阶段协议。

**Tech Stack:** Python 3.11+、FastAPI、SQLite、frozen dataclass、原生 JavaScript ES Modules、Node `node:test`、Playwright、pytest。

## Global Constraints

- 设计事实源：`docs/superpowers/specs/2026-11-23-strict-self-contained-canvas-runtime-design.md`。
- 四个运行字段 `backends`、`jev`、`jev_window`、`time_periods` 必须成组出现；旧文档、`runtime: null` 和数据库回退最终全部删除，不保留双轨兼容。
- 已保存画布运行只读自身快照；SQLite 只提供管理目录、下拉框和密钥，不参与已有画布运行装配。
- JEV 与窗口属于同一服务，选择和同步时必须一起替换；JSON 中 JEV 必须带 `id`，不得带 `api_key`。
- 未看到影响确认前，模型、provider、JEV 的引用变更不得写数据库或画布；确认后只修改受影响画布。
- 删除被 target 引用的模型必须提供替代模型；删除被画布引用的 JEV 必须提供替代 JEV。
- 数据库额外存在的资源不是 `runtime_mismatch`；mismatch 是检查提示，不是运行硬错误。
- 所有密钥保留在密钥存储或环境变量中。不得把真实 Key、Authorization 或派生密钥状态写入图 JSON、图 API 或快照内容。
- 遵循现有 frozen dataclass、ES Module、`GraphBatch`、单写锁、事务和活动图最后写入模式，不引入新依赖。
- 工作区已有多处未提交修改。每个任务只暂存本任务文件，不使用 `git add -A`、`git reset` 或覆盖他人改动。

---

## File Structure

- `router/jev/service.py`：多 JEV 的公开配置、窗口、快照和安全转换，不接触 SQLite。
- `router/store/jev_services.py`：`jev_services` 表的 CRUD 与端点匹配密钥读取；替代 `router/store/jev_config.py`。
- `router/control/impacts.py`：纯图引用扫描、影响清单、确认哈希核对和候选树转换，不接触 API。
- `router/control/catalog_sync.py`：目录写入与多图暂存、回滚、发布编排，复用 `GraphBatch`。
- `router/control/runtime.py`：数据库目录到显式初始快照的转换及密钥解析，不再为已有图回退或物化。
- `router/routing/runtime.py`、`tree.py`、`document.py`、`snapshot.py`：严格图契约和只读图快照。
- `router/api/routes_jev.py`、`routes_models.py`、`routes_time.py`、`routes_trees.py`、`routes_v1.py`：目录 API、影响确认 DTO 和明确的固定模型直连。
- `router/console/impact.js`：模型/provider/JEV 页面共用的影响确认框和 409 重试逻辑。
- `router/console/routing/runtime.js`：从目录生成初始快照、选择 JEV、补入模型的纯函数。
- `router/console/routing/graph.js`、`controller.js`、`app.js`、`inspector.js`：严格导入导出和画布编辑。
- `configs/cases/case-*.json`：五份可独立导入运行的完整 v3 文档。

### Task 1: 多 JEV 领域模型、数据库迁移与仓储

**Files:**
- Create: `router/jev/service.py`
- Create: `router/store/jev_services.py`
- Create: `tests/test_jev_services.py`
- Modify: `router/store/db.py`
- Delete after all imports move in Task 3: `router/store/jev_config.py`

**Interfaces:**
- Produces: `JevService`、`JevServiceRepo.list/get/save/delete/stored_api_key`。
- `JevService.snapshot_dict()` 只返回可写入画布的 JEV 字段；`window_dict()` 返回 `jev_window`；`settings(api_key=...)` 返回现有 `JevSettings`。

- [ ] **Step 1: 写迁移、CRUD 和脱敏失败测试**

```python
# tests/test_jev_services.py
from router.jev.service import JevService
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo


def service(service_id="jev-a", model="d1-a"):
    return JevService(
        id=service_id, base_url="http://jev-a.test", model=model,
        key_env="JEV_A_API_KEY", input_tokens=2048, context_tokens=4096,
    )


def test_service_roundtrip_has_no_secret_in_snapshot(tmp_path):
    conn = connect(str(tmp_path / "new.db"))
    migrate(conn)
    repo = JevServiceRepo(conn)
    repo.save(service(), api_key="secret-sample")
    loaded = repo.get("jev-a")
    assert loaded.snapshot_dict() == {
        "id": "jev-a", "base_url": "http://jev-a.test", "model": "d1-a",
        "key_env": "JEV_A_API_KEY", "timeout_seconds": 10.0,
        "instructions": loaded.instructions, "state_template": loaded.state_template,
    }
    assert loaded.window_dict() == {"input_tokens": 2048, "context_tokens": 4096}
    assert repo.stored_api_key(loaded) == "secret-sample"
    assert "api_key" not in loaded.snapshot_dict()


def test_v5_singleton_migrates_to_default_service(tmp_path):
    path = str(tmp_path / "old.db")
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES (5)")
    conn.execute("""CREATE TABLE jev_config (
        id INTEGER PRIMARY KEY CHECK (id = 1), base_url TEXT NOT NULL,
        model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '',
        timeout_seconds REAL NOT NULL, instructions TEXT NOT NULL,
        state_template TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO settings VALUES ('jev_input_tokens', '2048')")
    conn.execute("INSERT INTO settings VALUES ('jev_context_tokens', '4096')")
    conn.execute("""INSERT INTO jev_config VALUES
        (1, 'http://old-jev.test', 'd1-old', 'old-secret', 5.0, '指令',
         '【请求】{request}', '2026-11-01T00:00:00+00:00')""")
    migrate(conn)
    loaded = JevServiceRepo(conn).get("default")
    assert loaded.base_url == "http://old-jev.test"
    assert loaded.input_tokens == 2048 and loaded.context_tokens == 4096
    assert JevServiceRepo(conn).stored_api_key(loaded) == "old-secret"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='jev_config'").fetchone() is None
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.venv/bin/python -m pytest -q tests/test_jev_services.py`
Expected: FAIL，`ModuleNotFoundError: router.jev.service`。

- [ ] **Step 3: 实现公开服务模型和严格仓储**

```python
# router/jev/service.py
from __future__ import annotations

import re
from dataclasses import dataclass

from router.jev.config import (
    DEFAULT_INSTRUCTIONS, DEFAULT_STATE_TEMPLATE, JevSettings,
)
from router.jev.window import JevWindow

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class JevService:
    id: str
    base_url: str
    model: str
    key_env: str = ""
    timeout_seconds: float = 10.0
    instructions: str = DEFAULT_INSTRUCTIONS
    state_template: str = DEFAULT_STATE_TEMPLATE
    input_tokens: int = 1024
    context_tokens: int = 4096

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID_RE.fullmatch(self.id):
            raise ValueError("invalid JEV service id")
        if not isinstance(self.key_env, str) or (
                self.key_env and not _ENV_RE.fullmatch(self.key_env)):
            raise ValueError("invalid JEV key_env")
        settings = self.settings()
        object.__setattr__(self, "base_url", settings.base_url)
        object.__setattr__(self, "timeout_seconds", settings.timeout_seconds)
        JevWindow(self.input_tokens, self.context_tokens)

    def settings(self, *, api_key: str = "") -> JevSettings:
        return JevSettings(
            base_url=self.base_url, model=self.model, api_key=api_key,
            timeout_seconds=self.timeout_seconds,
            instructions=self.instructions, state_template=self.state_template,
        )

    def snapshot_dict(self) -> dict:
        return {
            "id": self.id, "base_url": self.base_url, "model": self.model,
            "key_env": self.key_env, "timeout_seconds": self.timeout_seconds,
            "instructions": self.instructions, "state_template": self.state_template,
        }

    def window_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "context_tokens": self.context_tokens}
```

`JevServiceRepo` 完整实现如下；`_from_row()` 是模块内私有转换函数：

```python
from router.jev.service import JevService
from router.store.repositories import now_iso, transaction


def _from_row(row):
    return JevService(
        id=row["id"], base_url=row["base_url"], model=row["model"],
        key_env=row["key_env"], timeout_seconds=row["timeout_seconds"],
        instructions=row["instructions"], state_template=row["state_template"],
        input_tokens=row["input_tokens"], context_tokens=row["context_tokens"],
    )


class JevServiceRepo:
    def __init__(self, conn):
        self.conn = conn

    def list(self):
        return [_from_row(row) for row in self.conn.execute(
            "SELECT * FROM jev_services ORDER BY id")]

    def get(self, service_id):
        row = self.conn.execute(
            "SELECT * FROM jev_services WHERE id = ?", (service_id,)).fetchone()
        return _from_row(row) if row is not None else None

    def save(self, service, *, api_key=None):
        previous = self.conn.execute(
            "SELECT * FROM jev_services WHERE id = ?", (service.id,)).fetchone()
        preserve = api_key is None and previous is not None and all(
            previous[field] == getattr(service, field)
            for field in ("base_url", "model")
        )
        key = previous["api_key"] if preserve else api_key or ""
        with transaction(self.conn):
            self.conn.execute("""INSERT INTO jev_services
                (id, base_url, model, key_env, api_key, timeout_seconds,
                 instructions, state_template, input_tokens, context_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                 base_url=excluded.base_url, model=excluded.model,
                 key_env=excluded.key_env, api_key=excluded.api_key,
                 timeout_seconds=excluded.timeout_seconds,
                 instructions=excluded.instructions, state_template=excluded.state_template,
                 input_tokens=excluded.input_tokens, context_tokens=excluded.context_tokens,
                 updated_at=excluded.updated_at""", (
                service.id, service.base_url, service.model, service.key_env, key,
                service.timeout_seconds, service.instructions, service.state_template,
                service.input_tokens, service.context_tokens, now_iso(),
            ))

    def delete(self, service_id):
        with transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM jev_services WHERE id = ?", (service_id,))
        return cursor.rowcount > 0

    def stored_api_key(self, service):
        row = self.conn.execute(
            "SELECT base_url, model, api_key FROM jev_services WHERE id = ?",
            (service.id,),
        ).fetchone()
        if row is None or row["base_url"] != service.base_url or row["model"] != service.model:
            return ""
        return row["api_key"] or ""
```

在 `router/store/db.py` 将 `SCHEMA_VERSION` 改为 `6`，新库的 `SCHEMA` 使用以下表，新增 `_upgrade_v5_to_v6()` 并在 `applied == 5` 分支调用：

```sql
CREATE TABLE jev_services (
    id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    key_env TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    timeout_seconds REAL NOT NULL,
    instructions TEXT NOT NULL,
    state_template TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    context_tokens INTEGER NOT NULL,
    updated_at TEXT NOT NULL
)
```

迁移在 `_in_transaction` 中复制旧行到 `default`，从 `settings` 读取窗口（缺省 `1024/4096`），设置 `key_env='ROUTER_JEV_API_KEY'`，随后 `DROP TABLE jev_config` 并写入版本 6。此迁移不是运行时兼容层；应用代码只读 `jev_services`。

- [ ] **Step 4: 运行迁移与仓储测试**

Run: `.venv/bin/python -m pytest -q tests/test_jev_services.py tests/test_db.py`
Expected: PASS；旧 `test_db.py` 中对 `jev_config` 的断言改为 `jev_services`，不断言保留旧表。

- [ ] **Step 5: 提交本任务**

```bash
git add router/jev/service.py router/store/jev_services.py router/store/db.py tests/test_jev_services.py tests/test_db.py
git commit -m "feat: add multi-JEV service catalog"
```

### Task 2: 严格化图运行契约并删除已有图数据库回退

**Files:**
- Modify: `router/routing/runtime.py`
- Modify: `router/routing/tree.py`
- Modify: `router/routing/document.py`
- Modify: `router/routing/snapshot.py`
- Modify: `router/control/runtime.py`
- Modify: `router/control/validation.py`
- Modify: `router/control/graphs.py`
- Modify: `router/api/routes_trees.py`
- Modify: `tests/routing_fixtures.py`
- Modify: `tests/test_graph_runtime.py`, `tests/test_graph_document.py`, `tests/test_routing_snapshot.py`, `tests/test_graph_try_api.py`, `tests/test_routing_trees_admin.py`
- Modify: `tests/fixtures/graph_contracts.json`

**Interfaces:**
- `RoutingTree.runtime` 改为必需的 `GraphRuntime`，不得再默认为 `None`。
- `GraphRuntime.jev` 使用 Task 1 的 `JevService | None`。
- `tree_issues(conn, tree)` 始终由 `tree.runtime` 构造 `EnableContext`。
- `build_snapshot_for_tree(conn, tree)` 在 `tree is not None` 时只读 `tree.runtime`。

- [ ] **Step 1: 写严格契约和快照隔离失败测试**

前两个测试加入 `tests/test_graph_runtime.py`：

```python
import pytest
from router.routing.tree import tree_from_dict
from tests.routing_fixtures import portable_tree_dict


@pytest.mark.parametrize("field", ["backends", "jev", "jev_window", "time_periods"])
def test_every_runtime_field_is_required(field):
    raw = portable_tree_dict()
    del raw[field]
    with pytest.raises(ValueError, match="runtime fields must appear together"):
        tree_from_dict(raw)


def test_jev_snapshot_requires_id():
    raw = portable_tree_dict()
    raw["jev"] = {"base_url": "http://jev.test", "model": "d1"}
    with pytest.raises(ValueError, match="jev.id"):
        tree_from_dict(raw)
```

快照隔离测试加入 `tests/test_self_contained_e2e.py`，直接使用该文件已有的 `json_only` fixture 和 `connect` import：

```python
from router.routing.snapshot import build_snapshot_for_tree


def test_graph_snapshot_does_not_change_when_db_catalog_changes(json_only):
    app, _client, settings, _calls = json_only
    before = app.state.snapshot.content
    tree = app.state.graph_repo.active()
    conn = connect(settings.db_path)
    with conn:
        conn.execute("INSERT INTO providers VALUES ('other', 'http://other.test/v1', '', 'openai', '')")
        conn.execute("INSERT INTO models VALUES ('fast', 'changed', 'other', '{}')")
    assert build_snapshot_for_tree(conn, tree).content == before
    conn.close()
```

将 `json_only` 的 `runtime_dict()` JEV fixture 改为 `id='json-jev-service'`、`key_env='ROUTER_JEV_API_KEY'`。

- [ ] **Step 2: 运行契约测试并确认旧兼容行为导致失败**

Run: `.venv/bin/python -m pytest -q tests/test_graph_runtime.py tests/test_graph_document.py tests/test_routing_snapshot.py`
Expected: 新增测试 FAIL；缺字段当前被接受，JEV 当前不要求 `id`。

- [ ] **Step 3: 实现严格解析与单向快照**

用以下约束替换对应实现：

```python
# router/routing/tree.py
@dataclass(frozen=True)
class RoutingTree:
    id: str
    name: str
    enabled: bool
    root: GraphRoot
    runtime: GraphRuntime
    sticky: Sticky = Sticky()
    timezone: str = "Asia/Shanghai"
    tree_revision: str = ""
    updated_at: str = ""
    document_sha256: str = ""

# tree_from_dict 中，runtime 不再是可选分支
runtime_fields = set(raw) & RUNTIME_FIELDS
if runtime_fields != RUNTIME_FIELDS:
    raise ValueError("runtime fields must appear together")
```

`tree_storage_dict()`、`tree_behavior_dict()` 和 `graph_document()` 都在返回前执行以下完整语句，不再用 `if tree.runtime is not None` 包裹：

```python
document.update(runtime_to_dict(tree.runtime))
```

`router/routing/runtime.py` 的 `_jev()` 改为解析 `JevService`：

```python
def _jev(raw, window: JevWindow) -> JevService | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("jev must be an object or null")
    reject_unknown_fields("jev", raw, {
        "id", "base_url", "model", "key_env", "timeout_seconds",
        "instructions", "state_template",
    })
    if "id" not in raw:
        raise ValueError("jev.id is required")
    return JevService(
        id=raw["id"], base_url=raw.get("base_url"), model=raw.get("model"),
        key_env=raw.get("key_env", ""), timeout_seconds=raw.get("timeout_seconds", 10.0),
        instructions=raw.get("instructions", DEFAULT_INSTRUCTIONS),
        state_template=raw.get("state_template", DEFAULT_STATE_TEMPLATE),
        input_tokens=window.input_tokens, context_tokens=window.context_tokens,
    )
```

`runtime_to_dict()` 使用 `runtime.jev.snapshot_dict()`；`GraphRuntime` 保留 `jev_input_tokens/context_tokens` 属性以避免无关改动，但它们的值必须来自同一 `jev_window` 解析结果。`snapshot_parts()` 仍调用 `resolve_jev_settings(conn, runtime.jev)`，该函数改为以下多服务密钥解析：

```python
def resolve_jev_settings(conn, service):
    if service is None:
        return None
    stored = JevServiceRepo(conn).stored_api_key(service)
    environment = os.environ.get(service.key_env, "") if service.key_env else ""
    return service.settings(api_key=stored or environment)
```

完成这些删除：

- `GraphPatch.runtime`、API 的 `runtime` 控制字段和 `_validate_runtime_control()`；
- `GraphService._materialize()` 及所有调用；
- `runtime_for_tree()`、`effective_runtime()`、管理面活动图投影；
- `build_enable_context()` 的旧图分支；
- `build_snapshot_for_tree()` 中 `tree is not None and tree.runtime is not None` 的条件及旧图装配段。

`tree is None` 时构造空 Auto 快照：`tree=None`、`backends={}`、`jev=None`、`capabilities={}`、`time_periods=[]`，固定模型直连在 Task 3 分离。所有测试 fixture 默认改为完整 `portable_tree_dict()`；明确测试缺字段的用例保留并改为拒绝断言。

- [ ] **Step 4: 运行后端图契约回归**

Run: `.venv/bin/python -m pytest -q tests/test_graph_runtime.py tests/test_graph_document.py tests/test_graph_try_api.py tests/test_routing_snapshot.py tests/test_routing_trees_admin.py`
Expected: PASS；没有测试引用 `runtime: null`、旧图物化或数据库回退。

- [ ] **Step 5: 提交本任务**

```bash
git add router/routing router/control/runtime.py router/control/validation.py router/control/graphs.py router/api/routes_trees.py tests/routing_fixtures.py tests/test_graph_runtime.py tests/test_graph_document.py tests/test_graph_try_api.py tests/test_routing_snapshot.py tests/test_routing_trees_admin.py tests/fixtures/graph_contracts.json
git commit -m "feat: require self-contained graph snapshots"
```

### Task 3: 多 JEV 管理 API、目录读取与固定模型直连分离

**Files:**
- Modify: `router/api/routes_jev.py`
- Modify: `router/api/routes_models.py`
- Modify: `router/api/routes_time.py`
- Modify: `router/api/routes_v1.py`
- Modify: `router/api/routes_test.py`
- Modify: `router/api/app.py`
- Modify: `router/control/config.py`
- Modify: `router/control/runtime.py`
- Modify: `tests/test_jev_admin.py`, `tests/test_jev_window.py`, `tests/test_admin_models.py`, `tests/test_app.py`, `tests/test_v1.py`
- Delete: `router/store/jev_config.py`

**Interfaces:**
- `GET /admin/jev` 返回 `{"services": [public_service, ...]}`。
- `POST /admin/jev` 创建服务；`PUT /admin/jev/{id}` 更新；`DELETE /admin/jev/{id}` 删除；`POST /admin/jev/{id}/test` 测试。
- `public_service` 包含 `snapshot_dict()`、`window_dict()`、`key_present`、`key_source`，不包含 `api_key`。
- 固定模型直连通过 `load_backends(conn)` 读取管理目录；Auto 继续只读活动图快照。

- [ ] **Step 1: 写多服务 API 与直连隔离失败测试**

多服务测试放入 `tests/test_jev_admin.py`，复用该文件现有 `make_client(tmp_path)`：

```python
def test_two_jev_services_have_independent_windows_and_no_keys(tmp_path):
    client, _app = make_client(tmp_path)
    for service_id, model, tokens in (("jev-a", "d1-a", 1024), ("jev-b", "d1-b", 2048)):
        response = client.post("/admin/jev", json={
            "id": service_id, "base_url": "http://" + service_id + ".test",
            "model": model, "key_env": "JEV_TEST_API_KEY",
            "input_tokens": tokens, "context_tokens": 4096,
        })
        assert response.status_code == 201, response.text
        assert "api_key" not in response.text
    services = client.get("/admin/jev").json()["services"]
    assert [(s["id"], s["input_tokens"]) for s in services] == [("jev-a", 1024), ("jev-b", 2048)]
    assert client.get("/admin/jev/window").status_code == 404
```

直连测试放入 `tests/test_self_contained_e2e.py`，使用其 `json_only` fixture：

```python
def test_fixed_model_uses_db_instead_of_active_canvas(json_only):
    _app, client, settings, calls = json_only
    conn = connect(settings.db_path)
    with conn:
        conn.execute("INSERT INTO providers VALUES ('db', 'http://db.test/v1', '', 'openai', '')")
        conn.execute("INSERT INTO models VALUES ('fast', 'db-upstream', 'db', '{}')")
    conn.close()
    response = client.post("/admin/test/chat/completions", json={
        "model": "fast", "messages": [{"role": "user", "content": "直连"}],
    })
    assert response.status_code == 200, response.text
    assert calls["upstream"][-1]["body"]["model"] == "db-upstream"
    assert calls["upstream"][-1]["url"] == "http://db.test/v1/chat/completions"
```

- [ ] **Step 2: 运行测试并确认旧单服务 API 失败**

Run: `.venv/bin/python -m pytest -q tests/test_jev_admin.py tests/test_jev_window.py tests/test_v1.py`
Expected: 新测试 FAIL，旧 `PUT /admin/jev` 与 `/admin/jev/window` 相关断言需要删除而不是兼容保留。

- [ ] **Step 3: 实现目录 DTO、启动种子和独立直连**

```python
class JevServiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Any = None
    base_url: Any = None
    model: Any = None
    key_env: Any = ""
    api_key: Any = None
    timeout_seconds: Any = 10.0
    instructions: Any = DEFAULT_INSTRUCTIONS
    state_template: Any = DEFAULT_STATE_TEMPLATE
    input_tokens: Any = 1024
    context_tokens: Any = 4096
    impact_confirmation: Any = None
```

`routes_jev.py` 用 `JevService(**values)` 校验，公共响应对每个服务调用：

```python
def public_service(repo, service):
    stored = repo.stored_api_key(service)
    env_present = bool(service.key_env and os.environ.get(service.key_env))
    return {
        **service.snapshot_dict(), **service.window_dict(),
        "key_present": bool(stored or env_present),
        "key_source": "stored" if stored else "env" if env_present else "none",
    }
```

`POST /admin/jev/{service_id}/test` 从仓储取服务并调用 `service.settings(api_key=repo.stored_api_key(service) or os.environ.get(service.key_env, ""))`，不读取 `app.state.snapshot.jev`。

`app.py` 的 `_seed_jev()` 只在 `JevServiceRepo.list()` 为空且 URL/model 成对配置时创建 `default`，不得调用图配置同步或读取活动图。

`routes_models.py` 和 `routes_time.py` 的 GET 始终查询 `ProviderRepo/ModelRepo/TimePeriodRepo`。删除 `_current_runtime()` 及所有以活动图切换数据源的分支。

`routes_test.py` 注入 `conn=Depends(get_conn)` 并传给 `run_chat_completion()`；固定模型分支采用：

```python
direct_backends = load_backends(conn) if fixed_model is not None else None
backend = direct_backends.get(fixed_model) if fixed_model is not None else snapshot.backends.get(decision.model)
if fixed_model is not None and backend is None:
    return _error("unknown_model", 404)
```

Auto 分支仍在 `snapshot is None` 时返回 `no_valid_snapshot`；固定模型分支不得因活动快照为空而失败。balancer 继续按模型 ID 管理 inflight 和熔断。

`ConfigurationService` 的普通目录保存暂时只写数据库并记录审计；删除 `_seed_db_from_active_runtime()` 和 `_sync_graph_runtimes()` 的无确认调用。Task 4/5 会加入确认后的选择性图同步。

- [ ] **Step 4: 运行管理 API、启动和直连回归**

Run: `.venv/bin/python -m pytest -q tests/test_jev_admin.py tests/test_jev_window.py tests/test_admin_models.py tests/test_app.py tests/test_v1.py`
Expected: PASS；代码搜索不再有 `JevConfigRepo`、`/admin/jev/window` 或 `_current_runtime`。

- [ ] **Step 5: 提交本任务**

```bash
git add router/api router/control/config.py router/control/runtime.py router/store/jev_config.py tests/test_jev_admin.py tests/test_jev_window.py tests/test_admin_models.py tests/test_app.py tests/test_v1.py
git commit -m "feat: expose multi-JEV catalog APIs"
```

### Task 4: 模型与 provider 的影响确认、替换删除和原子同步

**Files:**
- Create: `router/control/impacts.py`
- Create: `router/control/catalog_sync.py`
- Create: `tests/test_catalog_impacts.py`
- Modify: `router/control/config.py`
- Modify: `router/api/routes_models.py`
- Modify: `router/store/repositories.py`

**Interfaces:**
- `model_impacts(trees, model_id) -> list[dict]`
- `provider_impacts(trees, provider_name) -> list[dict]`
- `require_confirmation(impacts, confirmation, *, resource, replacements=()) -> None`
- `sync_model(tree, model, providers) -> RoutingTree`
- `delete_model(tree, model_id, replacement=None, providers=()) -> RoutingTree`
- `sync_provider(tree, provider) -> RoutingTree`

- [ ] **Step 1: 写未确认零写入、确认同步和替换失败测试**

在 `tests/test_catalog_impacts.py` 顶部建立真实双树 fixture；Task 4 和 Task 5 共用，不引用其他测试文件的私有 fixture：

```python
import copy
import pytest
from fastapi.testclient import TestClient
from router.api.app import create_app
from tests.routing_fixtures import portable_tree_dict, test_settings


def confirmation_from(response):
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "impact_confirmation_required"
    return {"trees": [
        {"id": item["id"], "document_sha256": item["document_sha256"]}
        for item in detail["affected_trees"]
    ]}


@pytest.fixture
def app_with_two_trees(tmp_path):
    app = create_app(test_settings(tmp_path))
    client = TestClient(app)
    main = portable_tree_dict("main")
    provider = dict(main["backends"]["providers"][0])
    provider.pop("name")
    assert client.put("/admin/providers/local", json=provider).status_code == 200
    fast = copy.deepcopy(main["backends"]["models"][0])
    backup = copy.deepcopy(fast)
    backup.update({"id": "backup", "upstream": "backup-upstream"})
    backup["profile"]["reasoning"] = {"efforts": {"off": None}, "default": "off"}
    assert client.post("/admin/models", json=fast).status_code == 201
    assert client.post("/admin/models", json=backup).status_code == 201
    for service_id in ("jev-a", "jev-b"):
        assert client.post("/admin/jev", json={
            "id": service_id, "base_url": "http://" + service_id + ".test",
            "model": service_id, "input_tokens": 1024, "context_tokens": 4096,
        }).status_code == 201
    main["jev"]["id"] = "jev-a"
    for node in main["root"]["nodes"]:
        if node["type"] == "target":
            node["models"][0]["reasoning"] = "high"
    assert client.post("/admin/trees", json=main).status_code == 201

    other = portable_tree_dict("other")
    other["jev"]["id"] = "jev-b"
    other["backends"]["models"] = [copy.deepcopy(backup)]
    for node in other["root"]["nodes"]:
        if node["type"] == "target":
            node["models"] = [{"model": "backup", "reasoning": None}]
    assert client.post("/admin/trees", json=other).status_code == 201
    assert client.post("/admin/trees/main/enable").status_code == 200
    return client, app.state.graph_repo


def test_model_update_requires_confirmation_and_syncs_only_affected(app_with_two_trees):
    client, trees = app_with_two_trees
    before_other = trees.get("other").document_sha256
    response = client.put("/admin/models/fast", json={"upstream": "new-upstream"})
    assert response.status_code == 409
    impacted = response.json()["detail"]["affected_trees"]
    assert [item["id"] for item in impacted] == ["main"]
    assert trees.get("main").runtime.backends.models[0]["upstream"] == "json-upstream"
    confirmed = client.put("/admin/models/fast", json={
        "upstream": "new-upstream",
        "impact_confirmation": confirmation_from(response),
    })
    assert confirmed.status_code == 200, confirmed.text
    assert trees.get("main").runtime.backends.models[0]["upstream"] == "new-upstream"
    assert trees.get("other").document_sha256 == before_other


def test_delete_model_replaces_target_and_unsupported_reasoning(app_with_two_trees):
    client, trees = app_with_two_trees
    preview = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "backup",
    })
    response = client.request("DELETE", "/admin/models/fast", json={
        "replacement_model_id": "backup",
        "impact_confirmation": confirmation_from(preview),
    })
    assert response.status_code == 200, response.text
    target = next(node for node in trees.get("main").root.nodes if node.type == "target")
    assert target.models[0].model == "backup"
    assert target.models[0].reasoning == "off"
```

再添加四个独立测试：确认删除但不提供替代模型返回 422；把确认哈希改成 64 个 `0` 返回 `graph_conflict`；target 同时引用 fast/backup 时替换后只剩一个 backup；把第二棵候选图预先写成非法引用时，数据库、活动图和第二幅图的字节均保持原值。每个测试都复用上述 fixture 和 `confirmation_from(response)`，断言必须直接检查 API 状态及文件内容。

- [ ] **Step 2: 运行测试并确认影响模块不存在**

Run: `.venv/bin/python -m pytest -q tests/test_catalog_impacts.py`
Expected: FAIL，`ModuleNotFoundError: router.control.impacts`。

- [ ] **Step 3: 实现纯影响函数与事务编排**

```python
# router/control/impacts.py
def _impact(tree, usage, node_id=None):
    return {
        "id": tree.id, "name": tree.name, "enabled": tree.enabled,
        "document_sha256": tree.document_sha256,
        "usage": usage, "node_id": node_id,
    }


def model_impacts(trees, model_id):
    impacts = []
    for tree in trees:
        targets = [node.id for node in tree.root.nodes
                   if any(item.model == model_id for item in getattr(node, "models", ()))
        ]
        catalog = any(model["id"] == model_id for model in tree.runtime.backends.models)
        if targets or catalog:
            impacts.append(_impact(tree, {
                "targets": targets, "runtime_catalog": catalog,
            }))
    return impacts


def require_confirmation(impacts, confirmation, *, resource, replacements=()):
    if not impacts:
        return
    if confirmation is None:
        raise ReferencedByError({
            "code": "impact_confirmation_required",
            "resource": resource,
            "affected_trees": impacts,
            "replacements": list(replacements),
        })
    expected = {item["id"]: item["document_sha256"] for item in impacts}
    supplied = {item.get("id"): item.get("document_sha256")
                for item in (confirmation or {}).get("trees", [])}
    if supplied != expected:
        raise GraphConflictError(",".join(sorted(expected.values())))
```

模型替换必须同时转换 root 和 runtime：

```python
def delete_model(tree, model_id, *, replacement=None, providers=()):
    if any(item.model == model_id for node in tree.root.nodes
           for item in getattr(node, "models", ())):
        if replacement is None:
            raise ValueError("replacement_model_id is required")
    new_root = replace_target_models(
        tree.root, model_id, replacement,
        default_reasoning=reasoning_default(replacement),
        supported_reasoning=reasoning_levels(replacement),
    )
    models = [model for model in tree.runtime.backends.models if model["id"] != model_id]
    if replacement is not None and all(model["id"] != replacement["id"] for model in models):
        models.append(copy.deepcopy(replacement))
    runtime = replace_runtime_catalog(
        tree.runtime, models=models, providers=merge_providers(
            tree.runtime.backends.providers, providers,
        ),
    )
    return replace(tree, root=new_root, runtime=runtime)
```

`replace_target_models()`、`reasoning_default()`、`reasoning_levels()`、`replace_runtime_catalog()` 和 `merge_providers()` 都在 `impacts.py` 定义并对输入做深拷贝；不修改调用方原树。替换后按模型 ID 去重并保留槽位顺序。

`catalog_sync.py` 提供以下完整事务骨架；`db_write(conn)` 和 `audit(conn)` 由调用方提供，`candidates` 是保留原 `document_sha256` 的新树列表：

```python
from router.control.errors import ValidationFailed
from router.control.validation import tree_issues
from router.store.repositories import write_transaction
from router.store.routing_trees import GraphChangedDuringLoad


class CatalogSyncService:
    def __init__(self, conn, trees, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    def commit(self, *, db_write, candidates, audit):
        batch = None
        with self.publisher.locked():
            try:
                with write_transaction(self.conn):
                    db_write(self.conn)
                    for tree in candidates:
                        issues = tree_issues(self.conn, tree)
                        if issues:
                            raise ValidationFailed(issues)
                    if candidates:
                        batch = self.trees.stage_updates([
                            (tree, tree.document_sha256) for tree in candidates
                        ])
                        batch.commit()
                    active = self.trees.active()
                    active_changed = active is not None and any(
                        tree.id == active.id for tree in candidates)
                    fingerprint = self.trees.fingerprint()
                    prepared = self.publisher.prepare(self.conn, active, fingerprint)
                    if fingerprint != self.trees.fingerprint():
                        raise GraphChangedDuringLoad(
                            "graph changed between candidate build and snapshot publish")
                    audit(self.conn)
                if batch is not None:
                    batch.discard()
                if active_changed:
                    self.publisher.publish(prepared)
            except BaseException:
                if batch is not None:
                    batch.rollback()
                raise
```

候选为空时不创建批次，也不发布活动快照。`GraphBatch` 已保证非活动图先写、活动图最后写；这里不另写文件替换逻辑。

provider 删除保留两道检查：`model_ids_using_provider()` 非空时返回数据库模型引用；`provider_impacts()` 非空时返回画布引用，不能用确认绕过。

- [ ] **Step 4: 运行影响与原有模型管理回归**

Run: `.venv/bin/python -m pytest -q tests/test_catalog_impacts.py tests/test_admin_models.py tests/test_routing_trees_admin.py`
Expected: PASS；未确认响应前后数据库和文件字节完全一致。

- [ ] **Step 5: 提交本任务**

```bash
git add router/control/impacts.py router/control/catalog_sync.py router/control/config.py router/api/routes_models.py router/store/repositories.py tests/test_catalog_impacts.py tests/test_admin_models.py tests/test_routing_trees_admin.py
git commit -m "feat: sync confirmed model and provider impacts"
```

### Task 5: JEV 更新与删除的影响同步

**Files:**
- Modify: `router/control/impacts.py`
- Modify: `router/control/config.py`
- Modify: `router/api/routes_jev.py`
- Modify: `tests/test_catalog_impacts.py`
- Modify: `tests/test_jev_admin.py`

**Interfaces:**
- `jev_impacts(trees, service_id) -> list[dict]`
- `sync_jev(tree, service: JevService) -> RoutingTree`
- `delete_jev(tree, service_id, replacement: JevService) -> RoutingTree`

- [ ] **Step 1: 写 JEV 更新、替换删除和窗口同步失败测试**

```python
def test_jev_update_syncs_service_and_window_only_to_users(app_with_two_trees):
    client, trees = app_with_two_trees
    response = client.put("/admin/jev/jev-a", json={
        "base_url": "http://jev-a-v2.test", "model": "d1-a-v2",
        "input_tokens": 3072, "context_tokens": 8192,
    })
    assert response.status_code == 409
    impacted = response.json()["detail"]["affected_trees"]
    confirmed = client.put("/admin/jev/jev-a", json={
        "base_url": "http://jev-a-v2.test", "model": "d1-a-v2",
        "input_tokens": 3072, "context_tokens": 8192,
        "impact_confirmation": {"trees": [
            {"id": item["id"], "document_sha256": item["document_sha256"]}
            for item in impacted
        ]},
    })
    assert confirmed.status_code == 200, confirmed.text
    runtime = trees.get("main").runtime
    assert runtime.jev.base_url == "http://jev-a-v2.test"
    assert runtime.jev_input_tokens == 3072 and runtime.jev_context_tokens == 8192


def test_delete_jev_requires_replacement_then_syncs(app_with_two_trees):
    client, trees = app_with_two_trees
    before_other = trees.get("other").document_sha256
    preview = client.request("DELETE", "/admin/jev/jev-a", json={})
    assert preview.status_code == 409
    confirmation = confirmation_from(preview)
    confirmed_without_replacement = client.request(
        "DELETE", "/admin/jev/jev-a",
        json={"impact_confirmation": confirmation},
    )
    assert confirmed_without_replacement.status_code == 422
    confirmed = client.request("DELETE", "/admin/jev/jev-a", json={
        "replacement_jev_id": "jev-b", "impact_confirmation": confirmation,
    })
    assert confirmed.status_code == 200, confirmed.text
    assert trees.get("main").runtime.jev.id == "jev-b"
    assert trees.get("other").document_sha256 == before_other
```

- [ ] **Step 2: 运行测试并确认缺少 JEV 影响实现**

Run: `.venv/bin/python -m pytest -q tests/test_catalog_impacts.py -k jev`
Expected: FAIL，当前没有 `jev_impacts` 或替换同步。

- [ ] **Step 3: 实现 JEV 候选转换和确认写入**

```python
def jev_impacts(trees, service_id):
    return [_impact(tree, {"jev": service_id})
            for tree in trees
            if tree.runtime.jev is not None and tree.runtime.jev.id == service_id]


def sync_jev(tree, service):
    runtime = replace(
        tree.runtime,
        jev=service,
        jev_input_tokens=service.input_tokens,
        jev_context_tokens=service.context_tokens,
    )
    return replace(tree, runtime=runtime)


def delete_jev(tree, service_id, *, replacement):
    if tree.runtime.jev is not None and tree.runtime.jev.id == service_id:
        if replacement is None:
            raise ValueError("replacement_jev_id is required")
        return sync_jev(tree, replacement)
    return tree
```

PUT 只更新 `jev_impacts()` 返回的画布。DELETE 先收集影响；有影响且未提供替代服务时允许返回 409 影响清单，但确认请求必须带有效 `replacement_jev_id`，否则 422。替代服务不存在返回 404。所有候选经 `tree_issues()` 后才可提交。

- [ ] **Step 4: 运行 JEV 影响和 API 回归**

Run: `.venv/bin/python -m pytest -q tests/test_catalog_impacts.py tests/test_jev_admin.py tests/test_jev_services.py`
Expected: PASS；更新 JEV 后窗口来自同一服务，未受影响画布字节不变。

- [ ] **Step 5: 提交本任务**

```bash
git add router/control/impacts.py router/control/config.py router/api/routes_jev.py tests/test_catalog_impacts.py tests/test_jev_admin.py
git commit -m "feat: sync confirmed JEV impacts"
```

### Task 6: 检查路由的数据库差异提示

**Files:**
- Create: `router/control/consistency.py`
- Create: `tests/test_runtime_consistency.py`
- Modify: `router/api/routes_trees.py`
- Modify: `router/api/issue_presenter.py`
- Modify: `router/console/routing/tryrun.js`

**Interfaces:**
- `runtime_mismatch_issues(conn, runtime: GraphRuntime) -> list[Issue]`
- Issue `code='runtime_mismatch'`，`level='hint'`，`params` 包含 `resource_kind`、`resource_id`、`field`、`canvas`、`database`；敏感值永远为空。

- [ ] **Step 1: 写差异、缺失和额外目录项测试**

在 `tests/test_runtime_consistency.py` 顶部用 `from tests.test_self_contained_e2e import json_only` 显式引入 fixture，并使用以下完整测试：

```python
def test_mismatch_is_warning_and_execution_still_uses_canvas(json_only):
    _app, client, _settings, calls = json_only
    response = client.post("/admin/try", json={
        "tree_id": "portable", "text": "测试", "now": "2026-11-20T10:00:00+08:00",
    })
    assert response.status_code == 200
    warnings = response.json()["warnings"]
    assert any(item["code"] == "runtime_mismatch" and
               item["params"]["resource_id"] == "fast" for item in warnings)
    assert response.json()["outcome"]["model"] == "fast"
    assert calls["jev"][0]["model"] == "json-jev"


def test_extra_database_models_do_not_warn(json_only):
    _app, client, settings, _calls = json_only
    conn = connect(settings.db_path)
    with conn:
        conn.execute("INSERT INTO providers VALUES ('extra', 'http://extra.test/v1', '', 'openai', '')")
        conn.execute("INSERT INTO models VALUES ('unused-model', 'unused', 'extra', '{}')")
    conn.close()
    response = client.post("/admin/try", json={"tree_id": "portable", "text": "测试"})
    assert response.status_code == 200, response.text
    assert not any(item.get("params", {}).get("resource_id") == "unused-model"
                   for item in response.json()["warnings"])
```

增加以下直接针对比较器的参数化测试；`dataclasses.replace`、JSON 和仓储均在测试文件顶部显式 import：

```python
@pytest.mark.parametrize(("resource_id", "field", "canvas_value", "database_value"), [
    ("json-jev-service", "base_url", "http://jev-json.test", "http://database-jev.test"),
    ("json-jev-service", "input_tokens", 1024, 2048),
    ("local", "base_url", "http://json-only.test/v1", "http://database.test/v1"),
    ("fast", "upstream", "json-upstream", "database-upstream"),
    ("fast", "profile.capability", "来自 JSON 的能力", "数据库能力"),
])
def test_mismatch_reports_exact_fields(
        json_only, resource_id, field, canvas_value, database_value):
    app, _client, settings, _calls = json_only
    runtime = app.state.graph_repo.active().runtime
    conn = connect(settings.db_path)
    for provider in runtime.backends.providers:
        ProviderRepo(conn).upsert(
            provider["name"], provider["base_url"], key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"])
    for model in runtime.backends.models:
        ModelRepo(conn).create(
            model["id"], model["upstream"], model["provider"],
            json.dumps(model["profile"], ensure_ascii=False))
    JevServiceRepo(conn).save(runtime.jev)
    if resource_id == "json-jev-service":
        JevServiceRepo(conn).save(replace(runtime.jev, **{field: database_value}))
    elif resource_id == "local":
        provider = runtime.backends.providers[0]
        ProviderRepo(conn).upsert(
            "local", database_value, key_env=provider["key_env"],
            reasoning_format=provider["reasoning_format"])
    elif field == "upstream":
        ModelRepo(conn).update("fast", upstream=database_value)
    else:
        profile = dict(runtime.backends.models[0]["profile"])
        profile["capability"] = database_value
        ModelRepo(conn).update("fast", profile=json.dumps(profile, ensure_ascii=False))
    issue = next(item for item in runtime_mismatch_issues(conn, runtime)
                 if item.params["resource_id"] == resource_id and
                 item.params["field"] == field)
    assert issue.params["canvas"] == canvas_value
    assert issue.params["database"] == database_value
    conn.close()
```

另以数据库完全为空的 `json_only` 验证缺失服务/模型会提示。所有响应和日志继续断言不包含 `ENV_SECRET`。`issues_payload()` 必须新增 `"params": issue.params or {}`，否则这些字段不会传到控制台。

- [ ] **Step 2: 运行测试并确认新模块不存在**

Run: `.venv/bin/python -m pytest -q tests/test_runtime_consistency.py`
Expected: FAIL，`ModuleNotFoundError: router.control.consistency`。

- [ ] **Step 3: 实现只读比较和提示展示**

```python
def runtime_mismatch_issues(conn, runtime):
    issues = []
    providers = {row["name"]: row for row in ProviderRepo(conn).list()}
    models = {row["id"]: row for row in ModelRepo(conn).list()}
    services = JevServiceRepo(conn)
    for provider in runtime.backends.providers:
        current = providers.get(provider["name"])
        for field in ("base_url", "key_env", "reasoning_format"):
            if current is None or current.get(field) != provider.get(field):
                issues.append(mismatch(
                    "provider", provider["name"], field,
                    provider.get(field), None if current is None else current.get(field),
                ))
    for model in runtime.backends.models:
        current = models.get(model["id"])
        # upstream/provider 直接比较；profile 解析后走同一 normalize/validate 投影。
        issues.extend(compare_model(model, current))
    issues.extend(compare_jev(runtime, services))
    return issues
```

`compare_model()` 和 `compare_jev()` 也在 `consistency.py` 完整实现；JEV 比较按 `id` 查服务并覆盖全部 `snapshot_dict()` 字段及两个窗口字段。`mismatch()` 只复制非密钥白名单字段，禁止传入 `api_key` 或密钥状态。

`/admin/try` 将现有启用级 warnings 与 `runtime_mismatch_issues()` 合并；保存和启用不调用此函数。`tryrun.js` 以 hint/warning 样式显示 resource ID 和字段，不把提示升级为运行失败。

- [ ] **Step 4: 运行一致性与自包含端到端测试**

Run: `.venv/bin/python -m pytest -q tests/test_runtime_consistency.py tests/test_self_contained_e2e.py`
Expected: PASS；空库会提示未同步，但 JEV/模型 mock 调用仍完全使用 JSON。

- [ ] **Step 5: 提交本任务**

```bash
git add router/control/consistency.py router/api/routes_trees.py router/api/issue_presenter.py router/console/routing/tryrun.js tests/test_runtime_consistency.py tests/test_self_contained_e2e.py
git commit -m "feat: report canvas and catalog drift"
```

### Task 7: 五份案例自包含化与部署脚本单一事实源

**Files:**
- Modify: `configs/cases/case-industry.json`
- Modify: `configs/cases/case-multimodal.json`
- Modify: `configs/cases/case-reasoning.json`
- Modify: `configs/cases/case-logic.json`
- Modify: `configs/cases/case-ha.json`
- Modify: `configs/default_routing_tree.json`, `configs/production_routing_tree.json`, `router/console/routing/example.json`
- Modify: `scripts/demo/apply_cases.py`, `scripts/demo/run_cases.py`
- Create: `tests/test_case_documents.py`

**Interfaces:**
- 案例文件是完整 v3 文档，不含 `id/enabled`；`manifest.json` 的 case ID 是部署 ID。
- `apply_cases.py` 的 `case_runtime(cases) -> dict` 从案例文件合并数据库目录，不再包含 `MODEL_SPECS`。
- 默认/生产/示例 JEV ID 分别为 `default`；案例统一为 `demo-jev`。

- [ ] **Step 1: 写所有案例可独立解析和引用完整测试**

```python
import json
from pathlib import Path
import pytest
from router.routing.tree import tree_from_dict
from router.control.validation import tree_issues

CASES = Path("configs/cases")


@pytest.mark.parametrize("path", sorted(CASES.glob("case-*.json")))
def test_case_is_self_contained(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["kind"] == "sr-routing-graph" and raw["version"] == 3
    assert "id" not in raw and "enabled" not in raw
    raw["id"] = path.stem
    raw["enabled"] = False
    tree = tree_from_dict(raw)
    assert tree.runtime.jev.id == "demo-jev"
    assert tree.runtime.jev.base_url == "http://127.0.0.1:30104"
    assert tree.runtime.jev_input_tokens == 2048
    assert tree_issues(None, tree) == []
    used = {item.model for node in tree.root.nodes for item in getattr(node, "models", ())}
    embedded = {model["id"] for model in tree.runtime.backends.models}
    assert used == embedded
    assert "api_key" not in path.read_text(encoding="utf-8")
```

另断言五个案例的模型集合分别为设计文档列出的 `6/2/3/2/4` 个，案例 provider 都是 `demo-tap`，默认/生产/示例均包含 JEV `id` 和 `key_env`。

- [ ] **Step 2: 运行案例测试并确认缺字段失败**

Run: `.venv/bin/python -m pytest -q tests/test_case_documents.py`
Expected: 五个案例均因缺 `kind/version` 和运行字段 FAIL。

- [ ] **Step 3: 回填真实案例配置并精简脚本**

每份案例在 `root` 后添加完整 `backends/jev/jev_window/time_periods`。JEV 文案使用 `docs/案例演示-2026-11-03/evidence/platform.json` 中已验证的 `timeout_seconds=10.0`、默认 instructions/state template；模型 profile 逐项取自现有 `apply_cases.py`，不得改写 capability 或推理档位。`case-logic` 内嵌白天、夜间定义；其他无时段引用的案例使用 `time_periods=[]`。

`apply_cases.py` 用以下纯函数替代硬编码模型清单：

```python
def case_runtime(case_documents):
    groups = {"providers": {}, "models": {}, "jev_services": {}, "time_periods": {}}

    def merge(group, key, value):
        if key in groups[group] and groups[group][key] != value:
            raise ValueError(f"conflicting {group} entry: {key}")
        groups[group][key] = value

    for document in case_documents:
        for provider in document["backends"]["providers"]:
            merge("providers", provider["name"], provider)
        for model in document["backends"]["models"]:
            merge("models", model["id"], model)
        service = {**document["jev"], **document["jev_window"]}
        merge("jev_services", service["id"], service)
        for period in document["time_periods"]:
            merge("time_periods", period["name"], period)
    return {name: list(values.values()) for name, values in groups.items()}
```

若同一 ID 在两个案例中配置不同，抛出 `ValueError` 并指出冲突 ID，不能后写覆盖先写。`upsert_tree(tree_id, document)` 从 manifest 取得 ID，POST/PUT 时剔除 `kind/version` 并携带四个运行字段。所有 API 409 `impact_confirmation_required` 默认打印影响清单并退出；新增 `--confirm-sync` 时才获取基线哈希并重试，不自动绕过确认。

`run_cases.py` 的 root-only 探针同时携带被引用树的四个运行字段，或携带 `tree_id` 让服务端继承该已存快照。

- [ ] **Step 4: 运行案例与自包含回归**

Run: `.venv/bin/python -m pytest -q tests/test_case_documents.py tests/test_self_contained_e2e.py tests/test_graph_runtime.py`
Expected: PASS；`apply_cases.py` 中搜索不到 `MODEL_SPECS`，五份案例无需数据库即可通过启用级校验。

- [ ] **Step 5: 提交本任务**

```bash
git add configs router/console/routing/example.json scripts/demo/apply_cases.py scripts/demo/run_cases.py tests/test_case_documents.py
git commit -m "feat: make demo cases self-contained"
```

### Task 8: 多 JEV 控制台目录页

**Files:**
- Modify: `router/console/jev.html`
- Modify: `router/console/jev.js`
- Modify: `router/console/jev-view.js`
- Modify: `router/console/i18n.js`
- Modify: `router/console/shared.css`
- Create: `scripts/console_qa/jev.mjs`
- Modify: `scripts/console_qa.mjs`

**Interfaces:**
- 页面动作：`listServices()`、`createService(draft)`、`updateService(id, draft)`、`deleteService(id, confirmation)`、`testService(id, text)`。
- 编辑 draft 包含连接、提示词、State、`key_env`、输入/上下文窗口；Key 输入只用于写请求，不在列表状态中保存。

- [ ] **Step 1: 写多服务页面失败用例**

`scripts/console_qa/jev.mjs` 复用现有 QA 基座，不创建第二套浏览器或 mock。以下 helper 和选择器就是本任务的页面契约：

```javascript
import assert from 'node:assert/strict';
import { page, routerBase, runIfMain, test } from '../console_qa.mjs';

async function createJev({ id, model, input }) {
  await page.locator('#jevNew').click();
  await page.locator('#jevServiceId').fill(id);
  await page.locator('#jevBaseUrl').fill('http://' + id + '.test');
  await page.locator('#jevModel').fill(model);
  await page.locator('#jevInputTokens').fill(String(input));
  await page.locator('#jevContextTokens').fill('4096');
  await page.locator('#jevSave').click();
  await page.waitForSelector('[data-jev-service-id="' + id + '"]');
}

test('creates and selects two JEV services with independent windows', async () => {
  await page.goto(routerBase + '/console/jev.html');
  await createJev({ id: 'jev-a', model: 'd1-a', input: 1024 });
  await createJev({ id: 'jev-b', model: 'd1-b', input: 2048 });
  const ids = await page.locator('[data-jev-service-id]').evaluateAll(
    elements => elements.map(element => element.dataset.jevServiceId).sort());
  assert.deepEqual(ids, ['jev-a', 'jev-b']);
  await page.locator('[data-jev-service-id="jev-a"]').click();
  assert.equal(await page.locator('#jevInputTokens').inputValue(), '1024');
  await page.locator('[data-jev-service-id="jev-b"]').click();
  assert.equal(await page.locator('#jevInputTokens').inputValue(), '2048');
  assert.equal(await page.locator('text=secret-sample').count(), 0);
});

test('language switch and failed test preserve the current service draft', async () => {
  await page.goto(routerBase + '/console/jev.html');
  await page.locator('[data-jev-service-id="jev-b"]').click();
  await page.locator('#jevInstructions').fill('未保存的多服务提示词');
  await page.locator('.langtog button[data-lang="en"]').click();
  assert.equal(await page.locator('#jevInstructions').inputValue(), '未保存的多服务提示词');
  await page.locator('#jevTest').click();
  assert.equal(await page.locator('#jevInputTokens').inputValue(), '2048');
  assert.equal(await page.locator('#jevInstructions').inputValue(), '未保存的多服务提示词');
});

await runIfMain(import.meta.url);
```

删除时的影响清单在 Task 10 的 `scripts/console_impact_test.mjs` 和本文件共同验证，不在这里伪造成功结果。

- [ ] **Step 2: 运行页面用例并确认旧单服务页面失败**

Run: `node scripts/console_qa/jev.mjs`
Expected: FAIL，页面没有 `#jevNew`、服务列表和按服务编辑窗口。

- [ ] **Step 3: 实现目录页面和异步动作**

```javascript
export async function createService(draft) {
  const body = nonSecretServiceBody(draft);
  if (draft.clearKey) body.api_key = '';
  else if (draft.apiKey) body.api_key = draft.apiKey;
  const saved = await api.post('/admin/jev', body);
  return saved.service;
}

export async function updateService(id, draft, impactConfirmation) {
  const body = nonSecretServiceBody(draft);
  if (draft.clearKey) body.api_key = '';
  else if (draft.apiKey) body.api_key = draft.apiKey;
  if (impactConfirmation) body.impact_confirmation = impactConfirmation;
  const saved = await api.put('/admin/jev/' + encodeURIComponent(id), body);
  return saved.service;
}
```

`nonSecretServiceBody(draft)` 使用完整白名单实现：

```javascript
function nonSecretServiceBody(draft) {
  return {
    id: draft.id,
    base_url: draft.baseUrl,
    model: draft.model,
    key_env: draft.keyEnv || '',
    timeout_seconds: Number(draft.timeoutSeconds || 10),
    instructions: draft.instructions,
    state_template: draft.stateTemplate,
    input_tokens: Number(draft.inputTokens),
    context_tokens: Number(draft.contextTokens),
  };
}
```

`jev-view.js` 以当前选中服务 ID 渲染列表和编辑态；切换服务前保留各自草稿，窗口不得沿用上一个服务。连接测试调用 `/admin/jev/{id}/test`，不再调用旧路径。

- [ ] **Step 4: 运行页面和既有控制台检查**

Run: `node scripts/console_qa/jev.mjs && npm run check:console`
Expected: 新增页面用例 PASS；既有控制台检查无回归。

- [ ] **Step 5: 提交本任务**

```bash
git add router/console/jev.html router/console/jev.js router/console/jev-view.js router/console/i18n.js router/console/shared.css scripts/console_qa/jev.mjs scripts/console_qa.mjs
git commit -m "feat(console): manage multiple JEV services"
```

### Task 9: 画布 JEV 选择、模型快照补入与严格前端契约

**Files:**
- Create: `router/console/routing/runtime.js`
- Create: `scripts/console_runtime_test.mjs`
- Modify: `router/console/routing/graph.js`
- Modify: `router/console/routing/controller.js`
- Modify: `router/console/routing/app.js`
- Modify: `router/console/routing/inspector.js`
- Modify: `router/console/routing/tryrun.js`
- Modify: `router/console/i18n.js`
- Modify: `scripts/graph_contract_test.mjs`, `scripts/console_graph_test.mjs`, `scripts/console_draft_test.mjs`

**Interfaces:**
- `createRuntime(catalog, { modelId, jevId }) -> runtime`
- `selectJev(runtime, serviceOrNull) -> runtime`
- `addRuntimeModel(runtime, model, provider) -> runtime`
- `createMinimalTree(model, runtime)` 必须收到完整 runtime。
- `selectJev()` 和 `addRuntimeModel()` 返回深拷贝，不修改原对象。

- [ ] **Step 1: 写纯函数和严格导入失败测试**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRuntime, selectJev, addRuntimeModel } from '../router/console/routing/runtime.js';

const a = { id: 'jev-a', base_url: 'http://a.test', model: 'd1-a', key_env: '',
  timeout_seconds: 10, instructions: '选择', state_template: '{request}',
  input_tokens: 1024, context_tokens: 4096 };
const b = { ...a, id: 'jev-b', base_url: 'http://b.test', model: 'd1-b',
  input_tokens: 2048, context_tokens: 8192 };
const local = { name: 'local', base_url: 'http://json.test/v1', key_env: '',
  reasoning_format: 'openai' };
const backupProvider = { ...local, name: 'backup-provider' };
const fast = { id: 'fast', upstream: 'json-upstream', provider: 'local', profile: {} };
const backup = { id: 'backup', upstream: 'backup-upstream', provider: 'backup-provider', profile: {} };
const catalog = { providers: [local, backupProvider], models: [fast, backup],
  services: [a, b], periods: [] };

test('JEV selection replaces service and window atomically', () => {
  const runtime = createRuntime(catalog, { modelId: 'fast', jevId: 'jev-a' });
  const next = selectJev(runtime, b);
  assert.equal(next.jev.id, 'jev-b');
  assert.deepEqual(next.jev_window, { input_tokens: 2048, context_tokens: 8192 });
  assert.equal(runtime.jev.id, 'jev-a');
  assert.equal(JSON.stringify(next).includes('api_key'), false);
});

test('model selection embeds new entries without overwriting existing model', () => {
  const runtime = createRuntime(catalog, { modelId: 'fast', jevId: 'jev-a' });
  let next = addRuntimeModel(runtime, { ...fast, upstream: 'changed' },
    { ...local, base_url: 'http://changed.test/v1' });
  assert.equal(next.backends.models[0].upstream, 'json-upstream');
  assert.equal(next.backends.providers[0].base_url, 'http://json.test/v1');
  next = addRuntimeModel(next, backup, backupProvider);
  assert.equal(next.backends.models[1].upstream, 'backup-upstream');
  assert.equal(next.backends.providers[1].name, 'backup-provider');
});
```

在 `scripts/graph_contract_test.mjs` 增加：缺任一运行字段拒绝、JEV 缺 `id` 拒绝、`runtime:null` 拒绝、完整文档序列化后四字段和坐标保留。

- [ ] **Step 2: 运行前端契约并确认失败**

Run: `node --test scripts/console_runtime_test.mjs scripts/graph_contract_test.mjs scripts/console_draft_test.mjs`
Expected: 新模块不存在或当前 `createMinimalTree` 无 runtime 导致 FAIL。

- [ ] **Step 3: 实现纯运行时函数并接入画布**

```javascript
export function selectJev(runtime, service) {
  const next = structuredClone(runtime);
  if (service === null) {
    next.jev = null;
    return next;
  }
  const { input_tokens, context_tokens, api_key, key_present, key_source, ...snapshot } = service;
  next.jev = snapshot;
  next.jev_window = { input_tokens, context_tokens };
  return next;
}

export function addRuntimeModel(runtime, model, provider) {
  const next = structuredClone(runtime);
  if (!next.backends.models.some(item => item.id === model.id)) {
    next.backends.models.push(publicModel(model));
  }
  if (!next.backends.providers.some(item => item.name === provider.name)) {
    next.backends.providers.push(publicProvider(provider));
  }
  return next;
}
```

`publicModel()` 只复制 `id/upstream/provider/profile`；`publicProvider()` 只复制 `name/base_url/key_env/reasoning_format`。`createRuntime()` 从当前数据库目录选择首个模型和指定 JEV，同时生成 `backends/jev/jev_window/time_periods`，没有目录项时给出明确错误而不是创建空 runtime。

`app.js` 启动时加载 providers/models/JEV/periods；只有已存树可不等待目录，创建新树前必须 await 目录。`controller.js` 删除 `runtime:null` 保存分支，`newDraftTree()` 必须收到 `createRuntime()` 结果。

`inspector.js` 的 `graphBranch()` 增加 JEV select 和“从数据库刷新运行配置”按钮。应用图设置时在一个 `store.change('behavior', ...)` 中写名称、时区、粘性、fallback 和新 JEV/runtime；含 question 且选择 null 时返回本地硬错误。target 模型提交调用 `addRuntimeModel()`，同 ID 已存在时不覆盖快照。

`graph.js` 的导入、运行时成组校验、序列化和 canonical 全部只接受完整 runtime；删除旧文档分支。`tryrun.js` 的完整草稿请求携带四字段；root-only 且引用已存树时由后端继承该树快照。

- [ ] **Step 4: 运行全部前端契约与画布检查**

Run: `node --test scripts/console_runtime_test.mjs scripts/graph_contract_test.mjs scripts/console_graph_test.mjs scripts/console_draft_test.mjs && npm run check:console`
Expected: PASS；导出 JSON 无 `runtime:null`、无 `api_key`，JEV 选择和撤销/重做同步窗口。

- [ ] **Step 5: 提交本任务**

```bash
git add router/console/routing router/console/i18n.js scripts/console_runtime_test.mjs scripts/graph_contract_test.mjs scripts/console_graph_test.mjs scripts/console_draft_test.mjs
git commit -m "feat(console): edit self-contained canvas runtime"
```

### Task 10: 模型、provider、JEV 共用的影响确认界面

**Files:**
- Create: `router/console/impact.js`
- Create: `scripts/console_impact_test.mjs`
- Modify: `router/console/models.js`
- Modify: `router/console/models.html`
- Modify: `router/console/jev.js`
- Modify: `router/console/jev.html`
- Modify: `router/console/i18n.js`
- Modify: `router/console/shared.css`
- Modify: `scripts/console_qa/models.mjs`, `scripts/console_qa/jev.mjs`

**Interfaces:**
- `isImpactError(error) -> boolean`
- `openImpact({ resource, detail, replacements, onConfirm }) -> Promise<boolean>`
- `withImpactConfirmation(request, detail, replacementId) -> Promise<result>`

- [ ] **Step 1: 写影响确认纯逻辑和页面失败测试**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import { confirmationFrom, isImpactError } from '../router/console/impact.js';

test('builds exact tree hash confirmation from 409 detail', () => {
  const detail = { code: 'impact_confirmation_required', affected_trees: [
    { id: 'main', document_sha256: 'a'.repeat(64) },
    { id: 'backup', document_sha256: 'b'.repeat(64) },
  ] };
  assert.ok(isImpactError({ status: 409, detail }));
  assert.deepEqual(confirmationFrom(detail), { trees: [
    { id: 'main', document_sha256: 'a'.repeat(64) },
    { id: 'backup', document_sha256: 'b'.repeat(64) },
  ] });
});
```

Playwright 用例必须验证：第一次保存后只出现清单、服务器未收到第二次写请求；取消不发送确认；模型/JEV 删除必须选替代项；确认后请求带精确哈希和 replacement ID；过期哈希显示重新加载提示。

- [ ] **Step 2: 运行影响界面测试并确认模块不存在**

Run: `node --test scripts/console_impact_test.mjs`
Expected: FAIL，`router/console/impact.js` 不存在。

- [ ] **Step 3: 实现共享确认流程并接入三个资源页面**

```javascript
export function confirmationFrom(detail) {
  return {
    trees: (detail.affected_trees || []).map(tree => ({
      id: tree.id, document_sha256: tree.document_sha256,
    })),
  };
}

export async function withImpactConfirmation(request, detail, replacementId) {
  if (!isImpactError(detail)) throw detail;
  const accepted = await openImpact({
    resource: detail.resource,
    detail,
    replacements: detail.replacements || [],
    onConfirm: async replacement => request({
      impact_confirmation: confirmationFrom(detail),
      replacement_id: replacement && replacement.id,
    }),
  });
  return accepted;
}
```

实际 API 字段在调用方映射为 `replacement_model_id` 或 `replacement_jev_id`，不得把通用 `replacement_id` 直接发给后端。影响框列出名称、ID、启用状态、target/runtime/JEV 使用位置；确认按钮在需要替代项但未选择时禁用。

`models.js` 的编辑保存、模型删除和 provider 保存删除都改用同一流程，删除原来的两段式按钮作为唯一确认机制。`jev.js` 的更新和删除复用同一组件。成功后重新加载目录和受影响画布列表，不假定活动画布已更新。

- [ ] **Step 4: 运行影响界面和完整控制台检查**

Run: `node --test scripts/console_impact_test.mjs && node scripts/console_qa/jev.mjs && npm run check:console`
Expected: PASS；取消和未选择替代项均无第二次写请求。

- [ ] **Step 5: 提交本任务**

```bash
git add router/console/impact.js router/console/models.js router/console/models.html router/console/jev.js router/console/jev.html router/console/i18n.js router/console/shared.css scripts/console_impact_test.mjs scripts/console_qa/models.mjs scripts/console_qa/jev.mjs
git commit -m "feat(console): confirm affected canvas synchronization"
```

### Task 11: 文档清理与完整端到端验收

**Files:**
- Modify: `README.md`, `README.zh-CN.md`
- Modify: `docs/案例演示-2026-11-03/cases.md`
- Modify: `router/upstream/catalog.py` and every file whose comments still describe legacy runtime behavior
- Modify: `tests/test_self_contained_e2e.py`
- Modify: `tests/test_architecture.py`

**Interfaces:**
- 文档只描述严格自包含、多 JEV、影响确认和快照语义。
- 最终代码搜索不得存在旧图物化、旧单 JEV API 或数据库回退路径。

- [ ] **Step 1: 写最终架构守卫与真实双实例验收**

```python
def test_no_legacy_runtime_fallback_symbols():
    forbidden = (
        "runtime: null", "_materialize", "effective_runtime",
        "runtime_for_tree", "JevConfigRepo", "/admin/jev/window",
    )
    for path in Path("router").rglob("*"):
        if path.suffix not in {".py", ".js", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path}: {marker}"
```

扩充 `tests/test_self_contained_e2e.py`：

1. 第一个空库实例从 JSON 选择 `jev-a` 和 `fast`，真实 mock JEV/模型调用成功。
2. 第二个空库实例仅复制导出字节，结果与 `tree_revision` 相同。
3. 第二个实例数据库配置完全不同的 JEV/模型，Auto 仍走 JSON，固定模型直连走数据库。
4. 数据库目录修改未确认时，两个字节文件和数据库行保持不变。
5. 确认修改后只有受影响 JSON 改变，活动实例发布新快照。
6. 全流程搜索 `api_key`、Authorization 样本和密钥状态，确保不进入图文件、图 API 或快照内容。

- [ ] **Step 2: 运行新验收并修复剩余旧行为**

Run: `.venv/bin/python -m pytest -q tests/test_architecture.py tests/test_self_contained_e2e.py`
Expected: PASS；若搜索命中旧注释或 docstring，同步改成当前行为，不通过删除架构测试绕过。

- [ ] **Step 3: 更新文档和过期注释**

README 必须明确：

- 四字段必填，旧文档直接拒绝，没有 `runtime:null` 或物化步骤；
- `jev.id/key_env` 与每服务窗口；
- 新建/导入/显式刷新/影响确认是仅有的快照来源；
- 数据库 mismatch 只是检查提示；
- 模型/JEV 删除需要替代项，provider 引用未解除时不能删除；
- 五个案例可直接导入，`apply_cases.py --confirm-sync` 是显式确认入口；
- 固定模型直连使用数据库目录，Auto 使用活动画布。

同时更新 `router/upstream/catalog.py` 顶部“每次 admin 写 model/provider 重建快照”的旧注释，以及所有仍描述活动图投影、单 JEV、全局批量回写的注释。

- [ ] **Step 4: 运行完整标准验证**

```bash
.venv/bin/python -m pytest -q
npm run check:console
node --test scripts/console_runtime_test.mjs scripts/console_impact_test.mjs && node scripts/console_qa/jev.mjs
```

Expected: 全部 PASS，无跳过本功能测试。随后检查：

```bash
git status --short
```

只保留计划内源码、测试、配置和文档修改；删除测试产生的临时日志、截图或数据库，不删除用户原有文件。

- [ ] **Step 5: 提交最终实现**

```bash
git add README.md README.zh-CN.md docs/案例演示-2026-11-03/cases.md router tests configs scripts
git commit -m "feat: complete portable canvas runtime and catalog synchronization"
```

最后一个命令只允许执行智能体在逐项核对暂存清单后使用；若工作区还有用户的未提交改动，必须改为明确文件列表，不得把无关改动纳入提交。
