# 恢复 v3 画布 + canvas key 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 撤销 Routing Policy v4 合并、恢复 v3 拖拽画布，并把画布坐标从节点对象迁到文档顶层 `canvas` key（严格单格式，无兼容层）。

**Architecture:** `git revert -m 1 f22d0ec` 恢复 v3 全栈（35 个提交整体撤销，chat 提交保留）；随后只在两个序列化边界做增量——后端 `document.py`/`graph/model.py`/`tree.py` 与前端 `graph.js`——把 `node.position` 改为顶层 `canvas: {node_id: {x, y}}`。domain 的 `GraphNode.position` 保留为内部工作状态，行为哈希/快照/校验/评估零改动。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic、httpx、SQLite、pytest；原生 ES Modules、Node `node:test`、Playwright Core。不得新增第三方依赖。

**Design source:** `docs/superpowers/specs/2026-11-19-restore-canvas-v3-design.md`

## Global Constraints

- 严格单格式：节点对象出现 `position` → `graph_parse_error`；`version` 保持 3；不做双格式兼容、不写迁移工具、不留兼容开关。
- `canvas` 可选；值必须是 `{x, y}` 有限数值；引用不存在节点 → 拒绝；节点缺条目合法（前端自动布局补位）。
- 行为哈希（`tree_revision_of`）/ 快照 / 启用校验 / GraphEvaluator / 调度 / 粘性 / 账本 / API 零改动。
- 前端内部 `node.position` 工作状态保留；只在序列化边界（导入/导出）转换。
- 拖拽坐标只产生 `layout` 变更类别，不产生 `behavior` dirty。
- 旧文件一次性重写（仓库 configs、example.json、用户 `configs/cases/*.json`、76 的 11 个图文件）；不提交永久迁移脚本。
- 工作区保护：用户未跟踪文件（`README.pdf`、`.pdf-build/`、`artifacts/`、PPT、`智能路由技术方案/`）不得加入提交；`configs/cases/` 先备份再改写。
- 部署前每个阶段都有独立备份（DB、routing-graph 目录、unit 文件）。

## File Structure

### Task 1（revert）恢复的既有文件
- `router/routing/graph/model.py`：图模型与 storage/behavior 两个序列化视图。
- `router/routing/tree.py`：envelope、`tree_from_dict`、`tree_storage_dict`、`tree_revision_of`。
- `router/routing/document.py`：v3 文档 bytes/parser（`FIELDS` 顶层字段表）。
- `router/console/routing/graph.js`：前端图模型、导入导出、校验、布局。
- 其余 v3 全栈（evaluator、reloader、routes_trees、canvas.js 等）由 revert 原样恢复，本计划不再改动。

### 本计划的增量文件
- Modify: `router/routing/graph/model.py`（`_position` 改为 canvas 注入；节点解析去掉 position；新增 `canvas_to_dict`）
- Modify: `router/routing/tree.py`（`tree_from_dict` 接受 `canvas` 并传给 `graph_from_dict`）
- Modify: `router/routing/document.py`（`FIELDS` 加 `canvas`；解析校验；序列化输出）
- Modify: `router/console/routing/graph.js`（`IMPORT_FIELDS`/`NODE_FIELDS`/`validateContract`/`serialize`/`parseImport`）
- Modify: `tests/test_graph_document.py`、`tests/test_graph_model.py`（新增 canvas 用例）
- Create: `scripts/canvas_rewrite.py`（一次性迁移脚本，仅本地运行，**不提交**）
- Rewrite: `configs/default_routing_tree.json`、`configs/production_routing_tree.json`、`router/console/routing/example.json`、`configs/cases/*.json`

---

### Task 1: 撤销 v4 合并并恢复 v3 基线

**Files:**
- 涉及整个仓库（revert 提交）

**Interfaces:**
- Consumes: 合并提交 `f22d0ec`（parents: `28d0098` = main、`067cab0` = v4 分支）。
- Produces: main 上新的 revert 提交；工作树回到 v3 全栈 + 11 个 chat 提交。

- [ ] **Step 1: 确认工作区状态与 revert 目标**

```bash
cd /Users/yuansu/work/smart-router
git status --short          # 只应有用户未跟踪文件（?? 开头）
git log --oneline -3        # HEAD 应为 c63fcc5（spec 提交），其父 f22d0ec
git log -1 --format="%H %P" f22d0ec
```

Expected: `f22d0ec... 28d0098b... 067cab0b...`（parent 1 = main，parent 2 = v4 分支）。若 parent 顺序不同，`-m` 取值相应改为另一个 parent。

- [ ] **Step 2: 执行 revert**

```bash
git revert -m 1 --no-edit f22d0ec
```

Expected: 生成一个 revert 提交，无冲突。若出现冲突（chat 提交与 v4 若有重叠），按「保留 chat 侧改动、撤销 v4 侧改动」解决后 `git revert --continue`。

- [ ] **Step 3: 验证 v3 全栈恢复**

```bash
ls router/routing/graph/model.py router/api/routes_trees.py router/console/routing/canvas.js
test ! -d router/routing/policy && echo "policy dir gone"
git grep -c "SCHEMA_VERSION = 4" router/store/db.py
git grep -l "sr-routing-graph" router/routing/document.py
```

Expected: 前三个文件存在；`policy dir gone`；`SCHEMA_VERSION = 4` 命中；`document.py` 含 `sr-routing-graph`。

- [ ] **Step 4: 跑全量基线门禁**

```bash
.venv/bin/python -m pytest -q
npm run check:console
```

