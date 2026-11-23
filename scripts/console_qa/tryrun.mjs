/* RoutePilot 控制台 QA · 真实试算用例（Task 13：真实 /admin/try 回放、波次 HUD、轨迹抽屉、门控/条件标签）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/tryrun.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {
  EXAMPLE_NODES,
  ROOT,
  TRY_SAMPLES,
  api,
  assertIncludes,
  clickCanvasAction,
  dragNodeBy,
  ensureBaselineTree,
  expectHttpStatusError,
  page,
  prepareTryCanvas,
  resetExpectedHttpStatusErrors,
  runIfMain,
  runTryManual,
  runTrySample,
  screenshot,
  test,
  trySnapshot,
  waitEdgeCondition,
  waitFor,
  waitInspectorBranch,
  waitTryResult
} from '../console_qa.mjs';

test('7.4-16-try-input-samples-loading-lock', async () => {
  await prepareTryCanvas();
  /* 试算台 = 文本输入 + 5 个样例 chip；运行中由下面断言 loading 与编辑锁。 */
  assert.equal(await page.locator('#cvTryInput').count(), 1, 'try input is missing');
  assert.equal(await page.locator('#cvTryChips [data-try-sample]').count(), 5,
    'the try bar does not offer exactly five sample chips');
  const samples = await page.evaluate(async () => {
    const RTTryRun = await import('/console/routing/tryrun.js');
    return (RTTryRun.samples());
});
  assert.equal(samples.length, 5);
  samples.forEach((sample, index) => {
    assert.equal(sample.text, TRY_SAMPLES[index].text, `sample ${index} text drifted`);
});
  const before = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    position: Object.assign({}, app.graph.nodes.find(node => node.id === 'start').position),
    undo: store.canUndo,
    nodes: app.graph.nodes.length
}));
});
  let release = null;
  const gate = new Promise(resolve => { release = resolve; });
  const tryMatcher = url => url.pathname === '/admin/try';
  await page.route(tryMatcher, async route => {
    await gate;
    await route.continue();
});
  try {
    await page.locator('[data-try-sample="0"]').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tryRunning === true);
}, null, { timeout: 5000 });
    const loading = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const { setLang, t, tf } = await import('/console/i18n.js');
    return (({
      running: app.tryRunning,
      runDisabled: document.getElementById('cvTryRun').disabled,
      topDisabled: document.getElementById('cvTryBtn').disabled,
      runText: document.getElementById('cvTryRun').textContent.trim(),
      expectedRunText: t('cv.tryRunning'),
      inputDisabled: document.getElementById('cvTryInput').disabled,
      chipDisabled: document.querySelector('[data-try-sample="0"]').disabled,
      bodyRunning: document.body.classList.contains('is-try-running'),
      stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
      stageCursor: getComputedStyle(document.getElementById('cvStage')).cursor,
      stagePulse: getComputedStyle(document.getElementById('cvStage'), '::after').animationName,
      palCollapsed: document.getElementById('cvPalette').classList.contains('is-collapsed'),
      inspCollapsed: document.getElementById('cvInspector').classList.contains('is-collapsed')
}));
});
    assert.equal(loading.running, true);
    assert.equal(loading.runDisabled, true, 'run button stayed enabled during the request');
    assert.equal(loading.topDisabled, true, 'top-bar try button stayed enabled during the request');
    assert.equal(loading.runText, loading.expectedRunText, 'run button does not show the localized loading copy');
    assert.equal(loading.inputDisabled, true, 'try input stayed editable during the request');
    assert.equal(loading.chipDisabled, true, 'sample chips stayed clickable during the request');
    assert.equal(loading.bodyRunning, true, 'body.is-try-running was not applied during the request');
    assert.equal(loading.stageLocked, true, 'stage.is-locked was not applied during the request');
    assert.equal(loading.stageCursor, 'progress', 'locked stage did not show the progress cursor affordance');
    assert.equal(loading.stagePulse, 'rtc-lock-pulse', 'locked stage did not show the token-based lock pulse');
    assert.equal(loading.palCollapsed, true, 'node library did not auto-collapse for the run');
    assert.equal(loading.inspCollapsed, true, 'inspector did not auto-collapse for the run');

    /* 请求在途时拖节点必须被拒绝：位置、历史栈和节点数都不能变。 */
    const box = await page.locator('[data-node-id="start"]').first().boundingBox();
    assert.ok(box, 'start node not visible');
    await page.mouse.move(box.x + 24, box.y + 10);
    await page.mouse.down();
    await page.mouse.move(box.x + 84, box.y + 58, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(140);
    const during = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
      position: Object.assign({}, app.graph.nodes.find(node => node.id === 'start').position),
      undo: store.canUndo,
      nodes: app.graph.nodes.length
}));
});
    assert.deepEqual(during.position, before.position, 'dragging a node mutated the draft during the run');
    assert.equal(during.undo, before.undo, 'dragging a node pushed history during the run');
    assert.equal(during.nodes, before.nodes);
    /* 即使绕过 disabled 直接派发 click，飞行中的 chip 也不能改写输入框。 */
    const guarded = await page.evaluate(() => {
      document.querySelector('[data-try-sample="3"]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      return document.getElementById('cvTryInput').value;
});
    assert.equal(guarded, TRY_SAMPLES[0].text, 'a chip click rewrote the in-flight input text');
} finally {
    /* gate 释放后请求继续；保留路由（已 resolve 的 gate 不再阻塞后续用例）。 */
    release();
}
  await waitTryResult({ text: TRY_SAMPLES[0].text });
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    running: app.tryRunning,
    runDisabled: document.getElementById('cvTryRun').disabled,
    topDisabled: document.getElementById('cvTryBtn').disabled,
    replay: !!app.replay,
    inputDisabled: document.getElementById('cvTryInput').disabled,
    chipDisabled: document.querySelector('[data-try-sample="0"]').disabled,
    bodyRunning: document.body.classList.contains('is-try-running'),
    stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
    stageCursor: getComputedStyle(document.getElementById('cvStage')).cursor,
    palCollapsed: document.getElementById('cvPalette').classList.contains('is-collapsed'),
    inspCollapsed: document.getElementById('cvInspector').classList.contains('is-collapsed')
}));
});
  assert.equal(after.running, false);
  assert.equal(after.runDisabled, false, 'run button stayed disabled after the result');
  assert.equal(after.topDisabled, false);
  assert.equal(after.replay, true);
  assert.equal(after.inputDisabled, false, 'try input stayed disabled after the result');
  assert.equal(after.chipDisabled, false, 'sample chips stayed disabled after the result');
  assert.equal(after.bodyRunning, false, 'body.is-try-running survived the result');
  assert.equal(after.stageLocked, false, 'stage.is-locked survived the result');
  assert.equal(after.stageCursor === 'progress', false, 'stage kept the progress cursor after the result');
  assert.equal(after.palCollapsed, true, 'node library expanded again after the run');
  assert.equal(after.inspCollapsed, true, 'inspector expanded again after the run');

  /* 用户仍可手动再展开两个面板（规格 §0-10：结束保持收起，可再展开）。 */
  await page.locator('#cvPalToggle').click();
  await page.locator('#cvInspToggle').click();
  await page.waitForFunction(() => !document.getElementById('cvPalette').classList.contains('is-collapsed') &&
    !document.getElementById('cvInspector').classList.contains('is-collapsed'));
});

