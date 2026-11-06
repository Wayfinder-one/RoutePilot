# 布尔边条件 UI 审核修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清除条件配置相关 UI 中遗留的 `= true / = false` 文案，修复历史开始边清除后的焦点恢复与 gate 回放文案，并补齐审核指出的回归和视觉验收。

**Architecture:** 继续以 `RTCondPop.conditionText/conditionLabel` 为唯一条件文案入口，不改存储或引擎。焦点修复复用稳定的开始边静态元素；gate 回放根据当前图的 `target + target_port` 找回 edge，不修改后端 trace。

**Tech Stack:** 原生 JavaScript、CSS、Node test、Playwright `scripts/console_qa.mjs`、Python i18n lint；不新增依赖。

**Starting Point:** 本文件 Task 1–4 已在 `d484fbd`、`e9a13c7`、`f44fa16`、`c7c4519` 落地，保留为原始实施记录，不要重复执行。2026-11-06 审查发现的普通 gate/fallback 覆盖缺口和焦点注释问题，只执行接续计划 `docs/superpowers/plans/2026-11-06-boolean-edge-review-follow-up.md`。

## Assumptions

- 文案清理覆盖条件配置语义：图例、校验提示、槽位端口 tooltip、条件 chip、检查器、删除确认、toast 与回放。
- 原始布尔运行值、执行状态、引擎测试名称及 `RTGraph.conditionLabel()` 的兼容 API 不在清理范围内；不得为通过文本扫描而修改它们。
- 静态开始边可以用 `tabindex="-1"` 接收程序化焦点，但仍不得变成按钮、进入常规 Tab 顺序或获得 `data-cond-edge`。
- 历史非空开始边仍不自动迁移；清除后写 `condition:null`，undo 恢复 `{op:'is', value:false}`。
- 不修改无关页面和已有未跟踪的 `configs/cases/`。

## Alternatives Considered

1. **采用：静态开始边保留稳定 id 并允许程序化聚焦。** 改动小，直接覆盖 chip 被替换的焦点断点，不引入可编辑入口。
2. **清除后改为聚焦整个槽位行。** 也可行，但会把焦点目标从条件扩大到槽位，且需要另一套 data 契约；不采用。
3. **给后端 gate trace 增加 edge_id/source。** 能直接定位 edge，但扩大服务端契约和测试范围；当前图已经能通过 `row.node_id + input.port` 唯一找到入边，不采用。

## Global Constraints

- 保持 `condition:null` / `{op:'is', value:false}` 规范化、求值、history/undo 和保存锁语义不变。
- 不修改 `router/routing/`、`router/console/routing/graph.js::evaluateCondition` 或后端 trace 数据结构。
- 数值边六算子与阈值校验不变；数值边缺条件必须显示 `cv.cond.unset`。
- 默认开始边仍渲染静态 `span[data-start-condition]`；直接 `RTCondPop.open()` 仍返回 false。
- 新增测试必须操作真实 DOM/App 状态，不得只调用文案 helper 代替 UI 断言。
- 所有命令使用当前环境可用的 `python3` 或 `.venv/bin/python`，不得使用不存在的 `python`。
- 截图生成必须显式设置 `QA_SCREENSHOTS=1`；普通测试输出不能替代视觉验收。
- 仅对明确列出的文件执行 `git add`；若新增计划或测试文件被忽略，只对这些路径使用 `git add -f`。

## File Structure

- Modify `router/console/i18n.js`：清理中英文图例旧文案。
- Modify `router/console/routing/graph.js`：只修改 `HINTS/HINT_EN` 与布尔槽位 `desc`；保留兼容 `conditionLabel()` 和求值代码。
- Modify `router/console/routing/condpop.js`：让焦点回退同时识别可编辑 chip 和静态开始边。
- Modify `router/console/routing/cards.js`：静态开始边携带稳定 edge id 与 `tabindex="-1"`。
- Modify `router/console/routing.css`：静态开始边不再呈现可编辑 hover。
- Modify `router/console/routing/tryrun.js`：gate 输入按真实 edge 生成条件文案。
- Modify `scripts/console_graph_test.mjs`：校验纯函数层的提示与槽位描述。
- Modify `scripts/console_qa.mjs`：覆盖双语提示、键盘焦点、静态 hover、gate 回放、删除/toast、数值缺条件和英文 UI。