Expected: pytest 全绿（数量应与 revert 后基线一致，约 700+）；`check:console` ALL PASS（含 chat 用例）。若 `package.json` 的 `check:console` 脚本被 revert 回旧版，确认它引用仍存在的文件（canvas/inspector 模块已恢复）。

- [ ] **Step 5: 提交**

```bash
git log --oneline -2
git status --short   # 除用户未跟踪文件（??）外应无变化：revert 已自动创建提交
```

Expected: HEAD 为 revert 提交；工作区仅剩 `??` 用户未跟踪文件。

---

### Task 2: 后端 canvas key 序列化

**Files:**
- Modify: `router/routing/graph/model.py`（`_position`、`_node_from_dict`、`graph_from_dict`、`_node_to_dict`、新增 `canvas_to_dict`）
- Modify: `router/routing/tree.py`（`tree_from_dict`）
- Modify: `router/routing/document.py`（`FIELDS`、`graph_document`、`parse_graph_document`）
- Test: `tests/test_graph_document.py`、`tests/test_graph_model.py`

**Interfaces:**
- Consumes: `Position`、`finite_number`、`reject_unknown_fields`（`router/routing/codec.py`）、`graph_to_dict`。
- Produces:
  - `canvas_to_dict(root: GraphRoot) -> dict[str, dict[str, float]]`
  - `graph_from_dict(raw, canvas: dict | None = None) -> GraphRoot`（第二参数新增、默认 None）
  - `graph_document(tree: RoutingTree) -> dict`（有坐标时含顶层 `"canvas"`）
  - `parse_graph_document(...)`（接受并校验 `canvas`）

- [ ] **Step 1: 写失败的文档格式测试**

在 `tests/test_graph_document.py` 追加：

```python
def test_document_moves_positions_to_top_level_canvas():
    raw = tree_dict()
    raw["root"]["nodes"][0]["position"] = {"x": 321, "y": 654}
    raw["root"]["nodes"][1]["position"] = {"x": 40, "y": 90}
    source = tree_from_dict(raw)

    document = json.loads(graph_document_bytes(source))
    assert "canvas" in document
    assert document["canvas"]["start"] == {"x": 321, "y": 654}
    assert document["canvas"][raw["root"]["nodes"][1]["id"]] == {"x": 40, "y": 90}
    for node in document["root"]["nodes"]:
        assert "position" not in node, node

    loaded = load(graph_document_bytes(source))
    assert loaded.root.nodes[0].position.x == 321
    assert loaded.root.nodes[1].position.y == 90
    assert loaded.tree_revision == tree_revision_of(source)


def test_document_omits_canvas_when_no_node_has_a_position():
    raw = tree_dict()
    for node in raw["root"]["nodes"]:
        node.pop("position", None)
    document = json.loads(graph_document_bytes(tree_from_dict(raw)))
    assert "canvas" not in document


def test_document_rejects_legacy_node_position():
    raw = tree_dict()
    raw["root"]["nodes"][0]["position"] = {"x": 1, "y": 2}
    content = json.dumps({
        "kind": "sr-routing-graph", "version": 3, "name": raw.get("name", ""),
        "timezone": raw["timezone"], "sticky": raw["sticky"], "root": raw["root"],
    }, ensure_ascii=False).encode("utf-8")
    with pytest.raises(ValueError, match="unknown graph document fields|unknown .* fields"):
        load(content)


@pytest.mark.parametrize("canvas, message", [
    ([], "canvas must be an object"),
    ({"start": {"x": 1}}, "canvas.start.x"),
    ({"ghost": {"x": 1, "y": 2}}, "canvas references unknown nodes"),
    ({"start": {"x": "1", "y": 2}}, "canvas.start.x"),
    ({"start": {"x": 1, "y": 2, "z": 3}}, "canvas.start"),
    ({"start": {"x": float("nan"), "y": 2}}, "canvas.start.x"),
])
def test_document_rejects_invalid_canvas(canvas, message):
    raw = tree_dict()
    for node in raw["root"]["nodes"]:
        node.pop("position", None)
    payload = {
        "kind": "sr-routing-graph", "version": 3, "name": raw.get("name", ""),
        "timezone": raw["timezone"], "sticky": raw["sticky"], "root": raw["root"],
        "canvas": canvas,
    }
    content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with pytest.raises(ValueError, match=message):
        load(content)


def test_canvas_edits_do_not_change_the_behavior_revision():
    raw = tree_dict()
    base = tree_from_dict(raw)
    moved = json.loads(graph_document_bytes(base))
    moved["canvas"] = {node["id"]: {"x": 10, "y": 20} for node in moved["root"]["nodes"]}
    shifted = load(json.dumps(moved, ensure_ascii=False).encode("utf-8"))
    assert shifted.tree_revision == base.tree_revision
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_graph_document.py`
Expected: FAIL —— `canvas` 不在 `FIELDS`（未知字段被拒）、`graph_document` 仍输出 `position`。

- [ ] **Step 3: 改 `router/routing/graph/model.py`**

把 `_position` 替换为 canvas 注入版本：

```python
def _position(canvas: dict, node_id: str, kind: str) -> Position | None:
    raw_position = canvas.get(node_id)
    if raw_position is None:
        return None
    if not isinstance(raw_position, dict):
        raise ValueError(f"{kind}.position must be an object with x/y")
    reject_unknown_fields(f"{kind}.position", raw_position, {"x", "y"})
    return Position(
        finite_number(raw_position.get("x"), f"{kind}.position.x"),
        finite_number(raw_position.get("y"), f"{kind}.position.y"),
    )
```

