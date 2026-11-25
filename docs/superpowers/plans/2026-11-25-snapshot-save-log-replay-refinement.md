# RoutePilot 快照、保存与日志回溯增量修订 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重写现有路由内核的前提下，收敛为“保存/启用”两个核心版本操作，并证明当前内存路由集、日志图快照去重和纯日志回溯都满足设计。

**Architecture:** 保留现有 `RoutingSnapshotSet` 原子替换、`graph_snapshots` 内容寻址和 `request_snapshots` 请求引用。此次只补回归证据、调整控制台版本操作与文档；历史图和 trace 始终从日志存储读取，不读取当前图文件。

**Tech Stack:** Python 3.11、FastAPI、SQLite、pytest、原生 ES Modules、Node test runner、Playwright。

## Global Constraints

- 只修改 `main`，最终只执行 `git push origin main`；不创建功能分支，不推送 GitHub。
- 本计划是独立增量计划，设计依据为 `docs/superpowers/specs/2026-11-24-department-graph-standalone-design.md` 第 5 节。
- 不重写 `RoutingSnapshotSet`、`GraphService`、`ConfigurationService` 或请求代理链路；现有保存/启用后端语义已经符合要求。
- 内存只保留当前路由集；保存/启停本身不写日志快照；只有请求记录才捕获图和 trace。
- 持久化图快照继续按 canonical 内容 SHA-256 去重，不改成每次保存、启用或请求都复制完整图。
- 历史回溯只依赖 `request_snapshots + graph_snapshots + request_logs`；内存 ring/pending 只是缓存，当前图文件和当前路由集不得成为历史回放数据源。
- 不新增自动删除、日志保留期、数据库 schema、API 端点或依赖，不涉及 APISIX/163/newapi/token-factory。
- 用户侧核心版本词汇只有“保存/Save”和“启用/Enable”；停用/设默认仍是生命周期控制。内部函数名 `SnapshotPublisher.publish()` 可以保留，但不得把“发布”作为额外用户动作。
- 不删除或清理当前工作区已有未跟踪文件，不提交无关产物。

## Current-State Audit

以下能力已存在，本计划只验证，不重复实现：

- `GraphService.update()`：启用图保存立即替换当前路由集，草稿保存不影响运行。
- `GraphService.enable()`：启用最新保存文档；控制台启用脏草稿时先保存。
- `SnapshotPublisher.publish()`：只替换 `app.state.snapshot_set`，不创建持久化发布历史。
- `request_snapshots.py`：canonical 图内容 SHA-256；`enabled/default/tree_revision/updated_at/document_sha256` 不拆分图去重。
- `request_logging.py`：`graph_snapshots` 使用 `INSERT OR IGNORE`，每个请求单独保存 trace 与图引用。
- `routes_logs.py`：快照端点先查内存，miss 后读取 SQLite，不读取当前图文件。
- `tests/test_requestlog.py`：已有同内容图去重、生命周期字段不拆分去重测试。
- `tests/test_admin.py`：已有进程重启后的 SQLite 回放测试。

真正需要修改的是：当前图变化后的回归测试、控制台按钮与文案、对应 QA 和 README。

---

### Task 1: 锁定内容去重与纯日志回溯边界

**Files:**
- Test: `tests/test_admin.py`
- Test: `tests/test_requestlog.py`
- Modify only if a regression is proven: `router/api/routes_logs.py`
- Modify only if a regression is proven: `router/request_snapshots.py`
- Modify only if a regression is proven: `router/request_logging.py`

**Interfaces:**
- Consumes: 既有 `/admin/requests/{request_id}/snapshot`、`RoutingTreeRepo.get/update()`、`RequestLogger.flush()`。
- Produces: 一条可证明“当前图变化不污染历史回放”的持久化回归测试；不改变任何生产接口。

- [ ] **Step 1: 先运行现有快照测试，确认基线**

Run:
```bash
.venv/bin/python -m pytest -q \
  tests/test_requestlog.py \
  tests/test_admin.py::test_request_recent_links_snapshot_and_endpoint_serves_tree_and_trace
```
Expected: PASS。现有测试已经证明同内容图只存一行、请求 trace 分行和重启后回放。若基线失败，停止并报告实际错误，不在本计划里夹带无关修复。

