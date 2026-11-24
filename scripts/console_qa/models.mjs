/* RoutePilot 控制台 QA · 模型目录与多 JEV 服务目录用例（catalogue 晚到、服务目录 CRUD/测试/影响确认、P0 接入模型入口）
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
  assertIncludes,
  ensureBaselineTree,
  expectHttpStatusError,
  mockBase,
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

/* ── 多 JEV 目录页（jev.html）────────────────────────────────────────────
   页面契约：列表项 [data-jev-service-id]；编辑字段 #jevServiceId/#jevBaseUrl/
   #jevModel/#jevKeyEnv/#jevTimeout/#jevInstructions/#jevStateTemplate/
   #jevInputTokens/#jevContextTokens；动作 #jevNew/#jevSave/#jevTest/#jevDelete。
   窗口与密钥都属于“当前选中的服务”：切换服务切换窗口；密钥只进写请求。 */
const JEV_SERVICE_ID = 'qa-jev-model';

/* 真实就绪：初始占位是 '—'（非空！），所以不能只等“非空文本”。
   目录加载完成 = #jevStatus 已换成服务 id / “未配置”，且 #jevMode 不再是 '—'。 */
async function waitJevReady() {
  await page.waitForFunction(() => {
    const status = document.getElementById('jevStatus');
    const mode = document.getElementById('jevMode');
    const idInput = document.getElementById('jevServiceId');
    if (!status || !mode || !idInput) return false;
    const label = status.textContent.trim();
    return label !== '' && label !== '—' && mode.textContent.trim() !== '—';
  }, null, { timeout: 20000 });
}

async function openJevDirectory() {
  await page.goto(routerBase + '/console/jev.html');
  await waitJevReady();
}

async function createJevService({ id, model, key, baseUrl, input = 1024, context = 4096, timeout = 5 }) {
  await page.locator('#jevNew').click();
  await page.locator('#jevServiceId').fill(id);
  await page.locator('#jevBaseUrl').fill(baseUrl || mockBase);
  await page.locator('#jevModel').fill(model);
  await page.locator('#jevTimeout').fill(String(timeout));
  await page.locator('#jevInputTokens').fill(String(input));
  await page.locator('#jevContextTokens').fill(String(context));
  if (key) await page.locator('#jevApiKey').fill(key);
  await page.locator('#jevSave').click();
  await page.waitForSelector('[data-jev-service-id="' + id + '"]');
}

test('P0 decision service directory creates, lists and edits a service', async () => {
  await openJevDirectory();
  /* 旧单服务页面的控件已彻底删除（无双轨残留）。 */
  assert.equal(await page.locator('#saveJev, #saveWindow, #winInput, #winContext, #baseUrl, #testJev').count(), 0,
    'legacy single-service controls are still present');
  assert.equal(await page.locator('#saveWindow').count(), 0);

  await createJevService({ id: JEV_SERVICE_ID, model: 'd1-model-a', key: 'secret-models-sample' });
  const item = page.locator('[data-jev-service-id="' + JEV_SERVICE_ID + '"]');
  const listText = (await item.innerText()).replace(/\s+/g, ' ');
  assertIncludes(listText, JEV_SERVICE_ID);
  assertIncludes(listText, 'd1-model-a');
  assertIncludes(listText, mockBase);
  assertIncludes(listText, '1024 / 4096');
  assert.equal(await page.locator('#jevStatus').innerText(), JEV_SERVICE_ID);
  assert.equal(await page.locator('text=secret-models-sample').count(), 0,
    'the stored key material leaked into the page');

  /* 编辑当前服务：保存只写这一条目录行，列表与状态随之刷新。 */
  await item.click();
  await page.locator('#jevModel').fill('d1-model-b');
  await page.locator('#jevInstructions').fill('多服务编辑后的指令');
  await page.locator('#jevInputTokens').fill('2048');
  await page.locator('#jevSave').click();
  await page.waitForFunction(serviceId => [...document.querySelectorAll('[data-jev-service-id]')]
    .some(element => element.dataset.jevServiceId === serviceId &&
      element.textContent.includes('d1-model-b')), JEV_SERVICE_ID, { timeout: 8000 });
  const saved = (await api('GET', '/admin/jev/' + JEV_SERVICE_ID)).service;
  assert.equal(saved.instructions, '多服务编辑后的指令');
  assert.equal(saved.input_tokens, 2048);
  assert.match((await page.locator('[data-jev-service-id="' + JEV_SERVICE_ID + '"]').innerText()), /d1-model-b/);
  /* 目录响应永远不含 api_key / 明文密钥。 */
  const catalog = await (await fetch(routerBase + '/admin/jev')).text();
  assert.equal(catalog.includes('api_key'), false);
  assert.equal(catalog.includes('secret-models-sample'), false);
});

