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

test('P0 decision service saves config and window then tests with friendly output', async () => {
  const calls = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/jev') calls.push('config');
    if (request.method() === 'PUT' && url.pathname === '/admin/jev/window') calls.push('window');
    if (request.method() === 'POST' && url.pathname === '/admin/jev/test') calls.push('test');
  };
  page.on('request', onRequest);
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
    assert.equal(await page.locator('#testCard').count(), 0);
    assert.equal(await page.locator('#saveWindow').count(), 0);
    await page.locator('#editConfig').click();
    assert.equal(await page.locator('#instructions').isVisible(), false);
    assert.equal(await page.locator('#winContext').isVisible(), false);
    await page.locator('#saveJev').click();
    await page.waitForFunction(() => document.getElementById('testOut').dataset.state === 'pass');
    assert.deepEqual(calls, ['config', 'window', 'test']);
    assert.match(await page.locator('#testOut').innerText(), /连接成功|Connection successful/);
    /* The raw payload must be revealed-but-collapsed. Asserting #testRaw alone
       would also pass when the details were never revealed at all. */
    assert.equal(await page.locator('#testRawDetails').isVisible(), true);
    assert.equal(await page.locator('#testRawDetails').evaluate(element => element.open), false);
    /* The successful save leaves edit mode: focus must land on a live control. */
    await page.waitForFunction(() => document.activeElement.id === 'editConfig');
  } finally {
    page.off('request', onRequest);
  }
});

test('P0 decision service reports partial save and skips test after window failure', async () => {
  const calls = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/jev') calls.push('config');
    if (request.method() === 'PUT' && url.pathname === '/admin/jev/window') calls.push('window');
    if (request.method() === 'POST' && url.pathname === '/admin/jev/test') calls.push('test');
  };
  page.on('request', onRequest);
  await page.route('**/admin/jev/window', async route => {
    if (route.request().method() === 'PUT') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'window save rejected by QA' })
      });
      return;
    }
    await route.continue();
  });
  expectHttpStatusError({ url: /\/admin\/jev\/window$/, status: /422/ });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
    await page.locator('#editConfig').click();
    await page.locator('#saveJev').click();
    await page.waitForFunction(() =>
      document.getElementById('saveMsg').textContent.includes('window save rejected by QA'));
    assert.deepEqual(calls, ['config', 'window']);
    assert.match(await page.locator('#saveMsg').innerText(), /连接与提示词已保存|Connection and prompts saved/);
    assert.equal(await page.locator('#editPane').isVisible(), true);
  } finally {
    page.off('request', onRequest);
    await page.unroute('**/admin/jev/window');
    resetExpectedHttpStatusErrors();
  }
});

test('P0 decision service stops after config failure', async () => {
  const calls = [];
  const onRequest = request => {
    const path = new URL(request.url()).pathname;
    if (path === '/admin/jev' && request.method() === 'PUT') calls.push('config');
    if (path === '/admin/jev/window' && request.method() === 'PUT') calls.push('window');
    if (path === '/admin/jev/test' && request.method() === 'POST') calls.push('test');
  };
  page.on('request', onRequest);
  await page.route('**/admin/jev', async route => {
    if (route.request().method() === 'PUT') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'config rejected by QA' })
      });
      return;
    }
    await route.continue();
  });
  expectHttpStatusError({ url: /\/admin\/jev$/, status: /422/ });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
    await page.locator('#editConfig').click();
    await page.locator('#saveJev').click();
    await page.waitForFunction(() =>
      document.getElementById('saveMsg').textContent.includes('config rejected by QA'));
    assert.deepEqual(calls, ['config']);
    /* The reworked copy must say the config was not saved, not just relay the
       server detail (this case already passed at the Task 4 predecessor). */
    assert.match(await page.locator('#saveMsg').innerText(), /配置未保存|Configuration not saved/);
    assert.equal(await page.locator('#editPane').isVisible(), true);
  } finally {
    page.off('request', onRequest);
    await page.unroute('**/admin/jev');
    resetExpectedHttpStatusErrors();
  }
});

