/* RoutePilot 控制台 QA · 试算面板（Task 9 · routing.html #policyTry）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实 router（临时库 +
   ROUTER_POLICY_DIR）、mock 决策服务、浏览器与 teardown 全部由
   ../console_qa.mjs 提供。用例走真实 /admin/try 与真实页面，覆盖：
   * 试算只出现一个 JEV 区块，规则按后端 trace 顺序展示并停在首条命中，
     最终目标/模型/命中规则来自响应字段；
   * jev_error（s5 场景整次调用 504）与 no_rule_matched 渲染不同文案；
   * 空规则草稿不触发 JEV，直接走兜底且不渲染 JEV/答案/规则区块；
   * 试算失败（503）保留草稿、保留输入框文本、保留上一次成功轨迹。
   前端不重算任何规则——所有断言读 app.tryResult.result 与页面 DOM。
   入口：`node scripts/console_qa.mjs`（全量）或
         `QA_ONLY=try node scripts/console_qa.mjs`；也可单独执行：
         `node scripts/console_qa/tryrun.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  EXAMPLE_RULES,
  FIXTURE,
  POLICY_ID,
  S1_TEXT,
  appSnapshot,
  assertIncludes,
  clearToasts,
  ensureBaselinePolicy,
  expectHttpStatusError,
  mockState,
  openEditor,
  page,
  resetExpectedHttpStatusErrors,
  runIfMain,
  test,
  waitFor,
  waitToastIncludes,
} from '../console_qa.mjs';

const scenarioText = id => {
  const scenario = FIXTURE.scenarios.find(item => item.id === id);
  if (!scenario) throw new Error('missing scenario fixture ' + id);
  return scenario.text;
};

/* 打开基线策略（与 policy.mjs 同一流程：恢复示例文档并启用后进编辑页）。 */
async function openBaselineEditor() {
  await ensureBaselinePolicy();
  await openEditor();
  const snap = await appSnapshot();
  if (snap.policyId !== POLICY_ID) {
    await page.selectOption('#cvPolicySelect', POLICY_ID);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.policyId === id && app.changeKind === 'clean';
    }, POLICY_ID, { timeout: 10000 });
  }
  return appSnapshot();
}

/* 跑一次试算并等服务端结论落地，返回 /admin/try 的响应。 */
async function runTryAndWait(text) {
  await page.locator('#cvTryInput').fill(text);
  await page.locator('#cvTryRun').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return !!(app.tryResult && app.tryResult.result) && !app.tryRunning;
  }, null, { timeout: 20000 });
  return page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.tryResult.result;
  });
}

test('dry run shows one JEV block with rules in backend order and the final target', async () => {
  await openBaselineEditor();
  const before = mockState.systemoneCalls.filter(call => call.scenario === 's1').length;

  const result = await runTryAndWait(S1_TEXT);
  assert.equal(result.target, 'power');
  assert.deepEqual(result.models, ['qwen3.8-flash-sft']);
  assert.equal(result.matched_rule, '电力业务优先');
  assert.equal(result.fallback, false);
  assert.equal(result.reason, null);
  /* 后端 trace：1 未命中 + 2 命中即停，恰一次 JEV。 */
  const ruleEvents = result.trace.filter(event => event.event === 'rule');
  assert.deepEqual(ruleEvents.map(row => row.matched), [false, true]);
  assert.equal(result.trace.filter(event => event.event === 'jev').length, 1);

  /* 结果区：恰一个 JEV 区块；答案区展示后端返回的各问题答案。 */
  assert.equal(await page.locator('#cvTryResult [data-sec="jev"]').count(), 1);
  assert.equal(await page.locator('#cvTryResult [data-sec="questions"]').count(), 1);
  assert.equal(await page.locator('#cvTryResult [data-sec="rules"]').count(), 1);
  const text = await page.locator('#cvTryResult').innerText();
  assertIncludes(text, '一次 JEV');
  assertIncludes(text, '3 个问题');
  assertIncludes(text, 'route');
  assertIncludes(text, 'power_business');
  assertIncludes(text, '选中项');
  assertIncludes(text, '84%');
  assertIncludes(text, '92%');

  /* 逐条规则按后端 trace 顺序渲染，且停在首条命中。 */
  const rows = await page.locator('#cvTryResult .lg-rule').allInnerTexts();
  assert.equal(rows.length, 2, 'the trace must stop at the first matching rule');
  const names = await page.locator('#cvTryResult .lg-rule .lg-rule-name').allInnerTexts();
  assert.deepEqual(names, ruleEvents.map(row => row.name), 'rules are not in backend order');
  assert.ok(rows[0].indexOf('未命中') >= 0, 'rule 1 was not marked as missed');
  assert.ok(rows[1].indexOf('命中') >= 0 && rows[1].indexOf('未命中') < 0,
    'rule 2 was not marked as the first hit');
  assertIncludes(text, '首条命中后停止');

  /* 恰好一次新的 JEV 调用（对当前草稿，不落盘不发布）。 */
  const after = mockState.systemoneCalls.filter(call => call.scenario === 's1').length;
  assert.equal(after, before + 1);
  assert.equal((await appSnapshot()).kind, 'clean', 'a dry run must not dirty the draft');
});