`_node_from_dict(raw)` 改签名为 `_node_from_dict(raw, canvas: dict)`；每个节点的
`reject_unknown_fields` 允许集**删掉 `"position"`**；先取 `node_id = _node_id(raw)`，
再把原先所有 `_position(raw, "xxx")` 调用改为 `_position(canvas, node_id, "xxx")`。
例如 start 分支变为：

```python
    if node_type == "start":
        reject_unknown_fields("start node", raw, {"id", "type", "title"})
        node_id = _node_id(raw)
        return StartNode(node_id, optional_title(raw, "start"), _position(canvas, node_id, "start"))
```

其余四类节点同样处理（question/logic/condition/target 的允许集删 `"position"`、
`node_id` 前置、`_position(canvas, node_id, ...)`）。

`graph_from_dict` 改签名并注入 canvas：

```python
def graph_from_dict(raw, canvas: dict | None = None) -> GraphRoot:
    if not isinstance(raw, dict):
        raise ValueError("graph must be an object")
    if canvas is None:
        canvas = {}
    if not isinstance(canvas, dict):
        raise ValueError("canvas must be an object")
    reject_unknown_fields(
        "graph", raw, {"id", "type", "nodes", "edges", "error_fallback"},
    )
    if raw.get("type") != "graph":
        raise ValueError(f"graph.type must be 'graph', got {raw.get('type')!r}")
    nodes = raw.get("nodes", [])
    edges = raw.get("edges", [])
    if not isinstance(nodes, list):
        raise ValueError("graph.nodes must be a list")
    if not isinstance(edges, list):
        raise ValueError("graph.edges must be a list")
    root = GraphRoot(
        text_field(raw.get("id"), "graph id"),
        tuple(_node_from_dict(node, canvas) for node in nodes),
        tuple(_edge_from_dict(edge) for edge in edges),
        _fallback_ref(raw),
    )
    unknown_canvas = sorted(set(canvas) - {node.id for node in root.nodes})
    if unknown_canvas:
        raise ValueError(f"canvas references unknown nodes: {', '.join(unknown_canvas)}")
    return root
```

`_node_to_dict` 删除 position 输出（删掉末尾两行 `if node.position is not None: ...`），
并新增：

```python
def canvas_to_dict(root: GraphRoot) -> dict:
    return {
        node.id: {"x": node.position.x, "y": node.position.y}
        for node in root.nodes
        if node.position is not None
    }
```

把 `canvas_to_dict` 加入 `__all__`（该模块现有 `__all__` 列表，按字母序插入）。

- [ ] **Step 4: 改 `router/routing/tree.py`**

`tree_from_dict` 的允许字段集加 `"canvas"`，并透传：

```python
    reject_unknown_fields(
        "tree",
        raw,
        {"id", "name", "enabled", "timezone", "sticky", "root", "canvas", "tree_revision",
         "updated_at", "document_sha256"},
    )
```

```python
        root=graph_from_dict(raw.get("root"), raw.get("canvas")),
```

`tree_storage_dict` 同步输出 canvas：

```python
def tree_storage_dict(tree: RoutingTree) -> dict:
    document = {
        "id": tree.id,
        "name": tree.name,
        "enabled": tree.enabled,
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": graph_to_dict(tree.root),
    }
    canvas = canvas_to_dict(tree.root)
    if canvas:
        document["canvas"] = canvas
    return document
```

（`from router.routing.graph.model import canvas_to_dict, graph_to_dict` 也需加入 import。）

- [ ] **Step 5: 改 `router/routing/document.py`**

```python
FIELDS = {"kind", "version", "name", "timezone", "sticky", "root", "canvas"}
```

`graph_document` 加 canvas：

```python
def graph_document(tree: RoutingTree) -> dict:
    document = {
        "kind": KIND,
        "version": VERSION,
        "name": tree.name,
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": graph_to_dict(tree.root),
    }
    canvas = canvas_to_dict(tree.root)
    if canvas:
        document["canvas"] = canvas
    return document
```

`parse_graph_document`：把 `canvas` 透传进 envelope；`canvas` 的类型与形状校验交给
`graph_from_dict`（单一校验点）：

```python
    tree = tree_from_dict({
        "id": tree_id,
        "name": raw["name"],
        "enabled": enabled,
        "timezone": raw["timezone"],
        "sticky": raw["sticky"],
        "root": raw["root"],
        "canvas": raw.get("canvas", {}),
        "updated_at": updated_at,
    })
```

（import 行加 `canvas_to_dict`。）

- [ ] **Step 6: 补模型层测试**

在 `tests/test_graph_model.py` 追加：

```python
def test_graph_from_dict_injects_positions_from_canvas():
    raw = {
        "id": "root", "type": "graph", "error_fallback": None,
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "t1", "type": "target", "models": [{"model": "fast"}], "priority": 1},
        ],
        "edges": [],
    }
    root = graph_from_dict(raw, {"start": {"x": 5, "y": 6}})
    assert root.nodes[0].position.x == 5
    assert root.nodes[1].position is None


def test_canvas_to_dict_skips_unpositioned_nodes():
    raw = tree_dict()
    for node in raw["root"]["nodes"]:
        node.pop("position", None)
    raw["root"]["nodes"][0]["position"] = {"x": 7, "y": 8}
    tree = tree_from_dict(raw)
    assert canvas_to_dict(tree.root) == {raw["root"]["nodes"][0]["id"]: {"x": 7, "y": 8}}
```

（按该文件现有 import 风格补 `graph_from_dict`、`canvas_to_dict`、`tree_dict`、`tree_from_dict`。）

- [ ] **Step 7: 运行后端测试**

Run: `.venv/bin/python -m pytest -q tests/test_graph_document.py tests/test_graph_model.py`
Expected: 全部 PASS。