test('P0 decision service rejects an invalid window before any write', async () => {
  const calls = [];
  const onRequest = request => {
    const path = new URL(request.url()).pathname;
    if (path === '/admin/jev' && request.method() === 'PUT') calls.push('config');
    if (path === '/admin/jev/window' && request.method() === 'PUT') calls.push('window');
    if (path === '/admin/jev/test' && request.method() === 'POST') calls.push('test');
  };
  page.on('request', onRequest);
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('winContext').value === '4096');
    await page.locator('#editConfig').click();
    /* The advanced section is left closed on purpose: the page itself must
       reveal it when it focuses the offending field. Inputs are set through
       the DOM because a closed <details> hides them from real user actions. */
    assert.equal(await page.locator('#jevAdvanced').evaluate(element => element.open), false);
    await page.locator('#winContext').evaluate(element => {
      element.value = '1024';
      element.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.locator('#winInput').evaluate(element => {
      element.value = '2048';
      element.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.locator('#saveJev').click();
    await page.waitForFunction(() => document.activeElement.id === 'winInput');
    assert.equal(await page.locator('#jevAdvanced').evaluate(element => element.open), true,
      'the invalid window did not reveal the advanced section');
    await page.waitForFunction(() => {
      const text = document.getElementById('saveMsg').textContent;
      return text.includes('未保存：上下文长度校验未通过') ||
        text.includes('Not saved: context length check failed');
    }, null, { timeout: 3000 });
    /* The important one: an invalid local window writes nothing at all. */
    assert.deepEqual(calls, []);
    assert.equal(await page.locator('#editPane').isVisible(), true);
  } finally {
    page.off('request', onRequest);
  }
});

test('P0 decision service reports saved config when connection test fails', async () => {
  const calls = [];
  const onRequest = request => {
    const path = new URL(request.url()).pathname;
    if (path === '/admin/jev' && request.method() === 'PUT') calls.push('config');
    if (path === '/admin/jev/window' && request.method() === 'PUT') calls.push('window');
    if (path === '/admin/jev/test' && request.method() === 'POST') calls.push('test');
  };
  page.on('request', onRequest);
  let failTest = false;
  await page.route('**/admin/jev/test', async route => {
    if (!failTest) {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 502,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'test rejected by QA' })
    });
  });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
    /* A passing standalone retest first reveals the raw details and paints a
       pass badge, so the assertions below cannot pass vacuously (the static
       HTML ships #testRawDetails with `hidden`). */
    await page.locator('#testJev').click();
    await page.waitForFunction(() => document.getElementById('testOut').dataset.state === 'pass');
    assert.equal(await page.locator('#testRawDetails').isVisible(), true);
    assert.match(await page.locator('#testRaw').evaluate(element => element.textContent),
      /selected_key/);
    calls.length = 0;
    failTest = true;
    expectHttpStatusError({ url: /\/admin\/jev\/test$/, status: /502/ });
    await page.locator('#editConfig').click();
    await page.locator('#saveJev').click();
    await page.waitForFunction(() =>
      document.getElementById('saveMsg').textContent.includes('test rejected by QA'));
    assert.deepEqual(calls, ['config', 'window', 'test']);
    assert.match(await page.locator('#saveMsg').innerText(), /配置已保存|Configuration saved/);
    /* data-state is the single source of test status: the stale pass must go. */
    assert.equal(await page.locator('#testOut').getAttribute('data-state'), 'fail');
    assert.match(await page.locator('#testBadge').innerText(), /测试失败|Test failed/);
    assert.equal(await page.locator('#testRawDetails').isVisible(), false);
    assert.equal(await page.locator('#testRaw').evaluate(element => element.textContent), '');
    assert.equal(await page.locator('#editPane').isVisible(), true);
  } finally {
    page.off('request', onRequest);
    await page.unroute('**/admin/jev/test');
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
