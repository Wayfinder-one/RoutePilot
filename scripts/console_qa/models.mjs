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
  ensureBaselineTree,
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
    assert.equal(await page.locator('#jevAdvanced').evaluate(element => element.open), false,
      'the decision-service advanced section must start closed');
    for (const id of ['timeout', 'clearKey', 'instructions', 'stateTemplate',
                      'winContext', 'winInput', 'testText']) {
      assert.equal(await page.locator('#' + id).isVisible(), false,
        `the advanced field #${id} leaked into the default edit view`);
    }
    await page.locator('#saveJev').click();
    await page.waitForFunction(() => document.getElementById('testOut').dataset.state === 'pass');
    assert.deepEqual(calls, ['config', 'window', 'test']);
    assert.match(await page.locator('#testOut').innerText(), /连接成功|Connection successful/);
    /* The raw payload must be revealed-but-collapsed. Asserting #testRaw alone
       would also pass when the details were never revealed at all. */
    assert.equal(await page.locator('#testRawDetails').isVisible(), true);
    assert.equal(await page.locator('#testRawDetails').evaluate(element => element.open), false);
    /* The successful save leaves edit mode: focus must land on a live control. */
    await page.waitForFunction(
      () => document.activeElement.id === 'editConfig',
      null,
      { timeout: 3000 }
    ).catch(() => assert.fail('save-and-test did not return focus to #editConfig'));
  } finally {
    page.off('request', onRequest);
  }
});

test('P0 decision service document title follows the language toggle', async () => {
  await page.goto(routerBase + '/console/jev.html');
  await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
  assert.match(await page.title(), /决策服务/, 'the zh document title is not the page label');
  try {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('en');
    });
    await page.waitForFunction(() => document.title.includes('Decision service'), null, { timeout: 3000 });
    assert.doesNotMatch(await page.title(), /决策服务/,
      'the document title kept the zh page label after switching to en');
    assert.equal(await page.evaluate(() => document.documentElement.lang), 'en');
  } finally {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    });
    await page.waitForFunction(() => document.title.includes('决策服务'), null, { timeout: 3000 });
  }
});

test('P0 decision service paints the custom test default in English on first load', async () => {
  const EN = 'Pick one of the candidates to verify the decision service.';
  const ZH = '请选择一个候选以验证决策服务。';
  /* 语言必须在 i18n 模块求值前落到 localStorage：这是真实的「英文会话首屏」，
     而不是加载后切换一次。 */
  await page.goto(routerBase + '/console/logs.html');
  await page.evaluate(() => localStorage.setItem('router_lang', 'en'));
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
    const first = await page.evaluate(() => ({
      lang: document.documentElement.lang,
      text: document.getElementById('testText').value
    }));
    assert.equal(first.lang, 'en', 'the en preset did not reach the page');
    assert.equal(first.text, EN,
      'an English session rendered a non-English custom test default: ' + first.text);
    /* The language-switch repair path must keep working without the old seed. */
    const switched = await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
      const zh = document.getElementById('testText').value;
      setLang('en');
      return { zh, en: document.getElementById('testText').value };
    });
    assert.equal(switched.zh, ZH, 'the custom test default did not follow the switch to zh');
    assert.equal(switched.en, EN, 'the custom test default did not follow the switch back to en');
  } finally {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    }).catch(() => {});
  }
});