test('P0 decision service rejects an invalid window before any write', async () => {
  await openJevDirectory();
  await page.locator('[data-jev-service-id="' + JEV_SERVICE_ID + '"]').click();
  const writes = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/jev/' + JEV_SERVICE_ID) writes.push(url.pathname);
  };
  page.on('request', onRequest);
  try {
    /* input > context：本地校验先失败，任何写请求都不许发出。 */
    await page.locator('#jevContextTokens').fill('1024');
    await page.locator('#jevInputTokens').fill('2048');
    await page.locator('#jevSave').click();
    await page.waitForFunction(() => {
      const text = document.getElementById('jevMsg').textContent;
      return text.includes('未保存') || text.includes('Not saved');
    }, null, { timeout: 4000 });
    assert.deepEqual(writes, [], 'an invalid local window still wrote the service');
    assert.equal(await page.locator('#jevMsg').evaluate(element => element.classList.contains('bad')), true);
  } finally {
    page.off('request', onRequest);
  }
});

test('P0 decision service tests the selected service connection with a friendly result', async () => {
  await openJevDirectory();
  await page.locator('[data-jev-service-id="' + JEV_SERVICE_ID + '"]').click();
  await page.locator('#jevTest').click();
  await page.waitForFunction(() => document.getElementById('jevTestResult').dataset.state === 'pass',
    null, { timeout: 20000 });
  assert.match(await page.locator('#jevTestResult').innerText(), /连接成功|Connection successful/);
  /* 原始响应按需展开：默认可见但收起，不能完全藏掉。 */
  assert.equal(await page.locator('#jevTestRawDetails').isVisible(), true);
  assert.equal(await page.locator('#jevTestRawDetails').evaluate(element => element.open), false);
  assert.match(await page.locator('#jevTestRaw').evaluate(element => element.textContent), /selected_key/);
});

test('P0 decision service reports a failed test and preserves the unsaved draft', async () => {
  await openJevDirectory();
  await page.locator('[data-jev-service-id="' + JEV_SERVICE_ID + '"]').click();
  await page.locator('#jevInstructions').fill('测试失败也不许覆盖的草稿');
  const matcher = '**/admin/jev/*/test';
  await page.route(matcher, route => route.fulfill({
    status: 502,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'test rejected by QA' })
  }));
  expectHttpStatusError({ url: new RegExp('/admin/jev/' + JEV_SERVICE_ID + '/test$'), status: /502/ });
  try {
    await page.locator('#jevTest').click();
    await page.waitForFunction(() => document.getElementById('jevTestResult').dataset.state === 'fail',
      null, { timeout: 8000 });
    assert.match(await page.locator('#jevTestResult').innerText(), /test rejected by QA/);
    /* 失败测试是只读操作：草稿不许被服务端值覆盖，旧的原始响应必须清空。 */
    assert.equal(await page.locator('#jevInstructions').inputValue(), '测试失败也不许覆盖的草稿');
    assert.equal(await page.locator('#jevTestRawDetails').isVisible(), false);
    assert.equal(await page.locator('#jevTestRaw').evaluate(element => element.textContent), '');
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
  }
});

