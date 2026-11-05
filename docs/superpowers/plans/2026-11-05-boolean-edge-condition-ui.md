# 布尔边条件 UI 简化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 开始节点的默认出边改为只读“总是通过”，所有布尔条件编辑去掉 true/false 下拉与条件文案，非开始节点用“取反”开关保留现有 `is:false` 能力。

**Architecture:** `RTCondPop` 提供唯一的开始节点判定、可编辑性和条件文案函数；卡片、检查器、删除确认与试算回放都调用它。存储、规范化和引擎求值继续使用 `null` / `{op:'is', value:false}`，只替换展示和编辑控件。

**Tech Stack:** 原生 JavaScript、现有 `RTGraph/RTCondPop/RTCards`、Playwright `scripts/console_qa.mjs`、Node test、Python i18n lint；不新增依赖。

**Source behavior:** `router/console/routing/condpop.js`, `cards.js`, `inspector.js`, `tryrun.js`, `app.js` 与 `scripts/console_qa.mjs` 的现有契约。

## Assumptions

- “去掉 true/false”指移除条件编辑器中的期望值下拉，以及条件 chip、检查器、删除确认、回放条件说明和相关 toast 中的 `= true / = false` 文案。
- 非开始节点仍需表达 `is:false`；统一改成“取反”开关，而不是删除该能力。开关关闭写 `null`，开启写 `{op:'is', value:false}`。
- 回放中的原始布尔值、执行状态、代码里的 boolean 数据和引擎测试不属于文案移除范围，不重写为其他类型或含义。
- 开始节点只有默认 `condition:null` 才显示“总是通过”。历史非空条件不静默迁移：`is:false` 显示“取反”，并保留开关让用户显式清除为 `null`。

## Alternatives Considered

1. **采用：开始节点只读，其他布尔源用取反开关。** 满足去掉 true/false 的要求，同时保留现有图能力和历史数据编辑路径。
2. **删除所有布尔条件编辑器。** 实现更少，但会丧失 `is:false` 的新建和清除能力，拒绝。
3. **只隐藏开始节点下拉，保留其他节点 true/false。** 不满足本次额外提出的移除要求，拒绝。

## Global Constraints

- 不修改 `router/routing/`、`router/jev/`、`router/console/routing/graph.js::evaluateCondition`、存储 schema 或引擎语义。
- `condition:null` 继续表示默认通过；`{op:'is', value:false}` 继续表示取反；提交 `{op:'is', value:true}` 仍按现有规范化写为 `null`。
- 数值边的六个算子、阈值校验、取消/Esc、保存锁、history/undo 和焦点恢复行为不得改变。
- 新建布尔边仍不自动打开条件浮层；开始节点默认边不渲染可点击的 `data-cond-edge`，直接调用 `RTCondPop.open()` 也必须拒绝。
- 历史非空开始边不得被显示为“总是通过”，不得在加载时自动改写；只允许用户显式提交清除。
- 文案必须中英文成对；`cv.condBool/cv.condTrue/cv.condFalse` 删除后，`scripts/i18n_dead_keys.py` 必须保持 0 dead keys。
- 不修改无关页面和已有未跟踪的 `configs/cases/`；仓库忽略 `scripts/` 与 `docs/` 时，只对计划列出的文件使用 `git add -f`。

## File Structure

- Modify `router/console/routing/condpop.js`：共享条件视图、开始节点保护、取反表单与提交。
- Modify `router/console/i18n.js`：中英文“直接通过 / 取反 / 总是通过”及提示文案。
- Modify `router/console/routing/cards.js`：开始节点静态条件文本和其他条件 chip。
- Modify `router/console/routing/inspector.js`：复用共享文案、隐藏默认开始边的编辑/保存入口。
- Modify `router/console/routing/app.js`：删除确认使用共享条件文案。
- Modify `router/console/routing/tryrun.js`：回放条件说明复用共享文案，原始值与状态保持不变。
- Modify `router/console/routing/graph.js`：只更新布尔条件校验提示，不改求值。
- Modify `router/console/routing.css`：静态条件文本不可点击的视觉状态。
- Modify `scripts/console_qa.mjs`：双入口取反、开始节点只读、历史清除和回放文案 QA。

## Shared Interfaces

Task 1 在 `window.RTCondPop` 上新增并导出：

```javascript
RTCondPop.isStartEdge(edge) => boolean
RTCondPop.canEditCondition(edge) => boolean
RTCondPop.conditionLabel(condition, { start: false }) => string
RTCondPop.conditionText(edge) => string
```

