/* RoutePilot 控制台 QA · 多 JEV 服务目录页（新建 / 选择 / 独立窗口 / 语言草稿 / 删除）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、多 JEV bootstrap、
   mock、浏览器、管理面 API 与 teardown 全部由 ../console_qa.mjs 提供
   （同一进程内单页顺序执行）。
   入口：`node scripts/console_qa/jev.mjs`（单独运行）或
         `npm run check:console`（全量；基座在 main() 里显式 import 本模块）。

   页面契约（本任务的实现依据）：
   - `#jevNew` 开启新服务草稿；`#jevServiceId/#jevBaseUrl/#jevModel/#jevKeyEnv/
      #jevTimeout/#jevInstructions/#jevStateTemplate/#jevInputTokens/
      #jevContextTokens` 是编辑字段；`#jevSave/#jevTest/#jevDelete` 是动作；
   - 列表项带 `[data-jev-service-id]`，按服务 id 排序；
   - 窗口属于服务：切换服务必须切换 `#jevInputTokens`；
   - 密钥只进写请求：任何响应/列表/重渲染都不出现密钥材料，只展示密钥状态。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  api,
  expectHttpStatusError,
  page,
  resetExpectedHttpStatusErrors,
  routerBase,
  runIfMain,
  test
} from '../console_qa.mjs';

const SECRET = 'secret-sample';

async function openPage() {
  await page.goto(routerBase + '/console/jev.html');
  /* 真实就绪：初始占位是 '—'（非空！），只等“非空文本”会在目录数据回来之前
     就开始点击。目录加载完成 = #jevStatus 是服务 id / “未配置”，且
     #jevMode 不再是 '—'（编辑中 / 新建）。 */
  await page.waitForFunction(() => {
    const status = document.getElementById('jevStatus');
    const mode = document.getElementById('jevMode');
    const idInput = document.getElementById('jevServiceId');
    if (!status || !mode || !idInput) return false;
    const label = status.textContent.trim();
    return label !== '' && label !== '—' && mode.textContent.trim() !== '—';
  }, null, { timeout: 20000 });
}

/* 计划里的 createJev 契约（adapted）：新建 → 填字段 → 保存 → 等列表项出现。 */
async function createJev({ id, model, input, key, baseUrl }) {
  await page.locator('#jevNew').click();
  await page.locator('#jevServiceId').fill(id);
  await page.locator('#jevBaseUrl').fill(baseUrl || 'http://' + id + '.test');
  await page.locator('#jevModel').fill(model);
  /* 失败的连接测试不能拖 10s：秒级超时让“测试失败仍保留草稿”用例保持廉价。 */
  await page.locator('#jevTimeout').fill('1');
  await page.locator('#jevInputTokens').fill(String(input));
  await page.locator('#jevContextTokens').fill('4096');
  if (key) await page.locator('#jevApiKey').fill(key);
  await page.locator('#jevSave').click();
  await page.waitForSelector('[data-jev-service-id="' + id + '"]');
  await page.waitForFunction(serviceId =>
    document.querySelector('[data-jev-service-id="' + serviceId + '"]').classList.contains('is-on'),
    id);
}

async function serviceIds() {
  return page.locator('[data-jev-service-id]').evaluateAll(
    elements => elements.map(element => element.dataset.jevServiceId));
}