---

### Task 1: 清理图例、校验提示与槽位 tooltip

**Files:**
- Modify: `router/console/i18n.js:323,794`
- Modify: `router/console/routing/graph.js:39-49,128-135`
- Test: `scripts/console_graph_test.mjs:144-161`
- Test: `scripts/console_qa.mjs`（在现有开始边 QA 前新增）

**Interfaces:**
- Consumes: 现有 `RTGraph.validate()`、`RTGraph.portOf()`、`window.App.issues`、`window.setLang()`。
- Produces: 更新后的 `cv.legendBool`、第三条 hint 和布尔槽位 `desc`；不新增 key，不改变接口签名。

- [ ] **Step 1: 添加纯函数层失败测试**

在 `scripts/console_graph_test.mjs` 的布尔条件求值测试后加入：

```javascript
test('boolean condition guidance uses pass-through and negation terminology', () => {
  const graph = exampleGraph();
  const hints = RTGraph.validate(graph).filter(issue => issue.level === 'hint');
  const capability = RTGraph.findNode(graph, 'capability');
  const input = RTGraph.portOf(capability, 'in_1', graph);
  const text = hints.map(issue => issue.zh + '\n' + issue.en).join('\n') + '\n' + input.desc;

  assert.doesNotMatch(text, /= (?:true|false)/);
  assert.match(text, /直接通过/);
  assert.match(text, /取反/);
  assert.match(text, /pass through/i);
  assert.match(text, /negat/i);
});
```

- [ ] **Step 2: 添加真实 UI 的中英文失败测试**

在 `scripts/console_qa.mjs` 新增：

```javascript
test('boolean condition guidance is bilingual and has no expected-value syntax', async () => {
  await openCanvas();
  await openIssuesPanel();

  async function guidance() {
    return page.evaluate(() => ({
      legend: document.querySelector('[data-i18n="cv.legendBool"]').textContent.trim(),
      issues: document.getElementById('cvIssues').textContent.replace(/\s+/g, ' ').trim(),
      slotTitle: document.querySelector(
        '#cvNodes [data-node-id="capability"] [data-port][data-port-dir="in"][data-port-id="in_1"]'
      ).getAttribute('title')
    }));
  }

  const zh = await guidance();
  assert.equal(zh.legend, '布尔线 · 默认直接通过');
  assert.match(zh.issues, /直接通过/);
  assert.match(zh.issues, /取反/);
  assert.doesNotMatch([zh.legend, zh.issues, zh.slotTitle].join('\n'), /= (?:true|false)/);

  await page.evaluate(() => window.setLang('en'));
  try {
    const en = await guidance();
    assert.equal(en.legend, 'Boolean edge · passes through by default');
    assert.match(en.issues, /pass through/i);
    assert.match(en.issues, /negat/i);
    assert.doesNotMatch([en.legend, en.issues, en.slotTitle].join('\n'), /= (?:true|false)/);
    await screenshot('boolean-condition-guidance-en-1440.png');
  } finally {
    await page.evaluate(() => window.setLang('zh'));
  }
});
```

- [ ] **Step 3: 运行定向测试并确认旧实现失败**

Run:

```bash
node --test --test-name-pattern='boolean condition guidance' scripts/console_graph_test.mjs
QA_ONLY='boolean condition guidance is bilingual' node scripts/console_qa.mjs
```

Expected: 两个命令均 FAIL；Node 测试命中 hint/槽位描述中的旧值，Playwright 测试首先在旧图例断言失败。

- [ ] **Step 4: 精确替换四处文案**

`router/console/i18n.js`：

```javascript
// zh
'cv.legendBool': '布尔线 · 默认直接通过',

// en
'cv.legendBool': 'Boolean edge · passes through by default',
```

`router/console/routing/graph.js` 的第三条中英文 hint：

```javascript
'布尔槽位不写条件即为直接通过；要反向判断可以开启取反，或显式加 NOT 门。',
```

```javascript
'A boolean slot without a condition passes through directly; enable negation (or add an explicit NOT gate) for the opposite test.',
```