语义固定为：

- `isStartEdge()` 通过 `RTGraph.findNode()` 判断 `edge.source` 节点是否为 `type === 'start'`。
- `canEditCondition()` 对数值边和其他布尔源返回 true；开始节点仅在 `edge.condition != null` 时返回 true，用于清除历史配置。
- `conditionLabel()` 对默认布尔值返回 `cv.cond.direct` 或 `cv.cond.startAlways`，对 `is:false` 返回 `cv.cond.negated`；数值条件继续使用 `RTGraph.OP` 和阈值。
- `conditionText()` 对布尔边调用 `conditionLabel(edge.condition, { start: RTCondPop.isStartEdge(edge) })`；数值边缺条件时保留 `cv.cond.unset`，不得误显示为直接通过。

---

### Task 1: 共享条件视图与取反表单

**Files:**
- Modify: `router/console/routing/condpop.js:44-155,263-280,299-375`
- Modify: `router/console/i18n.js:392-440,860-908`
- Test: `scripts/console_qa.mjs:1671-1732`

**Interfaces:**
- Produces: `isStartEdge()`、`canEditCondition()`、`conditionLabel()`、`conditionText()`，以及 `data-cond-field="negate"` 复选框。
- Consumes: 现有 `booleanValueOf()`、`normaliseCondition()`、`validateAndCommit()`，后两者保持现有规范化与提交语义。

- [ ] **Step 1: 将 Task 11 改为取反开关的失败测试**

用以下测试替换现有 `Task 11 · boolean conditions toggle = true / = false ...`。该测试暂不断言卡片 chip 文案，卡片在 Task 2 接入共享函数：

```javascript
test('Task 11 · boolean conditions use a negation switch from both entries', async () => {
  await openCanvas();
  const edgeId = 'e-selected-easy-light';
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="bool"]').count(), 0);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').count(), 1);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').isChecked(), false);

  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.locator('#cvCondPop [data-act="cond-cancel"]').click();
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  assert.equal((await edgeOf(edgeId)).condition, null);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: false });

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  assert.equal((await edgeOf(edgeId)).condition, null);

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });
  assert.equal(await page.evaluate(id => {
    const edge = window.App.graph.edges.find(item => item.id === id);
    return window.RTCondPop.conditionText(edge);
  }, edgeId), await page.evaluate(() => window.t('cv.cond.negated')));

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').isChecked(), true);
  await page.locator('#cvCondPop [data-cond-field="negate"]').uncheck();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(edgeId, null);
  assert.equal(await page.evaluate(id => {
    const edge = window.App.graph.edges.find(item => item.id === id);
    return window.RTCondPop.conditionText(edge);
  }, edgeId), await page.evaluate(() => window.t('cv.cond.direct')));

  await openCanvas();
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  await page.locator('#cvInspectorBody [data-cond-field="negate"]').check();
  await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, null);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });

  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  const startView = await page.evaluate(id => {
    const edge = window.App.graph.edges.find(item => item.id === id);
    return {
      canEdit: window.RTCondPop.canEditCondition(edge),
      label: window.RTCondPop.conditionText(edge),
      opened: window.RTCondPop.open(id)
    };
  }, startEdge.id);
  assert.equal(startView.canEdit, false);
  assert.equal(startView.label, await page.evaluate(() => window.t('cv.cond.startAlways')));
  assert.equal(startView.opened, false);
});
```

- [ ] **Step 2: 运行 QA 并确认旧实现失败**

Run: `node scripts/console_qa.mjs`
Expected: FAIL；找不到 `[data-cond-field="negate"]`，且 `RTCondPop.conditionText/canEditCondition` 尚不存在。

- [ ] **Step 3: 实现共享视图和取反表单**

在 `condpop.js` 的 `edgeType()` 后加入以下函数：

