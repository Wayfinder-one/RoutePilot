/* RoutePilot 控制台 QA · 计算日志页（Task 9 · logs.html）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、
   浏览器与 teardown 全部由 ../console_qa.mjs 提供。覆盖：
   * 真实路由请求的列表行与详情：JEV 调用列（0/1）、策略/版本/命中规则
     事实行、一个 JEV 区块、按后端顺序的逐条规则、最终目标与调度模型；
   * 固定模型直连记录：JEV 调用 0，无 JEV/答案/规则区块，空轨迹态；
   * 筛选只读 jev_calls / trace 事件；SQLite 恢复行统一显示空轨迹，
     绝不编造请求文本或过程。
   入口：`node scripts/console_qa.mjs`（全量）或
         `QA_ONLY=log node scripts/console_qa.mjs`；也可单独执行：
         `node scripts/console_qa/logs.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  POLICY_ID,
  S1_TEXT,
  api,
  assertIncludes,
  page,
  routerBase,
  runIfMain,
  test,
} from '../console_qa.mjs';

async function openLogs() {
  await page.goto(routerBase + '/console/logs.html');
  await page.waitForSelector('.lg-row', { timeout: 15000 });
}

async function openFirstDetail() {
  await page.locator('.lg-row').first().click();
  await page.waitForSelector('.lg-item.open .lg-expand', { timeout: 15000 });
  return page.locator('.lg-item.open');
}

test('calculation log shows policy facts, JEV calls and ordered rules', async () => {
  /* 页面源里不允许再出现轮次/波次表头：列固定为 时间/请求/JEV 调用/模型/耗时。 */
  const html = await (await fetch(routerBase + '/console/logs.html')).text();
  assert.equal(html.includes('轮次'), false, 'the logs page still shows the wave-era rounds column');
  assert.equal(html.includes('logs.path'), false, 'the wave-era path column is still declared');
  assert.ok(html.includes('logs.jevCalls'), 'the JEV-calls column header is missing');

  /* 一条真实路由请求：一次 JEV + 逐条规则 + 调度。 */
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
  });
  await openLogs();

  const row = page.locator('.lg-row').first();
  assert.equal(await row.locator('.lg-jev').innerText(), '1');
  assertIncludes(await row.innerText(), 'qwen3.8-flash-sft');

  const open = await openFirstDetail();
  const detail = await open.innerText();
  /* 详情事实行读取记录字段：策略 / 策略版本 / JEV 调用 / 命中规则。 */
  assertIncludes(detail, POLICY_ID);
  assertIncludes(detail, 'JEV 调用');
  assertIncludes(detail, '1');
  assertIncludes(detail, '电力业务优先');
  /* trace 区块：恰一个 JEV / 答案 / 规则 / 最终目标 / 调度 / 执行结果。 */
  assert.equal(await open.locator('[data-sec="input"]').count(), 1);
  assert.equal(await open.locator('[data-sec="jev"]').count(), 1);
  assert.equal(await open.locator('[data-sec="questions"]').count(), 1);
  assert.equal(await open.locator('[data-sec="rules"]').count(), 1);
  assert.equal(await open.locator('[data-sec="final"]').count(), 1);
  assert.equal(await open.locator('[data-sec="schedule"]').count(), 1);
  assert.equal(await open.locator('[data-sec="outcome"]').count(), 1);
  assertIncludes(await open.locator('[data-sec="jev"]').innerText(), '3 个问题');
  assertIncludes(await open.locator('[data-sec="final"]').innerText(), 'power');
  assertIncludes(await open.locator('[data-sec="schedule"]').innerText(), 'qwen3.8-flash-sft');
  assertIncludes(await open.locator('[data-sec="outcome"]').innerText(), '已完成');
  /* 规则按后端 trace 顺序渲染，停在首条命中（后续规则未求值）。 */
  const rows = await open.locator('.lg-rule').allInnerTexts();
  assert.equal(rows.length, 4, 'the trace must keep evaluated and skipped rules');
  assert.ok(rows[0].indexOf('对抗请求优先走安全目标') >= 0 && rows[0].indexOf('未命中') >= 0);
  assert.ok(rows[1].indexOf('电力业务优先') >= 0);
  assert.ok(rows[1].indexOf('命中') >= 0 && rows[1].indexOf('未命中') < 0);
  assert.ok(rows[2].indexOf('未求值') >= 0 && rows[2].indexOf('未命中') < 0,
    'a skipped rule must render 未求值, not 未命中');
  assert.ok(rows[3].indexOf('未求值') >= 0 && rows[3].indexOf('未命中') < 0);
  assertIncludes(detail, '首条命中后停止');
});

test('a direct model record shows JEV calls 0 and no policy sections', async () => {
  await api('POST', '/admin/test/chat/completions', {
    model: 'qwen3-8b',
    messages: [{ role: 'user', content: '固定模型直连，不经过策略。' }],
    stream: false,
  });
  await openLogs();

  const row = page.locator('.lg-row').first();
  assert.equal(await row.locator('.lg-jev').innerText(), '0');
  assertIncludes(await row.innerText(), 'qwen3-8b');

  const open = await openFirstDetail();
  const detail = await open.innerText();
  assertIncludes(detail, '固定模型测试');
  assert.equal(await open.locator('[data-sec="jev"]').count(), 0);
  assert.equal(await open.locator('[data-sec="questions"]').count(), 0);
  assert.equal(await open.locator('[data-sec="rules"]').count(), 0);
  assert.equal(await open.locator('[data-sec="final"]').count(), 0);
  assert.equal(await open.locator('[data-sec="schedule"]').count(), 0);
  /* 直连记录没有路由过程：只有输入文本与执行结果，绝不编造 JEV/规则。 */
  assert.equal(await open.locator('[data-sec="input"]').count(), 1);
  assertIncludes(detail, '固定模型直连，不经过策略。');
  assertIncludes(await open.locator('[data-sec="outcome"]').innerText(), '已完成');
  const facts = await open.locator('.lg-policyfacts').innerText();
  assert.ok(facts.indexOf('—') >= 0, 'the matched-rule fact must render a dash');
});

