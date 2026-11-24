# RoutePilot 部门路由独立模式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在同一个 RoutePilot 镜像和 `main` 代码线中完成 v4 图文档、多图启用、部门设置、唯一默认图、不可变快照集及配套控制台，不提前接入 APISIX。

**Architecture:** 每份图 JSON 自描述 `departments/enabled/default`，仓储扫描全部启用图，发布不可变 `RoutingSnapshotSet`。公开 `/v1` 只选默认图，管理端试算可按部门验证选图；第二阶段才复用同一执行器增加 APISIX 内部数据面。

**Tech Stack:** Python 3.11、FastAPI、httpx、SQLite、dataclasses、原生 ES Modules、Node test runner、Playwright、pytest。

## Global Constraints

- 只修改 `main`，代码与文档只推送 Gitee：`git push origin main`。不创建功能分支，不推送 GitHub。
- 设计事实源：`docs/superpowers/specs/2026-11-24-department-graph-standalone-design.md`。本计划只实现第一阶段；禁止修改 163、APISIX、newapi、token-factory 或增加 `/internal/v1/*`。
- 图文档只接受 `sr-routing-graph` v4。`departments/enabled/default` 为文件必填字段；不得保留 v3、`current` 指针或旧单活动图兼容分支。
- 公开 `/v1` 只使用默认图；`/admin/try` 和管理端测试对话用于部门选图验证。
- `departments` 参与单图 `tree_revision`；`enabled/default` 不参与单图 revision，但参与路由集指纹与 `snapshot_revision`。
- 有启用图时恰好一张默认图；零启用图合法。部门在启用图间唯一，默认图必须先精确匹配再兜底。
- 新建、复制和导入图强制 `enabled: false、default: false`。普通保存不得修改生命周期。
- 不新增依赖，不改 APISIX/token-factory 接口，不把密钥写入图 JSON、API 出参、浏览器持久化状态或日志。
- 保留现有 JEV、SQLite、模型代理、SSE、粘性、熔断、推理改写、请求账本和管理 API，不做无关重构。
- 每个任务先写失败测试，再写最小实现；按任务提交。若并行修改与本任务冲突，停止并报告，不覆盖他人工作。
- 最终必须通过 `.venv/bin/python -m pytest -q`、`npm run check:console`、`.venv/bin/python scripts/i18n_dead_keys.py` 和真实 standalone JSON/SSE 冒烟。

## File Map

- `router/routing/tree.py`、`router/routing/document.py`：v4 单图 envelope、部门规范化、文件解析与 behavior revision。
- `router/routing/route_set.py`（新增）：启用图集合不变量、部门索引与默认图索引，不依赖 FastAPI 或 SQLite。
- `router/routing/snapshot.py`：不可变 `RoutingSnapshotSet`、选图、候选构造、发布和状态。
- `router/store/routing_trees.py`：图文件 CRUD、启用图扫描、路由集指纹与多文件批次。
- `router/control/graphs.py`、`router/control/config.py`、`router/control/validation.py`：部门/生命周期写用例、默认切换事务、目录影响同步。
- `router/api/routes_v1.py`、`router/api/routes_trees.py`、`router/api/app.py`：公开数据面、管理 API、应用状态装配。
- `router/routing/types.py`：`NoDefaultTreeError`。
- `router/console/routing/graph.js`：v4 纯函数契约、canonical、导入导出。
- `router/console/routing/controller.js`：保存、启停、设默认、选择默认图的 API 编排。
- `router/console/routing/inspector.js`、`app.js`、`tryrun.js`、`routing.html`：部门编辑、状态与试算 UI。
- `router/console/test.js`、`index.html`：管理端测试对话的部门输入与默认图展示。
- `tests/routing_fixtures.py`、`tests/test_*.py`、`tests/fixtures/*.json`、`scripts/*test*.mjs`、`scripts/console_qa/*.mjs`：跨运行时契约与回归证据。
- `configs/*.json`、`configs/cases/*.json`、`router/console/routing/example.json`、`README.md`、`README.zh-CN.md`、`docs/决策图规范.md`、`docs/后端接口说明.md`：v4 示例和文档。

## Required Interfaces

后续任务统一使用以下接口，不得改名：

```python
@dataclass(frozen=True)
class RouteSetSelection:
    snapshot: "RoutingSnapshot"
    reason: str  # "department" | "default"

@dataclass(frozen=True)
class RoutingSnapshotSet:
    snapshots: Mapping[str, RoutingSnapshot]  # tree_id -> snapshot，仅启用图
    default_tree_id: str | None
    department_tree_ids: Mapping[str, str]
    snapshot_revision: str
    content: str
    fingerprint: str

    @property
    def default_snapshot(self) -> RoutingSnapshot | None: ...

    def select(self, department: str = "") -> RouteSetSelection: ...

class RouteSetInvariantError(ValueError):
    code: str                 # "no_default_tree" | "department_conflict"
    department: str
    tree_ids: tuple[str, ...]

def route_set_indices(trees: Iterable[RoutingTree]) -> tuple[str | None, dict[str, str]]: ...
def build_snapshot_set_for_trees(conn, trees: Iterable[RoutingTree], fingerprint: str) -> RoutingSnapshotSet: ...
def load_snapshot_set(conn, repo: RoutingTreeRepo) -> RoutingSnapshotSet: ...

class RoutingTreeRepo:
    def enabled(self) -> list[RoutingTree]: ...
    def route_fingerprint(self) -> str: ...

class SnapshotPublisher:
    def prepare(self, conn, trees: Iterable[RoutingTree], fingerprint: str) -> PreparedRouteSet: ...
    def publish(self, prepared: PreparedRouteSet) -> RoutingSnapshotSet: ...
    def refresh(self, conn) -> RoutingSnapshotSet: ...
    def refresh_if_changed(self, conn) -> RoutingSnapshotSet | None: ...
```

应用状态统一从 `app.state.snapshot` 改为 `app.state.snapshot_set`，不得保留别名。`PreparedRouteSet` 包含 `snapshot_set: RoutingSnapshotSet` 与 `fingerprint: str`。

---

### Task 1: 统一 Python 与 ES Module 的 v4 文档契约

**Files:**
- Modify: `router/routing/tree.py`
- Modify: `router/routing/document.py`
- Modify: `router/routing/graph/model.py`（仅在既有 v3 常量/注释需要同步时）
- Modify: `router/store/routing_trees.py`
- Modify: `router/control/config.py`
- Modify: `router/api/routes_trees.py`
- Modify: `router/console/routing/graph.js`
- Modify: `router/console/routing/controller.js`
- Modify: `tests/routing_fixtures.py`
- Test: `tests/test_graph_document.py`
- Test: `tests/test_graph_model.py`
- Test: `tests/test_graph_contract.py`
- Test: `scripts/console_graph_test.mjs`
- Test: `scripts/graph_contract_test.mjs`
- Modify: `tests/fixtures/*.json`
- Modify: `configs/*.json`
- Modify: `configs/cases/*.json`
- Modify: `router/console/routing/example.json`

**Interfaces:**
- Consumes: 既有 `tree_from_dict()`、`graph_document()`、`parse_graph_document()`、`RTGraph.parseImport/serialize/behaviorCanonical`。
- Produces: `RoutingTree.departments: tuple[str, ...]`、`RoutingTree.default: bool`；v4 文档必填 `departments/enabled/default`；`tree_behavior_dict()` 包含排序后的 `departments`，不包含 `enabled/default`。

- [ ] **Step 1: 写 Python 失败测试**

在 `tests/test_graph_document.py` 增加：

```python
import pytest
from dataclasses import replace
from router.routing.document import graph_document, graph_document_bytes, parse_graph_document
from router.routing.tree import tree_from_dict, tree_revision_of
from tests.routing_fixtures import tree_dict


def test_v4_roundtrip_contains_departments_and_lifecycle():
    raw = tree_dict(enabled=True, departments=["财务部", "法务部"])
    raw["default"] = True
    tree = tree_from_dict(raw)
    assert tree.departments == ("法务部", "财务部")
    assert tree.enabled is True and tree.default is True
    document = graph_document(tree)
    assert document["version"] == 4
    assert document["departments"] == ["法务部", "财务部"]
    assert document["enabled"] is True and document["default"] is True
    loaded = parse_graph_document(
        graph_document_bytes(tree), tree_id=tree.id,
        updated_at=tree.updated_at, document_sha256=tree.document_sha256)
    assert loaded.departments == tree.departments
    assert loaded.enabled is True and loaded.default is True


def test_departments_affect_revision_but_lifecycle_does_not():
    base = tree_from_dict(tree_dict(departments=[]))
    department = replace(base, departments=("财务部",))
    enabled = replace(base, enabled=True, default=True)
    assert department.tree_revision != base.tree_revision
    assert tree_revision_of(enabled) == tree_revision_of(base)


def test_v4_rejects_duplicate_department_and_disabled_default():
    duplicate = tree_dict(departments=["财务部", "财务部"])
    with pytest.raises(ValueError, match="duplicate department"):
        tree_from_dict(duplicate)
    invalid = tree_dict(enabled=False)
    invalid["default"] = True
    with pytest.raises(ValueError, match="default graph must be enabled"):
        tree_from_dict(invalid)
```