test('P0 decision service shows the shared impact list for a referenced update and cancel writes nothing', async () => {
  await openJevDirectory();
  const before = (await api('GET', '/admin/jev/default')).service;
  await page.locator('[data-jev-service-id="default"]').click();
  const changed = 'default-canvas-conflict';
  await page.locator('#jevModel').fill(changed);
  const writes = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/jev/default') {
      writes.push(request.postDataJSON());
    }
  };
  page.on('request', onRequest);
  expectHttpStatusError({ url: /\/admin\/jev\/default$/, status: /409/ });
  try {
    await page.locator('#jevSave').click();
    /* 409 只打开共享影响清单（受影响画布），不出现第二次写请求。 */
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    const modalText = (await page.locator('#impactBody').innerText()).replace(/\s+/g, ' ');
    assert.match(modalText, /电力优先决策组/,
      'the impact list does not name the affected canvas: ' + modalText);
    assert.equal(await page.locator('#impactTrees [data-tree-id]').count() > 0, true,
      'the impact modal listed no affected canvas');
    assert.equal(writes.length, 1, 'the 409 preview triggered a second write');
    assert.equal('impact_confirmation' in writes[0], false,
      'the preview request already carried a confirmation payload');
    /* 取消：不发确认、不写服务。 */
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
    await page.waitForTimeout(250);
    assert.equal(writes.length, 1, 'cancelling the impact dialog still wrote the service');
    const after = (await api('GET', '/admin/jev/default')).service;
    assert.equal(after.model, before.model, 'a 409 response still wrote the referenced service');
    /* 取消后编辑草稿仍在（保存动作可以被重试）。 */
    assert.equal(await page.locator('#jevModel').inputValue(), changed);
  } finally {
    page.off('request', onRequest);
    resetExpectedHttpStatusErrors();
  }
});

test('P0 decision service language switch preserves the unsaved draft', async () => {
  await openJevDirectory();
  await page.locator('[data-jev-service-id="' + JEV_SERVICE_ID + '"]').click();
  await page.locator('#jevInstructions').fill('语言切换保留的草稿');
  await page.locator('#jevInputTokens').fill('3072');
  try {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('en');
    });
    assert.equal(await page.locator('#jevInstructions').inputValue(), '语言切换保留的草稿');
    assert.equal(await page.locator('#jevInputTokens').inputValue(), '3072');
    assert.equal(await page.locator('#jevContextTokens').inputValue(), '4096');
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    });
    assert.equal(await page.locator('#jevInstructions').inputValue(), '语言切换保留的草稿');
    assert.equal(await page.locator('#jevInputTokens').inputValue(), '3072');
  } finally {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    }).catch(() => {});
  }
});