test('Task 13 · a setup exception still releases the run lock and reset cannot strand it', async () => {
  await prepareTryCanvas();
  const failure = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTTryRun = await import('/console/routing/tryrun.js');

    const originalCollapse = app.collapsePanels;
    app.collapsePanels = function () {
      app.kit.setLocked(true);
      document.body.classList.add('is-try-running');
      throw new Error('boom-setup');
};
    let result = null;
    try {
      result = await RTTryRun.run('setup failure');
} catch (error) {
      result = 'rejected:' + String(error && error.message ? error.message : error);
}
    app.collapsePanels = originalCollapse;
    return {
      result: result,
      running: app.tryRunning,
      locked: app.kit.isLocked(),
      stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
      bodyRunning: document.body.classList.contains('is-try-running'),
      inputDisabled: document.getElementById('cvTryInput').disabled,
      chipsDisabled: document.querySelector('[data-try-sample="0"]').disabled,
      runDisabled: document.getElementById('cvTryRun').disabled
};

});
  assert.equal(failure.result, false, 'run() did not absorb the setup exception');
  assert.equal(failure.running, false, 'a setup exception left App.tryRunning stuck');
  assert.equal(failure.locked, false, 'a setup exception left the canvas kit locked');
  assert.equal(failure.stageLocked, false, 'a setup exception left stage.is-locked');
  assert.equal(failure.bodyRunning, false, 'a setup exception left body.is-try-running');
  assert.equal(failure.inputDisabled, false, 'a setup exception left the try input disabled');
  assert.equal(failure.chipsDisabled, false, 'a setup exception left the sample chips disabled');
  assert.equal(failure.runDisabled, false, 'a setup exception left the run button disabled');

  /* reset()（清除/换组/编辑）碰到飞行中的 run 时必须主动解锁，而不是等一个 token 已失效的 finally。 */
  const stranded = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTTryRun = await import('/console/routing/tryrun.js');

    /* app.tryRunning 是只读 getter：旧写法赋值会被静默丢弃，用例永远绿。
       必须走 store 的 setter，并在 reset() 之前证明锁真的上了。 */
    store.setTryRunning(true);
    const lockWasSet = app.tryRunning === true;
    app.kit.setLocked(true);
    document.body.classList.add('is-try-running');
    RTTryRun.reset();
    const snapshot = {
      lockWasSet: lockWasSet,
      running: app.tryRunning,
      locked: app.kit.isLocked(),
      stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
      bodyRunning: document.body.classList.contains('is-try-running'),
      inputDisabled: document.getElementById('cvTryInput').disabled
};
    app.render();
    /* 断言失败也不要把运行锁漏给后续用例。 */
    if (store.tryRunning) store.setTryRunning(false);
    return snapshot;

});
  assert.equal(stranded.lockWasSet, true, 'the run lock was never set before reset()');
  assert.equal(stranded.running, false, 'reset() left App.tryRunning stuck');
  assert.equal(stranded.locked, false, 'reset() left the canvas kit locked');
  assert.equal(stranded.stageLocked, false, 'reset() left stage.is-locked');
  assert.equal(stranded.bodyRunning, false, 'reset() left body.is-try-running');
  assert.equal(stranded.inputDisabled, false, 'reset() left the try input disabled');
});

test('Task 13 · five chips mirror the mock scenarios and answers follow the live draft', async () => {
  await prepareTryCanvas();
  const samples = await page.evaluate(async () => {
    const RTTryRun = await import('/console/routing/tryrun.js');
    return (RTTryRun.samples());
});
  assert.equal(samples.length, TRY_SAMPLES.length);
  samples.forEach((sample, index) => {
    assert.equal(sample.text, TRY_SAMPLES[index].text, `chip ${index} text drifted from the mock scenario`);
    assert.equal(sample.now, TRY_SAMPLES[index].now, `chip ${index} now drifted from the fixture`);
});
  const labels = await page.evaluate(() => Array.prototype.map.call(
    document.querySelectorAll('#cvTryChips [data-try-sample]'), el => el.textContent.trim()));
  assert.deepEqual(labels, TRY_SAMPLES.map(sample => sample.name));

  /* IME 组词中的 Enter 只用于上屏，不能被当成运行快捷键。 */
  const composing = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const input = document.getElementById('cvTryInput');
    const event = new KeyboardEvent('keydown', {
      key: 'Enter', bubbles: true, cancelable: true, isComposing: true
});
    input.dispatchEvent(event);
    return { prevented: event.defaultPrevented, running: app.tryRunning };

});
  assert.equal(composing.prevented, false, 'Enter during IME composition was handled as a run');
  assert.equal(composing.running, false, 'Enter during IME composition started a run');

  await runTrySample(0);
  let snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'power-target');
  assert.equal(snap.now, TRY_SAMPLES[0].now, 'sample run did not send the fixed scenario now');

  /* 改当前草稿的阈值后重跑：服务端按新草稿求值，power 链不再命中。 */
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      const edge = tree.root.edges.find(item => item.id === 'e-domain-and-power');
      edge.condition = { op: 'gt', value: 0.99 };
});

});
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.replay);
}), null,
    'editing the draft after a result did not clear the replay');
  await runTrySample(0);
  snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'strong-target', 'the rerun did not use the edited threshold');
  assert.equal(snap.nodes['power-target'].replay, 'not-triggered');
  assert.equal(snap.nodes['strong-target'].replay, 'final');
  assert.deepEqual(snap.final.candidates.map(candidate => candidate.target_id), ['strong-target']);
});

