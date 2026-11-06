/* RoutePilot 控制台 QA · 计算日志页用例（logs.html：分阶段计算视图、失败 trace、移动端布局）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/logs.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  S1_TEXT,
  api,
  assertIncludes,
  page,
  routerBase,
  runIfMain,
  test
} from '../console_qa.mjs';

test('calculation-log renders staged process from a real request', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
});
  await page.goto(routerBase + '/console/logs.html');
  await page.waitForSelector('.lg-row');
  await page.locator('.lg-row').first().click();
  await page.waitForSelector('.lg-item.open .lg-rail');

  const open = page.locator('.lg-item.open');
  /* 图 v3 展示契约：只渲染 trace 里实际出现的阶段，阶段 id 即引擎的 phase。 */
  const phases = (await open.locator('.lg-stage-name small').allInnerTexts())
    .map(text => text.split(' · ')[0]);
  assert.deepEqual(phases,
    ['period', 'initial', 'condition', 'jev-wave-1', 'final', 'schedule', 'outcome']);
  assert.equal(await open.locator('.lg-stage').count(), 7);
  assert.equal(await open.locator('.lg-status').count(), 1);
  assertIncludes(await open.innerText(), 'power-target');
  assertIncludes(await open.innerText(), 'qwen3.8-flash-sft');
});

test('calculation-log-phase-order-matches-engine', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
});
  await page.goto(routerBase + '/console/logs.html');
  await page.locator('.lg-row').first().click();
  await page.waitForSelector('.lg-item.open .lg-rail');
  const phases = (await page.locator('.lg-item.open .lg-stage-name small').allInnerTexts())
    .map(text => text.split(' · ')[0]);
  const at = phase => phases.indexOf(phase);

  /* 阶段顺序 = 引擎 trace 的首现顺序，不允许按配置重新排序或合成。 */
  assert.ok(at('initial') >= 0 && at('condition') > at('initial'));
  assert.ok(at('jev-wave-1') > at('condition'));
  assert.ok(at('final') > at('jev-wave-1'));
  assert.ok(at('schedule') > at('final'));
  assert.ok(at('outcome') > at('schedule'));
});

test('calculation-log renders every trace stage inline', async () => {
  await page.goto(routerBase + '/console/logs.html');
  await page.locator('.lg-row').first().click();
  await page.waitForSelector('.lg-item.open .lg-rail');
  const open = page.locator('.lg-item.open');

  /* 阶段全部直接展开，不再有按需展开的槽位折叠区；只有技术事实保留在 <details> 里。 */
  assert.ok(await open.locator('.lg-stage').count() > 0);
  assert.equal(await open.locator('.lg-rail details').count(), 0);
  assert.equal(await open.locator('details.lg-raw').count(), 1);
  assert.equal(await open.locator('details.lg-raw').getAttribute('open'), null);
});

test('calculation-log keeps two request details open together', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored', messages: [{ role: 'user', content: S1_TEXT }], stream: false,
});
  await page.goto(routerBase + '/console/logs.html');
  await page.locator('.lg-row').nth(0).click();
  await page.locator('.lg-row').nth(1).click();
  assert.equal(await page.locator('.lg-item.open').count(), 2);
});

test('calculation-log restored row states that detail is absent', async () => {
  const matcher = '**/admin/requests/recent';
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ requests: [{
      id: 'restored', ts: '2026-11-04T10:00:00+08:00', text: '',
      model: 'fast', outcome_state: 'completed', total_ms: 1200,
      routing_ms: 4, waves: 1,
}] }),
}));
  try {
    await page.goto(routerBase + '/console/logs.html');
    await page.locator('.lg-row').first().click();
    /* 图 v3 不再区分「历史记录」：没有 trace 的记录统一显示 traceEmpty。 */
    const expected = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('logs.traceEmpty'));
});
    assertIncludes(await page.locator('.lg-item.open').innerText(), expected);
    assertIncludes(await page.locator('.lg-item.open .lg-lat').innerText(), '1.20s');
} finally {
    await page.unroute(matcher);
}
});