test('P0 decision service document title follows the language toggle', async () => {
  await openJevDirectory();
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
  /* 语言必须在 i18n 模块求值前落到 localStorage：这是真实的「英文会话首屏」。 */
  await page.goto(routerBase + '/console/logs.html');
  await page.evaluate(() => localStorage.setItem('router_lang', 'en'));
  try {
    await page.goto(routerBase + '/console/jev.html');
    await waitJevReady();
    const first = await page.evaluate(() => ({
      lang: document.documentElement.lang,
      text: document.getElementById('jevTestText').value
    }));
    assert.equal(first.lang, 'en', 'the en preset did not reach the page');
    assert.equal(first.text, EN,
      'an English session rendered a non-English custom test default: ' + first.text);
    const switched = await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
      const zh = document.getElementById('jevTestText').value;
      setLang('en');
      return { zh, en: document.getElementById('jevTestText').value };
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

test('P0 decision service deletes an unreferenced service with one click', async () => {
  await openJevDirectory();
  const id = 'qa-jev-delete';
  await createJevService({ id, model: 'd1-delete' });
  await page.locator('[data-jev-service-id="' + id + '"]').click();
  /* 共享影响确认是唯一确认机制：没有画布引用的服务单击即删，不存在本地二次门。 */
  await page.locator('#jevDelete').click();
  await page.waitForFunction(serviceId =>
    document.querySelectorAll('[data-jev-service-id="' + serviceId + '"]').length === 0,
    id, { timeout: 8000 });
  await assert.rejects(() => api('GET', '/admin/jev/' + id), /HTTP 404/);
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

/* ── 共享影响确认（Task 10）────────────────────────────────────────────
   这些用例用真实页面 + 路由拦截断言**请求/响应语义**（不是只看 UI 文案）：
   409 预览只出现一次、取消不发第二次写、确认请求体携带精确哈希与正确的
   replacement 字段、陈旧哈希只提示重载而不盲目重试、provider 删除不可绕过。 */
const IMPACT_HASH_A = 'a'.repeat(64);
const IMPACT_HASH_B = 'b'.repeat(64);
const IMPACT_HASH_C = 'c'.repeat(64);

function impactDetail({ kind, id, trees, replacements = [] }) {
  return {
    message: '该资源仍被画布引用，确认影响后才可写入',
    code: 'impact_confirmation_required',
    resource: { kind, id },
    affected_trees: trees,
    replacements
  };
}

const TARGET_TREE = {
  id: 'power-priority', name: '电力优先决策组', enabled: true,
  document_sha256: IMPACT_HASH_A,
  usage: { targets: ['power-target'], runtime_catalog: true }
};
const CATALOG_TREE = {
  id: 'backup-canvas', name: '备份画布', enabled: false,
  document_sha256: IMPACT_HASH_B,
  usage: { targets: [], runtime_catalog: true }
};

function jsonRoute(handler) {
  return async route => {
    const request = route.request();
    return handler(route, request);
  };
}

test('P0 model edit shows the shared impact list once and cancel sends no confirmation', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow="qwen3-8b"]');
  const writes = [];
  const matcher = '**/admin/models/qwen3-8b';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'PUT') return route.continue();
    const body = request.postData() ? JSON.parse(request.postData()) : {};
    writes.push(body);
    if (writes.length === 1) {
      return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: impactDetail({
          kind: 'model', id: 'qwen3-8b', trees: [TARGET_TREE, CATALOG_TREE]
        }) })
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, model: { id: 'qwen3-8b' }, synced_trees: ['power-priority'] })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/models\/qwen3-8b$/, status: /409/ });
  try {
    await page.locator('[data-editmodel="qwen3-8b"]').click();
    await page.locator('#modelModalSave').click();
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    const text = (await page.locator('#impactBody').innerText()).replace(/\s+/g, ' ');
    assert.match(text, /电力优先决策组/, 'the impact list did not name the affected canvas: ' + text);
    assert.match(text, /power-target/, 'the target usage was not shown: ' + text);
    /* 确认框必须在视口内（内容长时用内部滚动，不把页面撑出横向溢出）。 */
    const box = await page.locator('#impactModal .impact-card').boundingBox();
    const view = page.viewportSize();
    assert.ok(box && box.x >= -1 && box.x + box.width <= view.width + 1 &&
      box.y >= -1 && box.y + box.height <= view.height + 1,
      'the impact dialog leaves the 1440×960 viewport: ' + JSON.stringify(box));
    /* 409 预览之后绝不出现第二次写请求。 */
    await page.waitForTimeout(250);
    assert.equal(writes.length, 1, 'the 409 preview was followed by a second write');
    assert.equal('impact_confirmation' in writes[0], false,
      'the preview request already carried a confirmation payload');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
    await page.waitForTimeout(250);
    assert.equal(writes.length, 1, 'cancel still sent a confirmation write');
    /* 取消后编辑框仍打开，草稿可继续修改/重试。 */
    assert.equal(await page.locator('#modelModal').evaluate(element => element.hidden), false,
      'cancel closed the model editor and lost the draft');
    assert.equal(await page.locator('#cmUpstream').inputValue(), 'qwen3-8b');
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
    await page.locator('[data-close="modelModal"]').click().catch(() => {});
  }
});