先把 `tests/routing_fixtures.py::tree_dict` 的签名扩成下列形状，让测试能表达新字段；不要给文件解析器增加缺省兼容：

```python
def tree_dict(tree_id="main", *, root=None, enabled=False, default=False,
              departments=None, sticky=None, canvas=None, runtime=None):
    # 保留现有 root/canvas/runtime 构造
    raw = {
        "id": tree_id, "name": "测试图", "enabled": enabled,
        "default": default,
        "departments": list(departments or []),
        "timezone": "Asia/Shanghai",
        "sticky": sticky or {"enabled": False, "ttl_seconds": 1800},
        "root": root,
    }
    # 其余现有逻辑不变
    return raw
```

- [ ] **Step 2: 运行测试，确认因接口不存在而失败**

Run: `.venv/bin/python -m pytest -q tests/test_graph_document.py -k 'v4 or departments'`
Expected: FAIL；`tree_from_dict()` 拒绝 `default` 字段，且 `RoutingTree` 尚无 `departments/default`。不得在接受集合错误或直接跳过测试的情况下继续。

- [ ] **Step 3: 实现 Python v4 envelope 与文件解析**

在 `RoutingTree` 的非默认值字段之后增加：

```python
@dataclass(frozen=True)
class RoutingTree:
    id: str
    name: str
    enabled: bool
    root: GraphRoot
    runtime: GraphRuntime
    departments: tuple[str, ...] = ()
    default: bool = False
    sticky: Sticky = Sticky()
    timezone: str = "Asia/Shanghai"
    tree_revision: str = ""
    updated_at: str = ""
    document_sha256: str = ""
```

在 `tree.py` 增加唯一部门规范化函数，并在 `tree_from_dict()` 中读取 `default` 和 `departments`：

```python
def _departments_from_dict(raw) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("departments must be a list of strings")
    values = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("departments must be a list of strings")
        value = item.strip()
        if not value or len(value) > 64 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
            raise ValueError("department is empty, too long, or contains control characters")
        if value in values:
            raise ValueError(f"duplicate department: {value}")
        values.add(value)
    return tuple(sorted(values))
```

校验 `default` 必须为 bool，且 `default and not enabled` 时抛出 `ValueError("default graph must be enabled")`。`tree_storage_dict()` 写入 `departments/default`；`tree_behavior_dict()` 只额外写入 `departments`。

在 `document.py`：

```python
VERSION = 4
FIELDS = {
    "kind", "version", "name", "departments", "enabled", "default",
    "timezone", "sticky", "root", "canvas", *RUNTIME_FIELDS,
}
```

`graph_document()` 写入 `departments/enabled/default`。`parse_graph_document()` 删除 `enabled` 形参，直接把这些字段交给 `tree_from_dict()`；`kind/version` 错误统一为 `only sr-routing-graph v4 is accepted`。本任务必须同时修改 `router/` 与 `tests/` 中所有 `parse_graph_document()` 调用点，删除 `enabled=...` 实参，不保留兼容形参。

在 `TreeUpsertIn` 增加 `departments: Any = None`。客户端创建请求不发送 `enabled/default`，服务端生成图时强制 `enabled=False、default=False`，部门缺省为 `[]` 或使用显式部门；普通 PUT 只接受 `departments`，不得接受 `default` 作为生命周期写入口。

- [ ] **Step 4: 实现 ES Module v4 纯函数契约**

在 `router/console/routing/graph.js`：

```javascript
const VERSION = 4;
const IMPORT_FIELDS = [
  'kind', 'version', 'name', 'departments', 'enabled', 'default',
  'timezone', 'sticky', 'root', 'canvas'
].concat(RUNTIME_FIELDS);
const ENVELOPE_FIELDS = [
  'id', 'name', 'enabled', 'default', 'departments', 'timezone', 'sticky',
  'root', 'tree_revision', 'updated_at', 'runtime', 'document_sha256'
].concat(RUNTIME_FIELDS);
```

新增并用于导入、API envelope 和 `createMinimalTree()` 的规范化：

```javascript
function normalizeDepartments(value) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error('departments must be a list of strings');
  const values = value.map(item => {
    if (typeof item !== 'string') throw new Error('departments must be a list of strings');
    const department = item.trim();
    if (!department || department.length > 64 || /[\u0000-\u001f\u007f]/.test(department)) {
      throw new Error('department is empty, too long, or contains control characters');
    }
    return department;
  });
  if (new Set(values).size !== values.length) throw new Error('duplicate department');
  return values.sort();
}
```

`behaviorCanonical()` 加入 `departments`，不包含 `enabled/default`；`editableCanonical()` 加入 `departments`。`serialize()` 写出三个字段；`parseImport()` 要求三个字段但返回的内部树固定 `enabled:false、default:false`。部门规范化失败必须转换为 `{ok:false,error,issues:[graph_parse_error]}`，不能让未捕获异常中断导入。`createMinimalTree()` 返回 `departments:[]、enabled:false、default:false`。`controller.normaliseTree()` 调用 `normalizeDepartments()` 并保留服务端返回的 `default`。

- [ ] **Step 5: 机械迁移 fixture 与示例文件**

只处理 `kind == "sr-routing-graph"` 的 JSON；保留已有部门和生命周期，旧文件缺省时使用空部门、未启用、非默认：

```bash
python3 - <<'PY'
import json
from pathlib import Path
paths = list(Path('configs').rglob('*.json'))
paths += list(Path('tests/fixtures').glob('*.json'))
paths += [Path('router/console/routing/example.json')]
for path in paths:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('kind') != 'sr-routing-graph':
        continue
    data['version'] = 4
    data['departments'] = sorted(set(data.get('departments', [])))
    data['enabled'] = bool(data.get('enabled', False))
    data['default'] = bool(data.get('default', False))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(path)
PY
```

更新所有 Python/Node fixture 构造与预期：版本为 4，文件包含 `departments/enabled/default`，API 创建辅助函数只剔除 `enabled/default`，保留 `departments`。不要通过运行时兼容代码让旧 fixture 继续通过。

- [ ] **Step 6: 运行跨运行时契约测试**

Run:
```bash
.venv/bin/python -m pytest -q tests/test_graph_document.py tests/test_graph_model.py tests/test_graph_contract.py
node --test scripts/console_graph_test.mjs scripts/graph_contract_test.mjs
```
Expected: PASS；v4 roundtrip、部门重复拒绝、disabled default 拒绝、Python/JS canonical 一致。

- [ ] **Step 7: 提交**

```bash
git add router/routing/tree.py router/routing/document.py router/api/routes_trees.py \
  router/console/routing/graph.js router/console/routing/controller.js \
  tests configs router/console/routing/example.json scripts/console_graph_test.mjs \
  scripts/graph_contract_test.mjs
git commit -m "feat: introduce v4 department graph documents"
```

---

### Task 2: 用图文件扫描替代 current 指针

**Files:**
- Create: `router/routing/route_set.py`
- Modify: `router/routing/document.py`
- Modify: `router/store/routing_trees.py`
- Modify: `router/routing/types.py`
- Test: `tests/test_routing_tree_repository.py`
- Test: `tests/test_route_set.py`
- Modify: `tests/routing_fixtures.py`

**Interfaces:**
- Consumes: Task 1 的 v4 `RoutingTree`。
- Produces: `route_set_indices()`、`RouteSetInvariantError`、`RoutingTreeRepo.enabled()`、`RoutingTreeRepo.route_fingerprint()`、`NoDefaultTreeError`。

- [ ] **Step 1: 写仓储与集合不变量失败测试**

新建 `tests/test_route_set.py`：

```python
import pytest
from router.routing.route_set import RouteSetInvariantError, route_set_indices
from router.routing.tree import tree_from_dict
from tests.routing_fixtures import tree_dict


def enabled(tree_id, *, default=False, departments=()):
    return tree_from_dict(tree_dict(
        tree_id, enabled=True, default=default, departments=list(departments)))


def test_route_set_indices_maps_departments_and_default():
    default, mapping = route_set_indices([
        enabled('main', default=True),
        enabled('finance', departments=['财务部']),
    ])
    assert default == 'main'
    assert mapping == {'财务部': 'finance'}


def test_route_set_rejects_duplicate_department_and_missing_default():
    with pytest.raises(RouteSetInvariantError) as conflict:
        route_set_indices([
            enabled('main', default=True, departments=['财务部']),
            enabled('other', departments=['财务部']),
        ])
    assert conflict.value.code == 'department_conflict'
    assert conflict.value.department == '财务部'
    assert conflict.value.tree_ids == ('main', 'other')
    with pytest.raises(RouteSetInvariantError) as missing:
        route_set_indices([enabled('main', departments=['财务部'])])
    assert missing.value.code == 'no_default_tree'
```

在 `tests/test_routing_tree_repository.py` 增加：

```python
def test_route_fingerprint_ignores_draft_only_changes(repo):
    repo.create(tree_from_dict(tree_dict('main', enabled=True, default=True)))
    repo.create(tree_from_dict(tree_dict('draft')))
    before = repo.route_fingerprint()
    draft = repo.get('draft')
    raw = tree_dict('draft')
    raw['name'] = '只改草稿'
    repo.update(tree_from_dict(raw), base_document_sha256=draft.document_sha256)
    assert repo.route_fingerprint() == before
    main = repo.get('main')
    raw = tree_dict('main', enabled=True, default=True)
    raw['name'] = '改启用图'
    repo.update(tree_from_dict(raw), base_document_sha256=main.document_sha256)
    assert repo.route_fingerprint() != before
```