test('7.4-17-replay-slots-gates-targets-priority', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  const snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'power-target');
  assert.equal(snap.final.kind, 'target');
  assert.equal(snap.final.selection_reason, 'target');
  assert.equal(snap.final.model, 'qwen3.8-flash-sft');
  assert.deepEqual(snap.final.candidates, [
    { target_id: 'power-target', priority: 5, via_node: 'and-power', via_port: 'in_1', wave: 1 },
    { target_id: 'strong-target', priority: 3, via_node: 'capability', via_port: 'in_1', wave: 1 }
  ]);
  assert.deepEqual(snap.waves.map(wave => wave.questions.sort()), [['capability', 'domain']]);
  assert.equal(snap.resultHidden, false, 'conclusion panel stayed hidden');
  assert.equal(snap.resultFinal, 'power-target');
  assertIncludes(snap.resultText, 'power-target', 'conclusion does not name the final target');
  assertIncludes(snap.resultText, 'strong-target', 'conclusion does not show the rival candidate');
  assertIncludes(snap.resultText, 'p5', 'conclusion lost the winner priority');
  assertIncludes(snap.resultText, 'p3', 'conclusion lost the rival priority');

  /* 槽位：两个 true 槽位 + 条件文本都在卡片上。 */
  assert.equal(snap.slots.andPowerIn1.status, 'true');
  assertIncludes(snap.slots.andPowerIn1.classes, 'is-true');
  assertIncludes(snap.slots.andPowerIn1.live, '0.84');
  assertIncludes(snap.slots.andPowerIn1.live, 'true');
  assert.equal(snap.slots.andPowerIn2.status, 'true');
  assertIncludes(snap.slots.andPowerIn2.live, '0.92');
  assert.equal(snap.slots.powerIn1.status, 'true');

  /* 门三值、问题概率/selected、P(yes)、条件节点。 */
  assert.equal(snap.nodes['and-power'].gate, 'true');
  assert.equal(snap.nodes['or-ctx'].gate, 'true');
  assert.equal(snap.edgeStates['e-cap-below-strong-follow'].condition.op, 'lte');
  assert.equal(snap.edgeStates['e-cap-below-strong-follow'].status, 'false');
  assert.equal(snap.nodes['and-follow'].gate, 'false');
  assert.deepEqual(snap.nodes.capability.selectedOptions, ['strong']);
  assert.equal(snap.nodes.capability.live['prob.strong'], '0.84');
  /* p_top / margin 只留在 trace 与抽屉里，不再上卡片（2026-11-20 变更）。 */
  assert.equal(snap.nodes.capability.live.p_top, undefined, 'choice card still renders a p_top chip');
  assert.equal(snap.nodes.capability.live.margin, undefined, 'choice card still renders a margin chip');
  assert.equal(snap.nodes.domain.live.yes, '0.92');
  assert.equal(snap.nodes.domain.live.no, '0.08');
  assert.equal(snap.nodes['cond-text'].replay, 'hit');
  assert.equal(snap.nodes['cond-peak'].replay, 'miss');
  assertIncludes(snap.nodes['cond-text'].state, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.tryHit'));
}));

  /* target 命中/候选/未触发 + 边高亮 + 波次头条件。 */
  assert.equal(snap.nodes['power-target'].replay, 'final');
  assertIncludes(snap.nodes['power-target'].foot, 'p5');
  assert.equal(snap.nodes['strong-target'].replay, 'candidate');
  assertIncludes(snap.nodes['strong-target'].foot, 'p3');
  assert.equal(snap.nodes['light-target'].replay, 'not-triggered');
  assert.equal(snap.svgReplay, true);
  assertIncludes(snap.edges['e-cap-strong-and-power'], 'is-true');
  assertIncludes(snap.edges['e-selected-strong-strong'], 'is-true');
  assertIncludes(snap.edges['e-selected-easy-light'], 'is-false');
  /* HUD 与执行轨迹都已撤掉：命中条件只保留在回放数据（边状态）里。 */
  assert.equal(snap.edgeStates['e-cap-strong-and-power'].condition.value, 0.65);
  assert.equal(snap.edgeStates['e-domain-and-power'].condition.value, 0.7);
  await screenshot('s1-replay-1440.png');
});

test('Task 13 · s2 leaves the untriggered clarify question at 未触发 instead of zero', async () => {
  await prepareTryCanvas();
  await runTrySample(1);
  const snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'light-target');
  assert.equal(snap.final.kind, 'target');
  assert.deepEqual(snap.final.candidates.map(candidate => candidate.target_id), ['light-target']);
  assert.equal(snap.waves.length, 1);
  assert.equal(snap.nodeStates.clarify.status, 'not-triggered');
  assert.equal(snap.nodeStates.clarify.wave, null);
  assert.equal(snap.nodeStates.clarify.ports, null);
  assert.equal(snap.nodes.clarify.replay, 'not-triggered');
  const notTriggered = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.tryNotTriggered'));
});
  assert.equal(snap.nodes.clarify.state, notTriggered);
  assert.equal(snap.nodes.clarify.live.yes, '—', 'untriggered P(yes) was rendered as 0');
  assert.equal(snap.nodes.clarify.live.no, '—', 'untriggered P(no) was rendered as 0');
  assert.equal(snap.nodes.capability.selectedOptions.length, 1);
  assert.equal(snap.nodes.capability.selectedOptions[0], 'easy');
  assert.equal(snap.nodes['light-target'].replay, 'final');
});

test('Task 13 · s3/s4 replay two waves and the clarify slot condition hits', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  let snap = await trySnapshot();
  assert.equal(snap.waves.length, 2);
  assert.deepEqual(snap.waves[0].questions.sort(), ['capability', 'domain']);
  assert.deepEqual(snap.waves[1].questions, ['clarify']);
  assert.equal(snap.waves[1].failed, false);
  assert.ok(snap.waves[1].duration_ms > 0, 'second wave has no duration');
  assert.equal(snap.final.target_id, 'deep-target');
  assert.deepEqual(snap.final.candidates.map(candidate => candidate.target_id), ['deep-target', 'standard-target']);
  assert.equal(snap.nodeStates.clarify.status, 'done');
  assert.equal(snap.nodeStates.clarify.wave, 2);
  assert.equal(snap.slots.deepIn1.status, 'true');
  assertIncludes(snap.slots.deepIn1.live, '0.74');
  assertIncludes(snap.edges['e-clarify-deep'], 'is-true');
  assert.equal(snap.nodes['deep-target'].replay, 'final');
  assert.equal(snap.edgeStates['e-clarify-deep'].condition.value, 0.6);

  await runTrySample(3);
  snap = await trySnapshot();
  assert.equal(snap.waves.length, 2);
  assert.equal(snap.final.target_id, 'deep-target');
  assertIncludes(snap.slots.deepIn1.live, '0.83');
  assert.equal(snap.nodes['deep-target'].replay, 'final');
});

