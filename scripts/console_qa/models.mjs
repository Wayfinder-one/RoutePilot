/* RoutePilot 控制台 QA · 模型目录与决策服务用例（catalogue 晚到、JEV 窗口保存错误）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   bootstrap 自检（目录 / 时段 / 决策服务 / 初始策略）是基座用例，留在
   ../console_qa.mjs 的基座自检块里，本模块不再持有。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/models.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  BASELINE,
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

await runIfMain(import.meta.url);
