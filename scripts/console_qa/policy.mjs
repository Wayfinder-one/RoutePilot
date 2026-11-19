/* RoutePilot 控制台 QA · 策略编辑页（Task 8 · brief Step 6）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实 router（临时库 +
   ROUTER_POLICY_DIR）、mock 决策服务、浏览器与 teardown 全部由
   ../console_qa.mjs 提供。用例走真实 /admin/policies / /admin/try API 与
   真实页面，覆盖 brief 的场景链：
   创建 → 编辑规则 → 保存 → 重载 → If-Match 冲突 → 启用（先保存）→
   布局独立保存；外加 >8 选项软提示不阻塞、目录晚到不清草稿、
   试算面板只读展示一次 JEV 与逐条规则。
   入口：`node scripts/console_qa.mjs`（全量）或
         `QA_ONLY=policy node scripts/console_qa.mjs`；也可单独执行：
         `node scripts/console_qa/policy.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  EXAMPLE,
  EXAMPLE_QUESTIONS,
  EXAMPLE_RULES,
  POLICY_ID,
  S1_TEXT,
  api,
  appSnapshot,
  assertIncludes,
  clearToasts,
  dirtyBadge,
  ensureBaselinePolicy,
  expectHttpStatusError,
  mockState,
  openEditor,
  page,
  resetExpectedHttpStatusErrors,
  routerBase,
  runIfMain,
  serverPolicyResource,
  putPolicy,
  test,
  waitFor,
  waitToastIncludes,
} from '../console_qa.mjs';

/* 打开基线策略：ensureBaselinePolicy 恢复 power-policy 为示例文档并启用；
   页面 boot 会挑「已启用或第一个」策略，若不是 power-policy 就显式切换。 */
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

async function waitCleanSaved(timeout) {
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean' && !!app.saved;
  }, null, { timeout: timeout || 12000 });
}

/* 文本字段以 change（blur）提交：fill 后必须 blur 才进 store.change。 */
async function commitText(locator, value) {
  await locator.fill(value);
  await locator.evaluate(element => element.blur());
}

test('policy editor boots the enabled policy and renders its document', async () => {
  const snap = await openBaselineEditor();
  assert.equal(snap.booted, true);
  assert.equal(snap.policyId, POLICY_ID);
  assert.equal(snap.kind, 'clean');
  assert.deepEqual(snap.questions.slice().sort(), EXAMPLE_QUESTIONS.slice().sort());
  assert.deepEqual(snap.targets.slice().sort(), ['fast', 'power', 'safe', 'strong']);
  assert.equal(snap.rules.length, EXAMPLE_RULES);
  assert.equal(snap.rules[0].name, EXAMPLE.rules[0].name);

  /* 四个编辑区都渲染了真实卡片；规则区明确「首条命中生效」。 */
  assert.equal(await page.locator('#policyQuestions .pv-card').count(),
    EXAMPLE_QUESTIONS.length);
  assert.equal(await page.locator('#policyTargets .pv-card').count(), 4);
  assert.equal(await page.locator('#policyRules .pv-card').count(), EXAMPLE_RULES);
  assert.ok((await page.locator('.pv-first-hit').textContent()).indexOf('首条命中') >= 0);
  /* 顶栏：已启用徽章 + 策略选择框 + 版本号。 */
  assert.equal(await page.evaluate(() =>
    document.getElementById('cvEnabled').hidden), false);
  assert.equal(await page.locator('#cvPolicySelect').inputValue(), POLICY_ID);
  assert.equal(await page.locator('#cvPolicySelect option[value="' + POLICY_ID + '"]').count(), 1);
  assert.match(await page.locator('#cvRevision').textContent(), /版本 [0-9a-f]{4,}/);
});