- [ ] **Step 2: 运行测试，确认接口缺失**

Run: `.venv/bin/python -m pytest -q tests/test_route_set.py tests/test_routing_tree_repository.py -k 'route_set or fingerprint'`
Expected: FAIL；`router.routing.route_set` 不存在或 `RoutingTreeRepo.route_fingerprint` 不存在。

- [ ] **Step 3: 实现纯集合索引与异常**

在 `router/routing/types.py` 增加：

```python
class NoDefaultTreeError(RuntimeError):
    pass
```

新建 `router/routing/route_set.py`：

```python
from collections.abc import Iterable
from router.routing.tree import RoutingTree


class RouteSetInvariantError(ValueError):
    def __init__(self, code, *, department='', tree_ids=()):
        super().__init__(code)
        self.code = code
        self.department = department
        self.tree_ids = tuple(tree_ids)


def route_set_indices(trees: Iterable[RoutingTree]) -> tuple[str | None, dict[str, str]]:
    enabled = list(trees)
    defaults = [tree.id for tree in enabled if tree.default]
    if enabled and len(defaults) != 1:
        raise RouteSetInvariantError('no_default_tree', tree_ids=defaults)
    default = defaults[0] if defaults else None
    mapping = {}
    for tree in sorted(enabled, key=lambda item: item.id):
        for department in tree.departments:
            if department in mapping:
                raise RouteSetInvariantError(
                    'department_conflict', department=department,
                    tree_ids=(mapping[department], tree.id))
            mapping[department] = tree.id
    return default, mapping
```

- [ ] **Step 4: 改造仓储**

`RoutingTreeRepo` 删除 `current` 路径及 `active_id()/active()/enable()/disable()`。`initialize()` 只创建 `graphs/`。`list()` 和 `get()` 不再读取或推导活动 ID，`parse_graph_document()` 直接使用文件内的 `enabled/default`。

仓储 `create()` 只负责按给定 v4 文档落盘；强制新建图未启用由 `GraphService` 负责。`copy()` 继续强制副本未启用且非默认。`stage_updates()` 按 `default=False` 在前、默认图在后排序，保留现有哈希预检、暂存与回滚。

新增：

```python
def enabled(self) -> list[RoutingTree]:
    return [tree for tree in self.list() if tree.enabled]


def route_fingerprint(self) -> str:
    rows = [
        {'id': tree.id, 'default': tree.default, 'sha256': tree.document_sha256}
        for tree in self.enabled()
    ]
    rows.sort(key=lambda row: row['id'])
    return hashlib.sha256(canonical_json(rows).encode('utf-8')).hexdigest()
```

`canonical_json` 从 `router.routing.graph.model` 导入。`route_fingerprint()` 不包含未启用文件内容，因此草稿修改不触发运行集重载；启停、默认切换和启用文件修改都会改变它。

- [ ] **Step 5: 更新测试 fixture 并运行仓储测试**

删除测试对 `repo.enable()/disable()/active()/active_id()` 的依赖。需要启用文档时直接构造：

```python
repo.create(tree_from_dict(tree_dict('main', enabled=True, default=True)))
```

非默认启用图使用 `enabled=True, default=False`。验证 `current` 文件即使存在也不会被读取，旧 `repo.enable()` 相关测试改写为文件内生命周期和 `GraphBatch` 测试。

Run: `.venv/bin/python -m pytest -q tests/test_route_set.py tests/test_routing_tree_repository.py tests/test_graph_document.py`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add router/routing/route_set.py router/routing/types.py \
  router/routing/document.py router/store/routing_trees.py \
  tests/test_route_set.py tests/test_routing_tree_repository.py tests/routing_fixtures.py
git commit -m "feat: scan enabled graph documents by department"
```

---

### Task 3: 发布不可变 RoutingSnapshotSet

**Files:**
- Modify: `router/routing/snapshot.py`
- Modify: `router/routing/reloader.py`
- Modify: `router/api/app.py`
- Modify: `router/control/validation.py`
- Test: `tests/test_routing_snapshot.py`
- Test: `tests/test_graph_reloader.py`
- Test: `tests/test_app.py`
- Test: `tests/test_jev_window.py`

**Interfaces:**
- Consumes: Task 2 的 `route_set_indices()`、`RouteSetInvariantError`、`repo.enabled()/route_fingerprint()`。
- Produces: `RoutingSnapshotSet.select()`、`build_snapshot_set_for_trees()`、`load_snapshot_set()`、`PreparedRouteSet`、`app.state.snapshot_set`。

- [ ] **Step 1: 写选图与发布失败测试**

在 `tests/test_routing_snapshot.py` 增加：

```python
from dataclasses import replace
from router.routing.snapshot import assemble_snapshot_set
from router.routing.route_set import RouteSetInvariantError
from router.routing.types import NoActiveTreeError, NoDefaultTreeError


def test_snapshot_set_selects_department_then_default(conn):
    main = build_snapshot_for_tree(conn, portable_tree())
    finance_raw = tree_dict('finance', enabled=True, departments=['财务部'])
    finance_raw.update(stored_catalog_runtime())
    finance = build_snapshot_for_tree(conn, tree_from_dict(finance_raw))
    route_set = assemble_snapshot_set([main, finance], fingerprint='fp')
    assert route_set.select('财务部').snapshot is finance
    assert route_set.select('财务部').reason == 'department'
    assert route_set.select('未知部门').snapshot is main
    assert route_set.select('未知部门').reason == 'default'
    assert route_set.select('').snapshot is main


def test_snapshot_set_distinguishes_empty_and_missing_default(conn):
    empty = assemble_snapshot_set([], fingerprint='empty')
    with pytest.raises(NoActiveTreeError):
        empty.select('')

    valid = assemble_snapshot_set([build_snapshot_for_tree(conn, portable_tree())], fingerprint='valid')
    broken = replace(valid, default_tree_id=None)
    with pytest.raises(NoDefaultTreeError):
        broken.select('')

    raw = tree_dict('main', enabled=True, default=False)
    raw.update(stored_catalog_runtime())
    with pytest.raises(RouteSetInvariantError) as missing:
        assemble_snapshot_set(
            [build_snapshot_for_tree(conn, tree_from_dict(raw))], fingerprint='broken')
    assert missing.value.code == 'no_default_tree'
```

把 `portable_tree(values=None, tree_id='main', enabled=True, default=True, departments=())` 调整为能生成这些用例的辅助函数，默认生成启用中的默认图。

- [ ] **Step 2: 运行测试，确认接口缺失**

Run: `.venv/bin/python -m pytest -q tests/test_routing_snapshot.py -k 'snapshot_set'`
Expected: FAIL；`assemble_snapshot_set` 不存在。

- [ ] **Step 3: 实现快照集和选图**

在 `snapshot.py` 增加不可变类型：

```python
@dataclass(frozen=True)
class RouteSetSelection:
    snapshot: RoutingSnapshot
    reason: str


@dataclass(frozen=True)
class RoutingSnapshotSet:
    snapshots: Mapping[str, RoutingSnapshot]
    default_tree_id: str | None
    department_tree_ids: Mapping[str, str]
    snapshot_revision: str
    content: str
    fingerprint: str

    @property
    def default_snapshot(self) -> RoutingSnapshot | None:
        return self.snapshots.get(self.default_tree_id) if self.default_tree_id else None

    def select(self, department: str = '') -> RouteSetSelection:
        if not self.snapshots:
            raise NoActiveTreeError('no routing tree is enabled')
        tree_id = self.department_tree_ids.get(department) if department else None
        if tree_id is not None:
            return RouteSetSelection(self.snapshots[tree_id], 'department')
        if self.default_tree_id is None:
            raise NoDefaultTreeError('no default routing tree is enabled')
        return RouteSetSelection(self.snapshots[self.default_tree_id], 'default')
```

`assemble_snapshot_set()` 调用 `route_set_indices([item.tree for item in snapshots])`，用 `MappingProxyType` 固定两个映射。路由集 content 只含非密钥版本信息：

```python
document = {
    'default_tree_id': default_id,
    'department_tree_ids': dict(sorted(mapping.items())),
    'snapshots': {item.tree_id: item.snapshot_revision for item in ordered},
}
content = canonical_json(document)
revision = hashlib.sha256(content.encode('utf-8')).hexdigest()
```

`build_snapshot_set_for_trees()` 对每份启用树调用既有 `build_snapshot_for_tree()`，再调用 `assemble_snapshot_set()`。`load_snapshot_set()` 使用 `repo.enabled()` 与 `repo.route_fingerprint()`；`RouteSetInvariantError` 在装配边界转换为 `GraphStorageError`，使 Publisher 保留旧集合并报告 `stale`。

- [ ] **Step 4: 改造 Publisher 与应用状态**

将 `PreparedSnapshot` 改为 `PreparedRouteSet`，成员为 `snapshot_set` 和 `fingerprint`。Publisher 的 `prepare/refresh/publish/refresh_if_changed` 全部使用路由集；发布时写 `app.state.snapshot_set`，初始化时写 `None`。

`status()` 返回：

```python
{
    'ready': self.app.state.snapshot_set is not None,
    'stale': bool(self._last_error),
    'default_tree_id': route_set.default_tree_id if route_set else None,
    'enabled_tree_ids': sorted(route_set.snapshots) if route_set else [],
    'snapshot_revision': route_set.snapshot_revision if route_set else '',
    'route_fingerprint': self._fingerprint or '',
    'loaded_at': self._loaded_at,
    'last_load_error': self._last_error,
}
```

删除 `active_id/document_sha256` 状态字段。`GraphReloadWorker` 继续只调用 `refresh_if_changed()`。`active_tree_issues()` 改成 `enabled_tree_issues()`，逐一校验 `repo.enabled()` 并合并 Issue。

- [ ] **Step 5: 更新所有相关测试并运行**

把测试中的默认图读取改为 `app.state.snapshot_set.default_snapshot`，把空状态判断改为 `app.state.snapshot_set is None`，把 revision 比较区分为单图和路由集。不要增加 `app.state.snapshot` 兼容别名。

Run:
```bash
.venv/bin/python -m pytest -q \
  tests/test_routing_snapshot.py tests/test_graph_reloader.py \
  tests/test_app.py tests/test_jev_window.py