test('P0 model delete needs a replacement and sends exact hashes plus replacement_model_id', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow="qwen3-32b"]');
  const writes = [];
  const matcher = '**/admin/models/qwen3-32b';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'DELETE') return route.continue();
    const body = request.postData() ? JSON.parse(request.postData()) : undefined;
    writes.push(body);
    if (writes.length === 1) {
      return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: impactDetail({
          kind: 'model', id: 'qwen3-32b', trees: [TARGET_TREE, CATALOG_TREE],
          replacements: [
            { id: 'qwen3-8b', upstream: 'qwen3-8b', provider: 'local' },
            { id: 'deepseek-v3', upstream: 'deepseek-v3', provider: 'local' }
          ]
        }) })
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, deleted: 'qwen3-32b', replacement_model_id: 'qwen3-8b',
        synced_trees: ['power-priority', 'backup-canvas'] })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/models\/qwen3-32b$/, status: /409/ });
  try {
    await page.locator('[data-delmodel="qwen3-32b"]').click();
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    /* 被 target 引用 → 必须先选替代项；选择前确认按钮禁用。 */
    assert.equal(await page.locator('#impactConfirm').isDisabled(), true,
      'delete confirm was enabled before a replacement was chosen');
    assert.equal(await page.locator('#impactReplacement').count(), 1,
      'the delete impact dialog did not ask for a replacement');
    /* 未选择时点击确认不能发出写请求。 */
    await page.waitForTimeout(150);
    assert.equal(writes.length, 1);
    const treeText = (await page.locator('#impactTrees').innerText()).replace(/\s+/g, ' ');
    assert.match(treeText, /电力优先决策组/);
    assert.match(treeText, /备份画布/);
    await page.locator('#impactReplacement').selectOption('qwen3-8b');
    assert.equal(await page.locator('#impactConfirm').isDisabled(), false,
      'delete confirm stayed disabled after choosing a replacement');
    await page.locator('#impactConfirm').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 8000 });
    assert.equal(writes.length, 2, 'confirm did not retry the delete exactly once');
    assert.equal(writes[0], undefined,
      'the first delete already carried a body/confirmation');
    const retry = writes[1];
    assert.deepEqual(retry.impact_confirmation, {
      trees: [
        { id: 'power-priority', document_sha256: IMPACT_HASH_A },
        { id: 'backup-canvas', document_sha256: IMPACT_HASH_B }
      ]
    }, 'the confirmation did not carry the exact affected hashes');
    assert.equal(retry.replacement_model_id, 'qwen3-8b',
      'the delete did not map the replacement to replacement_model_id');
    assert.equal('replacement_id' in retry, false,
      'the generic replacement_id leaked to the backend');
    /* 同步反馈必须和删除结果合并成一条 toast：两条 toast 会互相覆盖，
       已同步数量 / 「重新加载」提示就丢了。 */
    await page.waitForFunction(() => {
      const toasts = [...document.querySelectorAll('.toast')];
      return toasts.length > 0 && toasts[toasts.length - 1].textContent.includes('已同步');
    }, null, { timeout: 4000 });
    const toasts = await page.locator('.toast').allInnerTexts();
    assert.equal(toasts.length, 1,
      'the delete feedback used two overlapping toasts: ' + JSON.stringify(toasts));
    assert.match(toasts[0], /已同步 2 个受影响画布/,
      'the single delete toast lost the synced-canvas count: ' + toasts[0]);
    assert.match(toasts[0], /重新加载/,
      'the single delete toast lost the reload prompt: ' + toasts[0]);
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
  }
});

test('P0 a stale impact confirmation asks for a reload and never retries blindly', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow="deepseek-v3"]');
  let calls = 0;
  const matcher = '**/admin/models/deepseek-v3';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'DELETE') return route.continue();
    calls += 1;
    if (calls === 1) {
      return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: impactDetail({
          kind: 'model', id: 'deepseek-v3', trees: [TARGET_TREE],
          replacements: [{ id: 'qwen3-8b', upstream: 'qwen3-8b', provider: 'local' }]
        }) })
      });
    }
    return route.fulfill({
      status: 409, contentType: 'application/json',
      body: JSON.stringify({ detail: {
        code: 'graph_conflict',
        actual_document_sha256: IMPACT_HASH_C
      } })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/models\/deepseek-v3$/, status: /409/ });
  expectHttpStatusError({ url: /\/admin\/models\/deepseek-v3$/, status: /409/ });
  try {
    await page.locator('[data-delmodel="deepseek-v3"]').click();
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    await page.locator('#impactReplacement').selectOption('qwen3-8b');
    await page.locator('#impactConfirm').click();
    await page.waitForSelector('#impactConflict', { timeout: 8000 });
    const text = (await page.locator('#impactConflict').innerText()).replace(/\s+/g, ' ');
    assert.match(text, /重新加载|Reload|reload/,
      'a stale hash did not ask the user to reload the canvases: ' + text);
    assert.match(text, /cccccccccccc/,
      'the reload notice did not report the actual current hash: ' + text);
    /* 陈旧哈希后没有第三次盲目重试；确认按钮消失，只能关闭。 */
    await page.waitForTimeout(300);
    assert.equal(calls, 2, 'a stale confirmation retried the write blindly');
    assert.equal(await page.locator('#impactConfirm').evaluate(element => element.hidden), true,
      'the stale dialog still offered the confirm action');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
    await page.waitForTimeout(200);
    assert.equal(calls, 2, 'closing the stale dialog wrote something');
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
  }
});

