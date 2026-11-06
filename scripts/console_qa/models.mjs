/* RoutePilot 控制台 QA · 模型目录与决策服务用例（bootstrap 目录、catalogue 晚到、JEV 窗口保存错误）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/models.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  BASELINE,
  JEV_MODEL,
  TREE_ID,
  api,
  appSnapshot,
  expectHttpStatusError,
  expectedHttpStatusErrors,
  mockBase,
  page,
  resetExpectedHttpStatusErrors,
  routerBase,
  runIfMain,
  test,
  waitFor,
  waitInspectorBranch
} from '../console_qa.mjs';

test('bootstrap registers catalog, periods, decision service and the enabled power graph', async () => {
  const models = await api('GET', '/admin/models');
  const modelIds = models.models.map(model => model.id).sort();
  for (const required of ['deepseek-v3', 'qwen3-8b', 'qwen3-32b', 'qwen3.8-flash-sft']) {
    assert.ok(modelIds.includes(required), 'missing model ' + required);
}
  assert.ok(models.models.every(model => model.provider === 'local'));

  const periods = await api('GET', '/admin/time-periods');
  assert.deepEqual(periods.periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.deepEqual(periods.periods.map(period => [period.start, period.end]), [
    ['08:00', '11:00'], ['11:00', '18:00'], ['18:00', '08:00']
  ]);

  const jev = await api('GET', '/admin/jev');
  assert.equal(jev.config.base_url, mockBase);
  assert.equal(jev.config.model, JEV_MODEL);

  const trees = await api('GET', '/admin/trees');
  const tree = trees.trees.find(item => item.id === TREE_ID);
  assert.ok(tree, 'power graph tree missing');
  assert.equal(tree.enabled, true);
  assert.equal(tree.root.nodes.length, 15);
  assert.equal(tree.root.edges.length, 15);
  assert.equal(tree.root.otherwise_target, undefined);
  assert.equal(tree.root.error_fallback, 'light-target');
  assert.match(tree.tree_revision, /^[0-9a-f]{64}$/);
});

test('Task 11 · boot unlocks the canvas before catalogues and late catalogue data does not wipe inspector edits', async () => {
  let release = () => {};
  const gate = new Promise(resolve => { release = resolve; });
  const hold = async route => { await gate; await route.continue(); };
  await page.route('**/admin/models', hold);
  await page.route('**/admin/time-periods', hold);
  try {
    await page.goto(routerBase + '/console/routing.html');
    await page.waitForSelector('#cvStage [data-node-id]', { timeout: 20000 });
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app && app.booted && app.tree);
});
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.models.length);
}), 0,
      'catalogue resolved before the test released it');

    /* Palette creation must work while models/periods are still pending. */
    const before = await appSnapshot();
    await page.locator('[data-lib-kind="logic-not"]').click();
    await waitFor(async count => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === count + 1);
}, before.nodes.length,
      { timeout: 4000 });
    assert.equal((await appSnapshot()).nodes.length, before.nodes.length + 1,
      'palette creation was blocked by the pending catalogues');

    /* Type, blur, then let the catalogues settle: the unsaved draft must survive. */
    await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.select('node', 'capability'));
});
    await waitInspectorBranch('node', 'capability');
    const draft = '目录晚到不能清掉这段未保存输入';
    await page.locator('#cvInspectorBody [data-field="title"]').fill(draft);
    await page.locator('#cvInspectorBody [data-field="title"]').evaluate(element => element.blur());
    release();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.models.length > 0 && app.periods.length > 0);
});
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#cvInspectorBody [data-field="title"]').inputValue(), draft,
      'late catalogue refresh wiped an unsaved inspector edit');
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name);
}), BASELINE.name);
} finally {
    release();
    await page.unroute('**/admin/models');
    await page.unroute('**/admin/time-periods');
}
});

test('review-jev-window-save-error-uses-window-message', async () => {
  const detail = 'window save rejected by QA';
  await page.route('**/admin/jev/window', async route => {
    if (route.request().method() === 'PUT') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail }),
});
      return;
}
    await route.continue();
});
  expectHttpStatusError({ url: /\/admin\/jev\/window$/, status: /422/ });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() =>
      document.getElementById('winContext').value === '4096' &&
      document.getElementById('winInput').value === '1024');
    await page.locator('#saveWindow').click();
    await page.waitForFunction(expected =>
      document.getElementById('windowMsg').textContent.includes(expected), detail);
    assert.equal(await page.locator('#saveMsg').textContent(), '');
    assert.equal(expectedHttpStatusErrors.length, 0,
      'the expected window PUT 422 was not observed');

    await page.unroute('**/admin/jev/window');
    const savedResponse = page.waitForResponse(response =>
      response.url().endsWith('/admin/jev/window') &&
      response.request().method() === 'PUT' && response.status() === 200);
    await page.locator('#saveWindow').click();
    assert.equal((await savedResponse).status(), 200);
    await page.waitForFunction(() => document.getElementById('windowMsg').textContent === '');
} finally {
    await page.unroute('**/admin/jev/window');
    resetExpectedHttpStatusErrors();
}
});

await runIfMain(import.meta.url);