test('dry run renders the distinct jev_error and no_rule_matched copy', async () => {
  await openBaselineEditor();

  /* jev_error：s5 场景整次 JEV 调用 504，无答案无规则判断，走兜底。 */
  const jevError = await runTryAndWait(scenarioText('s5'));
  assert.equal(jevError.fallback, true);
  assert.equal(jevError.reason, 'jev_error');
  assert.equal(jevError.matched_rule, null);
  assert.equal(jevError.target, 'safe');
  let text = await page.locator('#cvTryResult').innerText();
  assertIncludes(text, '决策服务失败');
  assert.ok(text.indexOf('没有规则命中') < 0, 'jev_error showed the no_rule_matched copy');
  assertIncludes(await page.locator('#cvTryResult [data-sec="jev"]').innerText(), '失败');
  assert.equal(await page.locator('#cvTryResult .lg-rule').count(), 0,
    'a failed JEV call must not show rule results');
  assert.equal(await page.locator('#cvTryResult [data-sec="questions"]').count(), 0,
    'a failed JEV call must not show answers');

  /* no_rule_matched：把草稿规则全部改成永不可能命中（mock 的 adversarial 固定 0.08）。 */
  const accepted = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.change('behavior', draft => {
      draft.policy.rules.forEach(rule => {
        rule.when = { answer: 'adversarial.yes', gte: 0.95 };
      });
    });
  });
  assert.equal(accepted, true, 'the draft edit was rejected');
  const noRule = await runTryAndWait(scenarioText('s2'));
  assert.equal(noRule.fallback, true);
  assert.equal(noRule.reason, 'no_rule_matched');
  assert.equal(noRule.matched_rule, null);
  assert.equal(noRule.target, 'safe');
  const rows = await page.locator('#cvTryResult .lg-rule').allInnerTexts();
  assert.equal(rows.length, EXAMPLE_RULES, 'all rules must be evaluated when none matches');
  assert.ok(rows.every(row => row.indexOf('未命中') >= 0),
    'a no-match trace must mark every rule as missed');
  text = await page.locator('#cvTryResult').innerText();
  assertIncludes(text, '没有规则命中');
  assert.ok(text.indexOf('决策服务失败') < 0, 'no_rule_matched showed the jev_error copy');
  assert.equal(text.indexOf('首条命中后停止') >= 0, false,
    'a no-match trace must not claim a first-match stop');
  /* 试算读的是草稿本体：dirty 状态原样保留。 */
  assert.equal((await appSnapshot()).kind, 'behavior');

  /* 收尾：撤销草稿编辑回到 clean 基线，供后续用例导航。 */
  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean';
  });
});

test('an empty-rules draft skips the JEV call and renders no JEV block', async () => {
  await openBaselineEditor();
  const callsBefore = mockState.systemoneCalls.length;
  const accepted = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.change('behavior', draft => { draft.policy.rules = []; });
  });
  assert.equal(accepted, true);

  const result = await runTryAndWait(scenarioText('s2'));
  assert.equal(result.fallback, true);
  assert.equal(result.reason, 'no_rule_matched');
  assert.equal(result.matched_rule, null);
  assert.equal(result.target, 'safe');
  /* 无规则 → 无 JEV：结果区不渲染 JEV/答案/规则区块。 */
  assert.equal(await page.locator('#cvTryResult [data-sec="jev"]').count(), 0);
  assert.equal(await page.locator('#cvTryResult [data-sec="questions"]').count(), 0);
  assert.equal(await page.locator('#cvTryResult [data-sec="rules"]').count(), 0);
  assertIncludes(await page.locator('#cvTryResult').innerText(), '没有规则命中');
  assert.equal(mockState.systemoneCalls.length, callsBefore,
    'an empty-rules dry run must not call the decision service');

  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean';
  });
});

test('a failed dry run keeps the input, the draft and the previous trace', async () => {
  await openBaselineEditor();
  /* 先成功一次，留下一条完整轨迹。 */
  const first = await runTryAndWait(S1_TEXT);
  assert.equal(first.target, 'power');
  await clearToasts();

  /* 再跑一次，让 /admin/try 整体失败（503）。 */
  expectHttpStatusError({ url: /\/admin\/try$/, status: /503/ });
  await page.route('**/admin/try', route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'mock dry-run outage' }),
  }));
  try {
    await page.locator('#cvTryInput').fill('这次试算会失败');
    await page.locator('#cvTryRun').click();
    await waitToastIncludes('试算失败');

    /* 失败不清结果：错误提示行之下保留上一次成功轨迹。 */
    const box = await page.locator('#cvTryResult').innerText();
    assertIncludes(box, '试算失败');
    assertIncludes(box, 'power');
    assertIncludes(box, '电力业务优先');
    assertIncludes(box, '一次 JEV');
    /* 输入框文本与草稿都不动。 */
    assert.equal(await page.locator('#cvTryInput').inputValue(), '这次试算会失败',
      'a failed dry run cleared the input');
    assert.equal((await appSnapshot()).kind, 'clean',
      'a failed dry run must not touch the draft');
  } finally {
    await page.unroute('**/admin/try');
    resetExpectedHttpStatusErrors();
  }
});

await runIfMain(import.meta.url);