test('P0 provider delete shows the non-bypassable reference list and cannot be confirmed', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('[data-delprov="local"]');
  let calls = 0;
  const matcher = '**/admin/providers/local';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'DELETE') return route.continue();
    calls += 1;
    return route.fulfill({
      status: 409, contentType: 'application/json',
      body: JSON.stringify({ detail: {
        code: 'provider_referenced',
        message: 'provider 同时被数据库模型与自包含画布引用，请先解除引用',
        models: ['qwen3-8b', 'qwen3-32b'],
        affected_trees: [{
          id: 'power-priority', name: '电力优先决策组', enabled: true,
          document_sha256: IMPACT_HASH_A,
          usage: { runtime_catalog: true, models: { use: ['qwen3-8b'] } }
        }]
      } })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/providers\/local$/, status: /409/ });
  try {
    await page.locator('[data-delprov="local"]').click();
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    const text = (await page.locator('#impactBody').innerText()).replace(/\s+/g, ' ');
    assert.match(text, /不可绕过|cannot be bypassed/,
      'the provider-reference dialog did not state that deletion cannot be bypassed: ' + text);
    assert.match(text, /qwen3-8b/,
      'the provider-reference dialog did not list the database models: ' + text);
    assert.match(text, /电力优先决策组/,
      'the provider-reference dialog did not list the affected canvas: ' + text);
    assert.equal(await page.locator('#impactConfirm').evaluate(element => element.hidden), true,
      'provider deletion incorrectly offered a confirm action');
    await page.waitForTimeout(300);
    assert.equal(calls, 1, 'the blocked provider deletion retried the delete');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
  }
});

test('P0 provider save shows the shared impact list and cancel writes nothing', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('[data-editprov="local"]');
  const writes = [];
  const matcher = '**/admin/providers/local';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'PUT') return route.continue();
    writes.push(request.postData() ? JSON.parse(request.postData()) : {});
    if (writes.length === 1) {
      return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: impactDetail({
          kind: 'provider', id: 'local', trees: [CATALOG_TREE]
        }) })
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, provider: { name: 'local' }, synced_trees: ['backup-canvas'] })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/providers\/local$/, status: /409/ });
  try {
    await page.locator('[data-editprov="local"]').click();
    await page.locator('#provModalSave').click();
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    await page.waitForTimeout(250);
    assert.equal(writes.length, 1, 'the provider 409 preview was followed by a second write');
    assert.equal(await page.locator('#impactReplacement').count(), 0,
      'a provider save incorrectly asked for a replacement');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
    await page.waitForTimeout(250);
    assert.equal(writes.length, 1, 'cancelling the provider impact dialog still wrote');
    assert.equal(await page.locator('#provModal').evaluate(element => element.hidden), false,
      'cancelling the impact dialog closed the provider editor');
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
    await page.locator('[data-close="provModal"]').click().catch(() => {});
  }
});

test('P0 a double-click on model delete opens only one impact dialog and sends one preview', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow="deepseek-v3"]');
  let calls = 0;
  const matcher = '**/admin/models/deepseek-v3';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'DELETE') return route.continue();
    calls += 1;
    /* 250ms 延迟制造「预览请求在途」窗口：重复点击必定落在窗口内。 */
    await new Promise(resolve => setTimeout(resolve, 250));
    return route.fulfill({
      status: 409, contentType: 'application/json',
      body: JSON.stringify({ detail: impactDetail({
        kind: 'model', id: 'deepseek-v3', trees: [CATALOG_TREE]
      }) })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/models\/deepseek-v3$/, status: /409/ });
  expectHttpStatusError({ url: /\/admin\/models\/deepseek-v3$/, status: /409/ });
  try {
    /* 在途窗口内的第二次点击不能再发 DELETE（会覆盖第一个确认框的 settle，
       让第一个 Promise 永远 pending）。 */
    await page.evaluate(() => document.querySelector('[data-delmodel="deepseek-v3"]').click());
    await page.evaluate(() => document.querySelector('[data-delmodel="deepseek-v3"]').click());
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    await page.waitForTimeout(400);
    assert.equal(calls, 1, 'the second delete click sent a duplicate preview request');
    assert.equal(await page.locator('#impactModal').count(), 1, 'more than one impact dialog exists');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
    /* 取消后 guard 必须复位：下一次点击仍能正常发出预览请求。 */
    await page.evaluate(() => document.querySelector('[data-delmodel="deepseek-v3"]').click());
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    assert.equal(calls, 2, 'the delete guard stayed armed after the dialog was cancelled');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
  }
});