`slotPorts()` 中 question/target 的布尔槽位描述：

```javascript
'布尔输入槽位（多个槽位按 OR）。数值线需要配置比较条件；布尔线默认直接通过。'
```

不要修改同文件的 `conditionLabel()`；它仍是兼容 API，且当前 UI 不再调用它。

- [ ] **Step 5: 运行 Task 1 验证**

Run:

```bash
node --test --test-name-pattern='boolean condition guidance' scripts/console_graph_test.mjs
QA_ONLY='boolean condition guidance is bilingual' node scripts/console_qa.mjs
npm run check:console
python3 scripts/i18n_dead_keys.py
```

Expected: 定向测试和完整控制台测试全部 PASS；i18n 输出 `dead keys: 0`。

- [ ] **Step 6: 提交 Task 1**

```bash
git add router/console/i18n.js router/console/routing/graph.js \
  scripts/console_graph_test.mjs scripts/console_qa.mjs
git add -f docs/superpowers/plans/2026-11-05-boolean-edge-condition-ui-review-fixes.md
git commit -m "fix(console): remove legacy boolean condition guidance"
```

---

### Task 2: 修复历史开始边焦点与静态 hover

**Files:**
- Modify: `router/console/routing/condpop.js:276-298`
- Modify: `router/console/routing/cards.js:112-120`
- Modify: `router/console/routing.css:362-376`
- Test: `scripts/console_qa.mjs:1679-1744,5904-5937`

**Interfaces:**
- Consumes: 现有 `triggerInfo()` 的 `condEdge`、`restoreTrigger()` 和静态 `data-start-condition`。
- Produces: 卡片静态元素 `span[data-start-condition="<edgeId>"][tabindex="-1"]`；不新增全局函数。

- [ ] **Step 1: 添加静态 hover 失败测试**

```javascript
test('static start condition has no editable hover feedback', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  const condition = page.locator(`#cvNodes [data-edge="${startEdge.id}"] [data-start-condition]`);
  const style = () => condition.evaluate(element => {
    const value = getComputedStyle(element);
    return {
      color: value.color,
      backgroundColor: value.backgroundColor,
      borderColor: value.borderColor,
      cursor: value.cursor
    };
  });

  await page.mouse.move(0, 0);
  const resting = await style();
  assert.equal(resting.cursor, 'default');
  await condition.hover();
  assert.deepEqual(await style(), resting);
});
```

- [ ] **Step 2: 添加键盘焦点失败测试**

```javascript
test('clearing legacy start negation restores keyboard focus', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = { op: 'is', value: false };
    });
  }, startEdge.id);

  const chip = page.locator(`#cvNodes [data-cond-edge="${startEdge.id}"]`);
  await chip.focus();
  await page.keyboard.press('Enter');
  await waitCondPop(startEdge.id);
  const negate = page.locator('#cvCondPop [data-cond-field="negate"]');
  assert.equal(await negate.isChecked(), true);
  await negate.focus();
  await page.keyboard.press('Space');
  assert.equal(await negate.isChecked(), false);
  await page.locator('#cvCondPop [data-act="cond-confirm"]').focus();
  await page.keyboard.press('Enter');
  await waitEdgeCondition(startEdge.id, null);

  const focused = await page.evaluate(() => ({
    tag: document.activeElement && document.activeElement.tagName,
    startCondition: document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.startCondition || '' : '',
    tabIndex: document.activeElement ? document.activeElement.tabIndex : null
  }));
  assert.deepEqual(focused, { tag: 'SPAN', startCondition: startEdge.id, tabIndex: -1 });
  assert.equal(await page.locator(`#cvNodes [data-cond-edge="${startEdge.id}"]`).count(), 0);

  await page.locator('#cvUndo').click();
  await waitEdgeCondition(startEdge.id, { op: 'is', value: false });
});
```

- [ ] **Step 3: 确认两个测试在旧实现失败**

Run:

```bash
QA_ONLY='static start condition has no editable hover|clearing legacy start negation restores keyboard focus' node scripts/console_qa.mjs
```

Expected: 两条均 FAIL；第一条的 hover 颜色发生变化，第二条的焦点不在静态开始边上。

- [ ] **Step 4: 实现稳定焦点目标并限制 hover**

`cards.js` 的静态分支完整替换为：

```javascript
: '<span class="rtc-cond is-static" data-start-condition="' + esc(edge.id) + '" tabindex="-1">' +
  esc(conditionText(graph, edge)) + '</span>';