test('editor creates a new policy, saves it and adopts the server resource', async () => {
  await openBaselineEditor();
  await clearToasts();
  assert.equal((await appSnapshot()).kind, 'clean');

  await page.locator('#cvNewPolicy').click();
  await waitToastIncludes('已新建策略草稿');
  const snap = await appSnapshot();
  assert.notEqual(snap.policyId, POLICY_ID);
  assert.equal(snap.kind, 'behavior', 'a fresh draft must show as unsaved behaviour');
  assert.deepEqual(snap.questions, ['route']);
  assert.equal(snap.rules.length, 2);
  assert.equal(snap.targets.length, 3);

  const list = await api('GET', '/admin/policies');
  assert.equal(list.policies.some(item => item.id === snap.policyId), false,
    'the draft leaked to the server before saving');

  /* 删除门控：未保存草稿只持有本地 UUID 形状的 policyId，删除必须禁用
     （否则 DELETE 打到不存在的资源，页面只剩「删除失败」toast）。 */
  assert.equal(await page.locator('#cvDeletePolicy').isDisabled(), true,
    'delete is clickable for a never-saved local draft');

  await commitText(page.locator('#policySettings [data-field="s-name"]'), 'QA 新建策略');
  await page.locator('#cvSave').click();
  await waitCleanSaved();
  assert.equal(await page.locator('#cvDeletePolicy').isDisabled(), false,
    'delete stayed disabled after the draft was saved');

  const created = await serverPolicyResource(snap.policyId);
  assert.equal(created.policy.name, 'QA 新建策略');
  assert.equal(created.enabled, false);
  assert.equal(created.policy.kind, 'sr-routing-policy');
  assert.equal(created.policy.rules.length, 2);
  assert.equal(created.policy.fallback, 'safe');
  assert.match(created.document_sha256, /^[0-9a-f]{64}$/);
  /* If-Match 语义的新基线：再次保存必须带上它（controller 契约测试已覆盖
     header 形状；这里验证真实回包成了保存基线）。 */
  const after = await appSnapshot();
  assert.equal(after.kind, 'clean');
  assert.equal(after.policyId, snap.policyId);
});

test('rule edits flow through store.change, save and survive a reload', async () => {
  /* 承接上一条：页面仍在刚创建的「QA 新建策略」上。 */
  const before = await appSnapshot();
  assert.equal(before.name, 'QA 新建策略');
  assert.equal(before.kind, 'clean');

  await commitText(page.locator('[data-ri="0"] [data-field="r-name"]'), 'QA 改过的规则');
  let snap = await appSnapshot();
  assert.equal(snap.kind, 'behavior');
  assert.equal(snap.history.undo, true, 'one edit did not create one history entry');
  assert.equal(snap.rules[0].name, 'QA 改过的规则');

  /* 规则顺序可编辑（首条命中的行为语义）。 */
  await page.locator('[data-ri="1"] [data-act="r-up"]').click();
  snap = await appSnapshot();
  assert.equal(snap.rules[0].name, '增强请求');

  await page.locator('#policyRules [data-act="r-add"]').click();
  snap = await appSnapshot();
  assert.equal(snap.rules.length, 3);

  await page.locator('#cvSave').click();
  await waitCleanSaved();
  const saved = await serverPolicyResource(snap.policyId);
  assert.deepEqual(saved.policy.rules.map(rule => rule.name),
    ['增强请求', 'QA 改过的规则', '新规则']);

  /* 重载（全新 boot）后切回该策略：读回的是服务端版本。 */
  await openEditor();
  await page.selectOption('#cvPolicySelect', snap.policyId);
  await waitFor(async id => {
    const { app } = await import('/console/routing/app.js');
    return app.policyId === id && app.changeKind === 'clean';
  }, snap.policyId, { timeout: 10000 });
  const after = await appSnapshot();
  assert.equal(after.policyId, snap.policyId);
  assert.deepEqual(after.rules.map(rule => rule.name),
    ['增强请求', 'QA 改过的规则', '新规则']);
  assert.equal(after.name, 'QA 新建策略');
});