test('Task 15 · the conclusion panel wraps without clipping at 1024', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  await page.setViewportSize({ width: 1024, height: 900 });
  await page.waitForTimeout(250);
  const info = await page.evaluate(() => {
    const host = document.getElementById('cvTryResult');
    const note = host.querySelector('.rtc-try-note');
    const facts = host.querySelector('.rtc-try-facts');
    const chip = host.querySelector('.rtc-try-cand.is-win');
    return {
      overflow: host.scrollWidth - host.clientWidth,
      noteOverflow: note ? note.scrollWidth - note.clientWidth : -1,
      noteWhiteSpace: note ? getComputedStyle(note).whiteSpace : '',
      noteHeight: note ? Math.round(note.getBoundingClientRect().height) : 0,
      factsHeight: facts ? Math.round(facts.getBoundingClientRect().height) : 0,
      chipText: chip ? chip.textContent.trim() : '',
      hasLessThan: host.textContent.indexOf('<') >= 0,
      hasRule: (note ? note.textContent : '').indexOf('>') >= 0
    };
  });
  assert.ok(info.overflow <= 1, `conclusion panel overflows horizontally by ${info.overflow}px`);
  assert.ok(info.noteOverflow <= 1, `rule note is clipped by ${info.noteOverflow}px instead of wrapping`);
  assert.equal(info.noteWhiteSpace, 'normal', 'rule note still refuses to wrap');
  assert.ok(info.noteHeight > 0);
  assert.ok(info.factsHeight > 0);
  assertIncludes(info.chipText, 'p5');
  assert.equal(info.hasLessThan, false, 'candidate comparison still uses a less-than sign');
  assert.equal(info.hasRule, true, 'candidate note lost the explicit priority rule');
  await screenshot('task15-conclusion-1024.png');
  await page.setViewportSize({ width: 1440, height: 960 });
});

test('Task 15 · replay paths ramp by wave and the winning edge is the strongest', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  const ramp = await page.evaluate(() => {
    const styleOf = id => {
      const path = document.querySelector('#cvEdges path.rtc-edge[data-edge-id="' + id + '"]');
      if (!path) return null;
      const style = getComputedStyle(path);
      return {
        step: path.dataset.step || '',
        final: path.classList.contains('is-final-edge'),
        stroke: style.stroke,
        width: parseFloat(style.strokeWidth)
      };
    };
    const steps = {};
    Array.prototype.forEach.call(document.querySelectorAll('#cvEdges path.rtc-edge'), path => {
      if (!path.dataset.step) return;
      steps[path.dataset.step] = (steps[path.dataset.step] || 0) + 1;
    });
    const firstPath = document.querySelector('#cvEdges path.rtc-edge[data-step="1"]');
    return {
      first: firstPath ? styleOf(firstPath.dataset.edgeId) : null,
      winning: styleOf('e-clarify-deep'),
      steps: steps
    };
});
  /* 第一波浅红、第二波玫红：色阶必须真的分出层次，而不是所有选中边一个颜色。 */
  assert.ok(ramp.steps['1'] > 0, 'no wave-1 edge carried the first ramp step');
  assert.ok(ramp.steps['2'] > 0, 'no wave-2 edge carried the second ramp step');
  assert.ok(ramp.first, 'no wave-1 edge carried the first ramp step');
  assert.equal(ramp.first.step, '1');
  assert.equal(ramp.first.stroke, 'rgb(251, 113, 133)', 'the first ramp step is not the lightest red');
  assert.equal(ramp.first.final, false);
  /* 最终命中链路单独走最深的酒红、更粗，是色阶的终点。 */
  assert.equal(ramp.winning.final, true, 'the winning edge is not marked as the final path');
  assert.equal(ramp.winning.stroke, 'rgb(136, 19, 55)', 'the winning edge is not the deepest red');
  assert.ok(ramp.winning.width > ramp.first.width,
    'the winning edge is not thicker than the wave-1 edges');
  await screenshot('task15-replay-ramp-1440.png');
});

test('Task 13 · s5 voids the failed wave, clears candidates and paints the amber error path', async () => {
  await prepareTryCanvas();
  await runTrySample(4);
  const snap = await trySnapshot();
  assert.equal(snap.final.kind, 'error_fallback');
  assert.equal(snap.final.target_id, 'light-target');
  assert.equal(snap.final.selection_reason, 'error_fallback');
  assert.deepEqual(snap.final.candidates, []);
  assert.deepEqual(snap.candidates, []);
  assert.equal(snap.waves.length, 1);
  assert.equal(snap.waves[0].failed, true);
  assert.ok(snap.waves[0].error, 'failed wave has no error text');
  assert.equal(snap.nodes['light-target'].replay, 'fallback');
  assertIncludes(snap.nodes['light-target'].classes, 'is-fallback');
  ['power-target', 'strong-target', 'deep-target', 'standard-target'].forEach(id => {
    assert.equal(snap.nodes[id].replay, 'not-triggered', `${id} was not voided by the failed wave`);
});
  const errorEdges = Object.keys(snap.edges).filter(id => snap.edges[id].indexOf('is-error-path') >= 0);
  /* §6.8：只点亮回退目标 light-target 的入边，不能把此前所有 true 边都染成琥珀。 */
  assert.deepEqual(errorEdges, ['e-selected-easy-light'],
    'error fallback path must be exactly the fallback target incoming edge');
  assertIncludes(snap.edges['e-selected-easy-light'], 'is-error-path');
  assert.equal(snap.edges['e-start-capability'].indexOf('is-error-path'), -1,
    'an earlier true edge was painted as an error path');
  assertIncludes(snap.slots.lightIn1.classes, 'is-error-path',
    'the fallback target slot row did not join the amber chain');
  assert.equal(snap.resultKind, 'error_fallback');
  assertIncludes(snap.resultText, '模型目标-5');
  assertIncludes(snap.resultText, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.tryKind.fallback'));
}));
  await screenshot('s5-fallback-amber-1440.png');

  /* 清除 s5 后琥珀/非法 class 必须一条不剩（旧用例只清过 s1）。 */
  await page.locator('#cvTryClear').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.replay === null &&
    document.getElementById('cvTryResult').hidden === true);
});
  const cleared = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    replay: app.replay,
    errorPaths: document.querySelectorAll('.is-error-path').length,
    invalid: document.querySelectorAll('.is-invalid').length,
    slotStatuses: document.querySelectorAll('#cvNodes .rtc-slot[data-status]').length,
    edgeStatuses: document.querySelectorAll('#cvEdges .rtc-edge[data-status]').length,
    failedNodes: document.querySelectorAll('#cvNodes .rtc-node.is-fail').length,
    fallbackNodes: document.querySelectorAll('#cvNodes .rtc-node.is-fallback').length
}));
});
  assert.equal(cleared.replay, null);
  assert.equal(cleared.errorPaths, 0, 'clear left is-error-path classes behind');
  assert.equal(cleared.invalid, 0, 'clear left is-invalid classes behind');
  assert.equal(cleared.slotStatuses, 0, 'clear left slot data-status attributes behind');
  assert.equal(cleared.edgeStatuses, 0, 'clear left edge data-status attributes behind');
  assert.equal(cleared.failedNodes, 0);
  assert.equal(cleared.fallbackNodes, 0);
});