test('creates and selects two JEV services with independent windows', async () => {
  await openPage();
  await createJev({ id: 'jev-a', model: 'd1-a', input: 1024, key: SECRET });
  await createJev({ id: 'jev-b', model: 'd1-b', input: 2048 });

  const ids = await serviceIds();
  assert.deepEqual(ids, ids.slice().sort(), 'the service list is not sorted by id');
  assert.ok(ids.includes('jev-a') && ids.includes('jev-b'),
    'the two created services are missing from the directory: ' + ids.join(','));

  await page.locator('[data-jev-service-id="jev-a"]').click();
  assert.equal(await page.locator('#jevInputTokens').inputValue(), '1024');
  assert.equal(await page.locator('#jevContextTokens').inputValue(), '4096');
  assert.equal(await page.locator('#jevServiceId').inputValue(), 'jev-a');

  await page.locator('[data-jev-service-id="jev-b"]').click();
  assert.equal(await page.locator('#jevInputTokens').inputValue(), '2048');
  assert.equal(await page.locator('#jevServiceId').inputValue(), 'jev-b');

  /* 密钥只进写请求：页面文本、列表重渲染与目录响应都不能带出密钥材料。 */
  assert.equal(await page.locator('text=' + SECRET).count(), 0,
    'the stored key material leaked into the page');
  await page.locator('[data-jev-service-id="jev-a"]').click();
  assert.equal(await page.locator('#jevApiKey').inputValue(), '',
    'the stored key was rendered back into the key input');
  const catalog = await (await fetch(routerBase + '/admin/jev')).text();
  assert.equal(catalog.includes(SECRET), false, 'GET /admin/jev returned the stored key material');
});

test('language switch and a failed connection test preserve the current service draft', async () => {
  await openPage();
  await page.locator('[data-jev-service-id="jev-b"]').click();
  await page.locator('#jevInstructions').fill('未保存的多服务提示词');

  /* 语言切换只重绘文案：编辑器里的未保存草稿必须原样保留。 */
  await page.locator('.langtog button[data-lang="en"]').click();
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  assert.equal(await page.locator('#jevInstructions').inputValue(), '未保存的多服务提示词');
  assert.equal(await page.locator('#jevInputTokens').inputValue(), '2048');
  /* 密钥保留提示必须说明“仅当地址与模型不变”：英文页不能出现中文标点/旧文案。 */
  const enHint = (await page.locator('#jevKeyHint').innerText()).replace(/\s+/g, ' ');
  assert.match(enHint, /base URL/i, 'the key-retention hint does not mention the base URL: ' + enHint);
  assert.match(enHint, /model/i, 'the key-retention hint does not mention the model: ' + enHint);
  assert.doesNotMatch(enHint, /[、，。；：（）]/, 'the English key hint contains Chinese punctuation: ' + enHint);

  try {
    /* jev-b.test 不可达：真实 /admin/jev/{id}/test 返回 502。 */
    expectHttpStatusError({ url: /\/admin\/jev\/jev-b\/test$/, status: /502/ });
    await page.locator('#jevTest').click();
    await page.waitForFunction(
      () => document.getElementById('jevTestResult').dataset.state === 'fail',
      null, { timeout: 20000 });
    assert.match(await page.locator('#jevTestResult').innerText(), /测试失败|Test failed/);
    /* 失败测试是只读操作：草稿与窗口都不许被服务端值覆盖。 */
    assert.equal(await page.locator('#jevInstructions').inputValue(), '未保存的多服务提示词');
    assert.equal(await page.locator('#jevInputTokens').inputValue(), '2048');
    assert.equal(await page.locator('#jevContextTokens').inputValue(), '4096');
  } finally {
    resetExpectedHttpStatusErrors();
    await page.locator('.langtog button[data-lang="zh"]').click();
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
  }
});

test('list shows key state without key material and delete removes a fresh service', async () => {
  await openPage();
  /* 独立创建一个带密钥的新服务（不依赖上一条用例的残留状态）。 */
  await createJev({ id: 'jev-key-state', model: 'd1-key', input: 1024, key: SECRET });
  const keyText = await page.locator('[data-jev-service-id="jev-key-state"] .pill').innerText();
  assert.match(keyText, /密钥已存|Key stored/,
    'the list does not report the stored-key state: ' + keyText);
  assert.equal(await page.locator('text=' + SECRET).count(), 0,
    'the stored key material leaked into the list');

  /* 未被任何画布引用的新服务可直接删除（两步确认）。 */
  await page.locator('#jevDelete').click();
  assert.match(await page.locator('#jevMsg').innerText(), /再次点击确认删除|Click again to confirm/);
  await page.locator('#jevDelete').click();
  await page.waitForFunction(() =>
    document.querySelectorAll('[data-jev-service-id="jev-key-state"]').length === 0,
    null, { timeout: 8000 });
  assert.match(await page.locator('#jevMsg').innerText(), /已删除|Deleted/);
});

