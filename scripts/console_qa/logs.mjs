/* RoutePilot 控制台 QA · 计算日志页用例（logs.html：分阶段计算视图、失败 trace、移动端布局）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/logs.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  EXAMPLE,
  EXAMPLE_ROOT,
  MOD,
  S1_TEXT,
  TREE_ID,
  api,
  assertIncludes,
  page,
  restartRouter,
  routerBase,
  runIfMain,
  test,
  waitFor
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
  /* 计算日志展示契约：只渲染 trace 里实际出现的阶段，阶段 id 即引擎的 phase。 */
  const phases = (await open.locator('.lg-stage-name small').allInnerTexts())
    .map(text => text.split(' · ')[0]);
  assert.deepEqual(phases,
    ['period', 'initial', 'condition', 'jev-wave-1', 'final', 'schedule', 'outcome']);
  assert.equal(await open.locator('.lg-stage').count(), 7);
  assert.equal(await open.locator('.lg-status').count(), 1);
  assertIncludes(await open.innerText(), 'power-target');
  assertIncludes(await open.innerText(), 'qwen3.8-flash-sft');

  /* 计算日志只统计决策耗时（routing_ms + jev_ms），不把上游状态或端到端
     总耗时混进智能路由的口径。 */
  const labels = await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
    return {
      latency: t('logs.stat.latency'), model: t('logs.decisionModel'),
      hint: t('logs.stat.latencyHint'),
    };
  });
  const kpi = page.locator('#kpis .lg-kpi').last();
  assertIncludes(await kpi.innerText(), labels.latency);
  assert.equal(await kpi.getAttribute('title'), labels.hint,
    'the decision-time KPI does not explain that upstream time is excluded');
  const metrics = (await open.locator('.lg-detail-metrics').innerText()).replace(/\s+/g, ' ');
  assertIncludes(metrics, labels.latency);
  assertIncludes(metrics, labels.model);
  assert.equal(/总耗时|Total time|上游状态|Upstream status/.test(metrics), false,
    'expanded metrics still report upstream/end-to-end facts: ' + metrics);

  /* 折叠行的计算路径只画路由决策，不画上游执行结果。 */
  const chain = (await page.locator('.lg-item .lg-chain').first().innerText()).replace(/\s+/g, ' ');
  assert.equal(/执行结果/.test(chain), false, 'the collapsed path still includes the execution result');
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
    /* 计算日志不再区分「历史记录」：没有 trace 的记录统一显示 traceEmpty。 */
    const expected = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('logs.traceEmpty'));
});
    assertIncludes(await page.locator('.lg-item.open').innerText(), expected);
    /* 旧日志没有图快照：折叠行也不能显示快照按钮，原文字阶段视图继续可用。 */
    assert.equal(await page.locator('.lg-item[data-id="restored"] .lg-snap-btn').count(), 0,
      'a log row without a snapshot still offered a snapshot button');
    /* 耗时列取决策耗时 routing_ms，不是端到端 total_ms。 */
    assertIncludes(await page.locator('.lg-item[data-id="restored"] .lg-lat').innerText(), '4ms');
    assertIncludes(await page.locator('.lg-thead').innerText(), '决策耗时');
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

test('calculation-log shows JEV values and comparisons without clicking a stage', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
  });
  await page.goto(routerBase + '/console/logs.html');
  await page.locator('.lg-row').first().click();
  await page.waitForSelector('.lg-item.open .lg-rail');

  const open = page.locator('.lg-item.open');
  const rail = open.locator('.lg-rail');
  const text = (await rail.innerText()).replace(/\s+/g, ' ');
  /* 不点任何阶段：事件行必须已经在 DOM 里。 */
  assert.ok(await open.locator('.lg-event').count() > 7,
    'the detail still collapses events into one row per phase');
  assertIncludes(text, '0.84 > 0.65 → true');
  assertIncludes(text, '0.92 > 0.7 → true');
  assertIncludes(text, 'p_top 0.84');
  assertIncludes(text, 'strong 0.84');
  assertIncludes(text, 'AND');
  assertIncludes(text, 'power-target p5');
  assertIncludes(text, 'strong-target p3');
  assertIncludes(text, 'power-target 胜出');
  assertIncludes(text, '→ false');

  const kinds = await open.locator('.lg-event-kind').allInnerTexts();
  assert.ok(kinds.includes('槽位'), 'event rows have no type label');
  assert.ok(kinds.includes('问题'), 'question rows are missing');
  assert.ok(kinds.includes('门'), 'gate rows are missing');
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