test('filters read jev_calls and trace events; restored rows keep the empty state', async () => {
  const matcher = '**/admin/requests/recent';
  const revision = 'f'.repeat(64);
  const records = [
    /* SQLite 恢复行：只有账本白名单字段——没有请求文本、trace、answers。 */
    {
      id: 'restored-routed', ts: '2026-11-19T10:00:00+08:00', decision: 'route',
      model: 'qwen3.8-flash-sft', target_id: 'power', policy_id: 'power-priority',
      policy_revision: revision, selection_reason: 'target', sticky_hit: 0,
      outcome_state: 'completed', jev_calls: 1, total_ms: 800, latency_ms: 800,
      route_label: { zh: '目标命中', en: 'Target selected' },
    },
    {
      id: 'restored-direct', ts: '2026-11-19T10:00:01+08:00', decision: 'test',
      model: 'qwen3-8b', target_id: null, policy_id: 'power-priority',
      policy_revision: revision, selection_reason: 'direct', sticky_hit: 0,
      outcome_state: 'completed', jev_calls: 0, total_ms: 120, latency_ms: 120,
      route_label: { zh: '固定模型测试', en: 'Direct model test' },
    },
    {
      id: 'failed', ts: '2026-11-19T10:00:02+08:00', text: '调度失败请求',
      model: null, target_id: 'power', policy_id: 'power-priority',
      policy_revision: revision, selection_reason: 'target', sticky_hit: 0,
      outcome_state: 'routing_error', error_class: 'no_available_backend',
      jev_calls: 1, routing_ms: 4, total_ms: 4,
      trace: [
        { event: 'time', active_periods: ['normal'], timezone: 'Asia/Shanghai' },
        { event: 'window', tokens_before: 6, tokens_after: 6, truncated: false },
        { event: 'jev', questions: ['route', 'power_business', 'adversarial'],
          duration_ms: 45, failed: false, error: null },
        { event: 'rule', index: 0, name: '对抗请求优先走安全目标', matched: false },
        { event: 'rule', index: 1, name: '电力业务优先', matched: true },
        { event: 'final', kind: 'target', target_id: 'power', reason: null },
        { event: 'schedule', target_id: 'power', strategy: 'least_inflight',
          configured_models: ['qwen3.8-flash-sft'],
          registered_models: ['qwen3.8-flash-sft'], available_models: [],
          unavailable: { 'qwen3.8-flash-sft': 'unregistered' }, inflight: {},
          sticky: { status: 'no_session', preferred_model: null } },
        { event: 'routing_error', stage: 'schedule',
          error_class: 'no_available_backend', message: "target 'power' has no available backend" },
      ],
    },
  ];
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ requests: records }),
  }));
  try {
    await openLogs();
    assert.equal(await page.locator('.lg-row').count(), 3);

    /* 筛选「JEV 调用」：只保留发生过的记录（恢复行读 jev_calls 字段）。 */
    await page.locator('#filter button[data-filter="jev"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.lg-row').length === 2);
    const jevRows = await page.locator('.lg-row .lg-jev').allInnerTexts();
    assert.deepEqual(jevRows, ['1', '1']);

    await page.locator('#filter button[data-filter="error"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.lg-row').length === 1);
    /* 异常行：调度失败保留前置 trace（JEV/规则/最终目标都在）。 */
    await page.locator('.lg-row').first().click();
    await page.waitForSelector('.lg-item.open .lg-expand');
    const failed = page.locator('.lg-item.open');
    const failedText = await failed.innerText();
    assertIncludes(failedText, '路由失败');
    assertIncludes(failedText, '电力业务优先');
    assertIncludes(failedText, 'power');
    assertIncludes(failedText, '未注册');
    assert.equal(await failed.locator('[data-sec="jev"]').count(), 1);
    assert.equal(await failed.locator('.lg-rule').count(), 2);

    await page.locator('#filter button[data-filter="fallback"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.lg-row').length === 0);

    /* 恢复行（无文本/无 trace）：空态 + 事实行，不编造请求文本。 */
    await page.locator('#filter button[data-filter="all"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.lg-row').length === 3);
    await page.locator('.lg-item[data-id="restored-routed"] .lg-row').click();
    await page.waitForSelector('.lg-item[data-id="restored-routed"] .lg-expand:not([hidden])');
    const restored = page.locator('.lg-item[data-id="restored-routed"]');
    assertIncludes(await restored.innerText(), '（无文本）');
    assertIncludes(await restored.innerText(), '这条请求没有路径记录');
    const facts = await restored.locator('.lg-policyfacts').innerText();
    assertIncludes(facts, 'power-priority');
    assertIncludes(facts, '—');
    assert.equal(await restored.locator('[data-sec="jev"]').count(), 0);
    assert.equal(await restored.locator('.lg-rule').count(), 0);
  } finally {
    await page.unroute(matcher);
  }
});

test('calculation log mobile keeps the detail within the viewport', async () => {
  await openLogs();
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    await page.locator('.lg-row').first().click();
    await page.waitForSelector('.lg-item.open .lg-expand');
    assert.equal(await page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth), true);
  } finally {
    await page.setViewportSize({ width: 1280, height: 800 });
  }
});

await runIfMain(import.meta.url);