test('If-Match conflict keeps the local draft and never overwrites it', async () => {
  await openBaselineEditor();

  /* 服务端被“别人”改过：document_sha256 已前进。 */
  const bumped = structuredClone(EXAMPLE);
  bumped.name = '被别人改过的策略';
  await putPolicy(POLICY_ID, bumped);

  await commitText(page.locator('[data-ri="0"] [data-field="r-name"]'), '本地未保存的规则名');
  assert.equal((await appSnapshot()).kind, 'behavior');

  /* 故意制造的 409 会被 Chromium 记成 console error：按基座惯例精确声明。 */
  await clearToasts();
  expectHttpStatusError({ url: /\/admin\/policies\/power-policy$/, status: /409/ });
  try {
    await page.locator('#cvSave').click();
    await waitToastIncludes('策略已被其他修改更新');
  } finally {
    resetExpectedHttpStatusErrors();
  }

  const snap = await appSnapshot();
  assert.equal(snap.kind, 'behavior', 'the conflict cleared the dirty state');
  assert.equal(snap.rules[0].name, '本地未保存的规则名',
    'the 409 response overwrote the local draft');
  const server = await serverPolicyResource(POLICY_ID);
  assert.equal(server.policy.name, '被别人改过的策略');
  assert.equal(server.policy.rules[0].name, EXAMPLE.rules[0].name,
    'the rejected draft leaked to the server');

  /* 撤销本地编辑后重载（不先 ensure：要看的就是服务端的“别人改过”版本）。 */
  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean';
  });
  await openEditor();
  const reloaded = await appSnapshot();
  assert.equal(reloaded.policyId, POLICY_ID);
  assert.equal(reloaded.name, '被别人改过的策略',
    'the reload did not show the server-side winner');
  assert.equal(reloaded.kind, 'clean');
  /* 收尾：恢复示例基线，供后续用例。 */
  await ensureBaselinePolicy();
});

test('enabling a dirty policy saves it first', async () => {
  await openBaselineEditor();
  /* 先停用：重开后显式切回 power-policy（列表按 updated_at 排序，
     boot 的「第一个」不保证是它）。 */
  await api('POST', '/admin/policies/' + POLICY_ID + '/disable');
  await openEditor();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.booted;
  }, null, { timeout: 20000 });
  if ((await appSnapshot()).policyId !== POLICY_ID) {
    await page.selectOption('#cvPolicySelect', POLICY_ID);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.policyId === id;
    }, POLICY_ID, { timeout: 10000 });
  }
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.resource.enabled === false;
  }, null, { timeout: 5000 });

  await commitText(page.locator('[data-ri="0"] [data-field="r-name"]'), '启用前先保存的规则');
  assert.equal((await appSnapshot()).kind, 'behavior');

  const requests = [];
  const listener = request => {
    if (request.url().indexOf('/admin/policies/' + POLICY_ID) >= 0) {
      requests.push(request.method() + ' ' + new URL(request.url()).pathname);
    }
  };
  page.on('request', listener);
  try {
    await page.locator('#cvEnable').click();
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.resource.enabled === true && app.changeKind === 'clean';
    }, null, { timeout: 15000 });
  } finally {
    page.removeListener('request', listener);
  }
  /* 保存先于启用：PUT 在 POST enable 之前，且各自只发一次。 */
  assert.equal(requests[0], 'PUT /admin/policies/' + POLICY_ID,
    'enable was issued before the save PUT');
  assert.equal(requests.filter(item => item === 'PUT /admin/policies/' + POLICY_ID).length, 1);
  assert.equal(requests.filter(
    item => item === 'POST /admin/policies/' + POLICY_ID + '/enable').length, 1);

  const server = await serverPolicyResource(POLICY_ID);
  assert.equal(server.enabled, true);
  assert.equal(server.policy.rules[0].name, '启用前先保存的规则');
  assert.equal(await page.evaluate(() =>
    document.getElementById('cvEnabled').hidden), false);
});