/* 快照被 page.route() 截获后的固定 tree envelope：直接复用示例图，
   保证节点/边 id 与事件顺序一一对应。严格契约：tree envelope 必须带完整四字段
   运行快照（只读回放不会执行，但控制器加载时仍会校验）。 */
function snapshotTree() {
  return {
    id: 'partial-tree',
    name: EXAMPLE.name,
    timezone: EXAMPLE.timezone,
    sticky: EXAMPLE.sticky,
    root: EXAMPLE_ROOT,
    canvas: EXAMPLE.canvas,
    backends: JSON.parse(JSON.stringify(EXAMPLE.backends)),
    jev: JSON.parse(JSON.stringify(EXAMPLE.jev)),
    jev_window: JSON.parse(JSON.stringify(EXAMPLE.jev_window)),
    time_periods: JSON.parse(JSON.stringify(EXAMPLE.time_periods))
  };
}

function fulfillSnapshot(requestId, trace, request) {
  return {
    request_id: requestId,
    graph_snapshot_id: requestId + '-graph',
    tree: snapshotTree(),
    trace,
    request
  };
}


test('routing snapshot replays a partial trace without a final event', async () => {
  const matcher = '**/admin/requests/partial/snapshot';
  const trace = [
    { event: 'condition', node_id: 'cond-text', value: true,
      matched: ['跳闸'], source: 'text' },
    { event: 'slot', node_id: 'or-ctx', port: 'in_1', edge_id: 'e-cond-text-or',
      source: 'cond-text', source_port: 'out', raw: true, condition: null,
      status: 'true', wave: 0 },
    { event: 'wave', wave: 1, questions: ['capability', 'domain'],
      duration_ms: 12.0, failed: false, error: null },
    { event: 'routing_error', stage: 'schedule',
      error_class: 'no_available_backend', message: 'none' }
  ];
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(fulfillSnapshot('partial', trace, {
      id: 'partial', ts: '2026-11-20T10:00:00+08:00', text: '部分回放',
      decision: 'route', model: null, outcome_state: 'routing_error',
      selection_reason: null
    }))
  }));
  try {
    await page.goto(routerBase + '/console/routing.html?request=partial');
    await page.waitForSelector('#cvNodes .rtc-node', { timeout: 15000 });
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return !!(app.booted && app.replay);
    });

    /* 状态绘制与最终结论解耦：没有 final 也要画出已命中的条件和边。 */
    assert.ok(await page.locator('#cvNodes .rtc-node.is-replay').count() > 0,
      'a partial replay painted no node state');
    assert.equal(await page.locator('#cvNodes .rtc-node.is-final').count(), 0,
      'a partial replay invented a final target');
    assert.equal(await page.locator('#cvNodes .rtc-node.is-fallback').count(), 0,
      'a partial replay invented a fallback target');
    assert.match(await page.locator('#cvTryResult').innerText(), /路由失败|Routing failed/);

    const opened = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      const condition = document.querySelector('#cvNodes [data-node-id="cond-text"]');
      const edge = document.querySelector('#cvEdges path.rtc-edge[data-edge-id="e-cond-text-or"]');
      return {
        locked: app.kit.isLocked(),
        snapshotMode: document.body.classList.contains('is-log-snapshot'),
        final: app.replay ? app.replay.final : 'missing',
        error: app.replay ? app.replay.error : null,
        conditionReplay: condition.classList.contains('is-replay'),
        edgeExists: !!edge,
        edgeClass: edge ? edge.className.baseVal : ''
      };
    });
    assert.equal(opened.final, null, 'a partial replay produced a final outcome');
    assert.equal(opened.error.error_class, 'no_available_backend');
    assert.equal(opened.error.stage, 'schedule');
    assert.equal(opened.conditionReplay, true, 'the hit condition was not painted');
    assert.equal(opened.edgeExists, true, 'the snapshot canvas lost its edges');
    assert.match(opened.edgeClass, /is-true/, 'the hit edge was not painted');
    assert.equal(opened.locked, true);
    assert.equal(opened.snapshotMode, true);
  } finally {
    await page.unroute(matcher);
  }
});