test('calculation-log separates routing failure from a complete trace', async () => {
  const matcher = '**/admin/requests/recent';
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ requests: [
      {
        id: 'failed', ts: '2026-11-04T10:00:00+08:00', text: '失败请求',
        model: null, outcome_state: 'routing_error', error_class: 'no_available_backend',
        routing_ms: 4, total_ms: 4, trace: [{
          event: 'routing_error', stage: 'schedule',
          error_class: 'no_available_backend', message: 'none',
          phase: 'routing_error',
          label: { zh: '路由中止', en: 'Routing stopped' },
          cell: { zh: '路由失败', en: 'Routing failed' },
          summary: { zh: '路由失败', en: 'Routing failed' },
          facts: [[{ zh: '原因', en: 'Reason' }, 'no_available_backend']],
          tone: 'bad',
}],
},
      {
        id: 'scheduled', ts: '2026-11-04T10:00:01+08:00', text: '完整记录',
        model: 'fast', outcome_state: 'completed', routing_ms: 4, total_ms: 10,
        route_label: { zh: '目标命中', en: 'Target selected' },
        trace: [
          { event: 'final', target_id: 'safe', kind: 'target', candidates: [], priority: 1,
            phase: 'final',
            label: { zh: '目标判定', en: 'Target selected' },
            cell: { zh: '目标命中', en: 'Target matched' },
            summary: { zh: '最终选择目标 safe。', en: 'Selected target safe.' },
            facts: [[{ zh: '目标', en: 'Target' }, 'safe']], tone: 'hit' },
          { event: 'scheduled', model: 'fast', reason: 'target', dry_run: false,
            phase: 'schedule',
            label: { zh: '模型调度', en: 'Model scheduling' },
            cell: 'fast',
            summary: { zh: '最终调度模型 fast。', en: 'Scheduled model fast.' },
            facts: [[{ zh: '模型', en: 'Model' }, 'fast']], tone: 'hit' },
        ],
},
    ] }),
}));
  try {
    await page.goto(routerBase + '/console/logs.html');
    await page.locator('.lg-item[data-id="failed"] .lg-row').click();
    assert.equal(await page.locator('.lg-item[data-id="failed"] .lg-execution').count(), 0);
    /* 服务端 presenter 已给出 phase 与文案；控制台只渲染这些字段。 */
    const routingFailed = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('logs.out.routing'));
});
    assertIncludes(await page.locator('.lg-item[data-id="failed"]').innerText(), routingFailed);
    assert.equal(await page.locator('.lg-item[data-id="failed"] .lg-status.bad').count(), 1);
    await page.locator('.lg-item[data-id="scheduled"] .lg-row').click();
    assertIncludes(await page.locator('.lg-item[data-id="scheduled"]').innerText(), '模型调度');
    assert.equal(await page.locator('.lg-item[data-id="scheduled"] .lg-status.bad').count(), 0);
} finally {
    await page.unroute(matcher);
}
});

test('calculation-log renders the server route label verbatim', async () => {
  const matcher = '**/admin/requests/recent';
  const finalEvent = {
    event: 'final', target_id: 'safe', kind: 'target', candidates: [], priority: 1,
    phase: 'final',
    label: { zh: '目标判定', en: 'Target selected' },
    cell: { zh: '目标命中', en: 'Target matched' },
    summary: { zh: '最终选择目标 safe。', en: 'Selected target safe.' },
    facts: [[{ zh: '目标', en: 'Target' }, 'safe']], tone: 'hit',
};
  const makeRecord = (id, routeLabel) => ({
    id,
    ts: '2026-11-04T10:00:00+08:00',
    text: id,
    model: 'fast',
    target_id: 'safe',
    selection_reason: 'target',
    sticky_hit: 0,
    outcome_state: 'completed',
    routing_ms: 4,
    total_ms: 10,
    waves: 1,
    route_label: routeLabel,
    trace: [finalEvent],
});
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ requests: [
      makeRecord('affinity', { zh: '会话粘性直达', en: 'Session affinity' }),
      makeRecord('fallback', { zh: '默认目标兜底', en: 'Default target fallback' }),
    ] }),
}));

  try {
    await page.goto(routerBase + '/console/logs.html');
    for (const id of ['affinity', 'fallback']) {
      await page.locator(`.lg-item[data-id="${id}"] .lg-row`).click();
}

    const routeOf = id => page.locator(`.lg-item[data-id="${id}"] .lg-route`).innerText();
    /* route_label 由服务端 presenter 生成，控制台不再按 selection_reason 猜测。 */
    assert.equal(await routeOf('affinity'), { zh: '会话粘性直达', en: 'Session affinity' }.zh);
    assert.equal(await routeOf('fallback'), { zh: '默认目标兜底', en: 'Default target fallback' }.zh);
    /* 阶段依据仍来自 presenter 给出的 facts。 */
    assertIncludes(await page.locator('.lg-item[data-id="affinity"]').innerText(), '目标判定');
} finally {
    await page.unroute(matcher);
}
});

test('calculation-log mobile keeps the main conclusion within the viewport', async () => {
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    await page.goto(routerBase + '/console/logs.html');
    await page.locator('.lg-row').first().click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
} finally {
    await page.setViewportSize({ width: 1280, height: 800 });
}
});

await runIfMain(import.meta.url);