/* ── Task 10 共享影响确认 + 测试运行态 ──────────────────────────────────
   真实请求/响应断言：延迟的 test 请求期间按钮禁用；删除的本地上膛状态在保存
   后复位；409 预览取消不写；确认请求体带精确哈希与 replacement_jev_id。 */
const IMPACT_HASH_D = 'd'.repeat(64);
const IMPACT_HASH_E = 'e'.repeat(64);

async function waitImpactOpen() {
  await page.waitForSelector('#impactModal:not([hidden])', { timeout: 8000 });
}
async function waitImpactClosed() {
  await page.waitForFunction(() => document.getElementById('impactModal').hidden, null, { timeout: 8000 });
}

test('Task 10 · a slow connection test paints the running state and disables service actions', async () => {
  await openPage();
  await page.locator('[data-jev-service-id="default"]').click();
  const matcher = '**/admin/jev/default/test';
  await page.route(matcher, async route => {
    /* 900ms 延迟：足够观察到 running 状态与按钮禁用。 */
    await new Promise(resolve => setTimeout(resolve, 900));
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ selected_key: 'd1-2b', duration_ms: 12, model: 'd1-2b' })
    });
  });
  try {
    await page.locator('#jevTest').click();
    await page.waitForFunction(
      () => document.getElementById('jevTestResult').dataset.state === 'running',
      null, { timeout: 4000 });
    for (const selector of ['#jevSave', '#jevTest', '#jevDelete']) {
      assert.equal(await page.locator(selector).isDisabled(), true,
        selector + ' was not disabled while the connection test was in flight');
    }
    await page.waitForFunction(
      () => document.getElementById('jevTestResult').dataset.state === 'pass',
      null, { timeout: 8000 });
    for (const selector of ['#jevSave', '#jevTest', '#jevDelete']) {
      assert.equal(await page.locator(selector).isDisabled(), false,
        selector + ' stayed disabled after the connection test finished');
    }
    assert.equal(await page.locator('#jevTestRawDetails').isVisible(), true);
    assert.match(await page.locator('#jevTestRaw').evaluate(element => element.textContent), /selected_key/);
    /* 测试结果属于当前服务：切换服务必须复位（不显示上一个服务的结果）。 */
    await page.locator('[data-jev-service-id="jev-secondary"]').click();
    const switched = await page.evaluate(() => ({
      state: document.getElementById('jevTestResult').dataset.state,
      raw: document.getElementById('jevTestRaw').textContent,
      rawHidden: document.getElementById('jevTestRawDetails').hidden,
      identity: document.getElementById('jevStatus').textContent.trim()
    }));
    assert.equal(switched.identity, 'jev-secondary');
    assert.equal(switched.state, 'untested',
      'the previous service connection-test result leaked into the new service');
    assert.equal(switched.rawHidden, true, 'the previous raw response stayed visible');
    assert.equal(switched.raw, '', 'the previous raw response leaked into the new service');
  } finally {
    await page.unroute(matcher);
  }
});