test('routing snapshot with an empty trace keeps the static graph and invents nothing', async () => {
  const matcher = '**/admin/requests/direct-only/snapshot';
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(fulfillSnapshot('direct-only', [], {
      id: 'direct-only', ts: '2026-11-20T10:00:00+08:00', text: '固定模型',
      decision: 'test', model: 'qwen3.8-flash-sft', outcome_state: 'completed',
      selection_reason: 'direct'
    }))
  }));
  try {
    await page.goto(routerBase + '/console/routing.html?request=direct-only');
    await page.waitForSelector('#cvNodes .rtc-node', { timeout: 15000 });
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return !!(app.booted && app.replay);
    });

    /* 空 trace 只能显示静态图：不得伪造 final、失败结论或执行轨迹。 */
    assert.ok(await page.locator('#cvNodes .rtc-node').count() > 0);
    assert.equal(await page.locator('#cvNodes .rtc-node.is-replay').count(), 0,
      'an empty trace painted replay node state');
    assert.equal(await page.locator('#cvEdges .rtc-edge.is-unknown').count(), 0,
      'an empty trace painted unknown edge state');
    assert.equal(await page.locator('#cvNodes .rtc-node.is-final').count(), 0);
    assert.equal(await page.locator('#cvNodes .rtc-node.is-fallback').count(), 0);
    assert.equal(await page.locator('#cvTryResult').isHidden(), true,
      'an empty trace rendered a conclusion');
    const empty = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        final: app.replay ? app.replay.final : 'missing',
        error: app.replay ? app.replay.error : 'missing',
        locked: app.kit.isLocked()
      };
    });
    assert.equal(empty.final, null);
    assert.equal(empty.error, null);
    assert.equal(empty.locked, true);
  } finally {
    await page.unroute(matcher);
  }
});

test('routing snapshot without graph events keeps static graph for empty and outcome-only traces', async () => {
  for (const [requestId, trace] of [
    ['direct-empty', []],
    ['direct-outcome', [{ event: 'request_outcome', outcome_state: 'completed' }]],
  ]) {
    const matcher = `**/admin/requests/${requestId}/snapshot`;
    await page.route(matcher, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(fulfillSnapshot(requestId, trace, {
        id: requestId, decision: 'test', model: 'qwen3.8-flash-sft',
        outcome_state: 'completed', selection_reason: 'direct'
      }))
    }));
    try {
      await page.goto(routerBase + '/console/routing.html?request=' + requestId);
      await page.waitForSelector('#cvNodes .rtc-node');
      await waitFor(async () => {
        const { app } = await import('/console/routing/app.js');
        return !!(app.booted && app.replay);
      });
      /* direct 请求没有图事件：不得把全部节点涂成回放、把全部边标为 unknown。 */
      assert.equal(await page.locator('#cvNodes .rtc-node.is-replay').count(), 0,
        requestId + ' painted replay nodes without a graph event');
      assert.equal(await page.locator('#cvEdges .rtc-edge.is-unknown').count(), 0,
        requestId + ' painted unknown edges without a graph event');
      assert.equal(await page.locator('#cvNodes .rtc-node.is-final').count(), 0);
      assert.equal(await page.locator('#cvTryResult').isHidden(), true);
      const replay = await page.evaluate(async () => {
        const { app } = await import('/console/routing/app.js');
        return {
          hasGraphEvents: app.replay.hasGraphEvents,
          final: app.replay.final,
          error: app.replay.error,
          locked: app.kit.isLocked()
        };
      });
      assert.deepEqual(replay, {
        hasGraphEvents: false, final: null, error: null, locked: true
      });
    } finally {
      await page.unroute(matcher);
    }
  }
});