- [ ] **Step 8: 提交**

```bash
git add router/routing/graph/model.py router/routing/tree.py router/routing/document.py tests/test_graph_document.py tests/test_graph_model.py
git commit -m "feat(routing): move canvas coordinates to a top-level canvas key"
```

---

### Task 3: 前端 graph.js 的 canvas 导入导出边界

**Files:**
- Modify: `router/console/routing/graph.js`
- Test: `scripts/console_graph_test.mjs`

**Interfaces:**
- Consumes: 既有 `parseImport(text)`、`serialize(tree)`、`validate(tree)`、`layoutPositions(mode, graph)`、`IMPORT_FIELDS`、`NODE_FIELDS`、`validateContract`。
- Produces: 导入接受顶层 `canvas` 并分发到内部 `node.position`；导出/下载把 `node.position` 收集进 `canvas`；节点对象不再接受 `position`。

- [ ] **Step 1: 写失败的前端契约测试**

在 `scripts/console_graph_test.mjs` 追加（沿用该文件现有 `test(...)`/断言风格与 import）：

```javascript
test('import dispatches top-level canvas into node positions', () => {
  const document = {
    kind: 'sr-routing-graph', version: 3, name: '导入', timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph', error_fallback: null,
      nodes: [
        { id: 'start', type: 'start' },
        { id: 't1', type: 'target', models: [{ model: 'fast' }], priority: 1 }
      ],
      edges: []
    },
    canvas: { start: { x: 11, y: 22 } }
  };
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  const start = result.tree.root.nodes.find(node => node.id === 'start');
  assert.deepEqual(start.position, { x: 11, y: 22 });
});

test('export collects node positions into a top-level canvas', () => {
  const tree = {
    name: '导出', timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph', error_fallback: null,
      nodes: [
        { id: 'start', type: 'start', position: { x: 3, y: 4 } },
        { id: 't1', type: 'target', models: [{ model: 'fast' }], priority: 1 }
      ],
      edges: []
    }
  };
  const document = JSON.parse(RTGraph.serialize(tree));
  assert.deepEqual(document.canvas, { start: { x: 3, y: 4 } });
  for (const node of document.root.nodes) assert.equal('position' in node, false);
});

test('import rejects a node-level position field', () => {
  const document = {
    kind: 'sr-routing-graph', version: 3, name: '', timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph', error_fallback: null,
      nodes: [{ id: 'start', type: 'start', position: { x: 1, y: 2 } }],
      edges: []
    }
  };
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, false);
  assert.ok(result.issues.some(item => item.code === 'graph_parse_error'));
});

test('invalid canvas shapes are rejected on import', () => {
  const base = {
    kind: 'sr-routing-graph', version: 3, name: '', timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: { id: 'root', type: 'graph', error_fallback: null,
      nodes: [{ id: 'start', type: 'start' }], edges: [] }
  };
  for (const canvas of [[], { ghost: { x: 1, y: 2 } }, { start: { x: 1 } }]) {
    const result = RTGraph.parseImport(JSON.stringify(Object.assign({}, base, { canvas })));
    assert.equal(result.ok, false, JSON.stringify(canvas));
  }
});
```

- [ ] **Step 2: 运行确认失败**

Run: `node --test scripts/console_graph_test.mjs`
Expected: 新增用例 FAIL（`canvas` 被判未知字段、导出仍写 `position`）。

- [ ] **Step 3: 改 `graph.js` 顶部常量**

```javascript
const IMPORT_FIELDS = ['kind', 'version', 'name', 'timezone', 'sticky', 'root', 'canvas'];
const NODE_FIELDS = {
  start: ['id', 'type', 'title'],
  question: ['id', 'type', 'question_type', 'instructions', 'title', 'options', 'criteria'],
  logic: ['id', 'type', 'operator', 'title'],
  condition: ['id', 'type', 'source', 'contains_any', 'periods', 'title'],
  target: ['id', 'type', 'models', 'priority', 'strategy', 'capability', 'title'],
};
```

- [ ] **Step 4: 改 `validateContract`**

删除节点级 position 的未知字段检查（原 `if (isRecord(node.position)) { addUnknownFields(...) }`），
新增 canvas 形状校验（放在 graph 节点遍历之前）：

```javascript
function validateCanvas(value, graph, issues) {
  if (value === undefined) return;
  if (!isRecord(value)) {
    issues.push(issue('graph_parse_error', 'canvas 必须是对象', 'canvas must be an object'));
    return;
  }
  const nodeIds = new Set((Array.isArray(graph.nodes) ? graph.nodes : [])
    .filter(isRecord)
    .map(node => (isNonEmptyString(node.id) ? node.id : ''))
    .filter(Boolean));
  Object.keys(value).forEach(nodeId => {
    if (!nodeIds.has(nodeId)) {
      issues.push(issue('graph_parse_error', 'canvas 引用了不存在的节点：' + nodeId,
        'canvas references an unknown node: ' + nodeId, { node_id: nodeId }));
      return;
    }
    const coords = value[nodeId];
    if (!isRecord(coords)) {
      issues.push(issue('graph_parse_error', 'canvas.' + nodeId + ' 必须是 {x,y} 对象',
        'canvas.' + nodeId + ' must be an {x,y} object', { node_id: nodeId }));
      return;
    }
    addUnknownFields(coords, ['x', 'y'], 'canvas.' + nodeId, issues, nodeId);
    if (!isFiniteNumber(coords.x) || !isFiniteNumber(coords.y)) {
      issues.push(issue('graph_parse_error', 'canvas.' + nodeId + ' 的 x/y 必须是有限数值',
        'canvas.' + nodeId + '.x/y must be finite numbers', { node_id: nodeId }));
    }
  });
}
```