- [ ] **Step 2: 增加“当前图后来变化”的回归断言**

在 `tests/test_admin.py::test_request_recent_links_snapshot_and_endpoint_serves_tree_and_trace` 第一个应用退出之后、创建 `restarted` 之前，仅改变当前图文件：

```python
current = repo.get('main')
repo.update(
    replace(current, name='后来的当前图'),
    base_document_sha256=current.document_sha256,
)
```

在重启后已有 `body = response.json()` 断言区域追加：

```python
assert body['tree']['name'] == '测试图'
assert body['tree']['name'] != '后来的当前图'
assert body['trace'] == trace
```

该断言使用文件中已有的 `replace` 导入，不新增依赖。不得通过修改快照端点去适应当前图；历史响应必须保持首次请求时的内容。

- [ ] **Step 3: 运行增强测试并确认它是有效回归证据**

Run:
```bash
.venv/bin/python -m pytest -q \
  tests/test_admin.py::test_request_recent_links_snapshot_and_endpoint_serves_tree_and_trace
```
Expected: PASS。当前实现应从日志数据库回放旧图。此测试是对既有正确行为的 characterization，不要求生产代码先失败。

做一次局部变异验证：仅临时把断言改为 `body['tree']['name'] == '后来的当前图'`，运行同一测试必须 FAIL；立即恢复为上面的正确断言并再次运行必须 PASS。不要把变异代码提交进去。

- [ ] **Step 4: 提交测试证据**

```bash
git add tests/test_admin.py
git commit -m "test: isolate historical replay from the current graph"
```

---

### Task 2: 控制台收敛为保存与启用

**Files:**
- Modify: `router/console/routing/app.js`
- Modify: `router/console/routing.html`
- Modify: `router/console/routing/controller.js`
- Modify: `router/console/routing/tryrun.js`
- Modify: `router/console/i18n.js`
- Test: `scripts/console_qa/canvas.mjs`
- Test: `scripts/console_qa/inspector.mjs`
- Test: `scripts/console_draft_test.mjs`

**Interfaces:**
- Consumes: 既有 `controller.save()`、`controller.enable()`、`cvEnableLabel`、`#cvSave`。
- Produces: 保存按钮键 `cv.save`；启用按钮只使用 `cv.enable/cv.published/cv.saving`；不再使用 `cv.publish/cv.publishUpdate/cv.saveDraft`。

- [ ] **Step 1: 先调整 QA 断言，形成失败测试**

在 `scripts/console_qa/canvas.mjs` 和 `inspector.mjs` 把主按钮状态矩阵改为：

| 当前图状态 | 主按钮 | 是否禁用 | 独立保存按钮 |
|---|---|---:|---|
| 新草稿/未启用 | `cv.enable` | 否 | `cv.save` |
| 已启用且有未保存修改 | `cv.published` | 是 | `cv.save` |
| 已启用且干净 | `cv.published` | 是 | `cv.save` |
| 保存或试算在途 | `cv.saving` 或既有 busy 文案 | 是 | 禁用 |

增加真实浏览器断言：

```javascript
assert.equal(await page.locator('#cvSave').textContent(), t('cv.save'));
assert.equal(await page.locator('#cvEnableLabel').textContent(), t('cv.enable'));
assert.equal(await page.locator('#cvEnable').isDisabled(), false);
```

启用图编辑后追加：

```javascript
assert.equal(await page.locator('#cvEnableLabel').textContent(), t('cv.published'));
assert.equal(await page.locator('#cvEnable').isDisabled(), true);
await page.locator('#cvSave').click();
assert.equal(await page.locator('#cvLifecycle').getAttribute('data-state'), 'running');
```

再验证未启用脏图点击启用仍先保存后启用，保存失败不发送 `/enable`；停用与设默认行为不变。

- [ ] **Step 2: 运行定向 QA，确认旧交互失败**

Run:
```bash
QA_ONLY='lifecycle|save|enable|inspector' node scripts/console_qa/canvas.mjs
QA_ONLY='lifecycle|save|enable|inspector' node scripts/console_qa/inspector.mjs
```
Expected: FAIL；旧实现仍出现 `cv.publish/cv.publishUpdate/cv.saveDraft`，启用图脏状态主按钮仍可直接保存。

