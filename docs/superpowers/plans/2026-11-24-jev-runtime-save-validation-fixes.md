# JEV 窗口同源与保存硬校验修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复两个已复现的契约缺口：阻止 JEV 服务与窗口跨服务混用，并让所有保存操作都执行画布自身完整硬校验。

**Architecture:** 在现有 `GraphService` 边界完成两处最小修改，不引入新模块或数据库回退。JEV ID 变化时要求调用方显式提供对应 `jev_window`；保存统一复用已包含结构校验和启用级校验的 `tree_issues()`。

**Tech Stack:** Python 3.11+、FastAPI、pytest、现有 `GraphService` / `GraphPatch` / `tree_issues()`。

## Global Constraints

- 目标分支：`feat/strict-self-contained-canvas`，审核基线：`1eb3b58f26e6264a5d8d4714c46bb83c52b6f360`。
- 不修改已确认的快照架构：已保存画布运行只读自身 JSON，数据库只提供管理目录、下拉框和密钥。
- 不从数据库自动补窗口。JEV ID 变化时，调用方必须在同一请求显式提供新服务的 `jev_window`，避免隐藏的数据库依赖。
- `jev: null` 仍合法，但仅适用于没有问题节点的画布；清除 JEV 不要求额外窗口。
- 同一 JEV ID 的提示词等非窗口字段更新可以继承当前窗口，不得被过度拦截。
- `tree_issues()` 已包含 `validate_graph_structure()`，不得重复执行两遍结构校验。
- 不放宽新回归测试来保留旧行为；只修改明确依赖旧保存语义的现有测试。
- 本计划不包含业务实现。执行者按任务顺序做 TDD，每个任务独立验证、独立提交。

---

## File Structure

- `router/control/graphs.py`：唯一的生产修改点。负责 JEV/window 补丁合并约束和保存硬校验。
- `tests/test_routing_trees_admin.py`：复用现有 `client`、`create_tree()`、`save_tree()`、`graph_with_model()`、`_direct_graph()` 辅助，覆盖两个真实 API 缺陷。
- `README.md`、`README.zh-CN.md`：补充原始树更新 API 的 JEV/window 同请求规则；保存硬校验的现有描述保持不变。

### Task 1: 阻止 JEV 服务与旧窗口混用

**Files:**
- Modify: `router/control/graphs.py:60-77,183-214`
- Test: `tests/test_routing_trees_admin.py`
- Modify: `README.md`, `README.zh-CN.md`

**Interfaces:**
- Consumes: 现有 `GraphPatch.jev`、`GraphPatch.jev_window`、`UNSET`、`RoutingTree.runtime.jev`、`ValidationFailed(graph_parse_failed(...))`。
- Produces: 当且仅当补丁把 JEV 改为不同的非空服务 ID 时，要求同一 PUT 请求也显式携带 `jev_window`；否则返回 422 `graph_parse_error`，详情包含 `jev_window is required when changing JEV service id`。

- [ ] **Step 1: 添加跨服务拒绝、成对接受、同 ID 继承和无问题节点清除测试**

把以下四个测试加入 `tests/test_routing_trees_admin.py`。`copy`、`create_tree()`、`save_tree()`、`graph_with_model()` 和 `_direct_graph()` 均已存在，不新增测试辅助。

```python
def test_update_jev_service_change_requires_matching_window(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]
    new_jev = copy.deepcopy(before["jev"])
    new_jev["id"] = "other-service"

    response = save_tree(client, {"jev": new_jev})

    assert response.status_code == 422, response.text
    issue = response.json()["detail"][0]
    assert issue["code"] == "graph_parse_error"
    assert "jev_window is required when changing JEV service id" in issue["zh"]
    assert client.get("/admin/trees/main").json()["tree"] == before


def test_update_jev_service_change_accepts_service_and_matching_window(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]
    new_jev = copy.deepcopy(before["jev"])
    new_jev["id"] = "other-service"
    new_window = {"input_tokens": 2048, "context_tokens": 8192}

    response = save_tree(client, {"jev": new_jev, "jev_window": new_window})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["jev"]["id"] == "other-service"
    assert response.json()["tree"]["jev_window"] == new_window


def test_update_same_jev_without_window_keeps_stored_window(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]
    changed = copy.deepcopy(before["jev"])
    changed["instructions"] = "只修改同一服务的提示词"

    response = save_tree(client, {"jev": changed})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["jev"]["instructions"] == "只修改同一服务的提示词"
    assert response.json()["tree"]["jev_window"] == before["jev_window"]


def test_clear_jev_without_questions_does_not_require_window(client):
    assert create_tree(client, tree_dict(root=_direct_graph("fast"))).status_code == 201

    response = save_tree(client, {"jev": None})

    assert response.status_code == 200, response.text
    assert response.json()["tree"]["jev"] is None
```