```

`condpop.js` 的 `restoreTrigger()` 完整替换为：

```javascript
function restoreTrigger(info) {
  if (!info) return;
  let target = info.element && info.element.isConnected ? info.element : null;
  if (!target && info.condEdge) {
    target = Array.from(document.querySelectorAll(
      '[data-cond-edge], [data-start-condition][tabindex="-1"]'
    )).find(element =>
      (element.dataset.condEdge || element.dataset.startCondition || '') === info.condEdge
    ) || null;
  }
  if (target && typeof target.focus === 'function' && target !== document.activeElement) {
    target.focus({ preventScroll: true });
  }
}
```

`routing.css` 使用以下两条 hover 规则，保留 `.rtc-cond.is-static { cursor: default; }`：

```css
.rtc-cond:not(.is-static):hover { border-color: var(--indigo-line); background: var(--indigo-soft); color: var(--indigo); }
.rtc-cond.is-num:not(.is-static):hover { border-color: var(--teal); }
```

不得给静态元素添加 `data-cond-edge`，不得将其改成 button 或设为 `tabindex="0"`。

- [ ] **Step 5: 恢复无关断言，防止修复测试时削弱旧契约**

在 Task 11 布尔双入口测试中恢复以下断言；这些不是新实现分支，现有代码应直接通过：

第一次 cancel 后、Esc 后分别增加：

```javascript
assert.equal((await appSnapshot()).kind, 'clean');
```

Esc 后增加：

```javascript
assert.equal(await page.evaluate(() =>
  document.activeElement && document.activeElement.dataset
    ? document.activeElement.dataset.condEdge || '' : ''
), edgeId, 'Esc did not return focus to the condition chip');
```

检查器布尔边保存前增加：

```javascript
assert.equal(await page.locator(
  '#cvInspectorBody [data-ins-branch="edge"]'
).getAttribute('data-edge-type'), 'boolean');
```

检查器保存产生 `{op:'is', value:false}` 后、undo 前增加：

```javascript
assert.equal((await appSnapshot()).kind, 'behavior');
```

- [ ] **Step 6: 运行 Task 2 验证**

Run:

```bash
QA_ONLY='static start condition has no editable hover|clearing legacy start negation restores keyboard focus|Task 11 · boolean conditions' node scripts/console_qa.mjs
npm run check:console
```

Expected: 定向测试与完整控制台测试全部 PASS；静态开始边仍不可编辑，历史清除仍只有一个 undo 项。

- [ ] **Step 7: 提交 Task 2**

```bash
git add router/console/routing/condpop.js router/console/routing/cards.js \
  router/console/routing.css scripts/console_qa.mjs