test('Task 13 · a failed /admin/try toasts the api error and fabricates no replay', async () => {
  await prepareTryCanvas();
  const matcher = url => url.pathname === '/admin/try';
  await page.route(matcher, route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'routing backend unavailable' })
}));
  try {
    expectHttpStatusError({ url: /\/admin\/try$/, status: /503/ });
    await page.locator('[data-try-sample="0"]').click();
    await page.waitForFunction(() => {
      const host = document.getElementById('cvToast');
      return !!host && host.textContent.indexOf('routing backend unavailable') >= 0;
}, null, { timeout: 10000 });
    await page.waitForTimeout(250);
    const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
      replay: app.replay,
      running: app.tryRunning,
      runDisabled: document.getElementById('cvTryRun').disabled,
      resultHidden: document.getElementById('cvTryResult').hidden,
      svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
      finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
      lastToast: app.lastToast
}));
});
    assert.equal(after.replay, null, 'a failed request fabricated a replay result');
    assert.equal(after.running, false);
    assert.equal(after.runDisabled, false, 'the run button stayed disabled after a failed request');
    assert.equal(after.resultHidden, true);
    assert.equal(after.svgReplay, false);
    assert.equal(after.finalNodes, 0);
    assertIncludes(after.lastToast, 'routing backend unavailable');
} finally {
    resetExpectedHttpStatusErrors();
    await page.unroute(matcher);
}
});

test('7.4-19-rerun-and-clear', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  const first = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    id: app.replay.id,
    target: app.replay.final.target_id,
    now: app.replay.now
}));
});
  assert.equal(first.now, TRY_SAMPLES[0].now);
  await page.locator('#cvTryRerun').click();
  await waitFor(async previous => {
    const { app, store } = await import('/console/routing/app.js');

    const replay = app.replay;
    return replay && !app.tryRunning && replay.id !== previous;

}, first.id, { timeout: 15000 });
  const second = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    id: app.replay.id,
    target: app.replay.final.target_id,
    now: app.replay.now,
    text: app.replay.text
}));
});
  assert.notEqual(second.id, first.id, 'rerun did not produce a new result');
  assert.equal(second.target, first.target, 'rerun changed the conclusion');
  assert.equal(second.now, first.now, 'rerun did not keep the scenario now');
  assert.equal(second.text, TRY_SAMPLES[0].text);

  await page.locator('#cvTryClear').click();
  const cleared = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    replay: app.replay,
    resultHidden: document.getElementById('cvTryResult').hidden,
    svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
    finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
    trueEdges: document.querySelectorAll('#cvEdges .rtc-edge.is-true').length,
    runDisabled: document.getElementById('cvTryRun').disabled
}));
});
  assert.equal(cleared.replay, null);
  assert.equal(cleared.resultHidden, true);
  assert.equal(cleared.svgReplay, false, 'clearing replay left the SVG replay class');
  assert.equal(cleared.finalNodes, 0, 'clearing replay left final node classes');
  assert.equal(cleared.trueEdges, 0, 'clearing replay left highlighted edges');
  assert.equal(cleared.runDisabled, false);

  await runTryManual(TRY_SAMPLES[0].text);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.replay.now);
}), null,
    'a manual run must not send a fixed now');
  assert.equal((await trySnapshot()).final.target_id, 'power-target');
});

test('Task 13 · any edit after a result clears the replay immediately', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (!!app.replay);
}), true);
  await dragNodeBy('start', 58, 34);
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    replay: app.replay,
    resultHidden: document.getElementById('cvTryResult').hidden,
    svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
    finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
    kind: app.kind()
}));
});
  assert.equal(after.replay, null, 'dragging a node did not clear the replay state');
  assert.equal(after.resultHidden, true);
  assert.equal(after.svgReplay, false);
  assert.equal(after.finalNodes, 0);
  assert.equal(after.kind, 'layout');
});

test('layout-by-waves-is-layout-only', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  const before = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (({
    canonical: RTGraph.behaviorCanonical(app.tree),
    positions: Object.fromEntries(app.graph.nodes.map(node => [node.id, {
      x: node.position.x, y: node.position.y
}])),
    undo: store.canUndo
}));
});
  const refused = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTTryRun = await import('/console/routing/tryrun.js');
const { setLang, t, tf } = await import('/console/i18n.js');

    const originalChange = app.change;
    const originalToast = app.toast;
    const toasts = [];
    app.change = () => false;
    app.toast = message => { toasts.push(String(message)); };
    let result = null;
    try {
      result = RTTryRun.layoutByWaves();
} finally {
      app.change = originalChange;
      app.toast = originalToast;
}
    return { result: result, toasts: toasts, done: t('cv.tryWavesLayoutDone') };

});
  assert.equal(refused.result, false, 'layoutByWaves claimed success after App.change refused');
  assert.equal(refused.toasts.indexOf(refused.done), -1,
    'layoutByWaves toasted success after App.change refused');

  const moved = await page.evaluate(async () => {
    const RTTryRun = await import('/console/routing/tryrun.js');
    return (RTTryRun.layoutByWaves());
});
  assert.equal(moved, true, 'layoutByWaves refused to run with a replay result');
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (({
    canonical: RTGraph.behaviorCanonical(app.tree),
    kind: app.kind(),
    replay: app.replay,
    nodes: Object.fromEntries(app.graph.nodes.map(node => [node.id, {
      x: node.position.x, y: node.position.y
}])),
    undo: store.canUndo
}));
});
  assert.equal(after.canonical, before.canonical, 'wave layout changed behavior canonical');
  assert.equal(after.kind, 'layout', 'wave layout was recorded as a behavior change');
  assert.equal(after.replay, null, 'wave layout edit did not clear the stale replay');
  assert.equal(after.undo, true, 'wave layout did not push a history entry');
  const changed = Object.keys(before.positions).some(id =>
    before.positions[id].x !== after.nodes[id].x || before.positions[id].y !== after.nodes[id].y);
  assert.equal(changed, true, 'wave layout did not move any node');

  const x = id => after.nodes[id].x;
  const y = id => after.nodes[id].y;
  assert.equal(x('start'), x('cond-text'));
  assert.equal(x('cond-text'), x('cond-peak'));
  assert.equal(x('capability'), x('domain'));
  assert.equal(x('capability'), x('and-power'));
  assert.equal(x('and-power'), x('and-follow'));
  assert.equal(x('and-follow'), x('standard-target'));
  assert.equal(x('clarify'), x('deep-target'));
  assert.ok(x('capability') > x('start'));
  assert.ok(x('clarify') > x('capability'));
  const untriggered = ['light-target', 'power-target', 'strong-target'];
  untriggered.forEach(id => {
    assert.equal(x(id), x('light-target'), `${id} is not in the untriggered column`);
});
  assert.ok(x('light-target') > x('clarify'), 'untriggered column is not after every wave column');
  const untriggeredOrder = untriggered.slice().sort((a, b) => y(a) - y(b));
  assert.deepEqual(untriggeredOrder, untriggered, 'untriggered column is not sorted by node id');
  const column = ['capability', 'domain', 'and-power', 'and-follow', 'standard-target']
    .sort((a, b) => y(a) - y(b));
  assert.deepEqual(column, column.slice().sort(),
    'a wave column is not sorted by node id (same column must be id-ascending)');

  /* 未触发列头必须存在、跟在未触发列 x 上，并随语言切换重译。 */
  const header = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const { setLang, t, tf } = await import('/console/i18n.js');

    const el = document.querySelector('#cvWorld .rtc-wavehead');
    const node = app.graph.nodes.find(item => item.id === 'light-target');
    return el ? {
      text: el.textContent,
      left: parseFloat(el.style.left),
      top: parseFloat(el.style.top),
      nodeX: node.position.x,
      nodeY: node.position.y,
      expected: t('cv.tryUntriggeredColumn')
} : null;

});
  assert.ok(header, 'wave layout did not label the untriggered column');
  assert.equal(header.text, header.expected, 'untriggered column header is not localized');
  assert.equal(header.left, header.nodeX, 'untriggered column header is not above its column');
  assert.ok(header.top < header.nodeY, 'untriggered column header is not above the cards');
  await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('en'));
});
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  const englishHeader = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (({
    text: document.querySelector('#cvWorld .rtc-wavehead').textContent,
    expected: t('cv.tryUntriggeredColumn')
}));
});
  assert.equal(englishHeader.text, englishHeader.expected,
    'untriggered column header did not retranslate on language switch');
  await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('zh'));
});
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');

  /* 第二次整理没有移动：不得入历史、不得清回放、不得报“已整理”，一次 undo 必须回到旧布局。 */
  await runTrySample(2);
  const again = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTTryRun = await import('/console/routing/tryrun.js');
