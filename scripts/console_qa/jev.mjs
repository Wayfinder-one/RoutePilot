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
  /* #jevStatus 是基座的单模块入口标记：目录加载完成后才非空。 */
  await page.waitForFunction(() => document.getElementById('jevStatus').textContent.trim() !== '');
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

await runIfMain(import.meta.url);