test('P0 decision service paints a running test state while the test request is in flight', async () => {
  let releaseTest = () => {};
  const gate = new Promise(resolve => { releaseTest = resolve; });
  const calls = [];
  let testSeenResolve = () => {};
  const testSeen = new Promise(resolve => { testSeenResolve = resolve; });
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/jev') calls.push('config');
    if (request.method() === 'PUT' && url.pathname === '/admin/jev/window') calls.push('window');
    if (request.method() === 'POST' && url.pathname === '/admin/jev/test') {
      calls.push('test');
      testSeenResolve();
    }
  };
  page.on('request', onRequest);
  await page.route('**/admin/jev/test', async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    await gate;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ selected_key: 'b', duration_ms: 12, probabilities: { a: 0.4, b: 0.6 } })
    });
  });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('baseUrl').value !== '');
    await page.locator('#editConfig').click();
    await page.locator('#saveJev').click();
    await page.waitForFunction(() =>
      document.getElementById('testOut').dataset.state === 'running', null, { timeout: 3000 });
    assert.match(await page.locator('#testBadgeText').innerText(), /测试中|Testing/,
      'the test badge did not announce the in-flight run');
    assert.match(await page.locator('#testOut').innerText(), /测试中|Testing/);
    /* The primary action and every edit control stay disabled while the
       ordered save-and-test pipeline is still running. */
    assert.equal(await page.locator('#saveJev').isDisabled(), true);
    assert.equal(await page.locator('#editConfig').isDisabled(), true);
    assert.equal(await page.locator('#baseUrl').isDisabled(), true);
    await Promise.race([
      testSeen,
      new Promise((_, reject) => setTimeout(() => reject(new Error('the test request never started')), 3000))
    ]);
    assert.deepEqual(calls, ['config', 'window', 'test'],
      'the running test started before config and window were written');
    releaseTest();
    await page.waitForFunction(() => document.getElementById('testOut').dataset.state === 'pass');
    assert.match(await page.locator('#testBadgeText').innerText(), /测试通过|Test passed/);
    assert.equal(await page.locator('#testRawDetails').isVisible(), true);
    assert.equal(await page.locator('#testRawDetails').evaluate(element => element.open), false);
    assert.match(await page.locator('#testRaw').evaluate(element => element.textContent), /selected_key/);
  } finally {
    releaseTest();
    page.off('request', onRequest);
    await page.unroute('**/admin/jev/test');
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
    /* 422 是故意制造的：浏览器那条 console error 必须已被期望消费掉。 */
    assert.equal(expectedHttpStatusErrors.length, 0,
      'the expected window PUT 422 was not observed');

    /* A failed write must not wedge the form: with the injected failure gone,
       the same primary must run the whole chain again and reach success. */
    await page.unroute('**/admin/jev/window');
    await page.locator('#saveJev').click();
    await page.waitForFunction(() => document.getElementById('testOut').dataset.state === 'pass',
      null, { timeout: 15000 });
    assert.equal(await page.locator('#saveJev').isEnabled(), true,
      'the recovery save left #saveJev disabled');
    assert.deepEqual(calls, ['config', 'window', 'config', 'window', 'test'],
      'the retry after a failed window save did not run config → window → test again');
    assert.match(await page.locator('#saveMsg').innerText(), /已保存，连接测试通过|Saved, connection test passed/,
      'the successful retry did not paint the success copy');
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

test('P0 decision service cancel keeps saved config but discards failed window draft', async () => {
  const beforeConfig = await api('GET', '/admin/jev');
  const beforeWindow = await api('GET', '/admin/jev/window');
  const changedModel = beforeConfig.config.model + '-saved-before-window-failure';
  await page.route('**/admin/jev/window', async route => {
    if (route.request().method() === 'PUT') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'window failed after config saved' })
      });
      return;
    }
    await route.continue();
  });
  expectHttpStatusError({ url: /\/admin\/jev\/window$/, status: /422/ });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('model').value !== '');
    await page.locator('#editConfig').click();
    await page.locator('#model').fill(changedModel);
    await page.locator('#jevAdvanced').evaluate(element => { element.open = true; });
    await page.locator('#winContext').fill('8192');
    await page.locator('#winInput').fill('2048');
    await page.locator('#saveJev').click();
    await page.waitForFunction(() =>
      document.getElementById('saveMsg').textContent.includes('window failed after config saved'));

    const savedConfig = await api('GET', '/admin/jev');
    assert.equal(savedConfig.config.model, changedModel, 'config did not reach the server');
    await page.locator('#editConfig').click();
    assert.equal(await page.locator('#roModel').textContent(), changedModel,
      'cancel restored the pre-edit model instead of the saved model');

    await page.locator('#editConfig').click();
    assert.equal(await page.locator('#model').inputValue(), changedModel);
    assert.equal(await page.locator('#winContext').inputValue(),
      String(beforeWindow.context_tokens), 'the failed window draft became the cancel baseline');
    assert.equal(await page.locator('#winInput').inputValue(), String(beforeWindow.input_tokens));
  } finally {
    await page.unroute('**/admin/jev/window');
    resetExpectedHttpStatusErrors();
  }
});

test('P0 decision service cancel after test failure preserves both saved stages', async () => {
  const beforeConfig = await api('GET', '/admin/jev');
  const changedModel = beforeConfig.config.model + '-saved-before-test-failure';
  await page.route('**/admin/jev/test', async route => {
    await route.fulfill({
      status: 502,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'test failed after both saves' })
    });
  });
  expectHttpStatusError({ url: /\/admin\/jev\/test$/, status: /502/ });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() => document.getElementById('model').value !== '');
    await page.locator('#editConfig').click();
    await page.locator('#model').fill(changedModel);
    await page.locator('#jevAdvanced').evaluate(element => { element.open = true; });
    await page.locator('#winContext').fill('8192');
    await page.locator('#winInput').fill('2048');
    await page.locator('#saveJev').click();
    await page.waitForFunction(() =>
      document.getElementById('saveMsg').textContent.includes('test failed after both saves'));

    await page.locator('#editConfig').click();
    assert.equal(await page.locator('#roModel').textContent(), changedModel);
    await page.locator('#editConfig').click();
    assert.equal(await page.locator('#model').inputValue(), changedModel);
    assert.equal(await page.locator('#winContext').inputValue(), '8192');
    assert.equal(await page.locator('#winInput').inputValue(), '2048');
    const savedWindow = await api('GET', '/admin/jev/window');
    assert.equal(savedWindow.context_tokens, 8192);
    assert.equal(savedWindow.input_tokens, 2048);
  } finally {
    await page.unroute('**/admin/jev/test');
    resetExpectedHttpStatusErrors();
  }
});