- [ ] **Step 2: 运行第一个测试，确认旧实现返回 200**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py::test_update_jev_service_change_requires_matching_window`
Expected: FAIL。当前实现返回 200，并把不同服务 ID 与旧 `jev_window` 一起保存。

- [ ] **Step 3: 在补丁合并边界增加服务变更守卫**

在 `GraphService._envelope()` 构造完 `payload` 后、调用 `tree_from_dict(payload)` 前插入以下完整逻辑。不要把守卫放进前端，也不要查询 JEV 数据库目录。

```python
        if patch.jev_window is UNSET and isinstance(patch.jev, dict):
            new_service_id = patch.jev.get("id")
            current_service_id = (
                current.runtime.jev.id if current.runtime.jev is not None else None)
            if isinstance(new_service_id, str) and new_service_id \
                    and new_service_id != current_service_id:
                raise ValidationFailed(graph_parse_failed(ValueError(
                    "jev_window is required when changing JEV service id")))
        try:
            return tree_from_dict(payload)
        except ValueError as exc:
            raise ValidationFailed(graph_parse_failed(exc)) from exc
```

这段守卫必须替换当前的 `try/except` 块，而不是在函数末尾再解析一次。它满足：

- `patch.jev is UNSET`：不会误触发；
- `patch.jev is None`：不会误触发，清除 JEV 仍由图校验决定；
- 非对象 JEV 或缺少 ID：仍由原解析器给出错误；
- 同 ID 更新：允许继承窗口；
- 不同 ID 且未显式给窗口：422，文件不变；
- 不同 ID 且窗口为 `null`：跳过守卫后由 `_window()` 返回 422，仍不会保存。

把 `GraphPatch` 第 72–76 行的旧注释替换为：

```python
    原始 API 的扁平 PATCH 中，若 ``jev`` 改为不同的非空服务 ID，必须在同一请求
    显式携带对应的 ``jev_window``；服务端拒绝继承旧服务窗口。相同服务 ID 的
    非窗口字段更新可以继承窗口，``jev: null`` 由图校验决定。console/inspector
    与目录确认同步始终把服务快照和窗口成对替换。
```

在 `README.zh-CN.md` 的 JEV 同源条目末尾补充：

```markdown
原始 `PUT /admin/trees/{id}` 补丁若改变 `jev.id`，必须同时携带新服务的 `jev_window`；否则返回 422，不继承旧服务窗口。
```

在 `README.md` 对应位置加入：

```markdown
When a raw `PUT /admin/trees/{id}` patch changes `jev.id`, it must also include the new service's `jev_window`; the request fails with 422 instead of inheriting the previous service's window.
```

- [ ] **Step 4: 运行本任务测试与树管理回归**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py -k 'jev_service_change or same_jev_without_window or clear_jev_without_questions'`
Expected: 4 个新测试全部 PASS。

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py tests/test_graph_runtime.py tests/test_graph_document.py`
Expected: 除 Task 2 明确要修改的两个旧保存行为测试外，其余测试 PASS；不得通过放宽 Task 1 的 422 断言处理失败。

- [ ] **Step 5: 提交本任务**

```bash
git add router/control/graphs.py tests/test_routing_trees_admin.py README.md README.zh-CN.md
git commit -m "fix: keep patched JEV service and window consistent"
```

### Task 2: 所有保存统一执行画布自身完整硬校验

**Files:**
- Modify: `router/control/graphs.py:29,96-135`
- Test: `tests/test_routing_trees_admin.py:328-376,561-574`

**Interfaces:**
- Consumes: 现有 `tree_issues(conn, tree)`，其内部依次执行 `validate_graph_structure()` 和模型、推理、时段、JEV 启用级校验。
- Produces: `GraphService.create()` 与 `GraphService.update()` 在启用和未启用状态下都对同一套画布自身问题返回 422；数据库目录差异仍不参与其中。

- [ ] **Step 1: 把旧保存行为测试改为统一硬校验，并增加 POST 缺 JEV 回归**

将 `test_closed_save_skips_enable_validation_but_enabled_save_uses_it` 整体替换为：

```python
def test_save_uses_full_canvas_validation_in_every_lifecycle(client, app):
    configure_jev(client)
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {"root": graph_with_model("ghost")})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"] == before

    root = graph_with_model()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []
    response = save_tree(client, {"root": root})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "target_models_required"
               for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"] == before

    assert save_tree(client, {"root": graph_with_model()}).status_code == 200
    assert client.post("/admin/trees/main/enable").status_code == 200
    published = app.state.snapshot.snapshot_revision

    response = save_tree(client, {"root": graph_with_model("ghost")})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published

    root = graph_with_model()
    for node in root["nodes"]:
        if node["type"] == "target":
            node["models"] = []
    response = save_tree(client, {"root": root})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "target_models_required"
               for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published

    root = graph_with_model()
    root["nodes"] = [node for node in root["nodes"] if node["type"] != "start"]
    response = save_tree(client, {"root": root})
    assert response.status_code == 422, response.text
    assert any(issue["code"] == "start_count" for issue in response.json()["detail"])
    assert app.state.snapshot.snapshot_revision == published