test('Task 10 · JEV delete resets the delete arm, then confirms exact hashes with replacement_jev_id', async () => {
  await openPage();
  const id = 'qa-jev-impact';
  await createJev({ id, model: 'd1-impact', input: 1024 });
  /* 先上膛再保存：保存成功必须复位“确认删除”，否则一次点击就能误删。 */
  await page.locator('#jevDelete').click();
  assert.match(await page.locator('#jevDelete').innerText(), /确认删除|Confirm delete/);
  await page.locator('#jevSave').click();
  await page.waitForFunction(() => document.getElementById('jevSave').disabled === false,
    null, { timeout: 8000 });
  assert.doesNotMatch(await page.locator('#jevDelete').innerText(), /确认删除|Confirm delete/,
    'the delete button kept the armed label after a successful save');
  assert.equal(await page.locator('#jevTestResult').evaluate(element => element.dataset.state), 'untested',
    'a save did not reset the per-service test result');

  const writes = [];
  const matcher = '**/admin/jev/' + id;
  await page.route(matcher, async route => {
    const request = route.request();
    if (request.method() !== 'DELETE') return route.continue();
    writes.push(request.postData() ? JSON.parse(request.postData()) : undefined);
    if (writes.length === 1) {
      return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: {
          message: '该资源仍被画布引用，确认影响后才可写入',
          code: 'impact_confirmation_required',
          resource: { kind: 'jev', id },
          affected_trees: [{
            id: 'power-priority', name: '电力优先决策组', enabled: true,
            document_sha256: IMPACT_HASH_D, usage: { jev: id }
          }],
          replacements: [
            { id: 'default', base_url: routerBase, model: 'd1-2b',
              input_tokens: 1024, context_tokens: 4096 },
            { id: 'jev-secondary', base_url: routerBase, model: 'd1-2b',
              input_tokens: 2048, context_tokens: 8192 }
          ]
        } })
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ deleted: id, replacement_jev_id: 'default',
        synced_trees: ['power-priority'] })
    });
  });
  expectHttpStatusError({ url: new RegExp('/admin/jev/' + id + '$'), status: /409/ });
  try {
    /* 复位后的第一次点击只上膛，不发 DELETE。 */
    await page.locator('#jevDelete').click();
    assert.match(await page.locator('#jevMsg').innerText(), /再次点击确认删除|Click again to confirm/);
    await page.waitForTimeout(200);
    assert.equal(writes.length, 0, 'the first delete click already sent a DELETE');
    await page.locator('#jevDelete').click();
    await waitImpactOpen();
    assert.equal(writes.length, 1, 'the 409 preview was followed by a second write');
    const modalText = (await page.locator('#impactBody').innerText()).replace(/\s+/g, ' ');
    assert.match(modalText, /电力优先决策组/, 'the impact list did not name the affected canvas: ' + modalText);
    /* JEV 被引用时删除必须选替代服务：选择前确认按钮禁用。 */
    assert.equal(await page.locator('#impactConfirm').isDisabled(), true,
      'JEV delete confirm was enabled before a replacement was chosen');
    await page.locator('#impactReplacement').selectOption('default');
    assert.equal(await page.locator('#impactConfirm').isDisabled(), false,
      'JEV delete confirm stayed disabled after choosing a replacement');
    await page.locator('#impactConfirm').click();
    await waitImpactClosed();
    assert.equal(writes.length, 2, 'confirm did not retry the delete exactly once');
    const retry = writes[1];
    assert.equal(writes[0], undefined,
      'the first delete already carried a body/confirmation payload');
    assert.deepEqual(retry.impact_confirmation, {
      trees: [{ id: 'power-priority', document_sha256: IMPACT_HASH_D }]
    }, 'the JEV confirmation did not carry the exact affected hash');
    assert.equal(retry.replacement_jev_id, 'default',
      'the JEV delete did not map the replacement to replacement_jev_id');
    assert.equal('replacement_id' in retry, false,
      'the generic replacement_id leaked to the JEV backend');
    await page.waitForFunction(
      serviceId => document.querySelectorAll('[data-jev-service-id="' + serviceId + '"]').length === 0,
      id, { timeout: 8000 });
  } finally {
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
    await api('DELETE', '/admin/jev/' + id).catch(() => {});
  }
});

await runIfMain(import.meta.url);