test('P0 preset connection hides model ids by default and uses one save action', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow]');
  assert.equal(await page.locator('#addProvider').count(), 0);
  await page.locator('#addModel').click();
  assert.equal(await page.locator('#pmAdvanced').evaluate(element => element.open), false,
    'the preset connection advanced section must start closed');
  assert.equal(await page.locator('#pmAdvancedBody').isVisible(), false);
  assert.equal(await page.locator('#pmBase').isVisible(), false,
    'the read-only base URL leaked into the default preset view');
  await page.locator('#pmProvider').selectOption('openai');
  const row = page.locator('#pmModels .preset-model').filter({
    has: page.locator('.pm-check[data-mid="gpt-4.1"]')
  });
  await row.locator('.pm-check').check();
  assert.equal(await row.locator('.pm-id').isVisible(), false);
  assert.equal(await page.locator('#pmBase').isVisible(), false);
  await page.locator('#pmAdvanced > summary').click();
  assert.equal(await page.locator('#pmBase').isVisible(), true,
    'expanding the preset advanced section did not expose the base URL');
  assert.equal(await page.locator('#pmBase').getAttribute('readonly'), '');
  assert.equal(await page.locator('#pmAdvancedBody .keyline').isVisible(), true,
    'the env-auth line is missing from the expanded advanced section');
  assert.match((await page.locator('#pmAdvancedBody .keyline').innerText()).trim(), /\S/);
  await page.locator('#pmAdvanced > summary').click();
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
  assert.equal(await page.locator('#cmAdvanced').evaluate(element => element.open), false,
    'the custom connection advanced section must start closed');
  assert.equal(await page.locator('#cmId').isVisible(), false);
  assert.equal(await page.locator('#reasonEditor').isVisible(), false,
    'the reasoning declaration leaked into the default custom view');
  await page.locator('#cmAdvanced').evaluate(element => { element.open = true; });
  assert.equal(await page.locator('#cmId').isVisible(), true);
  assert.equal(await page.locator('#reasonEditor').isVisible(), true,
    'expanding advanced did not expose the reasoning declaration');
  assert.ok(await page.locator('#reasonEditor [data-rl]').count() > 0,
    'the expanded reasoning declaration has no level controls');
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

test('P0 acceptance · changed console pages do not overflow at 1440×960 or 1280×800 in either language', async () => {
  await ensureBaselineTree();
  const targets = [
    { path: '/console/routing.html', ready: '#cvStage [data-node-id]' },
    { path: '/console/models.html', ready: '#modelTable tr[data-mrow]' },
    { path: '/console/jev.html', ready: '#jevStatus' },
    { path: '/console/logs.html', ready: '#refresh' }
  ];
  const viewports = [
    { width: 1440, height: 960 },
    { width: 1280, height: 800 }
  ];
  try {
    for (const lang of ['zh', 'en']) {
      await page.goto(routerBase + '/console/logs.html');
      await page.evaluate(async next => {
        const { setLang } = await import('/console/i18n.js');
        setLang(next);
      }, lang);
      for (const viewport of viewports) {
        await page.setViewportSize(viewport);
        for (const target of targets) {
          await page.goto(routerBase + target.path);
          await page.waitForSelector(target.ready, { timeout: 20000 });
          if (target.path.endsWith('/jev.html')) {
            await page.waitForFunction(() => document.getElementById('jevStatus').textContent.trim() !== '');
          }
          const size = await page.evaluate(() => ({
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth,
            lang: document.documentElement.lang
          }));
          assert.ok(size.scroll <= size.client + 1,
            `${target.path} overflows horizontally at ${viewport.width}×${viewport.height} (${lang}): ` +
            `${size.scroll} > ${size.client}`);
          assert.equal(size.lang, lang === 'zh' ? 'zh-CN' : 'en');
          if (target.path.endsWith('/routing.html')) {
            await page.locator('#cvMoreBtn').click();
            const menu = await page.evaluate(() => {
              const details = document.querySelector('#cvMore');
              const box = document.querySelector('#cvMore .rtc-more-menu').getBoundingClientRect();
              return { open: details.open,
                left: box.left, right: box.right, top: box.top, bottom: box.bottom,
                boxWidth: box.width, boxHeight: box.height,
                width: window.innerWidth, height: window.innerHeight };
            });
            assert.equal(menu.open, true,
              `the overflow menu did not open at ${viewport.width} (${lang})`);
            /* 全零矩形也能满足下面的边界比较，所以先排除零尺寸菜单。 */
            assert.ok(menu.boxWidth > 0 && menu.boxHeight > 0,
              `the overflow menu has no size at ${viewport.width} (${lang})`);
            assert.ok(menu.left >= -1 && menu.right <= menu.width + 1,
              `the overflow menu leaves the viewport horizontally at ${viewport.width} (${lang})`);
            assert.ok(menu.top >= -1 && menu.bottom <= menu.height + 1,
              `the overflow menu leaves the viewport vertically at ${viewport.width} (${lang})`);
            await page.keyboard.press('Escape');
          }
        }
      }
    }
  } finally {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    }).catch(() => {});
    await page.setViewportSize({ width: 1440, height: 960 });
  }
});

await runIfMain(import.meta.url);