在 `validateContract(value, graph, issues)` 内调用 `validateCanvas(value.canvas, graph, issues);`。

- [ ] **Step 5: 改 `serialize`**

```javascript
function serialize(tree) {
  const parts = envelopeParts(tree);
  const rootValue = clone(parts.root);
  const canvas = {};
  (Array.isArray(rootValue.nodes) ? rootValue.nodes : []).forEach(node => {
    if (!isRecord(node)) return;
    const position = node.position;
    if (isRecord(position) && isFiniteNumber(position.x) && isFiniteNumber(position.y)) {
      canvas[node.id] = { x: Math.round(position.x), y: Math.round(position.y) };
    }
    delete node.position;
  });
  const document = {
    kind: KIND,
    version: VERSION,
    name: parts.name,
    timezone: parts.timezone,
    sticky: parts.sticky,
    root: rootValue
  };
  if (Object.keys(canvas).length) document.canvas = canvas;
  return JSON.stringify(document, null, 2);
}
```

- [ ] **Step 6: 改 `parseImport`**

在 `missingImportFields` 检查中把 `canvas` 排除（可选字段）：

```javascript
  const missingImportFields = IMPORT_FIELDS
    .filter(field => field !== 'canvas')
    .filter(field => !Object.prototype.hasOwnProperty.call(data, field)).sort();
```

`canvas` 形状校验交给 `validate()`（Step 4 已加）。位置分发放在 `layoutPositions` 之前：

```javascript
  const graph = clone(raw);
  graph.id = isNonEmptyString(graph.id) ? graph.id : 'root';
  graph.type = 'graph';
  if (!graph.error_fallback && graph.error_fallback !== null) graph.error_fallback = null;

  /* 画布坐标集中在顶层 canvas：导入时分发回内部 node.position。 */
  const canvas = isRecord(data.canvas) ? data.canvas : {};
  graph.nodes.forEach(node => {
    if (!isRecord(node)) return;
    const coords = isNonEmptyString(node.id) ? canvas[node.id] : null;
    if (isRecord(coords) && isFiniteNumber(coords.x) && isFiniteNumber(coords.y)) {
      node.position = { x: coords.x, y: coords.y };
    }
  });

  const positions = layoutPositions('dag', graph);
```

（后续未定位节点的布局补位逻辑保持原样。）

- [ ] **Step 7: 运行前端契约测试**

Run: `node --test scripts/console_graph_test.mjs`
Expected: 全部 PASS（原有用例 + 4 个新用例）。

- [ ] **Step 8: 提交**

```bash
git add router/console/routing/graph.js scripts/console_graph_test.mjs
git commit -m "feat(console): read and write canvas coordinates as a top-level key"
```

---

### Task 4: 旧文件一次性重写为新格式（含 example.json 文档化）

**Files:**
- Create（仅本地，不提交）: `scripts/canvas_rewrite.py`
- Rewrite: `configs/default_routing_tree.json`、`configs/production_routing_tree.json`、`configs/cases/*.json`
- Rewrite + 格式变更: `router/console/routing/example.json`（root-only → 完整 v3 文档）
- Modify: `router/console/routing/controller.js`（`loadExample` 消费文档格式）
- Modify: `scripts/console_graph_test.mjs`（`readExample` helper）
- Modify: `scripts/console_qa.mjs`（`EXAMPLE`/`EXAMPLE_NODES`/`EXAMPLE_EDGES`/`EXAMPLE_START` 取文档 root 与 canvas）

**Interfaces:**
- Consumes: `router.routing.graph.model.graph_from_dict`、`router.routing.tree.tree_from_dict`；前端 `RTGraph.parseImport(text)`。
- Produces: 仓库内所有 v3 图文件均为新格式（无节点级 `position`，坐标在顶层 `canvas`）；`example.json` 是完整文档，`EXAMPLE_START` 来自 `canvas.start`。

**格式事实（已核实）**
- `configs/default_routing_tree.json`、`production_routing_tree.json`：完整文档（`kind/version/name/timezone/sticky/root`），节点含 `position`。
- `configs/cases/*.json`：envelope（`id/name/timezone/sticky/root`），节点含 `position`；`manifest.json` 无节点，跳过。
- `router/console/routing/example.json`：**root-only**（`id/type/nodes/edges/error_fallback`）；canvas 属文档顶层，故必须改为完整文档，并同步三个消费点。

- [ ] **Step 1: 备份用户用例并清点待重写文件**

```bash
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /tmp/canvas-migration-backup-$TS
cp -a configs/cases /tmp/canvas-migration-backup-$TS/
cp configs/default_routing_tree.json configs/production_routing_tree.json /tmp/canvas-migration-backup-$TS/
cp router/console/routing/example.json /tmp/canvas-migration-backup-$TS/
ls /tmp/canvas-migration-backup-$TS/
```

Expected: 备份目录含 cases/ 与三个 JSON。

- [ ] **Step 2: 写一次性重写脚本（不提交）**

创建 `scripts/canvas_rewrite.py`。脚本同时支持三种输入形态：完整文档（含 `kind`）、
envelope（含 `id`，如 `configs/cases/*.json`）、root-only（含 `type: "graph"`，如
`example.json`，此时包装成完整文档）：