```
Expected: PASS；空路由集、默认图缺失、坏文件保留旧集合、恢复后清除 `stale`、未变指纹跳过重载均有覆盖。

- [ ] **Step 6: 提交**

```bash
git add router/routing/snapshot.py router/routing/reloader.py \
  router/api/app.py router/control/validation.py \
  tests/test_routing_snapshot.py tests/test_graph_reloader.py \
  tests/test_app.py tests/test_jev_window.py
git commit -m "feat: publish immutable department snapshot sets"
```

---

### Task 4: 公开 standalone 数据面只使用默认图

**Files:**
- Modify: `router/api/routes_v1.py`
- Modify: `router/api/routes_trees.py`
- Modify: `router/routing/types.py`
- Test: `tests/test_v1.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `app.state.snapshot_set.select()`、`NoDefaultTreeError`。
- Produces: 公开默认图数据面、管理端可信部门 Header 解码、响应头 `x-sr-tree-id/x-sr-graph-selection`。

- [ ] **Step 1: 写公开入口失败测试**

在 `tests/test_v1.py` 增加：

```python
def test_public_v1_rejects_client_department_header(client):
    response = client.post(
        '/v1/chat/completions',
        headers={'x-sr-department': encode_department('财务部')},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]},
    )
    assert response.status_code == 400
    assert response.json() == {'error': {'message': 'department_header_not_allowed'}}


def test_public_v1_uses_default_graph_and_emits_selection_headers(client):
    response = client.post(
        '/v1/chat/completions',
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]},
    )
    assert response.status_code == 200, response.text
    assert response.headers['x-sr-graph-selection'] == 'default'
    assert response.headers['x-sr-tree-id'] == 'main'
```

新增编码辅助，管理端与后续网关共用：

```python
from urllib.parse import quote

def encode_department(value):
    return quote(value, safe='')
```

- [ ] **Step 2: 运行测试，确认新契约失败**

Run: `.venv/bin/python -m pytest -q tests/test_v1.py -k 'department_header or selection_headers'`
Expected: FAIL；公开请求仍读取旧 `app.state.snapshot`，且没有新响应头或 400 拒绝。

- [ ] **Step 3: 实现严格部门解码**

在 `routes_v1.py` 增加纯辅助函数，供管理端测试对话使用：

```python
import re
from urllib.parse import unquote_to_bytes

_BAD_PERCENT = re.compile(r'%(?![0-9A-Fa-f]{2})')


def decode_department(raw: str | None) -> str:
    if raw is None or raw == '':
        return ''
    if len(raw) > 768 or _BAD_PERCENT.search(raw):
        raise ValueError('invalid_department')
    try:
        value = unquote_to_bytes(raw).decode('utf-8').strip()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('invalid_department') from exc
    if len(value) > 64 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError('invalid_department')
    return value
```

公开 `/v1` 只要出现 `x-sr-department` 就返回 `400 department_header_not_allowed`。管理端测试路由设置 `allow_department_header=True`，解码失败返回 `400 invalid_department`。普通公开请求使用 `department=''`。

- [ ] **Step 4: 接入快照集选图与错误映射**

`run_chat_completion()` 增加 `allow_department_header: bool = False` 参数。Auto（`fixed_model is None`）必须执行选图；固定模型直连只复用默认快照做日志上下文，不要求路由集或默认图存在：

```python
route_set = request.app.state.snapshot_set
selection = None
if fixed_model is None:
    if route_set is None:
        return fail('no_valid_snapshot', 503, 'snapshot', 'no valid snapshot set')
    try:
        selection = route_set.select(department)
        snapshot = selection.snapshot
    except NoActiveTreeError as exc:
        return fail('no_active_tree', 503, 'snapshot', str(exc))
    except NoDefaultTreeError as exc:
        return fail('no_default_tree', 503, 'snapshot', str(exc))
else:
    snapshot = route_set.default_snapshot if route_set is not None else None
```

Auto 把选中的 `snapshot` 继续传给既有 `routing_service.route()` 和 `snapshot.backends`；两种路径都保留现有请求账本，不重写图执行器。Auto 成功响应头在现有 `x-sr-*` 基础上增加：

```python
headers['x-sr-tree-id'] = snapshot.tree_id
headers['x-sr-graph-selection'] = selection.reason
```

固定模型直连不输出 `x-sr-graph-selection`，其 `x-sr-selection` 仍为 `direct`。`_published_revision()` 改读路由集 `snapshot_revision`。

- [ ] **Step 5: 运行公开数据面与应用测试**

Run: `.venv/bin/python -m pytest -q tests/test_v1.py tests/test_app.py`
Expected: PASS；默认图 JSON/SSE、无启用图 `no_active_tree`、缺默认图 `no_default_tree`、无初始集合 `no_valid_snapshot`、伪造部门 Header 400 均正确。

- [ ] **Step 6: 提交**

```bash
git add router/api/routes_v1.py router/api/routes_trees.py \
  router/routing/types.py tests/test_v1.py tests/test_app.py
git commit -m "feat: route standalone requests through the default graph"
```

---

### Task 5: 部门设置、独立启停与默认图事务 API

**Files:**
- Modify: `router/control/graphs.py`
- Modify: `router/control/errors.py`（仅当需要新的结构化 409 类型时）
- Modify: `router/api/routes_trees.py`
- Modify: `router/store/routing_trees.py`
- Test: `tests/test_routing_trees_admin.py`
- Test: `tests/test_graph_model.py`

**Interfaces:**
- Consumes: `route_set_indices()`、`GraphBatch`、`SnapshotPublisher.prepare()`。
- Produces: `GraphPatch.departments`、`GraphService.set_departments()`、`GraphService.set_default()`、`PUT /admin/trees/{id}/departments`、`POST /admin/trees/{id}/default`。

- [ ] **Step 1: 写部门与生命周期 API 失败测试**

在 `tests/test_routing_trees_admin.py` 增加一个完整场景：

```python
def test_departments_multi_enable_default_and_disable_rules(client):
    create_portable_tree(client, 'main')
    create_portable_tree(client, 'finance')
    assert client.post('/admin/trees/main/enable').status_code == 200
    assert client.post('/admin/trees/finance/enable').status_code == 200

    trees = {item['id']: item for item in client.get('/admin/trees').json()['trees']}
    assert trees['main']['default'] is True
    assert trees['finance']['enabled'] is True and trees['finance']['default'] is False

    response = client.put('/admin/trees/finance/departments', json={
        'departments': ['财务部'],
        'base_document_sha256': trees['finance']['document_sha256'],
    })
    assert response.status_code == 200, response.text
    assert response.json()['tree']['departments'] == ['财务部']

    response = client.put('/admin/trees/main/departments', json={
        'departments': ['财务部'],
        'base_document_sha256': trees['main']['document_sha256'],
    })
    assert response.status_code == 422
    assert response.json()['detail'][0]['code'] == 'department_conflict'

    assert client.post('/admin/trees/main/disable').status_code == 409
    assert client.post('/admin/trees/finance/default').status_code == 200
    trees = {item['id']: item for item in client.get('/admin/trees').json()['trees']}
    assert trees['main']['enabled'] is True and trees['main']['default'] is False
    assert trees['finance']['enabled'] is True and trees['finance']['default'] is True

    assert client.post('/admin/trees/main/disable').status_code == 200
    assert client.post('/admin/trees/finance/disable').status_code == 200
    assert client.get('/admin/trees').json()['graph_status']['enabled_tree_ids'] == []
```

另写哈希过期测试：`base_document_sha256='0'*64` 必须返回 `409 graph_conflict` 且文件字节不变。

- [ ] **Step 2: 运行测试，确认 404/接口缺失**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py -k 'departments_multi_enable or department_hash'`
Expected: FAIL；`departments/default` 路由 404 或生命周期仍只有单活动图。

- [ ] **Step 3: 增加 DTO、Patch 与错误投影**

在 `GraphPatch` 增加：

```python
departments: list | None | _Unset = UNSET
```

`_envelope()` 继承当前 `departments/default/enabled`，普通更新只覆盖 `departments`。`TreeUpsertIn` 不包含可写 `default`；新增：

```python
class DepartmentUpdateIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    departments: Any = None
    base_document_sha256: Any = None