test('layout saves independently of the policy document', async () => {
  await openBaselineEditor();
  const beforeServer = await serverPolicyResource(POLICY_ID);

  /* 折叠规则区 = 纯 UI 数据：layout dirty，而不是 behavior。 */
  await page.locator('#policyRules [data-act="sec-toggle"]').click();
  let snap = await appSnapshot();
  assert.equal(snap.kind, 'layout');
  assert.equal(snap.layout.collapsed.policyRules, true);
  const badge = await dirtyBadge();
  assert.equal(badge.kind, 'layout');
  assert.ok((badge.text || '').indexOf('布局') >= 0);

  const requests = [];
  const listener = request => {
    if (request.url().indexOf('/admin/') >= 0) {
      requests.push(request.method() + ' ' + new URL(request.url()).pathname);
    }
  };
  page.on('request', listener);
  try {
    await page.locator('#cvSaveLayout').click();
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.changeKind === 'clean';
    });
  } finally {
    page.removeListener('request', listener);
  }
  /* 只 PUT /layout：不带 If-Match，也不碰策略资源。 */
  assert.deepEqual(requests, ['PUT /admin/policies/' + POLICY_ID + '/layout']);
  const afterServer = await serverPolicyResource(POLICY_ID);
  assert.equal(afterServer.document_sha256, beforeServer.document_sha256,
    'saving the layout bumped the policy document hash');
  assert.equal(afterServer.policy_revision, beforeServer.policy_revision,
    'saving the layout bumped the policy revision');

  /* 布局真的独立落盘：全新 boot 后规则区仍然折叠。 */
  await openEditor();
  const snap2 = await appSnapshot();
  if (snap2.policyId !== POLICY_ID) {
    await page.selectOption('#cvPolicySelect', POLICY_ID);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.policyId === id;
    }, POLICY_ID, { timeout: 10000 });
  }
  assert.equal(await page.evaluate(() =>
    document.getElementById('policyRules').classList.contains('is-collapsed')), true,
    'the saved layout did not survive a reload');
  assert.equal((await appSnapshot()).kind, 'clean');

  /* 收尾：展开并保存，避免影响后续用例。 */
  await page.locator('#policyRules [data-act="sec-toggle"]').click();
  await page.locator('#cvSaveLayout').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean';
  });
});

test('more than 8 choice options shows a non-blocking hint and save stays enabled', async () => {
  await openBaselineEditor();
  /* route 示例有 3 个选项：加 6 次到 9 个。 */
  for (let index = 0; index < 6; index += 1) {
    await page.locator('#policyQuestions [data-qid="route"] [data-act="opt-add"]').click();
  }
  const hint = await page.locator('#policyQuestions .pv-hint').textContent();
  assert.ok(hint.indexOf('9') >= 0, 'the option-count hint did not render');
  assert.ok(hint.indexOf('非阻塞') >= 0, 'the hint did not say it is non-blocking');
  assert.equal(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.policy.questions.route.options.length;
  }), 9);
  /* 软提示不禁用保存。 */
  assert.equal(await page.locator('#cvSave').isDisabled(), false,
    'the option-count hint disabled the save button');

  /* 撤销 6 次添加回到基线（3 选项、clean），供后续用例导航。 */
  for (let index = 0; index < 6; index += 1) {
    await page.locator('#cvUndo').click();
  }
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean';
  });
  assert.equal(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.policy.questions.route.options.length;
  }), EXAMPLE.questions.route.options.length);
});