- [ ] **Step 3: 修改主按钮和保存按钮**

在 `router/console/routing/app.js::paintLifecycle()` 使用：

```javascript
const primaryKey = store.saving
  ? 'cv.saving'
  : enabled
    ? 'cv.published'
    : 'cv.enable';
```

主按钮禁用条件改为：

```javascript
primary.disabled = !!store.tryRunning || !!store.saving || enabled;
```

保存按钮仍调用既有 `controller.save()`；启用按钮仍调用既有 `controller.enable()`。不要复制保存逻辑，也不要删除“启用前保存脏草稿”的现有编排。

`routing.html` 的 `#cvSave span` 改为 `data-i18n="cv.save"`；`app.js` 保存忙碌重绘恢复时同样使用 `t('cv.save')`。

- [ ] **Step 4: 更新双语词汇和注释**

`router/console/i18n.js`：

```javascript
'cv.save': '保存',
'cv.status.unpublished': '有未保存修改',
'cv.unpublished': '尚未启用',
'cv.tryScopeRouteSet': '当前路由集',
```

英文分别使用：

```javascript
'cv.save': 'Save',
'cv.status.unpublished': 'Unsaved changes',
'cv.unpublished': 'Not enabled',
'cv.tryScopeRouteSet': 'Active route set',
```

删除已无引用的 `cv.saveDraft`、`cv.publish`、`cv.publishUpdate`。保留 `cv.published` 作为“已启用/Enabled”状态键，保留内部 `unpublished` 状态名以避免无关 CSS/测试重构。

更新 `app.js`、`controller.js`、`tryrun.js` 中仍描述“保存并启用/保存并更新”的注释。内部“发布路由集”可改为“当前生效路由集”，但不要重命名后端 `publish()` 方法。

- [ ] **Step 5: 运行定向测试并提交**

Run:
```bash
QA_ONLY='lifecycle|save|enable|inspector' node scripts/console_qa/canvas.mjs
QA_ONLY='lifecycle|save|enable|inspector' node scripts/console_qa/inspector.mjs
node --test scripts/console_draft_test.mjs
.venv/bin/python scripts/i18n_dead_keys.py
```
Expected: PASS；保存/启用词汇一致、保存失败不启用、停用/设默认不变、无 i18n 死键。

```bash
git add router/console/routing/app.js router/console/routing.html \
  router/console/routing/controller.js router/console/routing/tryrun.js \
  router/console/i18n.js scripts/console_qa/canvas.mjs \
  scripts/console_qa/inspector.mjs scripts/console_draft_test.mjs
git commit -m "feat: simplify graph actions to save and enable"
```

---

### Task 3: 同步文档并执行完整验收

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/决策图规范.md`
- Modify: `docs/后端接口说明.md`
- Modify only if needed: `docs/superpowers/specs/2026-11-24-department-graph-standalone-design.md`

**Interfaces:**
- Consumes: Task 1 的回溯证据和 Task 2 的最终控制台行为。
- Produces: 与实现一致的用户文档和完整测试证据。

- [ ] **Step 1: 更新用户文档**

README 双语至少修改以下口径，不只改单个按钮名：

- 控制台核心版本操作为“保存/Save”和“启用/Enable”。
- 启用图保存立即对后续请求生效；草稿保存不影响运行；启用脏图时先保存。
- 停用/设默认是独立生命周期操作，没有“发布”或“保存并更新”用户概念。
- 当前内存只保留当前路由集，替换不是持久化历史。
- 请求发生时捕获完整 canonical 图和 trace；图按内容 SHA-256 去重，请求各自保留引用。
- 当前图改变或进程重启后，回放仍读取日志中的旧图和旧 trace。
- 快照写入仍是 best-effort；失败时显示不可回放，不伪造历史。
- 本阶段没有自动删除或保留期策略，不声称日志容量有上限。

`docs/决策图规范.md` 和 `docs/后端接口说明.md` 只需同步本次语义；不得借机加入第二阶段 APISIX 内容。

- [ ] **Step 2: 检查旧词汇和文档格式**

Run:
```bash
git grep -n -E '保存并启用|保存并更新|Save and enable|Save and update|Save draft' \
  -- README.md README.zh-CN.md docs/决策图规范.md docs/后端接口说明.md \
     router/console scripts