```javascript
function isStartEdge(edge) {
  const graph = root.App && root.App.graph;
  const source = edge && root.RTGraph.findNode(graph, edge.source);
  return !!(source && source.type === 'start');
}

function canEditCondition(edge) {
  if (!edge) return false;
  if (edgeType(edge) === 'number') return true;
  return !isStartEdge(edge) || edge.condition != null;
}

function conditionLabel(condition, { start = false } = {}) {
  if (condition == null) return t(start ? 'cv.cond.startAlways' : 'cv.cond.direct');
  if (condition.op === 'is' && typeof condition.value === 'boolean') {
    if (condition.value === false) return t('cv.cond.negated');
    return t(start ? 'cv.cond.startAlways' : 'cv.cond.direct');
  }
  const symbol = root.RTGraph.OP[condition.op];
  if (symbol && Number.isFinite(Number(condition.value))) {
    return symbol + ' ' + Number(condition.value);
  }
  return t('cv.cond.invalid');
}

function conditionText(edge) {
  if (!edge) return '';
  if (edgeType(edge) === 'number') {
    if (!edge.condition) return t('cv.cond.unset');
    return conditionLabel(edge.condition, { start: false });
  }
  return conditionLabel(edge.condition, { start: isStartEdge(edge) });
}
```

将 `formHTML()` 完整替换为以下版本，数值分支保持原校验默认值：

```javascript
function formHTML(edge) {
  if (!edge) return '';
  const type = edgeType(edge);
  const condition = edge.condition;
  if (type === 'number') {
    const validOp = condition && root.RTGraph.NUM_OPS.indexOf(condition.op) >= 0 ? condition.op : 'gt';
    const value = condition && Number.isFinite(Number(condition.value)) ? String(Number(condition.value)) : '0.5';
    return '<div class="field-row rtc-cond-fields">' +
      '<label class="field"><span>' + esc(t('cv.condOp')) + '</span>' +
      '<select data-cond-field="op" aria-label="' + esc(t('cv.condOp')) + '">' +
      root.RTGraph.NUM_OPS.map(op =>
        '<option value="' + esc(op) + '"' + (op === validOp ? ' selected' : '') + '>' +
        esc(root.RTGraph.OP[op]) + '</option>').join('') +
      '</select></label>' +
      '<label class="field"><span>' + esc(t('cv.condValue')) + '</span>' +
      '<input type="text" inputmode="decimal" data-cond-field="value" value="' + esc(value) +
      '" aria-label="' + esc(t('cv.condValue')) + '"></label>' +
      '</div>';
  }
  if (!canEditCondition(edge)) {
    return '<p class="rtc-cond-readonly" data-start-condition>' +
      esc(t('cv.cond.startAlways')) + '</p>';
  }
  const negated = booleanValueOf(condition) === false;
  return '<label class="field rtc-cond-negate">' +
    '<input type="checkbox" data-cond-field="negate" aria-label="' + esc(t('cv.condNegate')) + '"' +
    (negated ? ' checked' : '') + '><span>' + esc(t('cv.condNegate')) + '</span></label>';
}
```

将 `readForm()` 的布尔分支替换为以下代码；数值分支保持不变：

```javascript
if (!canEditCondition(edge)) return { ok: true, condition: null };
const negateElement = container.querySelector('[data-cond-field="negate"]');
const negated = !!(negateElement && negateElement.checked);
return { ok: true, condition: negated ? { op: 'is', value: false } : null };
```

在 `open()` 的 `const edge = edgeOf(edgeId); if (!edge) return false;` 后加入：

```javascript
if (!canEditCondition(edge)) return false;
```

将 `open()` 和 `refresh()` 的焦点选择器统一替换为：

```javascript
const focus = pop.querySelector(
  type === 'number' ? '[data-cond-field="value"]' : '[data-cond-field="negate"]'
);
```

在 `root.RTCondPop` 导出对象中加入：

```javascript
isStartEdge: isStartEdge,
canEditCondition: canEditCondition,
conditionLabel: conditionLabel,
conditionText: conditionText,
```

中英文文案做以下精确替换，并删除两种语言中的 `cv.condBool/cv.condTrue/cv.condFalse`：

```javascript
// zh
'cv.condHint': '数值线可改 6 个算子与阈值；布尔线可切换取反。取消或 Esc 不会写入草稿。',
'cv.condNegate': '取反',
'cv.cond.direct': '直接通过',
'cv.cond.negated': '取反',
'cv.cond.startAlways': '总是通过',
'cv.card.startOut': 'out 始终通过',
'cv.slot.orNote': '多个输入槽位按 OR 判定：任一条件成立即触发。',
'cv.toastBoolDefault': '已连接布尔线（默认直接通过）',

// en
'cv.condHint': 'Numeric edges get six operators and a threshold; boolean edges can be negated. Cancel or Esc writes nothing.',
'cv.condNegate': 'Negate',
'cv.cond.direct': 'Pass through',
'cv.cond.negated': 'Negated',
'cv.cond.startAlways': 'Always passes',
'cv.card.startOut': 'out always passes',
'cv.slot.orNote': 'Multiple input slots are OR-ed: any passing slot fires the node.',
'cv.toastBoolDefault': 'Boolean edge connected (passes through by default)',
```