```python
"""One-off: move v3 graph node positions into the top-level canvas key.

Handles three input shapes: full documents (kind=sr-routing-graph), tree
envelopes (id/name/timezone/sticky/root), and root-only graphs (type=graph,
wrapped into a full document). Not part of the product tree; delete after the
migration. Usage:
    .venv/bin/python scripts/canvas_rewrite.py FILE [FILE ...]
"""
import json
import sys

from router.routing.codec import finite_number
from router.routing.tree import tree_from_dict

DOCUMENT_FIELDS = ("kind", "version", "name", "timezone", "sticky")


def rewrite(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    if data.get("type") == "graph" and "root" not in data:
        data = {
            "kind": "sr-routing-graph",
            "version": 3,
            "name": "示例决策组",
            "timezone": "Asia/Shanghai",
            "sticky": {"enabled": False, "ttl_seconds": 1800},
            "root": data,
        }

    root = data.get("root")
    if not isinstance(root, dict):
        raise SystemExit(f"{path}: no graph root found")

    canvas = {}
    for node in root.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        position = node.pop("position", None)
        if isinstance(position, dict):
            canvas[node["id"]] = {
                "x": finite_number(position.get("x"), f"{node['id']}.x"),
                "y": finite_number(position.get("y"), f"{node['id']}.y"),
            }
    if canvas:
        data["canvas"] = canvas

    tree_from_dict({
        "id": data.get("id", root.get("id", "root")),
        "name": data.get("name", ""),
        "enabled": False,
        "timezone": data.get("timezone", "Asia/Shanghai"),
        "sticky": data.get("sticky", {"enabled": False, "ttl_seconds": 1800}),
        "root": root,
        "canvas": canvas,
    })
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    for target in sys.argv[1:]:
        rewrite(target)
        print("rewrote", target)
```

- [ ] **Step 3: 重写 configs、用户用例与 example.json**

```bash
.venv/bin/python scripts/canvas_rewrite.py \
  configs/default_routing_tree.json \
  configs/production_routing_tree.json \
  router/console/routing/example.json \
  configs/cases/case-*.json
```

Expected: 逐个打印 `rewrote ...`（`configs/cases/manifest.json` 不在匹配内，保持原样）。

- [ ] **Step 4: 改 `controller.js` 的 `loadExample` 消费文档格式**

原实现把 `example.json` 当 root 用（`example.type !== 'graph'` 校验 + `tree.root = clone(example)`）。
改为复用 `RTGraph.parseImport` 解析完整文档，成功后只替换 `tree.root`（envelope 的
id/enabled/name/timezone/sticky 保持不变，与旧行为一致）：

```javascript
  async loadExample() {
    const confirmed = await this.hooks.askDiscard('example',
      this.store.dirtyKind === 'clean' ? CONFIRM_LOAD_EXAMPLE : null);
    if (!confirmed) return false;
    try {
      const response = await fetch('routing/example.json', { cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const parsed = RTGraph.parseImport(await response.text());
      if (!parsed.ok) throw new Error(parsed.error);
      /* 示例是整图替换（不走 loadTree）：应用前让在途的 switch 过期，
         否则旧回包会在示例落地后把草稿盖回去。 */
      this.loadSeq += 1;
      /* 一次历史操作替换整张图；envelope 的 id/enabled/name/timezone/sticky 保持不变。 */
      if (this.hooks.change('behavior', tree => { tree.root = RTGraph.clone(parsed.tree.root); }) === false) {
        return false;
      }
      this.hooks.fitAfterReplace();
      this.hooks.toast(t('cv.toastExampleLoaded'));
      return true;
    } catch (error) {
      this.hooks.toast(tf('cv.toastLoadFail', {
        err: String(error && error.message != null ? error.message : error),
      }), 'error');
      return false;
    }
  }
```

- [ ] **Step 5: 改测试 helper 与 QA 的 example 消费点**

`scripts/console_graph_test.mjs` 的 `readExample()` 改为返回文档的 root（并把 canvas
分发回节点位置，使依赖位置的用例仍看到坐标）：

```javascript
function readExample() {
  const parsed = RTGraph.parseImport(fs.readFileSync(EXAMPLE_URL, 'utf8'));
  assert.equal(parsed.ok, true, 'example.json must import cleanly: ' + JSON.stringify(parsed.issues));
  return parsed.tree.root;
}
```

`scripts/console_qa.mjs`（:53-57 区域）改为：

```javascript
export const EXAMPLE = JSON.parse(fs.readFileSync(EXAMPLE_PATH, 'utf8'));
export const EXAMPLE_ROOT = EXAMPLE.root;
export const EXAMPLE_NODES = EXAMPLE_ROOT.nodes.length;
export const EXAMPLE_EDGES = EXAMPLE_ROOT.edges.length;
export const EXAMPLE_START = EXAMPLE.canvas.start;
export const EXAMPLE_TARGETS = { fallback: EXAMPLE_ROOT.error_fallback };
```

并同文件内把仍按 root 使用 `EXAMPLE` 的地方改为 `EXAMPLE_ROOT`（`grep -n "EXAMPLE\b"` 逐处核对；
`EXAMPLE_PATH`/`EXAMPLE_NODES`/`EXAMPLE_START`/`EXAMPLE_TARGETS` 的既有引用不受影响）。

- [ ] **Step 6: 验证新格式且能被后端解析**

```bash
git grep -n '"position"' -- configs router/console/routing/example.json   # 应无输出
.venv/bin/python - <<'PY'
import glob, hashlib
from router.routing.document import parse_graph_document
from router.routing.tree import tree_from_dict
for path in ["configs/default_routing_tree.json", "configs/production_routing_tree.json",
             "router/console/routing/example.json"]:
    content = open(path, "rb").read()
    parse_graph_document(content, tree_id="check", enabled=False, updated_at="",
                         document_sha256=hashlib.sha256(content).hexdigest())
    print("ok", path)
for path in sorted(glob.glob("configs/cases/case-*.json")):
    tree_from_dict(__import__("json").load(open(path, encoding="utf-8")))
    print("ok", path)
PY
node --test scripts/console_graph_test.mjs
```