```

新增路由：

```python
@router.put('/trees/{tree_id}/departments')
def update_departments(tree_id, body: DepartmentUpdateIn, request: Request,
                       conn=Depends(get_conn), trees=Depends(get_tree_repo)):
    updated = _service(request, conn, trees).set_departments(
        tree_id, body.departments, base_document_sha256=body.base_document_sha256)
    return {
        'tree': tree_to_dict(updated),
        'snapshot_revision': _published_revision(request),
        'graph_status': request.app.state.publisher.status(),
        'warnings': [],
    }


@router.post('/trees/{tree_id}/default')
def set_default(tree_id, request: Request, conn=Depends(get_conn),
                trees=Depends(get_tree_repo)):
    _service(request, conn, trees).set_default(tree_id)
    return _trees_payload(request, trees)
```

将 `RouteSetInvariantError(code='department_conflict')` 转为 Issue；默认图停用限制用结构化 409：

```python
def _route_set_issue(exc):
    return Issue(
        exc.code,
        params={
            'department': exc.department,
            'tree_ids': list(exc.tree_ids),
        },
    )

# 停用错误载荷：{'code':'default_graph_required','tree_id':...}
```

- [ ] **Step 4: 实现统一的多文件提交助手**

在 `GraphService` 内新增一个私有助手，所有启用相关写入共用：

```python
def _commit_enabled_changes(self, writes, *, candidate_enabled, action, detail):
    # writes: list[(RoutingTree, expected_document_sha256)]，仅实际写盘的图
    # candidate_enabled: list[RoutingTree]，变更后的完整启用集合
    with self.publisher.locked():
        for tree, _ in writes:
            issues = tree_issues(self.conn, tree)
            if issues:
                raise ValidationFailed(issues)
        try:
            route_set_indices(candidate_enabled)
        except RouteSetInvariantError as exc:
            raise ValidationFailed([_route_set_issue(exc)]) from exc
        batch = self.trees.stage_updates(writes)
        try:
            batch.commit()
            fingerprint = self.trees.route_fingerprint()
            prepared = self.publisher.prepare(self.conn, self.trees.enabled(), fingerprint)
            if self.trees.route_fingerprint() != fingerprint:
                raise GraphChangedDuringLoad('route set changed during publish')
            self._audit(action, detail)
        except BaseException:
            batch.rollback()
            raise
        else:
            batch.discard()
            self.publisher.publish(prepared)
        return prepared
```

调用方先构造 `candidate_enabled`，再生成仅含变化项的 `writes`。校验和发布使用完整启用集合，`stage_updates()` 只接收 `writes`。

`enable()`：第一张启用图设置 `default=True`，后续只设 `enabled=True`。`disable()`：默认图之外仍有启用图时返回 409，否则同时清除 `default`。`set_default()`：目标设 `enabled=True, default=True`，旧默认设 `enabled=True, default=False`。`set_departments()` 复用普通 `update()`，但只提交 `departments` patch。

- [ ] **Step 5: 实现创建/复制/普通更新语义**

`create/copy` 强制 `enabled=False, default=False`。普通 `update()` 继承生命周期并允许修改部门；只有被更新的图原本处于启用状态时才重算并发布路由集，纯草稿更新不发布。所有启用候选在写盘前调用 `route_set_indices()`，确保冲突不会落到磁盘。

- [ ] **Step 6: 运行管理 API 与回滚测试**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py`
Expected: PASS，包括：多图同时启用、第一张自动默认、部门冲突 422、哈希冲突 409、默认切换、默认图停用 409、最后一张停用为空、文件回滚后快照不变。

- [ ] **Step 7: 提交**

```bash
git add router/control/graphs.py router/control/errors.py \
  router/api/routes_trees.py router/store/routing_trees.py \
  tests/test_routing_trees_admin.py tests/test_graph_model.py
git commit -m "feat: add department and multi-graph lifecycle APIs"
```

---

### Task 6: 让目录影响同步重建完整启用快照集

**Files:**
- Modify: `router/control/config.py`
- Modify: `router/control/catalog_sync.py`（仅在它假设唯一活动图时）
- Modify: `router/control/consistency.py`（仅在它读取活动图时）
- Test: `tests/test_catalog_impacts.py`
- Test: `tests/test_admin_models.py`
- Test: `tests/test_runtime_consistency.py`
- Test: `tests/test_self_contained_e2e.py`

**Interfaces:**
- Consumes: Task 3 的 Publisher 和 Task 5 的完整启用集合写语义。
- Produces: `ConfigurationService._prepared_snapshot_set()`、启用图感知的 `commit_catalog_write()`。

- [ ] **Step 1: 写多图目录同步失败测试**

在 `tests/test_catalog_impacts.py` 增加：

```python
def test_catalog_sync_updates_each_enabled_graph_but_not_draft(client, app):
    create_portable_tree(client, 'main')
    create_portable_tree(client, 'finance')
    create_portable_tree(client, 'draft')
    assert client.post('/admin/trees/main/enable').status_code == 200
    assert client.post('/admin/trees/finance/enable').status_code == 200
    before_draft = app.state.graph_repo.get('draft').document_sha256

    response = client.put('/admin/providers/local', json={
        'base_url': 'http://changed.test/v1',
        'reasoning_format': 'openai',
    })
    assert response.status_code == 409, response.text
    response = client.put('/admin/providers/local', json={
        'base_url': 'http://changed.test/v1',
        'reasoning_format': 'openai',
        'impact_confirmation': _confirmation_from(response),
    })
    assert response.status_code == 200, response.text

    route_set = app.state.snapshot_set
    assert route_set.default_snapshot.backends['fast'].url == \
        'http://changed.test/v1/chat/completions'
    assert route_set.snapshots['finance'].backends['fast'].url == \
        'http://changed.test/v1/chat/completions'
    assert app.state.graph_repo.get('draft').document_sha256 == before_draft
```

另增加纯草稿目录同步断言：只有 `draft` 被影响时，路由集 `snapshot_revision` 和对象身份均不变。

- [ ] **Step 2: 运行测试，确认只有默认图被更新**

Run: `.venv/bin/python -m pytest -q tests/test_catalog_impacts.py -k 'each_enabled_graph or draft_only'`
Expected: FAIL；`finance` 未更新或草稿触发了不必要发布。

- [ ] **Step 3: 改造候选构造与指纹复核**

在 `ConfigurationService` 统一替换：

```python
def _prepared_snapshot_set(self):
    fingerprint = self.trees.route_fingerprint()
    return self.publisher.prepare(self.conn, self.trees.enabled(), fingerprint)
```

`_recheck_fingerprint()` 每次重试都重新读取 `route_fingerprint()` 和 `enabled()`，不得回退到单图。`_require_valid_active_graph()` 改为 `_require_valid_enabled_graphs()`，对全部 `self.trees.enabled()` 执行 `tree_issues()`，任一失败都回滚。

- [ ] **Step 4: 修正目录同步发布门**

`commit_catalog_write()` 的 `active_changed` 改为 `enabled_changed`：

```python
enabled_changed = any(
    tree.enabled and any(tree.id == candidate.id for candidate in candidates)
    for tree in self.trees.enabled()
)
```

只有 `enabled_changed` 时才发布新路由集；但候选快照与指纹复核仍按完整启用集合执行。无候选的纯数据库目录写保持 `prepare=False, publish=False`。回滚失败后的 `publisher.refresh()` 也重建完整路由集。

- [ ] **Step 5: 运行目录与自包含回归**

Run:
```bash
.venv/bin/python -m pytest -q \
  tests/test_catalog_impacts.py tests/test_admin_models.py \
  tests/test_runtime_consistency.py tests/test_self_contained_e2e.py
```
Expected: PASS；所有启用图同步、草稿不发布、回滚后完整集合恢复、密钥不出现在路由集 content。

- [ ] **Step 6: 提交**

```bash
git add router/control/config.py router/control/catalog_sync.py \
  router/control/consistency.py tests/test_catalog_impacts.py \
  tests/test_admin_models.py tests/test_runtime_consistency.py \
  tests/test_self_contained_e2e.py
git commit -m "fix: republish all enabled graphs after catalog sync"
```

---

### Task 7: 管理端按部门试算与测试对话

**Files:**
- Modify: `router/api/routes_trees.py`
- Modify: `router/api/routes_test.py`
- Modify: `router/api/routes_v1.py`
- Modify: `router/api/routes_logs.py`（仅当管理读取仍假设单活动图时）
- Test: `tests/test_graph_try_api.py`
- Test: `tests/test_test_chat.py`
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `decode_department()`、公开数据面选图逻辑、管理端生命周期 API。
- Produces: `TryIn.department`、`graph_selection` 响应字段、管理端测试对话部门选择。

- [ ] **Step 1: 写实际路由集试算失败测试**

在 `tests/test_graph_try_api.py` 先用无问题节点的图准备默认图 `main`（模型 `fast`）和部门图 `finance`（模型 `strong`），避免 mock JEV 影响选图断言：