git commit -m "fix(console): restore focus after clearing start negation"
```

---

### Task 3: 修复 gate 回放中的开始边文案

**Files:**
- Modify: `router/console/routing/tryrun.js:88-90,789-795`
- Test: `scripts/console_qa.mjs`（放在现有回放文案 QA 后）

**Interfaces:**
- Consumes: `row.node_id`、`input.port`、当前 `App.graph.edges`、`RTCondPop.conditionText(edge)`。
- Produces: gate 输入与卡片/槽位一致的条件文案；不改变 replay/trace 数据。

- [ ] **Step 1: 添加 start → logic 的真实回放失败测试**

```javascript
test('gate replay labels a start input as always passing', async () => {
  await prepareTryCanvas();
  await page.evaluate(() => {
    window.App.change('behavior', tree => {
      const result = window.RTGraph.addEdge(tree.root, {
        source: 'start', source_port: 'out', target: 'or-ctx', target_port: 'in_3'
      });
      if (!result.ok) throw new Error(result.reason || 'failed to add start gate input');
    });
  });
  await runTrySample(0);
  await page.locator('#cvTryDrawerBtn').click();
  await page.waitForSelector('#cvTraceDrawer:not([hidden])');

  const gate = page.locator('#cvTraceDrawer .rtc-dw-row--gate').filter({ hasText: 'in_3' });
  assert.equal(await gate.count(), 1);
  const text = await gate.innerText();
  assert.match(text, /总是通过/);
  assert.doesNotMatch(text, /直接通过/);
  assert.doesNotMatch(text, /= (?:true|false)/);
});
```

- [ ] **Step 2: 确认旧回放实现失败**

Run:

```bash
QA_ONLY='gate replay labels a start input as always passing' node scripts/console_qa.mjs
```

Expected: FAIL；gate 行仍显示“直接通过”，与槽位的“总是通过”不一致。

- [ ] **Step 3: 按 target/target_port 找回 edge**

将 `tryrun.js` 的 `case 'gate'` 完整替换为：

```javascript
case 'gate': {
  const graph = app() && app().graph;
  const inputs = (row.inputs || []).map(input => {
    const edge = graph && (graph.edges || []).find(item => item &&
      item.target === row.node_id && item.target_port === input.port);
    const label = edge
      ? root.RTCondPop.conditionText(edge)
      : root.RTCondPop.conditionLabel(input.condition, { start: false });
    return input.port + ' ' + rawText(input.raw) + ' ' + label + ' → ' + statusText(input.status);
  }).join(t('cv.trySepClause'));
  return tf('cv.tryGateRow', { operator: row.operator, inputs: inputs, value: String(row.value) });
}
```

删除已不再使用的局部函数：

```javascript
function conditionText(condition) {
  return root.RTCondPop.conditionLabel(condition, { start: false });
}
```

保留 `slotLine()` 的 fallback；找不到 edge 时仍根据 trace 中已有的 `condition` 显示，不伪造开始节点语义。

- [ ] **Step 4: 运行 Task 3 验证**

Run:

```bash
QA_ONLY='gate replay labels a start input as always passing|replay condition labels' node scripts/console_qa.mjs
npm run check:console
```

Expected: gate、普通直接通过/取反回放以及完整控制台测试全部 PASS；trace 和引擎文件无改动。

- [ ] **Step 5: 提交 Task 3**

```bash
git add router/console/routing/tryrun.js scripts/console_qa.mjs
git commit -m "fix(console): label start inputs consistently in gate replay"
```

---

### Task 4: 补齐文案契约与最终验收

**Files:**
- Test: `scripts/console_qa.mjs`
- Verify only: 除下列新增 QA 外不修改运行时代码；截图和临时数据库不提交。

**Interfaces:**
- Consumes: Task 1–3 的全部实现、现有 `waitToastIncludes()`、`clearToasts()` 和 `screenshot()`。
- Produces: 删除/toast、数值缺条件、英文布尔 UI 的持久回归覆盖。

- [ ] **Step 1: 添加删除确认与连接 toast 回归测试**

```javascript
test('boolean deletion and connection feedback use the new labels', async () => {
  await openCanvas();
  const edgeId = 'e-selected-easy-light';
  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = { op: 'is', value: false };
    });
  }, edgeId);
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  await page.locator('#cvInspectorBody [data-act="ins-delete-edge"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden);
  const deletion = await page.locator('#cvDeleteBody').innerText();
  assert.match(deletion, /取反/);
  assert.doesNotMatch(deletion, /= (?:true|false)/);
  await page.locator('#cvDeleteBody [data-act="delete-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvDeleteModal').hidden);

  await openCanvas();
  await clearToasts();
  await dragPort('cond-text', 'out', { node: 'capability', port: 'in_2' });
  const expected = await page.evaluate(() => window.t('cv.toastBoolDefault'));
  await waitToastIncludes(expected);
  assert.doesNotMatch(await toastText(), /= (?:true|false)/);
});
```

- [ ] **Step 2: 添加数值缺条件和英文布尔 UI 回归测试**

```javascript
test('numeric unset and English boolean condition labels stay explicit', async () => {
  await openCanvas();
  const numericEdge = 'e-clarify-deep';
  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = null;
    });
  }, numericEdge);
  assert.equal(
    (await page.locator(`#cvNodes [data-cond-edge="${numericEdge}"]`).textContent()).trim(),
    await page.evaluate(() => window.t('cv.cond.unset'))
  );

  await openCanvas();
  await page.evaluate(() => window.setLang('en'));
  try {
    const edges = (await appSnapshot()).edges;
    const startEdge = edges.find(edge => edge.source === 'start');
    const directEdge = edges.find(edge => edge.id === 'e-cond-text-or');
    assert.equal(
      (await page.locator(`#cvNodes [data-edge="${startEdge.id}"] [data-start-condition]`).textContent()).trim(),
      'Always passes'
    );
    const chip = page.locator(`#cvNodes [data-cond-edge="${directEdge.id}"]`);
    assert.equal((await chip.textContent()).trim(), 'Pass through');
    await chip.click();
    await waitCondPop(directEdge.id);
    assert.equal(
      (await page.locator('#cvCondPop .rtc-cond-negate span').textContent()).trim(),
      'Negate'
    );
    assert.equal(await page.locator('#cvCondPop [data-cond-field="bool"]').count(), 0);
    await screenshot('en-boolean-condition-1440.png');
  } finally {
    await page.keyboard.press('Escape');
    await page.evaluate(() => window.setLang('zh'));
  }
});
```

- [ ] **Step 3: 运行新增契约测试**

Run:

```bash
QA_ONLY='boolean deletion and connection feedback|numeric unset and English boolean' node scripts/console_qa.mjs
```

Expected: PASS。这些测试补的是审核发现的覆盖缺口；若失败，必须记录实际失败 UI，不得通过删除断言或放宽到只调用 helper 来通过。

- [ ] **Step 4: 重新生成并目视检查截图**

Run:

```bash
QA_SCREENSHOTS=1 QA_ONLY='boolean condition guidance is bilingual|7.4-06-condition-edit-dual-entry-dirty-undo|boolean start is read-only|numeric unset and English boolean' node scripts/console_qa.mjs
```

Expected: 所有选中用例 PASS，并实际更新：

- `boolean-condition-guidance-en-1440.png`
- `condition-editing-1440.png`
- `boolean-start-readonly-1440.png`
- `en-boolean-condition-1440.png`

逐张检查：图例无 `= true / = false`；默认开始边无可点击样式；数值编辑器不变；布尔编辑器只有取反；英文“Always passes / Pass through / Negate”不溢出或重叠。未逐张查看不得声称视觉验收完成。

- [ ] **Step 5: 运行完整回归与仓库检查**

Run:

```bash
npm run check:console && python3 scripts/i18n_dead_keys.py && .venv/bin/python -m pytest -q
git diff --check
git status --short
```

Expected: Node、Playwright、i18n 和 Python 全部 PASS；无空白错误；没有临时数据库或截图被纳入提交，`configs/cases/` 保持原有未跟踪状态。已有且无关的失败原样记录，不顺手修复。

- [ ] **Step 6: 提交新增回归测试**

```bash
git add scripts/console_qa.mjs
git commit -m "test(console): cover boolean condition review contracts"
```

- [ ] **Step 7: 交付审核闭环**

逐项汇报：遗留文案、焦点恢复、静态 hover、gate 回放、双语 UI、删除/toast、数值缺条件；附上完整测试计数、截图检查结果和最终 `git status --short`。不得把未运行的检查写为通过。

---

## Plan Self-Review Checklist

- 审核中的两个 Important 均有实现步骤和真实 UI 失败测试，不只是文本扫描。
- gate 只修改前端展示；原始 `true/false` 值、执行状态和引擎测试保留。
- 焦点回退不给默认开始边恢复 `data-cond-edge`，也不让它进入 Tab 顺序。
- `cv.legendBool`、`HINTS/HINT_EN`、槽位 `desc` 全部在 Task 1 明确处理，兼容 `conditionLabel()` 不误删。
- Task 2 恢复此前被移除的 Esc 焦点、clean、边类型和 dirty 状态断言。
- Task 4 覆盖删除确认、toast、数值 `cv.cond.unset` 和英文精确 UI 文案。
- i18n 命令统一为 `python3`；截图命令显式设置 `QA_SCREENSHOTS=1`，普通回归不伪称生成截图。