test('routing snapshot shows final and later routing error', async () => {
  const requestId = 'final-then-error';
  const matcher = `**/admin/requests/${requestId}/snapshot`;
  const trace = [
    { event: 'final', target_id: 'power-target', kind: 'target', priority: 5,
      candidates: [] },
    { event: 'scheduled', model: 'qwen3.8-flash-sft', reason: 'target' },
    { event: 'routing_error', stage: 'upstream', error_class: 'upstream_error',
      message: 'upstream timed out' }
  ];
  await page.route(matcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(fulfillSnapshot(requestId, trace, {
      id: requestId, decision: 'route', model: 'qwen3.8-flash-sft',
      outcome_state: 'upstream_error', selection_reason: 'target'
    }))
  }));
  try {
    await page.goto(routerBase + '/console/routing.html?request=' + requestId);
    await page.waitForSelector('#cvNodes [data-node-id="power-target"].is-final');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return !!(app.booted && app.replay && app.replay.error);
    });
    /* final 已命中后上游失败：目标路径保留，失败事实独立可见。 */
    assert.equal(await page.locator('#cvTryResult [data-routing-error]').count(), 1);
    const text = await page.locator('#cvTryResult').innerText();
    assert.match(text, /路由失败|Routing failed/);
    assert.match(text, /upstream/);
    assert.match(text, /upstream_error/);
    assert.match(text, /upstream timed out/);
    const result = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        final: app.replay.final.target_id,
        error: app.replay.error,
        datasetFinal: document.getElementById('cvTryResult').dataset.final,
        datasetError: document.getElementById('cvTryResult').dataset.error,
        locked: app.kit.isLocked()
      };
    });
    assert.equal(result.final, 'power-target');
    assert.equal(result.error.stage, 'upstream');
    assert.equal(result.error.error_class, 'upstream_error');
    assert.equal(result.datasetFinal, 'power-target');
    assert.equal(result.datasetError, 'routing_error');
    assert.equal(result.locked, true);
  } finally {
    await page.unroute(matcher);
  }
});