```python
def create_direct_model_tree(client, tree_id, model):
    root = {
        'id': 'root', 'type': 'graph',
        'nodes': [
            {'id': 'start', 'type': 'start'},
            {'id': 'target', 'type': 'target', 'priority': 1,
             'models': [{'model': model, 'reasoning': None}]},
        ],
        'edges': [{
            'id': 'e-start-target', 'source': 'start', 'source_port': 'out',
            'target': 'target', 'target_port': 'in_1', 'condition': None,
        }],
        'error_fallback': 'target',
    }
    raw = tree_dict(tree_id, root=root)
    raw.update(runtime_for_models([model]))
    payload = {key: value for key, value in raw.items()
               if key not in {'enabled', 'default'}}
    response = client.post('/admin/trees', json=payload)
    assert response.status_code == 201, response.text
    assert client.post(f'/admin/trees/{tree_id}/enable').status_code == 200


def test_try_without_draft_uses_department_route_set(client):
    create_direct_model_tree(client, 'main', 'fast')
    create_direct_model_tree(client, 'finance', 'strong')
    current = client.get('/admin/trees/finance').json()['tree']
    response = client.put('/admin/trees/finance/departments', json={
        'departments': ['财务部'],
        'base_document_sha256': current['document_sha256'],
    })
    assert response.status_code == 200, response.text

    response = client.post('/admin/try', json={
        'department': '财务部', 'text': '请处理这个请求', 'session_id': '',
    })
    assert response.status_code == 200, response.text
    assert response.json()['tree_id'] == 'finance'
    assert response.json()['graph_selection'] == 'department'
    assert response.json()['outcome']['model'] == 'strong'

    response = client.post('/admin/try', json={
        'department': '未知部门', 'text': '请处理这个请求', 'session_id': '',
    })
    assert response.status_code == 200, response.text
    assert response.json()['tree_id'] == 'main'
    assert response.json()['graph_selection'] == 'default'
    assert response.json()['outcome']['model'] == 'fast'
```

空 `department` 也必须走 `main`。带 `tree_id` 或草稿 `root` 的既有用例不得改为路由集选择。

- [ ] **Step 2: 运行测试，确认缺少 department 字段**

Run: `.venv/bin/python -m pytest -q tests/test_graph_try_api.py -k 'without_draft_uses_department'`
Expected: FAIL；Pydantic 拒绝额外 `department` 或仍要求 `root/tree_id`。

- [ ] **Step 3: 实现试算两种路径**

`TryIn` 增加 `department: Any = None`。在 `try_tree()` 开头分流：

```python
if body.tree_id is None and body.root is None:
    department = body.department if body.department is not None else ''
    if not isinstance(department, str):
        raise HTTPException(422, detail='try department must be a string')
    department = department.strip()
    if len(department) > 64 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in department):
        raise HTTPException(422, detail='invalid department')
    route_set = request.app.state.snapshot_set
    if route_set is None:
        raise HTTPException(503, detail='no_valid_snapshot')
    selection = route_set.select(department)
    snapshot = selection.snapshot
    tree = snapshot.tree
else:
    # 既有 tree_id / 草稿合并逻辑，graph_selection 不返回或设为 null
```

复用后续 `routing_service.route(..., dry_run=True)` 和响应构造。实际路由集路径返回 `graph_selection: selection.reason`，草稿路径返回 `graph_selection: None`。`NoActiveTreeError/NoDefaultTreeError` 分别映射 503 `no_active_tree/no_default_tree`。

- [ ] **Step 4: 管理端测试对话接入可信部门**

`routes_test.py` 调用：

```python
return await run_chat_completion(
    request, use_requested_model=True, conn=conn,
    allow_department_header=True)
```

管理端测试对话从 `X-SR-Department` 解码部门，Auto 请求复用路由集选择并返回 `x-sr-tree-id/x-sr-graph-selection`；固定模型仍直接读取 SQLite 目录，不调用 JEV。

分别用 `department=''` 和百分号编码的 `财务部` 调用管理端测试对话后，增加账本断言：

```python
recent = client.get('/admin/requests/recent').json()['requests']
assert [row['tree_id'] for row in recent[:2]] == ['finance', 'main']
assert recent[0]['model'] == 'strong'
assert recent[1]['model'] == 'fast'
```


- [ ] **Step 5: 运行管理试算、测试对话与全量后端回归**

Run:
```bash
.venv/bin/python -m pytest -q \
  tests/test_graph_try_api.py tests/test_test_chat.py tests/test_admin.py
.venv/bin/python -m pytest -q
```
Expected: PASS；部门命中、未知/空部门默认、草稿不受影响、固定模型不受影响，后端全量无旧 `app.state.snapshot`、`current` 或 v3 依赖。

- [ ] **Step 6: 提交**

```bash
git add router/api tests/test_graph_try_api.py tests/test_test_chat.py tests/test_admin.py
git commit -m "feat: exercise department routing through admin APIs"
```

---

### Task 8: 控制台保存与生命周期编排

**Files:**
- Modify: `router/console/routing/controller.js`
- Modify: `router/console/routing/app.js`
- Modify: `router/console/routing.html`
- Modify: `router/console/i18n.js`
- Test: `scripts/console_draft_test.mjs`
- Test: `scripts/console_graph_test.mjs`
- Test: `scripts/console_qa/canvas.mjs`

**Interfaces:**
- Consumes: `PUT /admin/trees/{id}/departments`、`POST enable/disable/default`、v4 树 envelope。
- Produces: `RoutingController.setDefault()`、默认图优先的启动选择、多图生命周期状态同步。

- [ ] **Step 1: 先写失败的前端状态与浏览器测试**

在 `scripts/console_draft_test.mjs` 增加 canonical 行为测试：

```javascript
test('departments are behavior while enabled and default are lifecycle only', () => {
  const store = storeWith();
  assert.equal(store.change('behavior', tree => { tree.departments = ['财务部']; }), true);
  assert.equal(store.dirtyKind, 'behavior');
  store.undo();
  const before = store.tree;
  store.tree.enabled = true;
  store.tree.default = true;
  assert.equal(RTGraph.behaviorCanonical(before), RTGraph.behaviorCanonical(store.tree));
});
```

文件顶部补充 `import * as RTGraph from '../router/console/routing/graph.js';`。

在 `scripts/console_qa/canvas.mjs` 增加真实浏览器场景：启用 `main` 后再启用 `finance`，两者均保持启用，`main` 为默认；点击 `finance` 的 `#cvDefault` 后，`finance.default === true`、`main.default === false` 且两者仍启用。立即再保存一次当前图，必须成功而不能出现 `graph_conflict`。最后停用 `main` 成功，再停用默认的 `finance`，路由集为空。

- [ ] **Step 2: 运行测试，确认旧行为失败**

Run:
```bash
node --test scripts/console_draft_test.mjs
QA_ONLY='multi.*default|department.*lifecycle' node scripts/console_qa/canvas.mjs
```
Expected: Node 测试因 `RTGraph` 未导入或 behavior 不一致而失败；浏览器场景因 `#cvDefault` 不存在或启用另一图时前图被停用而失败。补齐 import 后必须仍因新行为未实现而失败。

- [ ] **Step 3: 修正保存与响应状态合并**

`applySavedResponse()` 同时合并：

```javascript
tree.departments = Array.isArray(savedTree.departments) ? savedTree.departments.slice() : [];
tree.enabled = !!savedTree.enabled;
tree.default = !!savedTree.default;
```

`save()` 的 payload 增加 `departments`，继续排除 `enabled/default/tree_revision/updated_at/document_sha256`。生命周期回包不得触发 behavior dirty；部门修改必须触发。

`setEnabled()` 与 `setDefault()` 都必须从 `result.trees` 找到当前图，并调用 `applySavedResponse()` 合并完整 envelope。`enabled/default` 已写入图文件，会改变 `document_sha256`；若不更新保存基线，下一次保存必然误报 `graph_conflict`。新增：

```javascript
async setDefault() {
  const store = this.store;
  if (!store.tree || store.saving || store.tryRunning) return false;
  if (store.dirtyKind !== 'clean' || !store.saved) {
    const saved = await this.save();
    if (!saved) return false;
  }
  store.setSaving(true);
  this.hooks.syncSaving();
  const targetTree = store.tree;
  const targetId = this.treeId;
  try {
    const result = await api.post(
      '/admin/trees/' + encodeURIComponent(targetId) + '/default');
    if (store.tree !== targetTree || this.treeId !== targetId) return false;
    if (Array.isArray(result.trees)) this.trees = result.trees;
    const current = this.trees.find(item => item.id === targetId);
    if (!current) throw new Error('default response is missing the current tree');
    this.applySavedResponse({
      tree: current,
      snapshot_revision: result.snapshot_revision,
    });
    this.hooks.renderTreeSelect();
    this.hooks.render();
    this.hooks.refreshDirty();
    this.hooks.toast(t('cv.toastDefault'));
    return true;
  } catch (error) {
    this.hooks.toast(tf('cv.toastDefaultFail', { err: apiErrText(error) }), 'error');
    return false;
  } finally {
    store.setSaving(false);
    this.hooks.syncSaving();
  }
}
```

- [ ] **Step 4: 增加设默认按钮与启动默认图选择**

`routing.html` 在 `#cvDisable` 前增加：

```html
<button type="button" class="btn sm" id="cvDefault" data-i18n="cv.setDefault" hidden></button>
```

`app.js` 的事件映射增加 `cvDefault: () => controller.setDefault()`，忙碌按钮集合加入 `cvDefault`。`updateToolbar()` 仅在 `store.tree && !store.tree.default` 时显示该按钮。顶栏另增默认状态 pill；现有四种生命周期文案保持不变。