test('late catalogues do not lock the editor or wipe unsaved edits', async () => {
  await ensureBaselinePolicy();
  let release = () => {};
  const gate = new Promise(resolve => { release = resolve; });
  const hold = async route => { await gate; await route.continue(); };
  await page.route('**/admin/models', hold);
  await page.route('**/admin/time-periods', hold);
  try {
    await page.goto(routerBase + '/console/routing.html');
    /* 策略先于目录装配：boot 不被慢目录锁住。 */
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.booted && !!app.policy;
    }, null, { timeout: 20000 });
    assert.equal(await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.models.length;
    }), 0, 'the catalogue resolved before the test released it');
    assert.equal(await page.locator('#policyRules .pv-card').count(), EXAMPLE_RULES,
      'the editor did not render while catalogues were pending');

    /* 目录挂起时编辑规则：一次 store.change，一个历史项。 */
    await commitText(page.locator('[data-ri="0"] [data-field="r-name"]'),
      '目录晚到也要保住这条编辑');
    const dirty = await appSnapshot();
    assert.equal(dirty.kind, 'behavior');
    assert.equal(dirty.rules[0].name, '目录晚到也要保住这条编辑');

    release();
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.models.length > 0 && app.periods.length > 0;
    });
    await page.waitForTimeout(300);

    /* 晚到的目录重绘不得清掉未保存编辑。 */
    const after = await appSnapshot();
    assert.equal(after.rules[0].name, '目录晚到也要保住这条编辑',
      'the late catalogue refresh wiped an unsaved edit');
    assert.equal(after.kind, 'behavior');
    /* 卡片本体只有一张（q-del 按钮也带 data-qid，故限定 article）。 */
    assert.equal(await page.locator('#policyQuestions article[data-qid="route"]').count(), 1);
    /* 重绘后的目标下拉框真的拿到了目录。 */
    assertIncludes(await page.locator('#policyTargets [data-field="m-model"]')
      .first().textContent(), 'qwen3-8b');
  } finally {
    release();
    await page.unroute('**/admin/models');
    await page.unroute('**/admin/time-periods');
  }
  /* 收尾：撤销编辑，保持下一条用例可导航（beforeunload 拦截 dirty 草稿）。 */
  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.changeKind === 'clean';
  });
});

test('dry run panel shows one JEV, ordered rules and the final target', async () => {
  await openBaselineEditor();
  const powerCallsBefore = mockState.systemoneCalls
    .filter(call => call.scenario === 'power').length;

  await page.locator('#cvTryInput').fill(S1_TEXT);
  await page.locator('#cvTryRun').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return !!(app.tryResult && app.tryResult.result) && !app.tryRunning;
  }, null, { timeout: 20000 });

  const result = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.tryResult.result;
  });
  assert.equal(result.target, 'power');
  assert.deepEqual(result.models, ['qwen3.8-flash-sft']);
  assert.equal(result.matched_rule, '电力业务优先');
  assert.equal(result.fallback, false);
  assert.equal(result.reason, null);
  assert.equal(result.trace.filter(event => event.event === 'jev').length, 1);

  /* 面板只读展示：目标/模型/命中规则 + 一次 JEV + 逐条规则判断
     （规则行由 trace.js 渲染，Task 9 起复用 .lg-rule 区块）。 */
  const text = await page.locator('#cvTryResult').innerText();
  assertIncludes(text, 'power');
  assertIncludes(text, 'qwen3.8-flash-sft');
  assertIncludes(text, '电力业务优先');
  assertIncludes(text, '一次 JEV');
  /* 首条命中即停：已求值规则（1 未命中、2 命中）+ 未求值规则（3/4）。 */
  const rows = await page.locator('#cvTryResult .lg-rule').allInnerTexts();
  assert.equal(rows.length, 4, 'the trace must keep evaluated and skipped rules');
  assert.ok(rows[0].indexOf('未命中') >= 0, 'rule 1 was not marked as missed');
  assert.ok(rows[1].indexOf('命中') >= 0 && rows[1].indexOf('未命中') < 0,
    'rule 2 was not marked as the first hit');
  assert.ok(rows[2].indexOf('未求值') >= 0 && rows[2].indexOf('未命中') < 0,
    'rule 3 must render as skipped, not missed');
  assert.ok(rows[3].indexOf('未求值') >= 0 && rows[3].indexOf('未命中') < 0,
    'rule 4 must render as skipped, not missed');

  /* 恰好一次新的 JEV 调用（对当前草稿，不落盘不发布）。 */
  const powerCallsAfter = mockState.systemoneCalls
    .filter(call => call.scenario === 'power').length;
  assert.equal(powerCallsAfter, powerCallsBefore + 1);
  assert.equal((await appSnapshot()).kind, 'clean', 'a dry run must not dirty the draft');
});

await runIfMain(import.meta.url);