const { setLang, t, tf } = await import('/console/i18n.js');
    return (({
    result: RTTryRun.layoutByWaves(),
    toast: app.lastToast,
    same: t('cv.tryWavesLayoutSame'),
    replay: !!app.replay
}));
});
  assert.equal(again.result, true, 'a no-op wave layout should still succeed');
  assert.equal(again.replay, true, 'a no-op wave layout cleared the replay');
  assert.equal(again.toast, again.same, 'a no-op wave layout toasted the moved copy');
  await clickCanvasAction('cvUndo');
  await waitFor(async previous => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return Math.abs(node.position.x - previous.x) < 0.01 && Math.abs(node.position.y - previous.y) < 0.01;

}, before.positions.start);
  assert.equal(await page.evaluate(() => !!document.querySelector('#cvWorld .rtc-wavehead')), false,
    'undo left a stale untriggered column header');
});

test('7.4-18-replay-data-from-admin-try', async () => {
  /* 回放唯一数据源必须是 /admin/try 的 trace。ESM 命名空间是只读的，旧用例对
     window.RTGraph 的 monkeypatch 已不可用；这里改为在“浏览器实际加载的”
     graph.js 函数体里埋计数探针：回放期间任何直接 / 解构 / 别名调用都会经过它。
     源码正则只作为补充（它只证明文本，不证明运行行为）。 */
  const graphMatcher = '**/console/routing/graph.js';
  let instrumented = false;
  await page.route(graphMatcher, async route => {
    const response = await route.fetch();
    let body = await response.text();
    const marker = 'function evaluateCondition(edge, graphValue, value) {';
    if (body.includes(marker)) {
      instrumented = true;
      body = body.replace(marker, marker +
        '\n  globalThis.__rtEvalCalls = (globalThis.__rtEvalCalls || 0) + 1;');
    }
    await route.fulfill({ response, body });
  });
  try {
    await prepareTryCanvas();
    assert.equal(instrumented, true,
      'could not instrument evaluateCondition in the served graph.js');
    /* 探针自检：主动调用一次必须计数，再清零，证明回放期间读到的 0 不是
       “探针根本没装上”的假绿。 */
    const probe = await page.evaluate(async () => {
      const RTGraph = await import('/console/routing/graph.js');
      const before = globalThis.__rtEvalCalls || 0;
      RTGraph.evaluateCondition({ condition: null }, { nodes: [], edges: [] }, true);
      const after = globalThis.__rtEvalCalls || 0;
      globalThis.__rtEvalCalls = 0;
      return { before: before, after: after };
    });
    assert.equal(probe.after, probe.before + 1, 'the evaluateCondition probe did not count a call');

    const tryRunSource = fs.readFileSync(path.join(ROOT, 'router/console/routing/tryrun.js'), 'utf8');
    assert.doesNotMatch(tryRunSource, /\.evaluateCondition\s*\(/,
      'replay module calls local condition evaluation');
    await runTrySample(2);
    const proof = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return ({
        calls: globalThis.__rtEvalCalls || 0,
        final: app.replay.final.target_id,
        waves: app.replay.waves.length,
        clarify: app.replay.nodeStates.clarify.status,
        deepSlot: app.replay.edgeStates['e-clarify-deep'].status,
        resultHidden: document.getElementById('cvTryResult').hidden,
        finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length
});
});
    assert.equal(proof.calls, 0,
      'replay evaluated conditions locally (evaluateCondition ran behind the trace)');
    assert.equal(proof.final, 'deep-target', 'trace-only replay did not finish');
    assert.equal(proof.waves, 2);
    assert.equal(proof.clarify, 'done');
    assert.equal(proof.deepSlot, 'true');
    assert.equal(proof.resultHidden, false);
    assert.ok(proof.finalNodes >= 1);
  } finally {
    await page.unroute(graphMatcher);
  }
});

test('Task 13 · replay copy is bilingual and leaves no raw i18n keys', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  const chinese = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (({
    result: document.getElementById('cvTryResult').textContent.replace(/\s+/g, ' ').trim(),
    priorityLabel: t('cv.tryFactPriority')
}));
});
  assertIncludes(chinese.result, chinese.priorityLabel, 'result facts lost the localized priority label');
  assertIncludes(chinese.result, 'p5', 'result facts lost the winner priority value');
  assert.equal(chinese.result.indexOf('<'), -1,
    'candidate comparison still renders a less-than sign even though larger priority wins');
  const english = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');

    setLang('en');
    return {
      lang: document.documentElement.lang,
      result: document.getElementById('cvTryResult').textContent.replace(/\s+/g, ' ').trim(),
      clear: document.getElementById('cvTryClear').textContent.trim(),
      finalLabel: t('cv.tryFinal'),
      priorityLabel: t('cv.tryFactPriority'),
      inspectorTitle: document.getElementById('cvInspToggle').title,
      paletteTitle: document.getElementById('cvPalToggle').title,
      inspectorToggle: t('cv.inspectorToggle'),
      paletteToggle: t('cv.paletteToggle'),
      cjkPunctuation: /[；、]/.test(document.getElementById('cvTryResult').textContent),
      rawKey: /cv\.[a-zA-Z]/.test(document.getElementById('cvTryResult').textContent)
};

});
  assert.equal(english.lang, 'en');
  assert.equal(english.clear, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.tryClear'));
}));
  assertIncludes(english.result, english.finalLabel);
  assertIncludes(english.result, english.priorityLabel);
  assert.equal(english.inspectorTitle, english.inspectorToggle, 'inspector toggle tooltip is not localized');
  assert.equal(english.paletteTitle, english.paletteToggle, 'palette toggle tooltip is not localized');
  assert.equal(english.cjkPunctuation, false, 'English replay UI leaked CJK separators');
  assert.equal(english.rawKey, false, 'replay UI leaked raw i18n keys in English');
  assert.notEqual(english.finalLabel, 'cv.tryFinal');
  await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('zh'));
});
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (!!app.replay);
}), true,
    'language switch dropped the replay');
  await screenshot('task13-replay-s3.png');
});