`boot()` 改为下列选择，并同步把后续 `enabled/enabled.id` 引用改为 `preferred/preferred.id`：

```javascript
const preferred = controller.trees.find(tree => tree.default) || controller.trees[0];
```

`renderTreeSelect()` 为默认图添加“默认/Default”标记，不再用唯一 `enabled` 推导当前活动图。

- [ ] **Step 5: 运行前端测试并提交**

Run:
```bash
node --test scripts/console_draft_test.mjs scripts/console_graph_test.mjs
QA_ONLY='multi.*default|department.*lifecycle' node scripts/console_qa/canvas.mjs
```
Expected: PASS；启用不互斥、默认切换正确、保存不带生命周期、部门参与 behavior。

```bash
git add router/console/routing/controller.js router/console/routing/app.js \
  router/console/routing.html router/console/i18n.js \
  scripts/console_draft_test.mjs scripts/console_graph_test.mjs scripts/console_qa/canvas.mjs
git commit -m "feat: coordinate multi-graph lifecycle in the console"
```

---

### Task 9: 部门编辑、试算与测试对话 UI

**Files:**
- Modify: `router/console/routing/inspector.js`
- Modify: `router/console/routing/tryrun.js`
- Modify: `router/console/routing/app.js`
- Modify: `router/console/test.js`
- Modify: `router/console/index.html`
- Modify: `router/console/i18n.js`
- Modify: `router/console/routing.css`（仅新增部门输入布局）
- Test: `scripts/console_qa/inspector.mjs`
- Test: `scripts/console_qa/canvas.mjs`
- Test: `scripts/console_chat_test.mjs`

**Interfaces:**
- Consumes: `departments` envelope、`POST /admin/try {department}`、管理端测试对话 `X-SR-Department`。
- Produces: 图设置部门输入、实际选图结果展示、测试对话部门 Header。

- [ ] **Step 1: 写部门编辑与试算失败测试**

在 `scripts/console_qa/inspector.mjs` 增加场景：

1. 打开图设置，在 `#cvDepartments` 输入 `财务部, 法务部` 并应用。
2. 保存后服务端 `departments == ['法务部','财务部']`（服务端排序）。
3. 另一个启用图配置相同部门后保存，页面显示 `department_conflict`，且前一张图部门不变。

在 `scripts/console_qa/canvas.mjs` 增加试算断言：默认图模型 `fast`、部门图模型 `strong`，先在 `#cvTryScope` 选择 `route_set`，再在 `#cvTryDepartment` 输入 `财务部`，结果包含 `finance` 和 `department`；输入未知部门后结果包含 `main` 和 `default`。另切回 `draft`，确认仍测试当前画布而不是默认图。

在 `scripts/console_chat_test.mjs` 增加 `import { chatHeaders } from '../router/console/chat/protocol.js';`，再增加请求头测试：

```javascript
test('admin chat sends a percent-encoded department when provided', async () => {
  const headers = chatHeaders({ sessionId: 's1', department: '财务部' });
  assert.equal(headers['x-sr-session-id'], 's1');
  assert.equal(headers['x-sr-department'], encodeURIComponent('财务部'));
});
```

把现有发送请求的 Header 构造提取为纯函数 `chatHeaders()` 并导出，先让该测试因函数不存在而失败。

- [ ] **Step 2: 运行测试，确认 UI/辅助函数缺失**

Run:
```bash
node --test scripts/console_chat_test.mjs
QA_ONLY='department' node scripts/console_qa/inspector.mjs
QA_ONLY='department' node scripts/console_qa/canvas.mjs
```
Expected: FAIL；`chatHeaders` 不存在，`#cvDepartments/#cvTryDepartment` 不存在。

- [ ] **Step 3: 实现图设置部门编辑**

`inspector.js::graphBranch()` 在图名称之后加入：

```javascript
'<label class="field">' + esc(t('cv.ins.departments')) +
'<input id="cvDepartments" type="text" data-field="departments" value="' +
esc((tree && tree.departments || []).join(', ')) +
'" placeholder="财务部, 法务部" aria-label="' + esc(t('cv.ins.departments')) + '">' +
'<span class="rtc-ins-note">' + esc(t('cv.ins.departmentsHint')) + '</span></label>'
```

图设置提交时按逗号或换行拆分、去空白、去重并排序；空输入得到 `[]`。本地重复部门直接提示，跨启用图冲突展示服务端 `department_conflict`。保存仍统一走 `controller.save()`，由它携带 `departments`。

试算区增加范围选择，默认保持现有草稿行为：

```html
<select id="cvTryScope" aria-label="试算范围">
  <option value="draft">当前草稿</option>
  <option value="route_set">实际路由集</option>
</select>
<input id="cvTryDepartment" type="text" placeholder="部门（路由集模式）">
```

`tryrun.js::payloadFor()` 必须先判断范围，路由集模式不得携带 `root/tree_id` 或四个运行字段：

```javascript
const scope = byId('cvTryScope').value;
if (scope === 'route_set') {
  const payload = { text: text, session_id: '' };
  const department = byId('cvTryDepartment').value.trim();
  if (department) payload.department = department;
  if (now) payload.now = now;
  return payload;
}
// scope === 'draft' 时保留现有 root/canvas/timezone/sticky/runtime 构造。
```

试算响应结论区增加 `tree_id` 与 `graph_selection`。`route_set` 模式空部门也走默认图；`draft` 模式继续返回 `graph_selection: null`。

- [ ] **Step 4: 实现测试对话部门输入**

`index.html` 在 `#testInput` 前增加：

```html
<label class="tc-department">
  <span data-i18n="test.department"></span>
  <input id="testDepartment" type="text" autocomplete="off"
         data-i18n-placeholder="test.departmentPlaceholder">
</label>
```

`test.js` 的 `state` 增加 `department: ''`，角色门控和 busy 锁同时控制 `#testDepartment`。发送前设置 `state.department = $('#testDepartment').value.trim()`。

在模块顶层或 `chat/protocol.js` 提取并导出：

```javascript
export function chatHeaders({ sessionId, department }) {
  const headers = { 'x-sr-session-id': sessionId };
  if (department) headers['x-sr-department'] = encodeURIComponent(department);
  return headers;
}
```

发送请求使用该函数；不得把部门放进 OpenAI 请求体。`boot()` 使用 `trees.find(tree => tree.default)` 展示默认图，不再取第一个 `enabled`。

- [ ] **Step 5: 运行部门 UI 与聊天测试**

Run:
```bash
node --test scripts/console_chat_test.mjs
QA_ONLY='department' node scripts/console_qa/inspector.mjs
QA_ONLY='department' node scripts/console_qa/canvas.mjs
```
Expected: PASS；部门排序与冲突、实际选图、百分号编码 Header、默认图展示、viewer/busy 锁定均正确。

- [ ] **Step 6: 提交**

```bash
git add router/console/routing/inspector.js router/console/routing/tryrun.js \
  router/console/routing/app.js router/console/test.js router/console/index.html \
  router/console/i18n.js router/console/routing.css router/console/chat/protocol.js \
  scripts/console_qa/inspector.mjs scripts/console_qa/canvas.mjs scripts/console_chat_test.mjs
git commit -m "feat: edit and exercise departments in the console"
```

---

### Task 10: 文档、示例和旧行为注释收口

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/决策图规范.md`
- Modify: `docs/后端接口说明.md`
- Modify: `docs/superpowers/specs/2026-11-24-department-graph-standalone-design.md`（只修正与最终接口的偏差）
- Modify: `configs/*.json`
- Modify: `configs/cases/*.json`
- Modify: `router/console/routing/example.json`
- Modify: 前述任务仍含旧语义的 `router/**/*.py` 与 `router/console/**/*.js`

**Interfaces:**
- Consumes: Task 1–9 的最终代码与 API。
- Produces: 与 v4 完全一致的操作文档和示例，不再有运行时的旧行为描述。

- [ ] **Step 1: 先运行文档与示例契约测试并列出口径**

Run:
```bash
.venv/bin/python -m pytest -q tests/test_case_documents.py
git grep -n -E 'version 3|v3|current pointer|current 指针|single active|唯一启用|唯一活动' \
  -- README.md README.zh-CN.md docs/决策图规范.md docs/后端接口说明.md \
     router configs tests/fixtures scripts