test('calculation-log snapshot link opens a read-only graph replay', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
  });
  await page.goto(routerBase + '/console/logs.html');
  /* 快照入口在折叠行上，不需要先展开计算过程。 */
  const link = page.locator('.lg-item .lg-snap-btn').first();
  await link.waitFor({ state: 'visible' });
  assert.equal(await page.locator('.lg-item.open').count(), 0,
    'the snapshot button required expanding the row first');
  /* 「显眼」是需求本身：入口必须是一个填了底色的按钮，而不是一排灰字。 */
  const skin = await link.evaluate(element => {
    const style = getComputedStyle(element);
    return { background: style.backgroundColor, color: style.color, border: style.borderTopColor };
  });
  assert.notEqual(skin.background, 'rgba(0, 0, 0, 0)',
    'the snapshot entry is not painted as a button');
  assert.notEqual(skin.color, skin.background, 'the snapshot button text does not contrast with its fill');
  /* 方框图标会被误认成单选/复选：快照入口不能画任何矩形框。 */
  assert.equal(await link.locator('svg rect').count(), 0,
    'the snapshot icon still draws a box that reads as a radio button');
  /* 表头必须和列内容同向对齐：模型左、决策耗时右。 */
  const alignment = await page.evaluate(() => {
    const heads = document.querySelectorAll('.lg-thead span');
    const row = document.querySelector('.lg-row');
    return {
      headModel: getComputedStyle(heads[3]).textAlign,
      cellModel: getComputedStyle(row.querySelector('.lg-model')).textAlign,
      headLat: getComputedStyle(heads[4]).textAlign,
      cellLat: getComputedStyle(row.querySelector('.lg-lat')).textAlign,
    };
  });
  assert.equal(alignment.headModel, alignment.cellModel,
    'the model header is not aligned with the model column');
  assert.equal(alignment.headLat, alignment.cellLat,
    'the decision-time header is not aligned with the decision-time column');
  const href = await link.getAttribute('href');
  assert.match(href, /^\/console\/routing\.html\?request=.+/);
  await Promise.all([page.waitForURL(/routing\.html\?request=/), link.click()]);
  await page.waitForSelector('#cvNodes [data-node-id="power-target"].is-final', { timeout: 15000 });

  const opened = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const css = selector => {
      const el = document.querySelector(selector);
      return el ? getComputedStyle(el).display : 'missing';
    };
    const start = app.graph.nodes.find(node => node.id === 'start').position;
    return {
      final: app.replay ? app.replay.final.target_id : null,
      locked: app.kit.isLocked(),
      snapshotMode: document.body.classList.contains('is-log-snapshot'),
      badgeHidden: document.getElementById('cvSnapshotBadge').hidden,
      palette: css('#cvPalette'),
      inspector: css('#cvInspector'),
      actions: css('.rtc-actions'),
      lifecycle: css('#cvLifecycle'),
      tryRow: css('.rtc-try-row'),
      chips: css('#cvTryChips'),
      runButton: css('#cvTryRun'),
      playIcon: css('.rtc-try-ico'),
      rerun: !!document.getElementById('cvTryRerun'),
      clear: !!document.getElementById('cvTryClear'),
      selectDisabled: document.getElementById('cvTreeSelect').disabled,
      requestInput: document.getElementById('cvTryInput'),
      inputValue: document.getElementById('cvTryInput').value,
      inputReadOnly: document.getElementById('cvTryInput').readOnly,
      replayText: app.replay ? app.replay.text : null,
      inspectorBody: document.getElementById('cvInspectorBody').innerHTML,
      zoomVisible: getComputedStyle(document.getElementById('cvZoom')).display,
      position: { x: start.x, y: start.y },
    };
  });
  assert.equal(opened.final, 'power-target');
  assert.equal(opened.locked, true, 'the snapshot canvas is not locked');
  assert.equal(opened.snapshotMode, true);
  assert.equal(opened.badgeHidden, false, 'the read-only badge is missing');
  assert.equal(await page.locator('#cvSnapshotBadge').isVisible(), true,
    'the snapshot badge is not visible');
  assert.match(await page.locator('#cvSnapshotBadge').innerText(), /只读|read-only/,
    'the snapshot badge does not say the replay is read-only');
  /* No save, enable or editing affordance may survive in snapshot mode. */
  for (const id of ['cvSave', 'cvEnable', 'cvAutoLayout', 'cvUndo', 'cvTryBtn', 'cvDisable']) {
    assert.equal(await page.locator('#' + id).isVisible(), false,
      `snapshot mode still exposes #${id}`);
  }
  for (const key of ['palette', 'inspector', 'actions', 'lifecycle', 'chips']) {
    assert.equal(opened[key], 'none', `snapshot mode still shows ${key}`);
  }
  /* 快照问题放回画布原有的试算问题输入位：只读、无运行按钮/图标/样例。 */
  assert.notEqual(opened.tryRow, 'none', 'snapshot mode hid the logged request question');
  assert.equal(opened.runButton, 'none', 'snapshot mode shows the try-run button');
  assert.equal(opened.playIcon, 'none', 'snapshot mode shows the try-run icon');
  assert.equal(opened.rerun, false, 'snapshot mode offers rerun');
  assert.equal(opened.clear, false, 'snapshot mode offers clear');
  assert.equal(opened.selectDisabled, true, 'the snapshot group selector is still enabled');
  assert.equal(opened.inputValue, S1_TEXT, 'the snapshot question is not in the try input');
  assert.equal(opened.inputReadOnly, true, 'the snapshot try input is editable');
  assert.equal(opened.replayText, S1_TEXT, 'the replay does not carry the logged request text');
  assert.equal(opened.inspectorBody, '', 'snapshot mode rendered inspector forms');
  assert.notEqual(opened.zoomVisible, 'none', 'snapshot mode lost the zoom controls');

  /* 拖动 start 节点：只读画布只能选中，不能改坐标。 */
  const box = await page.locator('#cvNodes [data-node-id="start"]').boundingBox();
  const centerX = box.x + box.width / 2;
  const centerY = box.y + box.height / 2;
  await page.mouse.move(centerX, centerY);
  await page.mouse.down();
  await page.mouse.move(centerX + 90, centerY + 50, { steps: 6 });
  await page.mouse.up();
  const dragged = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const start = app.graph.nodes.find(node => node.id === 'start').position;
    return { x: start.x, y: start.y };
  });
  assert.deepEqual(dragged, opened.position, 'a log snapshot allowed dragging a node');

  /* 完整只读：拖线、Delete/Backspace、撤销、条件 chip 和保存都不能改图或发写请求。 */
  const graphBefore = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return JSON.stringify(app.tree);
  });
  const writes = [];
  const onWrite = request => {
    if (request.method() === 'GET') return;
    if (!request.url().startsWith(routerBase)) return;
    writes.push(request.method() + ' ' + request.url());
  };
  page.on('request', onWrite);
  try {
    /* 输出端口拖到输入端口：只读画布不能新增连线。 */
    const from = await page.locator(
      '#cvNodes [data-node-id="start"] [data-port][data-port-dir="out"]').first().boundingBox();
    const to = await page.locator(
      '#cvNodes [data-node-id="capability"] [data-port][data-port-dir="in"]').first().boundingBox();
    assert.ok(from && to, 'snapshot canvas lost its connection ports');
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 10 });
    await page.mouse.up();

    /* 选中节点后 Delete/Backspace 与撤销都不能改图。 */
    const target = await page.locator('#cvNodes [data-node-id="power-target"]').boundingBox();
    await page.mouse.click(target.x + 24, target.y + 12);
    await page.keyboard.press('Delete');
    await page.keyboard.press('Backspace');
    await page.keyboard.press(MOD + '+z');
    await page.waitForTimeout(60);

    /* 条件 chip 不得打开编辑器。 */
    const chip = page.locator('#cvNodes [data-cond-edge]').first();
    if (await chip.count()) {
      await chip.click();
      assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), true,
        'a log snapshot opened the condition editor');
    }
  } finally {
    page.off('request', onWrite);
  }
  const graphAfter = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return JSON.stringify(app.tree);
  });
  assert.equal(graphAfter, graphBefore, 'a read-only interaction changed the graph JSON');
  assert.deepEqual(writes, [], 'a read-only interaction issued a write request');
  assert.equal(await page.evaluate(
    () => getComputedStyle(document.getElementById('cvInspector')).display), 'none',
  'snapshot mode rendered the inspector after selection');
  assert.equal(await page.evaluate(
    () => getComputedStyle(document.querySelector('.rtc-actions')).display), 'none',
  'the snapshot still offers toolbar actions');

  /* 刷新同一 URL 仍能回放：图来自不可变快照，不依赖当前编辑中的 tree。 */
  await page.reload();
  await page.waitForSelector('#cvNodes [data-node-id="power-target"].is-final', { timeout: 15000 });
  const reloaded = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return {
      final: app.replay ? app.replay.final.target_id : null,
      locked: app.kit.isLocked(),
      snapshotMode: document.body.classList.contains('is-log-snapshot'),
      inputValue: document.getElementById('cvTryInput').value,
      inputReadOnly: document.getElementById('cvTryInput').readOnly,
    };
  });
  assert.equal(reloaded.final, 'power-target');
  assert.equal(reloaded.locked, true);
  assert.equal(reloaded.snapshotMode, true);
  assert.equal(reloaded.inputValue, S1_TEXT, 'a reload lost the snapshot question');
  assert.equal(reloaded.inputReadOnly, true);

  /* 条件编辑入口也必须在只读模式失效：点击边上的条件 chip 不得弹出编辑器。 */
  const chip = page.locator('#cvNodes [data-cond-edge]').first();
  if (await chip.count()) {
    await chip.click();
    assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), true,
      'a log snapshot opened the condition editor');
  }
  assert.equal(await page.evaluate(
    () => getComputedStyle(document.querySelector('.rtc-actions')).display), 'none',
  'the snapshot still offers toolbar actions');
});