test('P0 impact dialog is localized in English and shows the stale-hash error in English', async () => {
  await page.goto(routerBase + '/console/models.html');
  await page.waitForSelector('#modelTable tr[data-mrow="qwen3-32b"]');
  await page.locator('.langtog button[data-lang="en"]').click();
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  let calls = 0;
  const matcher = '**/admin/models/qwen3-32b';
  await page.route(matcher, jsonRoute(async (route, request) => {
    if (request.method() !== 'DELETE') return route.continue();
    calls += 1;
    if (calls === 1) {
      return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: impactDetail({
          kind: 'model', id: 'qwen3-32b', trees: [TARGET_TREE, CATALOG_TREE],
          replacements: [{ id: 'qwen3-8b', upstream: 'qwen3-8b', provider: 'local' }]
        }) })
      });
    }
    return route.fulfill({
      status: 409, contentType: 'application/json',
      body: JSON.stringify({ detail: {
        code: 'graph_conflict',
        actual_document_sha256: IMPACT_HASH_C
      } })
    });
  }));
  expectHttpStatusError({ url: /\/admin\/models\/qwen3-32b$/, status: /409/ });
  expectHttpStatusError({ url: /\/admin\/models\/qwen3-32b$/, status: /409/ });
  try {
    await page.locator('[data-delmodel="qwen3-32b"]').click();
    await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
    const title = (await page.locator('#impactTitle').innerText()).trim();
    const subtitle = (await page.locator('#impactBody .hint').first().innerText())
      .replace(/\s+/g, ' ').trim();
    const confirmLabel = (await page.locator('#impactConfirm').innerText()).trim();
    const cancelLabel = (await page.locator('#impactCancel').innerText()).trim();
    const resourceLine = (await page.locator('#impactResource').innerText()).trim();
    assert.equal(title, 'Confirm model impact and synchronize canvases',
      'the English impact title is wrong: ' + title);
    assert.equal(subtitle,
      'Stored canvases reference this resource. Confirming synchronizes their runtime snapshots ' +
      'to the latest catalog facts; nothing is written before you confirm.',
      'the English impact description is wrong: ' + subtitle);
    assert.equal(confirmLabel, 'Confirm and synchronize',
      'the English confirm label is wrong: ' + confirmLabel);
    assert.equal(cancelLabel, 'Cancel', 'the English cancel label is wrong: ' + cancelLabel);
    assert.equal(resourceLine, 'Target resource: model · qwen3-32b',
      'the impact dialog does not name the operated resource: ' + resourceLine);
    for (const [label, text] of Object.entries({ title, subtitle, confirmLabel, cancelLabel, resourceLine })) {
      assert.doesNotMatch(text, /[\u4e00-\u9fff]/,
        'the English impact dialog text "' + label + '" contains CJK: ' + text);
    }
    /* 英文错误态：陈旧哈希只提示重新加载，不盲目重试。 */
    await page.locator('#impactReplacement').selectOption('qwen3-8b');
    await page.locator('#impactConfirm').click();
    await page.waitForSelector('#impactConflict', { timeout: 8000 });
    const stale = (await page.locator('#impactConflict').innerText()).replace(/\s+/g, ' ');
    assert.match(stale, /This confirmation is stale/,
      'the stale-hash notice is not in English: ' + stale);
    assert.match(stale, /Reload the canvas and re-read the impact list/,
      'the English stale notice does not ask for a reload: ' + stale);
    assert.match(stale, /cccccccccccc/,
      'the English stale notice does not report the actual hash: ' + stale);
    assert.equal((await page.locator('#impactCancel').innerText()).trim(), 'Got it',
      'the English stale dialog close label is wrong');
    assert.equal(await page.locator('#impactConfirm').evaluate(element => element.hidden), true,
      'the English stale dialog still offered the confirm action');
    await page.locator('#impactCancel').click();
    await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 4000 });
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
    await page.locator('.langtog button[data-lang="zh"]').click().catch(() => {});
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN', null, { timeout: 4000 })
      .catch(() => {});
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
          if (target.path.endsWith('/jev.html')) await waitJevReady();
          const size = await page.evaluate(() => ({
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth,
            lang: document.documentElement.lang
          }));
          assert.ok(size.scroll <= size.client + 1,
            `${target.path} overflows horizontally at ${viewport.width}×${viewport.height} (${lang}): ` +
            `${size.scroll} > ${size.client}`);
          assert.equal(size.lang, lang === 'zh' ? 'zh-CN' : 'en');
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