- [ ] **Step 4: 运行 Task 1 验证**

Run: `node scripts/console_qa.mjs`
Expected: 全部 PASS。Task 2 才同步替换开始节点静态入口和旧 chip 文案断言，因此 Task 1 不得带着失败测试提交。

Run: `python scripts/i18n_dead_keys.py`
Expected: `dead keys: 0`。

- [ ] **Step 5: 提交 Task 1**

```bash
git add router/console/routing/condpop.js router/console/i18n.js
git add -f scripts/console_qa.mjs docs/superpowers/plans/2026-11-05-boolean-edge-condition-ui.md
git commit -m "feat(console): replace boolean expected values with negation"
```

---

### Task 2: 开始节点静态展示、全入口文案与回放

**Files:**
- Modify: `router/console/routing/cards.js:99-136`
- Modify: `router/console/routing/inspector.js:97-113,156-186,344-387`
- Modify: `router/console/routing/app.js:713-715`
- Modify: `router/console/routing/tryrun.js:88-106,780-791`
- Modify: `router/console/routing/graph.js:253-257`
- Modify: `router/console/routing.css`
- Test: `scripts/console_qa.mjs:1210-1234,1912-1946`

**Interfaces:**
- Consumes: Task 1 的 `RTCondPop.isStartEdge/canEditCondition/conditionLabel/conditionText` 和 `data-cond-field="negate"`。
- Produces: 默认开始边的 `span[data-start-condition]`；该元素不是 button，也没有 `data-cond-edge`。

- [ ] **Step 1: 更新旧 QA 并加入开始节点/回放失败测试**

将现有布尔默认 chip 断言改为以下形式；第一处使用既有变量 `booleanEdge`，第二处使用既有变量 `edge`，不得混用：

```javascript
const startSlot = page.locator(`#cvNodes [data-edge="${booleanEdge.id}"]`);
assert.equal(await startSlot.locator('[data-cond-edge]').count(), 0);
assert.equal((await startSlot.locator('[data-start-condition]').textContent()).trim(),
  await page.evaluate(() => window.t('cv.cond.startAlways')));
assert.equal(await page.evaluate(id => window.RTCondPop.open(id), booleanEdge.id), false);
```

第二处 Task 10 使用同样的三个断言，仅把 `booleanEdge.id` 换成 `edge.id`。

将 `7.4-09-question-target-rows` 中第 0 行改为 `[data-start-condition]` + `cv.cond.startAlways`，第 1 行改为 `cv.cond.direct`；将布尔编辑动作从 `selectOption('false')` 改为 `check()`，完成文案改为 `cv.cond.negated`。

新增以下测试，覆盖历史配置和真实回放：

```javascript
test('boolean start is read-only and legacy negation needs an explicit clear', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  let slot = page.locator(`#cvNodes [data-edge="${startEdge.id}"]`);
  assert.equal(await slot.locator('[data-cond-edge]').count(), 0);
  assert.equal(await slot.locator('[data-start-condition]').count(), 1);
  await screenshot('boolean-start-readonly-1440.png');

  await clickEdge(startEdge.id);
  await waitInspectorBranch('edge', startEdge.id);
  assert.equal(await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').count(), 0);
  assert.equal(await page.locator('#cvInspectorBody [data-cond-field="negate"]').count(), 0);

  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      const edge = tree.root.edges.find(item => item.id === id);
      edge.condition = { op: 'is', value: false };
    });
  }, startEdge.id);
  slot = page.locator(`#cvNodes [data-edge="${startEdge.id}"]`);
  assert.equal((await slot.locator('[data-cond-edge]').textContent()).trim(),
    await page.evaluate(() => window.t('cv.cond.negated')));
  await slot.locator('[data-cond-edge]').click();
  await waitCondPop(startEdge.id);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').isChecked(), true);
  await page.locator('#cvCondPop [data-cond-field="negate"]').uncheck();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(startEdge.id, null);
  assert.equal(await slot.locator('[data-cond-edge]').count(), 0);
  assert.equal((await slot.locator('[data-start-condition]').textContent()).trim(),
    await page.evaluate(() => window.t('cv.cond.startAlways')));
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(startEdge.id, { op: 'is', value: false });
});