git diff --check
```
Expected: 当前用户文档、控制台和 QA 不再出现这些旧动作名称；无空白错误。技术注释中的 `SnapshotPublisher.publish()` 允许保留。

- [ ] **Step 3: 运行完整标准门禁**

Run:
```bash
.venv/bin/python -m pytest -q
npm run check:console
.venv/bin/python scripts/i18n_dead_keys.py
```
Expected: 全部 PASS，无失败，无新增跳过，无 i18n 死键。不得通过排除旧 QA、删除断言或降低测试覆盖率来通过。

- [ ] **Step 4: 核对实际快照持久化结果**

在项目根目录执行以下完整脚本。它使用现有测试 fixture、真实 `RequestLogger.flush()` 和临时 SQLite，不依赖目测：

```bash
.venv/bin/python - <<'PY'
import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from router.request_logging import RequestLogger
from router.routing.tree import tree_from_dict
from tests.routing_fixtures import tree_dict
from tests.test_requestlog import record

with tempfile.TemporaryDirectory(prefix='routepilot-log-dedup-') as directory:
    db = Path(directory) / 'router.db'
    log = RequestLogger(str(db))
    conn = sqlite3.connect(db)
    from router.store.db import migrate
    migrate(conn)
    conn.close()
    snapshot = SimpleNamespace(
        tree=tree_from_dict(tree_dict()), snapshot_revision='s' * 64)
    log.enqueue(record(1), snapshot=snapshot)
    log.enqueue(record(2), snapshot=snapshot)
    asyncio.run(log.flush())
    conn = sqlite3.connect(db)
    graphs = conn.execute('SELECT COUNT(*) FROM graph_snapshots').fetchone()[0]
    requests = conn.execute('SELECT COUNT(*) FROM request_snapshots').fetchone()[0]
    references = conn.execute(
        'SELECT COUNT(DISTINCT graph_snapshot_id) FROM request_snapshots'
    ).fetchone()[0]
    conn.close()
    assert (graphs, requests, references) == (1, 2, 1)
    print({'graph_snapshots': graphs, 'request_snapshots': requests,
           'distinct_graph_references': references})
PY
```

Expected: `{'graph_snapshots': 1, 'request_snapshots': 2, 'distinct_graph_references': 1}`。`TemporaryDirectory` 自动清理数据库。

Task 1 已覆盖“修改当前图并重启后仍回放旧图和旧 trace”，这里不重复制造第二个环境。不得保留测试日志、截图或其他产物；用户已有未跟踪文件保持不动。

- [ ] **Step 5: 提交并只推送 Gitee main**

```bash
git add README.md README.zh-CN.md
git add -f docs/决策图规范.md docs/后端接口说明.md \
  docs/superpowers/specs/2026-11-24-department-graph-standalone-design.md
git diff --cached --check
git commit -m "docs: clarify snapshot deduplication and log replay"
git push origin main
```

Run: `git status --short --branch`
Expected: `main...origin/main` 不 ahead/behind；没有本任务产生的临时文件，现有无关未跟踪文件保持原样。

## Completion Checklist

- [ ] 已证明当前图变化和进程重启不会污染历史图与 trace。
- [ ] 已证明同内容多请求只产生一个图快照行，请求 trace 分别保留。
- [ ] 控制台只有“保存/启用”核心版本操作，停用/设默认不变。
- [ ] 启用图保存立即生效、草稿保存不影响运行、启用脏图先保存。
- [ ] 内存不保留发布历史，保存/启停不写日志快照。
- [ ] 没有新增 schema、API、依赖、自动删除或第二阶段集成内容。
- [ ] README、图规范、接口说明、代码注释和 QA 使用同一语义。
- [ ] pytest、Node/Playwright、i18n、`git diff --check` 全部通过。
- [ ] 所有提交只进入 Gitee `main`，工作区没有新增临时产物。