Expected: grep 无输出；全部 `ok`；node 测试 PASS（`readExample` 走 parseImport 后仍给原有用例提供坐标）。

- [ ] **Step 7: 跑相关测试并提交**

```bash
.venv/bin/python -m pytest -q
npm run check:console
git add configs/default_routing_tree.json configs/production_routing_tree.json \
        router/console/routing/example.json router/console/routing/controller.js \
        scripts/console_graph_test.mjs scripts/console_qa.mjs
git commit -m "chore(configs): move shipped graph positions into the canvas key"
```

Expected: 全绿；`configs/cases/` 为用户未跟踪数据，**不提交**（文件已在本地重写）。

---

### Task 5: 全量门禁与画布 QA 验证

**Files:**
- 无代码改动（纯验证）；如发现回归则按失败点回到相应任务修

**Interfaces:**
- Consumes: Task 1–4 的成果。
- Produces: 可部署的 main 树。

- [ ] **Step 1: 全量测试**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q router
```

Expected: pytest 全绿、compileall exit 0。

- [ ] **Step 2: 完整控制台检查**

```bash
npm run check:console
```

Expected: 全部 PASS，浏览器无 console error；画布 QA（canvas/inspector 模块）运行。

- [ ] **Step 3: 符号与格式扫描**

```bash
git grep -n '"position"' -- configs router/console/routing/example.json   # 无输出
git grep -n 'canvas' -- router/routing/document.py router/routing/graph/model.py | head
git status --short                                                        # 仅用户未跟踪文件
```

Expected: 第一条无输出；第二条可见 canvas 相关代码；工作区仅剩 `??` 用户文件。

- [ ] **Step 4: 本机冒烟（临时端口 + 临时库）**

```bash
ROUTER_DB=/tmp/canvas-smoke.db ROUTER_GRAPH_DIR=/tmp/canvas-smoke-graph \
ROUTER_USER_AUTH=0 ROUTER_HOST=127.0.0.1 ROUTER_PORT=8410 \
.venv/bin/python -m router & echo $! > /tmp/canvas-smoke.pid
sleep 3
curl -s http://127.0.0.1:8410/healthz; echo
curl -s -o /dev/null -w "trees:%{http_code}\n" http://127.0.0.1:8410/admin/trees
curl -s -o /dev/null -w "routing:%{http_code}\n" http://127.0.0.1:8410/console/routing.html
kill $(cat /tmp/canvas-smoke.pid); rm -rf /tmp/canvas-smoke.db* /tmp/canvas-smoke-graph
```

Expected: `{"ok":true}`、`trees:200`、`routing:200`。

---

### Task 6: 部署到 76（两阶段）

**Files:**
- 远端：`/opt/smart-router-v3/`（代码、`routing-graph/`、unit `smart-router-v3.service`）

**Interfaces:**
- Consumes: Task 1–5 的 main 树、`/opt/router-v4-cutover-backup-20261119-154714.db`、`/opt/smart-router-v3-unit-backup-20261119-154714`。
- Produces: 76 运行 v3 画布 + canvas key 格式。

- [ ] **Step 1: Phase 0 备份并恢复 DB**

```bash
ssh root@192.168.95.76 'set -e
systemctl stop smart-router-v3
cp -a /opt/smart-router-v3/router-v3.db /opt/router-v3-prev4-$(date +%Y%m%d-%H%M%S).db
cp -a /opt/router-v4-cutover-backup-20261119-154714.db /opt/smart-router-v3/router-v3.db
rm -f /opt/smart-router-v3/router-v3.db-wal /opt/smart-router-v3/router-v3.db-shm
ls -la /opt/smart-router-v3/router-v3.db'
```

Expected: 停服；DB 为切换前备份（schema v4、约 188 行账本）。

- [ ] **Step 2: Phase 0 导出并同步 revert 后的 v3 原始代码（不含 canvas 增量）**

Phase 0 的目标是「旧格式代码 + 旧格式图 = 画布立即复活」，因此**不能**同步 main HEAD
（canvas 严格格式会拒绝旧图）。用 revert 提交的树导出：

```bash
REVERT=$(git log --format='%H %s' -n 20 | grep 'revert: restore drag-and-drop canvas' | cut -d' ' -f1)
test -n "$REVERT" || { echo "revert commit not found"; exit 1; }
rm -rf /tmp/v3-baseline && mkdir -p /tmp/v3-baseline
git archive "$REVERT" router | tar -x -C /tmp/v3-baseline
rsync -a --delete \
  --exclude='__pycache__/' \
  /tmp/v3-baseline/router/ root@192.168.95.76:/opt/smart-router-v3/router/
ssh root@192.168.95.76 'set -e
rm -rf /opt/smart-router-v3/router/routing/policy /opt/smart-router-v3/routing-policy
UNIT=/etc/systemd/system/smart-router-v3.service
sed -i "s|ROUTER_POLICY_DIR=.*|ROUTER_GRAPH_DIR=/opt/smart-router-v3/routing-graph|; s|ROUTER_POLICY_WRITER=.*|ROUTER_GRAPH_WRITER=true|" $UNIT
systemctl daemon-reload
grep -E "ROUTER_GRAPH|ROUTER_POLICY" $UNIT
grep -c "position" /opt/smart-router-v3/router/routing/graph/model.py'
```

Expected: 代码为 v3 原始版（`model.py` 仍含 `position` 处理，`grep -c` ≥ 1）；unit 只有
`ROUTER_GRAPH_DIR/ROUTER_GRAPH_WRITER`（无 POLICY 残留）。

- [ ] **Step 3: Phase 0 启动并验证旧格式图立即可用**

```bash
ssh root@192.168.95.76 'systemctl start smart-router-v3; sleep 3
systemctl is-active smart-router-v3
curl -s -m 5 http://127.0.0.1:8400/healthz; echo
TOKEN=$(curl -s -X POST http://127.0.0.1:8400/admin/login -H "Content-Type: application/json" \
  -d "{\"email\":\"admin@test.com\",\"password\":\"1qaz@WSX\"}" \
  | /opt/smart-router-v3/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)[\"token\"])")
