/* RoutePilot 控制台 QA · 模型目录与决策服务用例（catalogue 晚到、JEV 窗口保存错误、P0 接入模型入口）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   bootstrap 自检（目录 / 时段 / 决策服务 / 初始图）是基座用例，留在
   ../console_qa.mjs 的基座自检块里，本模块不再持有。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/models.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  BASELINE,
  api,
  appSnapshot,
  expectHttpStatusError,
  expectedHttpStatusErrors,
  page,
  resetExpectedHttpStatusErrors,
  routerBase,
  runIfMain,
  test,
  waitFor,
  waitInspectorBranch
} from '../console_qa.mjs';

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
    await page.locator('[data-lib-kind="logic-or"]').click();
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
    /* JEV 页面是「只读档案 + 按需编辑」：窗口字段只在编辑态可见，先进编辑态。 */
    await page.locator('#editConfig').click();
    await page.waitForSelector('#saveWindow', { state: 'visible', timeout: 15000 });
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

test('P0 preset connection hides model ids by default and uses one save action', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow]');
  assert.equal(await page.locator('#addProvider').count(), 0);
  await page.locator('#addModel').click();
  await page.locator('#pmProvider').selectOption('openai');
  const row = page.locator('#pmModels .preset-model').filter({
    has: page.locator('.pm-check[data-mid="gpt-4.1"]')
  });
  await row.locator('.pm-check').check();
  assert.equal(await row.locator('.pm-id').isVisible(), false);
  assert.equal(await page.locator('#pmBase').isVisible(), false);
  await row.locator('.pm-advanced').evaluate(element => { element.open = true; });
  assert.equal(await row.locator('.pm-id').isVisible(), true);
  await row.locator('.pm-id').fill('gpt-4.1-custom');
  assert.equal(await row.locator('.pm-id').inputValue(), 'gpt-4.1-custom');
  await row.locator('.pm-id').fill('gpt-4.1');
  await row.locator('.pm-advanced').evaluate(element => { element.open = false; });
  try {
    await page.locator('#modelModalSave').click();
    await page.waitForSelector('#modelTable tr[data-mrow="gpt-4.1"]');
  } finally {
    await api('DELETE', '/admin/models/gpt-4.1').catch(() => {});
    await api('DELETE', '/admin/providers/openai').catch(() => {});
  }
});


test('P0 custom connection generates a blank model id from upstream', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow]');
  await page.locator('#addModel').click();
  await page.locator('#modelTabs [data-tab="custom"]').click();
  await page.locator('#cmProvider').selectOption('local');
  await page.locator('#cmUpstream').fill('vendor/custom-alpha');
  await page.locator('#cmCapability').fill('适合通用问答。');
  assert.equal(await page.locator('#cmId').isVisible(), false);
  await page.locator('#cmAdvanced').evaluate(element => { element.open = true; });
  await page.locator('#cmId').fill('manual-custom');
  await page.evaluate(async () => {
    const { setLang } = await import('/console/i18n.js');
    setLang('en');
  });
  assert.equal(await page.locator('#cmId').inputValue(), 'manual-custom');
  assert.equal(await page.locator('#cmUpstream').inputValue(), 'vendor/custom-alpha');
  assert.equal(await page.locator('#cmProvider').inputValue(), 'local');
  assert.equal(await page.locator('#cmCapability').inputValue(), '适合通用问答。');
  assert.equal(await page.locator('#cmAdvanced').evaluate(element => element.open), true);
  await page.evaluate(async () => {
    const { setLang } = await import('/console/i18n.js');
    setLang('zh');
  });
  await page.locator('#cmId').fill('');
  try {
    await page.locator('#modelModalSave').click();
    await page.waitForSelector('#modelTable tr[data-mrow="vendor-custom-alpha"]');
  } finally {
    await api('DELETE', '/admin/models/vendor-custom-alpha').catch(() => {});
  }
});

await runIfMain(import.meta.url);