test('try-uses-draft-timezone-and-sticky', async () => {
  await prepareTryCanvas();
  try {
    await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.clearSelection());
});
    await waitInspectorBranch('graph', 'root');
    await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
    await page.locator('#cvInspectorBody [data-field="sticky"]').uncheck();
    await page.locator('#cvInspectorBody [data-field="stickyTtl"]').fill('600');
    await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.timezone === 'UTC' &&
      app.tree.sticky.enabled === false && app.tree.sticky.ttl_seconds === 600);
});

    const expectedRuntime = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      const runtime = app.tree.runtime;
      return {
        jevId: runtime.jev && runtime.jev.id,
        jevBase: runtime.jev.base_url,
        jevWindow: runtime.jev_window,
        providerUrl: runtime.backends.providers[0].base_url,
        periods: runtime.time_periods.map(period => period.name)
};
});
    const captured = page.waitForRequest(request =>
      request.method() === 'POST' && new URL(request.url()).pathname === '/admin/try');
    await page.locator('[data-try-sample="0"]').click();
    const payload = (await captured).postDataJSON();
    assert.equal(payload.timezone, 'UTC', 'try did not send the draft timezone');
    assert.deepEqual(payload.sticky, { enabled: false, ttl_seconds: 600 },
      'try did not send the draft sticky settings');
    /* §C2：草稿自包含 runtime 时试算 payload 必须整组携带，服务端不能再按活动树继承。 */
    ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
      assert.ok(Object.prototype.hasOwnProperty.call(payload, field),
        'try payload dropped the ' + field + ' runtime field');
});
    assert.deepEqual(payload.time_periods.map(period => period.name), expectedRuntime.periods,
      'try payload sent a different runtime catalog than the draft');
    assert.equal(payload.jev.base_url, expectedRuntime.jevBase,
      'try payload did not carry the draft runtime JEV endpoint');
    /* 严格自包含：payload 带 JEV 服务 id，且不存在 runtime:null / 嵌套 runtime 控制字段。 */
    assert.ok(expectedRuntime.jevId, 'the draft runtime has no JEV service id');
    assert.equal(payload.jev.id, expectedRuntime.jevId,
      'try payload did not carry the draft runtime JEV service id');
    assert.deepEqual(payload.jev_window, expectedRuntime.jevWindow,
      'try payload did not carry the draft JEV window');
    assert.equal(Object.prototype.hasOwnProperty.call(payload, 'runtime'), false,
      'try payload sent the removed runtime control field');
    assert.equal(payload.backends.providers[0].base_url, expectedRuntime.providerUrl,
      'try payload did not carry the draft runtime provider endpoint');
    assert.ok(payload.root && payload.root.nodes.length === EXAMPLE_NODES,
      'try did not send the draft graph');
    assert.equal(payload.now, TRY_SAMPLES[0].now);
    assert.equal(payload.session_id, '');

    await waitTryResult({ text: TRY_SAMPLES[0].text, now: TRY_SAMPLES[0].now });
    const trace = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
      timezone: app.replay.meta.timezone,
      active: app.replay.meta.active_periods.slice()
}));
});
    assert.equal(trace.timezone, 'UTC', 'the returned trace did not use the draft timezone');
    assert.deepEqual(trace.active, ['offpeak'], 'UTC 04:00 should resolve to the offpeak period');
} finally {
    await page.evaluate(async () => {
    const RTTryRun = await import('/console/routing/tryrun.js');
    return (RTTryRun.clear());
}).catch(() => {});
    await ensureBaselineTree();
}
});

test('Task 14 · an in-flight run rejects condition and inspector saves instead of closing as success', async () => {
  await prepareTryCanvas();
  const edgeId = 'e-cap-strong-and-power';
  const original = await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTCondPop = await import('/console/routing/condpop.js');

    app.kit.select('edge', id);
    RTCondPop.open(id);
    return app.graph.edges.find(edge => edge.id === id).condition;

}, edgeId);
  assert.deepEqual(original, { op: 'gt', value: 0.65 });

  let release = null;
  const gate = new Promise(resolve => { release = resolve; });
  const matcher = url => url.pathname === '/admin/try';
  await page.route(matcher, async route => { await gate; await route.continue(); });
  try {
    await page.locator('[data-try-sample="0"]').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tryRunning === true);
}, null, { timeout: 5000 });
    assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), true,
      'starting a run left the condition editor open');

    /* 运行锁下仍强行打开并确认：change() 返回 false，浮层必须保持打开并报错。 */
    const conditionResult = await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTCondPop = await import('/console/routing/condpop.js');

      RTCondPop.open(id);
      const pop = document.getElementById('cvCondPop');
      pop.querySelector('[data-cond-field="value"]').value = '0.91';
      pop.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      const box = pop.querySelector('[data-cond-error]');
      return {
        hidden: pop.hidden,
        error: box ? box.textContent : '',
        errorHidden: box ? box.hidden : true,
        value: app.graph.edges.find(edge => edge.id === id).condition.value
};

}, edgeId);
    assert.equal(conditionResult.hidden, false, 'the condition editor closed on a rejected save');
    assert.equal(conditionResult.value, 0.65, 'a rejected condition edit mutated the draft');
    assert.equal(conditionResult.errorHidden, false, 'no inline condition error was shown');
    assert.equal(conditionResult.error, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.saveBlocked'));
}));

    /* 检查器走同一条 App.change 守卫，也必须内联报错且不写草稿。 */
    const inspectorResult = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

      app.kit.select('node', 'capability');
      const body = document.getElementById('cvInspectorBody');
      const form = body.querySelector('[data-ins-form="node"]');
      form.querySelector('[data-field="instructions"]').value = '运行中不应写入的文本';
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      const box = body.querySelector('[data-ins-error]');
      return {
        error: box ? box.textContent : '',
        hidden: box ? box.hidden : true,
        instructions: app.graph.nodes.find(node => node.id === 'capability').instructions
};

});
    assert.equal(inspectorResult.hidden, false, 'the inspector did not show an inline error');
    assert.equal(inspectorResult.error, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.saveBlocked'));
}));
    assert.notEqual(inspectorResult.instructions, '运行中不应写入的文本',
      'a rejected inspector save mutated the draft');
} finally {
    release();
}
  await waitTryResult({ text: TRY_SAMPLES[0].text });
  await page.evaluate(async () => {
    const RTCondPop = await import('/console/routing/condpop.js');
    return (RTCondPop.close());
});
  await page.unroute(matcher);
});