```

将 `test_update_explicit_jev_null_is_saved_and_only_enable_rejects_it` 整体替换为：

```python
def test_update_explicit_jev_null_is_rejected_for_question_graph(client):
    assert create_tree(client, tree_dict(root=graph_with_model())).status_code == 201
    before = client.get("/admin/trees/main").json()["tree"]

    response = save_tree(client, {"jev": None})

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "decision_service_unavailable"
               for issue in response.json()["detail"])
    assert client.get("/admin/trees/main").json()["tree"] == before
```

新增 POST 场景，直接覆盖审核时返回 201 的复现路径：

```python
def test_create_question_graph_without_jev_is_rejected(client):
    raw = tree_dict(root=graph_with_model())
    raw["jev"] = None

    response = create_tree(client, raw)

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "decision_service_unavailable"
               for issue in response.json()["detail"])
    assert client.get("/admin/trees").json()["trees"] == []
```

- [ ] **Step 2: 运行三个测试，确认旧保存行为逐项失败**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py -k 'full_canvas_validation_in_every_lifecycle or explicit_jev_null_is_rejected_for_question_graph or create_question_graph_without_jev_is_rejected'`
Expected: 三个测试都 FAIL。当前未启用保存分别返回 200、200、201。

- [ ] **Step 3: 统一 create/update 的校验入口**

把 `GraphService.create()` 的校验改成：

```python
    def create(self, tree: RoutingTree) -> RoutingTree:
        issues = tree_issues(self.conn, tree)
        if issues:
            raise ValidationFailed(issues)
        with self.publisher.locked():
            try:
                created = self.trees.create(tree)
            except GraphAlreadyExistsError:
                raise AlreadyExists(f"tree {tree.id} already exists") from None
            self._audit("create", {"id": tree.id})
        return created
```

把 `GraphService.update()` 中当前的启用状态分档：

```python
            if current.enabled:
                issues = tree_issues(self.conn, tree)
            else:
                issues = validate_graph_structure(tree)
```

替换为单行：

```python
            issues = tree_issues(self.conn, tree)
```

最后从 `router/routing/graph/validate.py` 的 import 中移除已不再使用的 `validate_graph_structure`，保留 `Issue`：

```python
from router.routing.graph.validate import Issue
```

不要删除 `validate_graph_structure()` 本身；API 试算和其他模块仍在使用它。不要修改 `tree_issues()`，它已经包含结构校验。

- [ ] **Step 4: 运行本任务及保存相关回归**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py -k 'full_canvas_validation_in_every_lifecycle or explicit_jev_null_is_rejected_for_question_graph or create_question_graph_without_jev_is_rejected or clear_jev_without_questions'`
Expected: 4 个相关测试全部 PASS；无问题节点清除 JEV 的合法路径不受影响。

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py tests/test_graph_try_api.py tests/test_catalog_impacts.py`
Expected: 全部 PASS。特别是 `test_catalog_impacts.py` 中自包含 ghost 画布是合法引用，不应被改写成绕过校验的仓储直写。

- [ ] **Step 5: 提交本任务**

```bash
git add router/control/graphs.py tests/test_routing_trees_admin.py
git commit -m "fix: validate every saved canvas completely"
```

## Final Verification

- [ ] **Step 1: 运行完整标准测试**

```bash
.venv/bin/python -m pytest -q
npm run check:console
```

Expected: Python 全部通过，至少包含原 1070 项和本计划新增的 5 项测试；控制台单元、图契约和 190 项浏览器 QA 全部通过。

- [ ] **Step 2: 核对两个原始复现现在均被阻断**

确认测试套件中存在并通过：

```bash
.venv/bin/python -m pytest -q \
  tests/test_routing_trees_admin.py::test_update_jev_service_change_requires_matching_window \
  tests/test_routing_trees_admin.py::test_create_question_graph_without_jev_is_rejected
```

Expected: 两个测试 PASS。第一个证明跨服务窗口混用返回 422 且文件不变；第二个证明含问题节点但 `jev: null` 的画布在保存时即返回 422，而不是等到启用。

- [ ] **Step 3: 检查交付范围和测试产物**

```bash
git diff --check
git status --short
```

Expected: 无空白错误。只允许出现本计划两个任务及文档的计划内修改；删除测试生成的临时数据库、日志或截图，不删除用户已有文件。执行者完成每项修复后应再次进行代码审核，重点确认没有新增数据库回退或放宽旧测试。