test('calculation-log snapshot replays from SQLite after a real router restart', async () => {
  await api('POST', '/v1/chat/completions', {
    model: 'ignored',
    messages: [{ role: 'user', content: S1_TEXT }],
    stream: false,
  });
  await page.goto(routerBase + '/console/logs.html');
  const link = page.locator('.lg-item .lg-snap-btn').first();
  await link.waitFor({ state: 'visible' });
  await Promise.all([page.waitForURL(/routing\.html\?request=/), link.click()]);
  await page.waitForSelector('#cvNodes [data-node-id="power-target"].is-final', { timeout: 15000 });

  const before = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const stable = value => JSON.stringify(value, (key, item) =>
      item && typeof item === 'object' && !Array.isArray(item)
        ? Object.keys(item).sort().reduce((sorted, name) => {
            sorted[name] = item[name];
            return sorted;
          }, {})
        : item);
    const start = app.graph.nodes.find(node => node.id === 'start').position;
    return {
      final: app.replay.final.target_id,
      trace: app.replay.trace,
      tree: stable(app.tree),
      position: { x: start.x, y: start.y },
      requestText: document.getElementById('cvTryInput').value,
    };
  });
  assert.equal(before.requestText, S1_TEXT, 'the live snapshot lost the request text');

  /* 真实进程重启：同一临时目录/端口，内存 ring 与 pending 索引全部清空。 */
  await restartRouter();
  const hydrated = await api('GET', '/admin/requests/recent');
  assert.ok(hydrated.requests.length > 0, 'the restarted router lost the ledger rows');
  assert.equal('text' in hydrated.requests[0], false,
    'the restarted router still served live request text from the database');
  const stats = await api('GET', '/admin/requests/stats');
  assert.equal(stats.pending_snapshots, 0, 'the restarted router resumed a stale pending index');

  await page.reload();
  await page.waitForSelector('#cvNodes [data-node-id="power-target"].is-final', { timeout: 15000 });
  const after = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const stable = value => JSON.stringify(value, (key, item) =>
      item && typeof item === 'object' && !Array.isArray(item)
        ? Object.keys(item).sort().reduce((sorted, name) => {
            sorted[name] = item[name];
            return sorted;
          }, {})
        : item);
    const start = app.graph.nodes.find(node => node.id === 'start').position;
    return {
      final: app.replay.final.target_id,
      trace: app.replay.trace,
      tree: stable(app.tree),
      position: { x: start.x, y: start.y },
      requestText: document.getElementById('cvTryInput').value,
    };
  });
  /* 图和 trace 与重启前一致；正文按既有契约不持久化。 */
  assert.deepEqual(after, { ...before, requestText: '' },
    'the restart did not replay the same immutable snapshot');

  /* 修改当前 tree 后，旧快照仍显示请求发生时的布局。 */
  const current = (await api('GET', '/admin/trees/' + TREE_ID)).tree;
  const movedCanvas = JSON.parse(JSON.stringify(current.canvas));
  movedCanvas.start = {
    x: movedCanvas.start.x + 70,
    y: movedCanvas.start.y + 40,
  };
  try {
    const moved = await api('PUT', '/admin/trees/' + TREE_ID, {
      root: current.root,
      canvas: movedCanvas,
      base_document_sha256: current.document_sha256,
    });
    assert.notDeepEqual(moved.tree.canvas.start, before.position,
      'the tree edit did not move the edited graph');
    await page.reload();
    await page.waitForSelector('#cvNodes [data-node-id="power-target"].is-final', { timeout: 15000 });
    const replayed = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      const stable = value => JSON.stringify(value, (key, item) =>
        item && typeof item === 'object' && !Array.isArray(item)
          ? Object.keys(item).sort().reduce((sorted, name) => {
              sorted[name] = item[name];
              return sorted;
            }, {})
          : item);
      const start = app.graph.nodes.find(node => node.id === 'start').position;
      return {
        final: app.replay.final.target_id,
        position: { x: start.x, y: start.y },
        tree: stable(app.tree),
      };
    });
    assert.deepEqual(replayed.position, before.position,
      'the snapshot picked up a later tree edit');
    assert.equal(replayed.tree, before.tree, 'the snapshot tree changed after a later edit');
    assert.equal(replayed.final, before.final);
  } finally {
    /* 恢复基线图，后续用例继续使用同一 QA 进程。 */
    const latest = (await api('GET', '/admin/trees/' + TREE_ID)).tree;
    await api('PUT', '/admin/trees/' + TREE_ID, {
      root: latest.root,
      canvas: current.canvas,
      base_document_sha256: latest.document_sha256,
    });
  }
});

await runIfMain(import.meta.url);