test('replay condition labels use direct, negated and start-only text', async () => {
  await prepareTryCanvas();
  const edges = (await appSnapshot()).edges;
  const startEdge = edges.find(edge => edge.source === 'start');
  const conditionEdge = edges.find(edge => edge.source === 'cond-text');
  await runTrySample(0);
  assert.match(await page.locator(`#cvNodes [data-edge="${startEdge.id}"]`).getAttribute('title'),
    /总是通过/);
  assert.doesNotMatch(await page.locator(`#cvNodes [data-edge="${startEdge.id}"]`).getAttribute('title'),
    /= true/);

  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = { op: 'is', value: false };
    });
  }, conditionEdge.id);
  await runTrySample(0);
  const negatedTitle = await page.locator(`#cvNodes [data-edge="${conditionEdge.id}"]`).getAttribute('title');
  assert.match(negatedTitle, /取反/);
  assert.doesNotMatch(negatedTitle, /= false/);

  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = null;
    });
  }, conditionEdge.id);
  await runTrySample(0);
  const directTitle = await page.locator(`#cvNodes [data-edge="${conditionEdge.id}"]`).getAttribute('title');
  assert.match(directTitle, /直接通过/);
  assert.doesNotMatch(directTitle, /= true/);
});
```

- [ ] **Step 2: 运行 QA 并确认入口/回放断言失败**

Run: `node scripts/console_qa.mjs`
Expected: FAIL；开始节点仍渲染可点击 `= true`，检查器仍有保存按钮，回放仍输出 `= true / = false`。

- [ ] **Step 3: 接入共享文案和开始节点保护**

在 `cards.js` 将 `conditionText(graph, edge)` 改为：

```javascript
function conditionText(graph, edge) {
  return root.RTCondPop.conditionText(edge);
}
```

将 `slotRow()` 中现有条件 button 替换为以下分支，保留前后槽位元素：

```javascript
const editable = root.RTCondPop.canEditCondition(edge);
const conditionControl = editable
  ? '<button type="button" class="rtc-cond' + (kind === 'number' ? ' is-num' : '') + '"' +
    ' data-cond-edge="' + esc(edge.id) + '" title="' + esc(t('cv.card.editCondition')) + '"' +
    ' aria-label="' + esc(tf('cv.card.editConditionAria', { slot: port.id })) + '">' +
    esc(conditionText(graph, edge)) + '</button>'
  : '<span class="rtc-cond is-static" data-start-condition>' +
    esc(conditionText(graph, edge)) + '</span>';
```

把 `conditionControl` 插入原 button 的位置。`inspector.js` 做以下精确修改：

```javascript
function conditionText(edge) {
  return root.RTCondPop.conditionText(edge);
}

function formTail(saveAction, extraActions, showSave = true) {
  return '<div class="rtc-ins-error" data-ins-error role="alert" hidden></div>' +
    '<div class="rtc-dialog-acts">' + (extraActions || '') +
    (showSave
      ? '<button type="submit" class="btn sm pri" data-act="' + saveAction + '">' +
        esc(t('cv.ins.save')) + '</button>'
      : '') +
    '</div>';
}
```

`slotsSection()` 仅在 `canEditCondition(edge)` 为 true 时渲染 `ins-slot-edit`，`ins-slot-remove` 始终保留：

```javascript
(root.RTCondPop.canEditCondition(edge)
  ? '<button type="button" class="btn sm ghost" data-act="ins-slot-edit" data-edge="' + esc(edge.id) + '">' +
    esc(t('cv.ins.slotEdit')) + '</button>'
  : '')
```

`edgeBranch()` 计算 `const editable = root.RTCondPop.canEditCondition(edge);`，并将结尾改为：

```javascript
formTail('ins-save-edge',
  '<button type="button" class="btn sm danger" data-act="ins-delete-edge">' +
  esc(t('cv.ins.deleteEdge')) + '</button>', editable)
```

`app.js` 的删除确认改用：

```javascript
const condition = type === 'number' || (edge.condition && edge.condition.op === 'is')
  ? ' · ' + root.RTCondPop.conditionText(edge)
  : '';