curl -s -o /dev/null -w "trees:%{http_code}\n" -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8400/admin/trees
curl -s -o /dev/null -w "routing-page:%{http_code}\n" http://127.0.0.1:8400/console/routing.html'
```

Expected: active、`{"ok":true}`、`trees:200`、`routing-page:200`——画布服务恢复。

- [ ] **Step 4: Phase 1 备份并重写 76 的 11 个图文件**

```bash
ssh root@192.168.95.76 'set -e
BACKUP=/opt/routing-graph-backup-$(date +%Y%m%d-%H%M%S)
cp -a /opt/smart-router-v3/routing-graph $BACKUP
echo "backup: $BACKUP"
ls /opt/smart-router-v3/routing-graph/graphs/'
```

再把重写脚本送到服务器并执行：

```bash
scp scripts/canvas_rewrite.py root@192.168.95.76:/tmp/canvas_rewrite.py
ssh root@192.168.95.76 'cd /opt/smart-router-v3 && .venv/bin/python /tmp/canvas_rewrite.py /opt/smart-router-v3/routing-graph/graphs/*.json'
ssh root@192.168.95.76 'grep -l "\"position\"" /opt/smart-router-v3/routing-graph/graphs/*.json || echo "no position left"'
```

Expected: 11 个文件全部重写；grep 输出 `no position left`。

- [ ] **Step 5: Phase 1 同步 canvas 增量代码并验证**

图文件已重写为新格式，此时才同步 main HEAD（含 canvas 严格解析）：

```bash
rsync -a --delete \
  --exclude='.venv/' --exclude='node_modules/' --exclude='.git/' \
  --exclude='.superpowers/' --exclude='artifacts/' --exclude='__pycache__/' \
  --exclude='.pytest_cache/' --exclude='.DS_Store' --exclude='*.db' \
  --exclude='*.db-shm' --exclude='*.db-wal' --exclude='routing-graph/' \
  --exclude='routing-policy/' --exclude='.deploy-backup-*' \
  --exclude='.router-v3.db-litestream' \
  ./router/ root@192.168.95.76:/opt/smart-router-v3/router/
ssh root@192.168.95.76 'set -e
grep -c "canvas" /opt/smart-router-v3/router/routing/document.py
systemctl restart smart-router-v3; sleep 3
systemctl is-active smart-router-v3
curl -s -m 5 http://127.0.0.1:8400/healthz; echo
TOKEN=$(curl -s -X POST http://127.0.0.1:8400/admin/login -H "Content-Type: application/json" \
  -d "{\"email\":\"admin@test.com\",\"password\":\"1qaz@WSX\"}" \
  | /opt/smart-router-v3/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)[\"token\"])")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8400/admin/trees \
  | /opt/smart-router-v3/.venv/bin/python -c "
import sys,json
d=json.load(sys.stdin)
print(\"trees:\", [(t[\"id\"], t.get(\"enabled\")) for t in d[\"trees\"]])
"'
```

Expected: `document.py` 含 canvas；active、`{"ok":true}`、trees 列表含 `default-jev` 且 enabled
（启用态从 `current` 指针恢复）。**若此处 503/500，说明仍有图文件未重写**：查看
`journalctl -u smart-router-v3 -n 30` 定位文件，用 Step 4 的脚本补重写后重启。

- [ ] **Step 6: 真实请求与画布布局持久化验证**

```bash
ssh root@192.168.95.76 'TOKEN=$(curl -s -X POST http://127.0.0.1:8400/admin/login -H "Content-Type: application/json" \
  -d "{\"email\":\"admin@test.com\",\"password\":\"1qaz@WSX\"}" \
  | /opt/smart-router-v3/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)[\"token\"])")
curl -s -m 60 -D /tmp/h http://127.0.0.1:8400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句话解释什么是负载均衡\"}]}" -o /tmp/body
grep -i "x-sr" /tmp/h
REV=$(curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8400/admin/trees/default-jev \
  | /opt/smart-router-v3/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)[\"tree\"][\"tree_revision\"])")
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"default-jev\",\"layout\":{\"start\":{\"x\":10,\"y\":20}}}" \
  http://127.0.0.1:8400/admin/trees/default-jev/layout > /dev/null
REV2=$(curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8400/admin/trees/default-jev \
  | /opt/smart-router-v3/.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)[\"tree\"][\"tree_revision\"])")
echo "revision unchanged by layout: $([ \"$REV\" = \"$REV2\" ] && echo yes || echo NO)"'
```

Expected: `x-sr-model`/`x-sr-selection` 头存在；`revision unchanged by layout: yes`。
（若该部署版本的 layout 端点路径不同，先 `curl /openapi.json` 确认实际路径再调整。）

- [ ] **Step 7: 记录部署结果**

在工作记录中写明：部署时间、DB 恢复来源、routing-graph 备份路径、11 个图文件重写结果、健康与路由验证输出。
