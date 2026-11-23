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

async function openJevDirectory() {
  await page.goto(routerBase + '/console/jev.html');
  /* #jevStatus 是基座的单模块入口标记：目录加载完成后才非空。 */
  await page.waitForFunction(() => document.getElementById('jevStatus').textContent.trim() !== '');
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

test('P0 decision service surfaces impact_confirmation_required instead of silently writing', async () => {
  await openJevDirectory();
  const before = (await api('GET', '/admin/jev/default')).service;
  await page.locator('[data-jev-service-id="default"]').click();
  await page.locator('#jevModel').fill('default-canvas-conflict');
  expectHttpStatusError({ url: /\/admin\/jev\/default$/, status: /409/ });
  try {
    await page.locator('#jevSave').click();
    await page.waitForFunction(() => {
      const text = document.getElementById('jevMsg').textContent;
      return text.includes('确认影响') || text.includes('impact confirmation');
    }, null, { timeout: 8000 });
    const after = (await api('GET', '/admin/jev/default')).service;
    assert.equal(after.model, before.model, 'a 409 response still wrote the referenced service');
    assert.equal(await page.locator('#jevMsg').evaluate(element => element.classList.contains('bad')), true);
  } finally {
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
    await page.waitForFunction(() => document.getElementById('jevStatus').textContent.trim() !== '');
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

test('P0 decision service deletes an unreferenced service after an explicit confirmation', async () => {
  await openJevDirectory();
  const id = 'qa-jev-delete';
  await createJevService({ id, model: 'd1-delete' });
  await page.locator('[data-jev-service-id="' + id + '"]').click();
  /* 删除是两步确认：第一次只上膛，第二次才真正发 DELETE。 */
  await page.locator('#jevDelete').click();
  assert.match(await page.locator('#jevMsg').innerText(), /确认删除|Click again to confirm/);
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