```

`graph.js` 只把布尔校验提示改为以下文本，其他代码和 `conditionLabel()` 兼容 API 不动：

```javascript
return '布尔输入条件只支持默认通过与取反';
```

`tryrun.js` 用以下实现替换局部 `conditionText()` 和 `slotLine()`，不改 `rawText()`：

```javascript
function conditionText(condition) {
  return root.RTCondPop.conditionLabel(condition, { start: false });
}

function slotLine(slot) {
  const graph = app() && app().graph;
  const edge = graph && (graph.edges || []).find(item => item && item.id === slot.edge_id);
  const label = edge
    ? root.RTCondPop.conditionText(edge)
    : root.RTCondPop.conditionLabel(slot.condition, { start: false });
  const pieces = [rawText(slot.raw)];
  if (slot.condition !== undefined && slot.condition !== null) pieces.push(label);
  else if (typeof slot.raw === 'boolean') pieces.push(label);
  pieces.push('→', statusText(slot.status));
  return pieces.join(' ');
}
```

将 gate 输入中原来的 `'= true'` 默认值改为：

```javascript
(input.condition ? conditionText(input.condition) : root.RTCondPop.conditionLabel(null))
```

在 `routing.css` 增加：

```css
.rtc-cond.is-static { cursor: default; }
```

- [ ] **Step 4: 运行完整控制台验证**

Run: `npm run check:console`
Expected: Node test 与 Playwright 全部 PASS，包括默认开始边、历史取反清除、双入口开关、回放文案、数值条件和 undo。

Run: `python scripts/i18n_dead_keys.py`
Expected: `dead keys: 0`。

- [ ] **Step 5: 提交 Task 2**

```bash
git add router/console/routing/cards.js router/console/routing/inspector.js \
  router/console/routing/app.js router/console/routing/tryrun.js \
  router/console/routing/graph.js router/console/routing.css
git add -f scripts/console_qa.mjs
git commit -m "feat(console): make start edges read-only and clarify boolean labels"
```

---

### Task 3: 最终回归与视觉验收

**Files:**
- Verify only: 不新增代码修改；生成的 QA 截图和临时数据库不提交。

**Interfaces:**
- Consumes: Task 1–2 的完整实现。

- [ ] **Step 1: 检查遗留编辑器选择器和文案**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
files = [
    Path('router/console/routing/condpop.js'),
    Path('router/console/routing/cards.js'),
    Path('router/console/routing/inspector.js'),
    Path('router/console/routing/app.js'),
    Path('router/console/routing/tryrun.js'),
]
for path in files:
    text = path.read_text()
    assert 'data-cond-field="bool"' not in text, path
    assert "'= true'" not in text, path
    assert "'= false'" not in text, path
print('boolean editor selectors and legacy labels: clean')
PY
```

Expected: PASS。不得把该检查扩大到 `rawText()`、引擎测试或存储中的 boolean 值。

- [ ] **Step 2: 运行完整回归**

Run: `npm run check:console && python scripts/i18n_dead_keys.py && .venv/bin/python -m pytest -q`
Expected: 全部 PASS；已有且与本改动无关的失败需原样记录，不得顺手修改。

- [ ] **Step 3: 检查 QA 截图与仓库状态**

检查 `condition-editing-1440.png`，并新增一张默认开始边只读状态的截图，确认：没有 true/false 下拉、默认开始边没有可点击 chip、历史取反仍能打开开关、英文文案不溢出。

Run: `git status --short`
Expected: 没有 QA 临时数据库、截图或 `artifacts/` 被纳入提交；`configs/cases/` 保持用户原有状态。

- [ ] **Step 4: 交付验收结果**

汇报修改文件、完整测试输出、开始节点默认/历史两种状态、取反开关双入口和回放文案。若视觉检查未执行，明确写“未做视觉验收”，不得以测试通过代替视觉验收。

---

## Plan Self-Review Checklist

- 开始节点默认只读：Task 1 的 `open()` 保护与 Task 2 的卡片/检查器入口共同覆盖，不能只做其中一层。
- 去掉 true/false：Task 1 删除下拉，Task 2 覆盖 chip、检查器、删除、回放和 toast；原始运行值与引擎语义明确排除。
- 保留 `is:false`：Task 1 开关写回同一数据结构，Task 2 历史开始边可显式清除，undo 可恢复。
- 数值边和可访问性：沿用原数值分支、取消/Esc、保存锁、history 和焦点恢复；完整控制台回归为必过项。
- 接口一致性：所有视图只使用 `conditionLabel/conditionText/canEditCondition/isStartEdge`，没有第二套开始节点或取反判断。
