# 布尔边审核后续修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐普通 gate 的直接通过/取反回放断言和 trace fallback 断言，并同步焦点恢复注释，闭合 2026-11-06 代码审查的两项 Minor。

**Architecture:** 不修改运行时行为。新增两组真实 Playwright 回放测试：一组读取 gate 抽屉中的普通布尔输入，另一组拦截真实 `/admin/try` 响应，仅替换 gate inputs 以构造当前图中不存在的目标端口。焦点修复只更新已经过时的注释。

**Tech Stack:** 原生 JavaScript、Node test、Playwright `scripts/console_qa.mjs`、Python i18n lint；不新增依赖。

**Starting Point:** 当前 HEAD 为 `c7c4519`，原计划 Task 1–4 已落地。本计划只处理审查遗留，不重复实施布尔取反、开始边只读、焦点恢复或 gate edge 匹配。

## Assumptions

- 当前运行时实现经审查正确；新增测试属于覆盖缺口的 characterization tests，在当前 HEAD 上预期 PASS，不伪造失败步骤。
- 原 `replay condition labels use direct, negated and start-only text` 继续验证槽位标题；本计划新增独立 gate 抽屉测试，不把两种断言混在一个用例中。
- fallback 测试必须从真实 `/admin/try` 响应构造受控 trace，并通过完整页面回放断言，不直接调用 `conditionLabel()` 代替 UI 测试。
- 不修改引擎、后端 trace、`RTTryRun.rowText()`、焦点逻辑、样式或翻译。
- 不修改已有未跟踪的 `configs/cases/`。

## Global Constraints

- 保留 `condition:null` / `{op:'is', value:false}`、history/undo、静态开始边和现有回放语义。
- 普通 gate 测试使用真实边 `e-cond-text-or`，目标为 `or-ctx.in_1`；测试结束 undo 恢复 `condition:null`。
- fallback 测试只修改拦截响应中的 gate inputs，使用当前图不存在的 `in_trace_direct` 和 `in_trace_negated`，不得改写持久化草稿。
- 所有 Python 命令使用 `python3` 或 `.venv/bin/python`。
- 本次没有视觉或运行时改动，不要求重新生成截图；不得把普通测试输出描述为新生成的视觉证据。
- 仅对明确列出的文件执行 `git add`；被忽略的计划文件只按完整路径使用 `git add -f`。

## File Structure

- Modify `scripts/console_qa.mjs`：新增普通 gate 与缺失 edge fallback 的真实回放测试。
- Modify `router/console/routing/condpop.js:286-287`：让注释同时描述可编辑 chip 和静态开始边的焦点恢复。
- Modify `docs/superpowers/plans/2026-11-05-boolean-edge-condition-ui-review-fixes.md:11`：标明后续执行边界。
- Create `docs/superpowers/plans/2026-11-06-boolean-edge-review-follow-up.md`：本接续计划。

---

### Task 1: 补齐 gate 回放契约并同步焦点注释

**Files:**
- Modify: `scripts/console_qa.mjs:6136-6156`（在现有 start gate 用例后追加）
- Modify: `router/console/routing/condpop.js:286-287`
- Test: `scripts/console_qa.mjs`

**Interfaces:**
- Consumes: `prepareTryCanvas()`、`runTrySample(0)`、`waitEdgeCondition()`、`page.route()`、`route.fetch()`、现有 `#cvTryDrawerBtn` 和 `.rtc-dw-row--gate[data-trace-node]`。
- Produces: 测试 `gate replay labels direct and negated boolean inputs` 与 `gate replay falls back to trace conditions for missing edges`；不新增运行时 API。

- [ ] **Step 1: 添加普通 gate 的直接通过与取反测试**

在 `scripts/console_qa.mjs` 的 `gate replay labels a start input as always passing` 后添加完整用例：

```javascript
test('gate replay labels direct and negated boolean inputs', async () => {
  await prepareTryCanvas();

  async function gateText() {
    await page.locator('#cvTryDrawerBtn').click();
    await page.waitForSelector('#cvTraceDrawer:not([hidden])');
    const row = page.locator(
      '#cvTraceDrawer .rtc-dw-row--gate[data-trace-node="or-ctx"]'
    );
    assert.equal(await row.count(), 1);
    return row.innerText();
  }

  await runTrySample(0);
  const direct = await gateText();
  assert.match(direct, /in_1[^；]*直接通过/);
  assert.doesNotMatch(direct, /= (?:true|false)/);

  await page.evaluate(() => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(item => item.id === 'e-cond-text-or').condition = {
        op: 'is', value: false
      };
    });
  });
  await runTrySample(0);
  const negated = await gateText();
  assert.match(negated, /in_1[^；]*取反/);
  assert.doesNotMatch(negated, /in_1[^；]*直接通过/);
  assert.doesNotMatch(negated, /= (?:true|false)/);

  await page.locator('#cvUndo').click();
  await waitEdgeCondition('e-cond-text-or', null);
});
```

该用例必须在抽屉的 `or-ctx` 行上断言，不能只检查画布槽位 `title`。`App.change()` 会清除上一次回放并关闭抽屉，因此第二次 `runTrySample(0)` 后必须重新打开抽屉。

