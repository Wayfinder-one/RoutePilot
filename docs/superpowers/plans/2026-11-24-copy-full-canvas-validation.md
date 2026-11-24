# 复制画布完整硬校验修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `GraphService.copy()` 与新建、更新一样，在写入副本前执行画布自身完整硬校验，阻止无效历史画布继续扩散。

**Architecture:** 只修改 `GraphService.copy()` 的控制流：在现有单写锁内读取源画布、调用 `tree_issues()`，校验通过后才调用 `RoutingTreeRepo.copy()`。不改变副本内容、文件格式、审计结构或正常复制路径，不引入新模块。

**Tech Stack:** Python 3.11+、FastAPI、pytest、现有 `GraphService` / `tree_issues()` / `RoutingTreeRepo`。

## Global Constraints

- 目标分支：`feat/strict-self-contained-canvas`，审核基线：`c0975544ddb695d84ab7b01240bcb5886a1e4318`。
- 只修复 `copy()` 绕过完整校验的问题，不重复修改已完成的 JEV/window 同源和 create/update 校验。
- 校验必须发生在 `publisher.locked()` 内，与后续 `trees.copy()` 使用同一源快照，避免读取后源画布变化。
- 源画布不存在时仍返回 404；校验失败返回 422 Issue 数组，不创建目标文件、不写审计、不改变源文件或内存快照。
- 测试通过仓储直接构造一份历史无效画布，模拟修复前允许保存的数据；不得为了测试而在公开 API 中保留无效写入路径。
- 正常复制仍保留完整运行快照，`enabled=false`，不发布新运行快照。
- 本计划不包含业务实现；执行者按 TDD 顺序完成一次小步提交。

---

## File Structure

- `router/control/graphs.py`：在 `copy()` 中补充源画布存在性检查与 `tree_issues()` 校验，并修正顶部“源文档此前已通过完整校验”的错误假设。
- `tests/test_routing_trees_admin.py`：新增无效源画布复制回归；复用现有 `client/app` fixture、`tree_dict()`、`graph_with_model()` 和 `_canvas_state()`。

### Task 1: 复制前执行完整画布校验

**Files:**
- Modify: `router/control/graphs.py:1-6,135-143`
- Test: `tests/test_routing_trees_admin.py`

**Interfaces:**
- Consumes: `GraphService.copy(tree_id, new_id, new_name)`、`tree_issues(conn, tree)`、`ValidationFailed`、`MissingError`。
- Produces: 无效源画布复制返回 422 且零副作用；合法源画布复制行为保持不变。

- [ ] **Step 1: 添加无效源画布复制失败测试**

把以下测试加入 `tests/test_routing_trees_admin.py`，并在该文件导入区新增 `from router.routing.tree import tree_from_dict`。`connect` 和 `_canvas_state()` 直接复用现有定义。

```python
def test_copy_rejects_invalid_source_without_file_or_audit(client, app):
    raw = tree_dict("invalid-source")
    raw["root"] = graph_with_model("not-in-snapshot")
    # 模拟修复前允许落盘的历史数据：target 模型不在该画布运行目录里。
    # 仓储只用于播种，公开 create/update 仍必须拒绝这类新写入。
    app.state.graph_repo.create(tree_from_dict(raw))
    source_before = _canvas_state(app, ("invalid-source",))
    target = app.state.graph_repo.graphs / "invalid-copy.json"
    assert not target.exists()
    conn = connect(app.state.settings.db_path)
    audit_before = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    conn.close()

    response = client.post("/admin/trees/invalid-source/copy", json={
        "id": "invalid-copy", "name": "无效副本",
    })

    assert response.status_code == 422, response.text
    assert any(issue["code"] == "unknown_model" for issue in response.json()["detail"])
    assert not target.exists()
    assert _canvas_state(app, ("invalid-source",)) == source_before
    conn = connect(app.state.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == audit_before
    conn.close()
```

该测试使用 `graph_with_model()` 生成与默认 `GRAPH_CANVAS` 相同的 `q` / `safe` 节点，因此不会出现无关的 canvas 引用错误；真正触发的是 target 模型不在 `runtime.backends.models` 中的 `unknown_model`。

- [ ] **Step 2: 运行测试并确认当前实现返回 201**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py::test_copy_rejects_invalid_source_without_file_or_audit`
Expected: FAIL。当前 `copy()` 返回 201，并创建 `invalid-copy.json`。

- [ ] **Step 3: 在写锁内校验源画布后再复制**

将 `GraphService.copy()` 整体替换为以下实现：

```python
    def copy(self, tree_id: str, new_id: str, new_name: str) -> RoutingTree:
        with self.publisher.locked():
            source = self.trees.get(tree_id)
            if source is None:
                raise MissingError(f"unknown tree {tree_id}")
            issues = tree_issues(self.conn, source)
            if issues:
                raise ValidationFailed(issues)
            try:
                copied = self.trees.copy(tree_id, new_id, new_name)
            except KeyError:
                raise MissingError(f"unknown tree {tree_id}") from None
            except GraphAlreadyExistsError:
                raise AlreadyExists(f"tree {new_id} already exists") from None
            self._audit("copy", {"source": tree_id, "id": new_id})
        return copied
```

把文件顶部第 4 行注释改为：

```python
- `copy`：锁内读取源画布并做同一套完整校验（`tree_issues`）→（锁内）文档写入 → 审计；不发布快照（副本为关闭态）。
```

不要调用 `validate_graph_structure()` 替代 `tree_issues()`，否则仍会漏掉 `unknown_model`、`decision_service_unavailable`、推理档位和时段问题。不要在 `self._audit()` 后补第二次校验；拒绝必须发生在目标文件和审计写入之前。

- [ ] **Step 4: 运行新增测试和复制/保存回归**

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py::test_copy_rejects_invalid_source_without_file_or_audit`
Expected: PASS。响应为 422，目标文件不存在，源文件和审计计数不变。

Run: `.venv/bin/python -m pytest -q tests/test_routing_trees_admin.py -k 'copy or full_canvas_validation or jev_service_change'`
Expected: 全部 PASS，包括既有正常复制往返、完整保存校验和 JEV/window 同源测试。

- [ ] **Step 5: 提交本任务**

```bash
git add router/control/graphs.py tests/test_routing_trees_admin.py
git commit -m "fix: validate source canvas before copying"
```

## Final Verification

- [ ] **Step 1: 运行完整标准回归**

```bash
.venv/bin/python -m pytest -q
npm run check:console
```

Expected: Python 全部通过，至少包含原 1075 项和新增的无效复制测试；控制台单元、图契约和 190 项浏览器 QA 全部通过。

- [ ] **Step 2: 核对实现范围与交付物**

```bash
git diff --check
git status --short
```

Expected: 无空白错误。只允许 `router/control/graphs.py` 和 `tests/test_routing_trees_admin.py` 的实现性修改，以及计划内测试产物清理；不得出现放宽旧测试、数据库回退或额外业务改动。审核时重点确认 `create()`、`update()`、`copy()` 三条保存路径现在都调用 `tree_issues()`。