```
Expected: 示例测试若仍有 v3 文件则 FAIL；文本列出必须在本次修改的运行时文档。历史设计文档只保留明确标注的历史背景，不得把旧规则写成当前行为。

- [ ] **Step 2: 更新图规范和后端接口说明**

`docs/决策图规范.md` 必须明确：

- v4 三层 envelope 与 `departments/enabled/default` 必填规则；`current` 不再存在。
- 部门规范化、跨启用图唯一、默认图约束和空路由集。
- `departments` 参与 `tree_revision`，生命周期不参与；路由集 `snapshot_revision` 的组成。
- 启用图全量编译与 `stale`、无默认图和部门冲突错误码。

`docs/后端接口说明.md` 必须完整记录：

- `PUT /admin/trees/{id}/departments`、`POST /admin/trees/{id}/default` 的请求、响应和错误。
- 多图 `enable/disable`、第一张自动默认、默认图停用 409、最后一张停用为空。
- `/admin/try` 的 `department` 与实际路由集路径。
- `x-sr-tree-id/x-sr-graph-selection`、公开 `/v1` 拒绝 `X-SR-Department`。
- `graph_status.default_tree_id/enabled_tree_ids/snapshot_revision/route_fingerprint`。

README 双语同步独立模式用法，不提前写第二阶段 `/internal/v1` 或 APISIX 部署命令。

- [ ] **Step 3: 清理代码注释和示例**

把“活动画布唯一事实源”“current 指针”“启用会替换其他图”等注释改为“启用快照集/默认图/部门映射”。确认所有示例 JSON 都是 v4 且生命周期与用途一致：示例草稿为 `enabled:false,default:false`，用于展示默认路由的示例必须同时有完整图结构和合法默认状态。

- [ ] **Step 4: 复跑文档检查并提交**

Run:
```bash
.venv/bin/python -m pytest -q tests/test_case_documents.py
git diff --check
.venv/bin/python scripts/i18n_dead_keys.py
```
Expected: PASS；示例全部可解析，diff 无空白错误，i18n 无死键。

```bash
git add README.md README.zh-CN.md docs configs router scripts tests
git commit -m "docs: align standalone routing with v4 graph sets"
```

---

### Task 11: 全量门禁与真实 standalone 冒烟

**Files:**
- Create: `scripts/standalone_v4_smoke.py`
- Test: `tests/test_self_contained_e2e.py`
- Test: `tests/test_graph_multi_replica.py`
- Modify: `README.md`、`README.zh-CN.md`（只补充已实际执行且通过的验证命令）

**Interfaces:**
- Consumes: 全部前序功能。
- Produces: 可重复的 standalone 验收证据与干净交付工作区。

- [ ] **Step 1: 运行完整标准门禁**

Run:
```bash
git diff --check
.venv/bin/python -m pytest -q
npm run check:console
.venv/bin/python scripts/i18n_dead_keys.py
```
Expected: 全部 PASS、无失败、无跳过新增部门测试、无 i18n 死键。任何失败都必须在本任务修复，不得通过排除旧测试或降低断言通过。

- [ ] **Step 2: 启动真实应用并准备默认图与两张部门图**

使用临时数据库和图目录，真实 `python -m router` 进程及模拟 JEV/模型 HTTP 服务。不得只调用内部函数。模拟模型服务记录每次请求的 `model`、Authorization、SSE 帧；JEV 返回与图选项匹配的合法答案。设置 `ROUTER_USER_AUTH=0`、`ROUTER_HOST=127.0.0.1`、`ROUTER_PORT=18400`，管理接口只用于本机临时实例。

`standalone_v4_smoke.py` 至少包含以下可执行构造，不使用字符串占位图：

```python
import json
import urllib.request


def graph_doc(tree_id, model, *, departments=(), enabled=False, default=False):
    return {
        'id': tree_id,
        'name': tree_id,
        'departments': sorted(departments),
        'enabled': enabled,
        'default': default,
        'timezone': 'Asia/Shanghai',
        'sticky': {'enabled': False, 'ttl_seconds': 1800},
        'root': {
            'id': 'root', 'type': 'graph',
            'nodes': [
                {'id': 'start', 'type': 'start'},
                {'id': 'target', 'type': 'target', 'priority': 1,
                 'models': [{'model': model, 'reasoning': None}]},
            ],
            'edges': [{
                'id': 'e-start-target', 'source': 'start', 'source_port': 'out',
                'target': 'target', 'target_port': 'in_1', 'condition': None,
            }],
            'error_fallback': 'target',
        },
        'backends': {
            'providers': [{
                'name': 'mock', 'base_url': 'http://127.0.0.1:18080/v1',
                'key_env': '', 'reasoning_format': 'openai',
            }],
            'models': [{
                'id': model, 'upstream': model, 'provider': 'mock', 'profile': {},
            }],
        },
        'jev': None,
        'jev_window': {'input_tokens': 1024, 'context_tokens': 4096},
        'time_periods': [],
    }


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        return response.status, dict(response.headers), json.loads(raw or b'{}')


def create_and_enable(base, document):
    payload = {key: value for key, value in document.items()
               if key not in {'enabled', 'default'}}
    status, _, _ = call(base, 'POST', '/admin/trees', payload)
    assert status == 201
    status, _, _ = call(base, 'POST', f"/admin/trees/{document['id']}/enable")
    assert status == 200
    if document['departments']:
        current = call(base, 'GET', f"/admin/trees/{document['id']}")[2]['tree']
        status, _, _ = call(base, 'PUT', f"/admin/trees/{document['id']}/departments", {
            'departments': document['departments'],
            'base_document_sha256': current['document_sha256'],
        })
        assert status == 200
    if document['default']:
        status, _, _ = call(base, 'POST', f"/admin/trees/{document['id']}/default")
        assert status == 200
```

模拟上游必须实际处理 `/v1/chat/completions`：非流式返回 OpenAI `choices` JSON，流式逐块返回 `data:` 帧并以 `[DONE]` 结束；同时记录请求体。JEV mock 按请求中的问题类型返回 `choice/noul/score` 合法答案。脚本启动时先轮询 `/readyz`，不能通过固定 sleep 猜测服务已就绪。

准备：

- `main`：默认图，模型 `fast`。
- `finance`：启用，`departments=['财务部']`，模型 `strong`。
- `legal`：启用，`departments=['法务部']`，模型 `legal`。

公开 `/v1` 没有内部部门数据面，因此公开请求只应命中 `main`。部门选图通过 `/admin/try` 和管理端测试对话验证。

- [ ] **Step 3: 验证公开 JSON 与 SSE**

对真实进程执行：

```bash
curl -fsS -D /tmp/routepilot-v4-headers.txt \
  http://127.0.0.1:18400/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"用一句话回答：1+1=?"}]}'

curl -fsS -N http://127.0.0.1:8400/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"逐字回答：你好"}]}'
```

Expected: JSON 200、SSE 正常结束；响应头 `x-sr-tree-id: main`、`x-sr-graph-selection: default`；模拟上游收到 `model: fast`，没有用户伪造部门；SSE 不被完整缓冲。

再发送 `X-SR-Department: %E8%B4%A2%E5%8A%A1%E9%83%A8`，预期公开 `/v1` 返回 400 `department_header_not_allowed`。

- [ ] **Step 4: 验证部门试算、默认切换和故障恢复**

真实调用 `/admin/try`：

```json
{"department":"财务部","text":"处理请求"}
{"department":"法务部","text":"处理请求"}
{"department":"未知部门","text":"处理请求"}
{"department":"","text":"处理请求"}
```

Expected: 分别命中 `finance/legal/main/main`，`graph_selection` 分别为 `department/department/default/default`，模型为 `strong/legal/fast/fast`。

随后：

1. `POST /admin/trees/legal/default`，确认 `legal` 默认且 `main` 仍启用。
2. 公开 `/v1` 改用 `legal` 模型。
3. 停用 `main`，再尝试停用默认 `legal`；因 `finance` 仍启用而返回 409 `default_graph_required`。
4. 停用 `finance` 后停用 `legal`，确认空路由集和公开 `/v1` 的 `503 no_active_tree`。
5. 重新启用图后，将一张启用图写成非法 JSON；确认 `/readyz` 报告 `stale:true` 且旧集合仍服务。恢复文件后确认 `stale:false`。
6. 重启第二个只读副本，确认路由集与写者一致。

- [ ] **Step 5: 检查最终工作区与提交**

删除冒烟产生的临时数据库、图目录、日志、header 文件和临时脚本输出，不删除用户已有文件。

Run:
```bash
git status --short --branch
git log --oneline --max-count=12
git diff --check
```
Expected: 只有本功能预期的源码、测试和文档变更；没有临时数据库、日志、截图、`.db`、header 文件或冒烟产物。按逻辑提交最后修复：

```bash
git add router tests scripts configs README.md README.zh-CN.md docs
git commit -m "test: verify standalone department routing end to end"
git push origin main
```

最终确认 `git status --short --branch` 与 `origin/main` 一致，且没有推送 `github`。

## Completion Checklist

- [ ] v4 文件含 `departments/enabled/default`，代码无 v3/current 兼容路径。
- [ ] 多图启用、唯一默认、部门冲突、空路由集和默认切换事务均有测试。
- [ ] 公开 `/v1` 默认图 JSON/SSE 与管理端部门选图均通过真实调用。
- [ ] 控制台部门、默认状态、试算、测试对话、导入导出与中英文文案完整。
- [ ] 所有启用图目录同步、只读副本重载和 `stale` 恢复正确。
- [ ] 全量 pytest、Node/Playwright、i18n 和 `git diff --check` 全绿。
- [ ] 未修改 163、APISIX、newapi、token-factory，未增加内部网关端点。
- [ ] 未新增依赖、未泄露密钥、未保留临时产物，所有提交仅在 Gitee `main`。

## Phase 2 Boundary

第一阶段验收前禁止实施以下内容：内部 `/internal/v1/chat/completions`、`ROUTER_GATEWAY_TOKEN`、APISIX Auto 中间件、163 部署、newapi/token-factory 替换、部门接口的带身份 163 实测。第二阶段以本设计第 9–10 节为输入另写实施计划，并分别验证当前 newapi 与未来 token-factory。