- [ ] **Step 2: 添加缺失 edge 时的 trace fallback 测试**

紧接上一用例添加：

```javascript
test('gate replay falls back to trace conditions for missing edges', async () => {
  await prepareTryCanvas();
  const matcher = url => url.pathname === '/admin/try';
  await page.route(matcher, async route => {
    const response = await route.fetch();
    const body = await response.json();
    const gate = body.trace.find(event =>
      event.kind === 'gate' && event.node_id === 'or-ctx');
    assert.ok(gate, 'real try response did not contain the or-ctx gate');
    gate.inputs = [
      { port: 'in_trace_direct', raw: true, condition: null, status: 'true' },
      {
        port: 'in_trace_negated', raw: true,
        condition: { op: 'is', value: false }, status: 'false'
      }
    ];
    gate.value = true;
    await route.fulfill({
      response,
      contentType: 'application/json',
      body: JSON.stringify(body)
    });
  });

  try {
    await runTrySample(0);
    await page.locator('#cvTryDrawerBtn').click();
    await page.waitForSelector('#cvTraceDrawer:not([hidden])');
    const row = page.locator(
      '#cvTraceDrawer .rtc-dw-row--gate[data-trace-node="or-ctx"]'
    );
    assert.equal(await row.count(), 1);
    const text = await row.innerText();
    assert.match(text, /in_trace_direct true 直接通过 → true/);
    assert.match(text, /in_trace_negated true 取反 → false/);
    assert.doesNotMatch(text, /in_trace_(?:direct|negated)[^；]*总是通过/);
    assert.doesNotMatch(text, /= (?:true|false)/);
  } finally {
    await page.unroute(matcher);
  }
});
```

这两个端口在当前图中没有对应 edge，因此实现只能走 `RTCondPop.conditionLabel(input.condition, { start: false })` 回退。其余响应仍来自真实后端，不得用完整手写响应替代 `route.fetch()`。

- [ ] **Step 3: 运行定向测试并确认覆盖当前正确实现**

Run:

```bash
QA_ONLY='gate replay labels direct and negated boolean inputs|gate replay falls back to trace conditions for missing edges|gate replay labels a start input as always passing' node scripts/console_qa.mjs
```

Expected: 三个用例全部 PASS。普通 gate 行分别出现“直接通过”和“取反”；fallback 行分别出现 `in_trace_direct true 直接通过 → true` 与 `in_trace_negated true 取反 → false`。这些测试关闭审查发现的覆盖缺口，不是现存运行时 bug 的失败复现。

若失败，先记录实际抽屉文本和拦截响应中的 gate event；不得通过删除精确断言、只调用文案 helper 或修改引擎数据来通过。

- [ ] **Step 4: 同步焦点恢复注释**

将 `router/console/routing/condpop.js:286-287` 的注释完整替换为：

```javascript
/* 关闭后把焦点还给触发元素（a11y §6.11）。确认条件会触发 App.change →
   renderAll 重绘卡片；按条件 edge id 重新定位可编辑 chip 或静态开始边。 */
```

不要修改相邻的 `restoreTrigger()`；其现有选择器和 `tabindex="-1"` 行为已经通过回归测试。

- [ ] **Step 5: 运行完整验证**

Run:

```bash
npm run check:console && python3 scripts/i18n_dead_keys.py && .venv/bin/python -m pytest -q
git diff --check
git status --short
```

Expected: 按当前基线，Node 59 个测试、Playwright 117 个用例、Python 660 个测试全部 PASS；i18n 为 681 个 key、`dead keys: 0`；无空白错误。若上游测试数量变化，以实际新增两个 Playwright 用例后的数量为准。`configs/cases/` 保持原有未跟踪状态，不新增临时数据库、截图或日志到提交。

本次只有测试和注释变化，不重新生成四张既有截图，也不声称完成了新的视觉回归。

- [ ] **Step 6: 提交审查后续修复**

```bash
git add scripts/console_qa.mjs router/console/routing/condpop.js
git add -f docs/superpowers/plans/2026-11-05-boolean-edge-condition-ui-review-fixes.md \
  docs/superpowers/plans/2026-11-06-boolean-edge-review-follow-up.md
git commit -m "test(console): cover gate replay condition contracts"
```

- [ ] **Step 7: 交付审核闭环**

汇报普通 gate 直接通过/取反、缺失 edge fallback、开始边 gate 既有回归、焦点注释和完整测试计数。附最终 `git status --short`；明确说明本次没有运行时行为或截图变化，不得把未运行的检查写为通过。

---

## Plan Self-Review Checklist

- 普通 gate 断言读取真实抽屉行，不再把槽位标题测试当作 gate 覆盖。
- fallback 使用真实响应和受控缺失端口，同时覆盖 `condition:null` 与 `{op:'is', value:false}`。
- 新测试在当前正确实现上预期 PASS，没有伪造 RED 或要求修改正确实现。
- 焦点注释同时描述 `data-cond-edge` 与静态开始边，不要求改动已经正确的代码。
- 所有步骤具有完整代码、精确命令和预期结果；不新增依赖、后端字段或无关视觉验收。