test('try result keeps the graph and verdict without an execution trace drawer', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  const state = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const RTTryRun = await import('/console/routing/tryrun.js');
    return {
      final: app.replay && app.replay.final.target_id,
      resultHidden: document.getElementById('cvTryResult').hidden,
      traceDrawer: document.querySelectorAll('.rtc-trace, .rtc-dw-row, [data-try-act="drawer"]').length,
      finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
      drawerApi: Object.keys(RTTryRun).filter(key => /drawer/i.test(key)),
      execHeight: document.getElementById('cvExecZone').classList.contains('has-height'),
    };
  });
  assert.equal(state.final, 'power-target');
  assert.equal(state.resultHidden, false, 'the verdict panel did not stay visible');
  assert.equal(state.traceDrawer, 0, 'the removed execution trace drawer still renders DOM');
  assert.ok(state.finalNodes > 0, 'the replay lost its final node highlight');
  assert.deepEqual(state.drawerApi, [], 'the trial trace drawer API still exists');
  assert.equal(state.execHeight, false, 'the removed execution resize left a pinned height');
});

/* ── Task 16 · 画布/目录漂移是 hint-only ───────────────────────────────
   真实 /admin/try 路径：把**画布草稿快照**里的模型 capability 改成与数据库
   目录不同，服务端会把它作为 runtime_mismatch（level=hint）返回；控制台必须
   渲染可见的提示行，且结论/运行态保持正常，不能升级成 routing_error。 */
test('Task 16 · catalog drift renders hint-only rows and never flips the run to an error', async () => {
  await prepareTryCanvas();
  const drift = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const models = app.tree.runtime.backends.models;
    const model = models.find(item => item && item.profile) || models[0];
    const previous = {
      id: model.id,
      profile: JSON.parse(JSON.stringify(model.profile || {}))
    };
    model.profile = Object.assign({}, model.profile || {}, {
      capability: 'QA 画布快照里的旧能力说明（与数据库目录不同）'
    });
    return previous;
  });
  try {
    await runTrySample(0);
    const snap = await trySnapshot();
    /* 执行结论仍来自画布快照；提示不许把试算变成失败态。 */
    assert.equal(snap.final.target_id, 'power-target',
      'a hint-only drift changed the routing outcome');
    assert.equal(snap.resultKind, 'target', 'a hint-only drift changed the result kind');
    assert.equal(snap.resultHidden, false, 'a hint-only drift hid the verdict panel');
    assert.equal(snap.running, false, 'a hint-only drift left the run lock on');
    assert.equal(snap.runDisabled, false, 'the run button stayed disabled after a hint-only result');
    const replay = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        error: app.replay.error || null,
        warnings: app.replay.warnings.map(item => ({ code: item.code, level: item.level }))
      };
    });
    assert.equal(replay.error, null, 'a catalog drift hint produced a routing error');
    assert.deepEqual(
      replay.warnings.filter(item => item.code === 'runtime_mismatch').map(item => item.level),
      ['hint'], 'runtime_mismatch was not reported as a hint');

    /* 提示列表可见，并逐行给出画布/数据库两侧值。 */
    const hint = await page.evaluate(() => {
      const host = document.getElementById('cvTryResult');
      const box = host.querySelector('[data-runtime-mismatch]');
      if (!box) return null;
      const style = getComputedStyle(box);
      return {
        count: Number(box.dataset.runtimeMismatch),
        visible: !!(box.offsetWidth || box.offsetHeight || box.getClientRects().length) &&
          style.display !== 'none' && style.visibility !== 'hidden',
        title: (box.querySelector('.rtc-try-cmp-l') || { textContent: '' }).textContent.trim(),
        rows: [...box.querySelectorAll('.rtc-try-cmp .rtc-try-note')].map(row => ({
          text: row.textContent.replace(/\s+/g, ' ').trim(),
          warn: row.classList.contains('is-warn')
        })),
        errorBadge: !!host.querySelector('[data-routing-error]'),
        errorKind: !!host.querySelector('.rtc-try-kind.is-fb'),
        kind: host.dataset.kind,
        errorAttr: host.dataset.error || ''
      };
    });
    assert.ok(hint, 'the runtime_mismatch hint list is missing from the result panel');
    assert.equal(hint.count, 1, 'the result panel reported the wrong hint count: ' + hint.count);
    assert.equal(hint.visible, true, 'the runtime_mismatch hint list is hidden');
    assert.match(hint.title, /目录差异|Catalog drift/,
      'the hint list has no localized title: ' + hint.title);
    assert.equal(hint.rows.length, 1, 'the hint list did not render exactly one drift row');
    assert.match(hint.rows[0].text, new RegExp(drift.id),
      'the drift row does not name the model: ' + hint.rows[0].text);
    assert.match(hint.rows[0].text, /profile\.capability/,
      'the drift row does not name the differing field: ' + hint.rows[0].text);
    assert.match(hint.rows[0].text, /画布|canvas/,
      'the drift row does not show the canvas value: ' + hint.rows[0].text);
    assert.match(hint.rows[0].text, /数据库|database/,
      'the drift row does not show the database value: ' + hint.rows[0].text);
    assert.equal(hint.rows[0].warn, true, 'the drift row is not styled as a warning hint');
    assert.equal(hint.errorBadge, false, 'a hint-only drift rendered the routing-error badge');
    assert.equal(hint.errorKind, false, 'a hint-only drift rendered the error kind badge');
    assert.equal(hint.kind, 'target', 'a hint-only drift changed the result kind');
    assert.equal(hint.errorAttr, '', 'a hint-only drift set the error dataset');
    await screenshot('task16-runtime-mismatch-hint.png');
  } finally {
    await page.evaluate(async previous => {
      const { app } = await import('/console/routing/app.js');
      const model = app.tree.runtime.backends.models.find(item => item.id === previous.id);
      if (model) model.profile = previous.profile;
      const RTTryRun = await import('/console/routing/tryrun.js');
      RTTryRun.clear();
    }, drift).catch(() => {});
    await ensureBaselineTree();
  }
});

await runIfMain(import.meta.url);
