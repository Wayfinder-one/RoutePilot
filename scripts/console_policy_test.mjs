/* Policy v4 草稿与控制器契约测试（Task 8 · brief Step 2/Step 3）。
 *
 * 运行：node --test scripts/console_policy_test.mjs
 * DraftStore 用例是 brief 的逐字测试（policy/layout 独立 dirty 与基线）；
 * 控制器用例拦截 api.req —— 管理面客户端的唯一出口 —— 断言：
 *   · 新建 POST /admin/policies 的 body 恰为 {id, policy}；
 *   · 已有资源 PUT 的 body 直接是完整 v4 文档，options.headers 带 If-Match；
 *   · 保存布局只 PUT /admin/policies/{id}/layout，不带策略哈希；
 *   · 409 冲突不覆盖本地草稿与基线；
 *   · 启用 dirty policy 必须先 savePolicy 成功，否则绝不发 enable。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { api } from '../router/console/api.js';
import { DraftStore } from '../router/console/routing/draft.js';
import { RoutingController } from '../router/console/routing/controller.js';
import { behaviorCanonical, clone, createPolicy } from '../router/console/routing/policy.js';
import { initTryRun, resetTryRun } from '../router/console/routing/tryrun.js';

/* tryrun.js 的渲染入口按 id 查 DOM；node 直测没有页面，给一个永远
   返回 null 的最小 document 桩：渲染路径整体空跑，状态逻辑照常执行。 */
if (typeof globalThis.document === 'undefined') {
  globalThis.document = { getElementById: () => null };
}

/* ── DraftStore：brief 逐字用例 ─────────────────────────────────── */

function makeResource() {
  return {
    id: 'main', enabled: false,
    document_sha256: 'a'.repeat(64), policy_revision: 'b'.repeat(64),
    updated_at: '2026-11-18T00:00:00+00:00', policy: createPolicy('fast')
  };
}

test('policy and layout have independent dirty state and baselines', () => {
  const store = new DraftStore();
  const resource = makeResource();
  store.load(resource, { panels: { left: 20 } }, { saved: true });
  assert.equal(store.dirtyKind, 'clean');
  store.change('behavior', draft => { draft.policy.rules.reverse(); });
  assert.equal(store.dirtyKind, 'behavior');
  store.undo();
  store.change('layout', draft => { draft.layout.panels.left = 80; });
  assert.equal(store.dirtyKind, 'layout');
  assert.equal(store.saved.resource.policy.rules[0].name, '轻量请求');
  store.markLayoutSaved({ panels: { left: 80 } });
  assert.equal(store.dirtyKind, 'clean');
});

test('markSaved replaces the policy baselines only and keeps layout dirty', () => {
  const store = new DraftStore();
  store.load(makeResource(), { panels: { left: 20 } }, { saved: true });
  store.change('layout', draft => { draft.layout.panels.left = 80; });
  assert.equal(store.dirtyKind, 'layout');

  const savedBack = makeResource();
  savedBack.document_sha256 = 'c'.repeat(64);
  assert.equal(store.markSaved(savedBack), true);
  assert.equal(store.dirtyKind, 'layout', 'saving the policy cleared the layout dirty state');
  assert.equal(store.saved.resource.document_sha256, 'c'.repeat(64));
  assert.equal(store.saved.policy.fallback, 'safe');
  assert.equal(store.saved.layout.panels.left, 20, 'markSaved replaced the layout baseline');

  store.markLayoutSaved({ panels: { left: 80 } });
  assert.equal(store.dirtyKind, 'clean');
  /* 布局保存也不清策略基线：resource 哈希保持服务端值。 */
  assert.equal(store.saved.resource.document_sha256, 'c'.repeat(64));
});

/* Task 8 评审裁决：name 改动 dirty 可见、行为不可见——它是可保存的文档
   字段（徽章/beforeunload/启用先保存都要生效），但不进 behaviorCanonical，
   行为串与 policy_revision 语义不受影响。 */
test('a name-only edit is dirty-visible but behaviour-invisible', () => {
  const store = new DraftStore();
  store.load(makeResource(), { panels: { left: 20 } }, { saved: true });
  assert.equal(store.dirtyKind, 'clean');
  const canonical = behaviorCanonical(store.policy);
  assert.equal(behaviorCanonical(store.saved.policy), canonical);

  store.change('behavior', draft => { draft.policy.name = '新名字'; });
  assert.equal(store.dirtyKind, 'behavior',
    'a name-only edit must count as an unsaved document change');
  assert.equal(behaviorCanonical(store.policy), canonical,
    'name is not part of the behaviour string');
  assert.equal(behaviorCanonical(store.saved.policy), canonical);

  /* 撤销同样回到 clean（历史快照包含 name）。 */
  assert.equal(store.undo(), true);
  assert.equal(store.dirtyKind, 'clean');

  store.change('behavior', draft => { draft.policy.name = '新名字'; });
  assert.equal(store.dirtyKind, 'behavior');
  /* 服务端回带新名字：基线整体替换后回到 clean，行为串全程相等。 */
  const saved = makeResource();
  saved.policy = clone(store.policy);
  saved.document_sha256 = 'd'.repeat(64);
  assert.equal(store.markSaved(saved), true);
  assert.equal(store.dirtyKind, 'clean');
  assert.equal(store.saved.policy.name, '新名字');
  assert.equal(behaviorCanonical(store.policy), canonical);
  assert.equal(behaviorCanonical(store.saved.policy), canonical);
});

/* ── RoutingController：拦截 api.req 断言管理面 wire ─────────────── */

function stubHooks() {
  const toasts = [];
  return {
    toasts: toasts,
    toast: (message, tone) => { toasts.push({ message: message, tone: tone }); },
    render: () => {},
    renderIssues: () => {},
    revealIssues: () => {},
    refreshDirty: () => {},
    syncSaving: () => {},
    setIssues: () => {},
    renderPolicySelect: () => {},
    askDiscard: async () => true,
    askDelete: async () => true,
    getModels: () => [],
  };
}

function httpError(status, detail) {
  const error = new Error(String(status));
  error.status = status;
  error.detail = detail;
  return error;
}

function makeStoreWithPolicy() {
  const store = new DraftStore();
  store.load(makeResource(), { panels: { left: 20 } }, { saved: true });
  return store;
}

test('creating a policy POSTs {id, policy} and adopts the server resource', async () => {
  const calls = [];
  const realReq = api.req;
  api.req = async (method, path, body, opts) => {
    calls.push({ method: method, path: path, body: body, opts: opts });
    const resource = makeResource();
    resource.id = 'brand-new';
    resource.document_sha256 = 'd'.repeat(64);
    if (body && body.policy) resource.policy = clone(body.policy);
    return { policy_resource: resource };
  };
  try {
    const store = new DraftStore();
    const controller = new RoutingController({ store: store, hooks: stubHooks() });
    controller.loadNewDraft();
    const localId = controller.policyId;
    const localPolicy = clone(store.policy);
    assert.equal(store.saved, null, 'a fresh draft must be unsaved');

    assert.equal(await controller.savePolicy(), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'POST');
    assert.equal(calls[0].path, '/admin/policies');
    assert.deepEqual(Object.keys(calls[0].body).sort(), ['id', 'policy']);
    assert.equal(calls[0].body.id, localId);
    assert.deepEqual(calls[0].body.policy, localPolicy);
    assert.equal(calls[0].opts, undefined, 'creating a policy must not send If-Match');

    assert.equal(controller.policyId, 'brand-new');
    assert.equal(store.saved.resource.id, 'brand-new');
    assert.equal(store.saved.resource.document_sha256, 'd'.repeat(64));
    assert.equal(store.dirtyKind, 'clean', 'the server baseline did not match the saved draft');
  } finally {
    api.req = realReq;
  }
});

test('saving an existing policy PUTs the document directly with If-Match', async () => {
  const calls = [];
  const realReq = api.req;
  const resource = makeResource();
  const store = makeStoreWithPolicy();
  const controller = new RoutingController({ store: store, hooks: stubHooks() });
  controller.loadPolicy(resource, { panels: { left: 20 } }, true);
  api.req = async (method, path, body, opts) => {
    calls.push({ method: method, path: path, body: body, opts: opts });
    const updated = clone(resource);
    updated.document_sha256 = 'e'.repeat(64);
    updated.policy = clone(body);
    return { policy_resource: updated };
  };
  try {
    store.change('behavior', draft => { draft.policy.rules[0].name = '改名规则'; });
    assert.equal(store.dirtyKind, 'behavior');
    assert.equal(await controller.savePolicy(), true);

    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'PUT');
    assert.equal(calls[0].path, '/admin/policies/main');
    /* body 直接是完整 v4 文档，没有任何信封字段。 */
    assert.equal(calls[0].body.kind, 'sr-routing-policy');
    assert.equal(calls[0].body.version, 4);
    assert.deepEqual(calls[0].body.rules.map(rule => rule.name), ['改名规则', '增强请求']);
    assert.equal('id' in calls[0].body, false, 'PUT body must not carry the resource id');
    assert.equal('document_sha256' in calls[0].body, false, 'PUT body must not carry hashes');
    /* If-Match 携带保存基线里的 document_sha256。 */
    assert.deepEqual(calls[0].opts, { headers: { 'If-Match': 'a'.repeat(64) } });

    /* 成功后基线被服务端完整 resource 整体替换（不逐字段合并）。 */
    assert.equal(store.saved.resource.document_sha256, 'e'.repeat(64));
    assert.equal(store.saved.policy.rules[0].name, '改名规则');
    assert.equal(store.dirtyKind, 'clean');
  } finally {
    api.req = realReq;
  }
});

test('saving the layout PUTs only /layout and never sends policy hashes', async () => {
  const calls = [];
  const realReq = api.req;
  const store = makeStoreWithPolicy();
  const controller = new RoutingController({ store: store, hooks: stubHooks() });
  controller.loadPolicy(makeResource(), { panels: { left: 20 } }, true);
  api.req = async (method, path, body, opts) => {
    calls.push({ method: method, path: path, body: body, opts: opts });
    return clone(body);
  };
  try {
    store.change('layout', draft => { draft.layout.panels.left = 80; });
    assert.equal(store.dirtyKind, 'layout');
    assert.equal(await controller.saveLayout(), true);

    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'PUT');
    assert.equal(calls[0].path, '/admin/policies/main/layout');
    assert.deepEqual(calls[0].body, { panels: { left: 80 } });
    assert.equal(calls[0].opts, undefined, 'layout save must not send an If-Match header');

    /* 布局保存不动策略基线：哈希/revision 仍是服务端旧值，dirty 归零。 */
    assert.equal(store.saved.resource.document_sha256, 'a'.repeat(64));
    assert.equal(store.saved.resource.policy_revision, 'b'.repeat(64));
    assert.equal(store.saved.layout.panels.left, 80);
    assert.equal(store.dirtyKind, 'clean');
  } finally {
    api.req = realReq;
  }
});

test('a 409 conflict keeps the local draft and the saved baseline intact', async () => {
  const calls = [];
  const realReq = api.req;
  const hooks = stubHooks();
  const store = makeStoreWithPolicy();
  const controller = new RoutingController({ store: store, hooks: hooks });
  controller.loadPolicy(makeResource(), { panels: { left: 20 } }, true);
  api.req = async (method, path, body, opts) => {
    calls.push({ method: method, path: path, body: body, opts: opts });
    throw httpError(409, { code: 'policy_conflict', document_sha256: 'f'.repeat(64) });
  };
  try {
    store.change('behavior', draft => { draft.policy.fallback = 'strong'; });
    assert.equal(await controller.savePolicy(), false);

    /* 冲突绝不覆盖本地草稿：未保存的编辑原样保留，基线哈希不变。 */
    assert.equal(store.policy.fallback, 'strong', 'the conflict overwrote the local draft');
    assert.equal(store.saved.resource.document_sha256, 'a'.repeat(64),
      'the conflict replaced the baseline hash');
    assert.equal(store.saved.policy.fallback, 'safe');
    assert.equal(store.dirtyKind, 'behavior');
    assert.ok(hooks.toasts.length >= 1, 'the conflict produced no visible toast');
  } finally {
    api.req = realReq;
  }
});

test('enabling a dirty policy requires a successful save first', async () => {
  const calls = [];
  const realReq = api.req;
  const store = makeStoreWithPolicy();
  const hooks = stubHooks();
  const controller = new RoutingController({ store: store, hooks: hooks });
  controller.loadPolicy(makeResource(), { panels: {} }, true);
  let putSucceeds = false;
  api.req = async (method, path) => {
    calls.push({ method: method, path: path });
    if (method === 'PUT') {
      if (!putSucceeds) {
        throw httpError(409, { code: 'policy_conflict', document_sha256: 'f'.repeat(64) });
      }
      const updated = makeResource();
      updated.policy = clone(store.policy);
      updated.document_sha256 = 'e'.repeat(64);
      return { policy_resource: updated };
    }
    if (path === '/admin/policies/main/enable') {
      return { policies: [], snapshot_revision: '', policy_status: {} };
    }
    throw new Error('unexpected request ' + method + ' ' + path);
  };
  try {
    store.change('behavior', draft => { draft.policy.fallback = 'strong'; });
    assert.equal(store.dirtyKind, 'behavior');

    /* 保存失败（409）：enable 绝不发出，本地草稿保留。 */
    assert.equal(await controller.enable(), false);
    assert.deepEqual(calls.map(call => call.method + ' ' + call.path),
      ['PUT /admin/policies/main'],
      'enable was sent although the preceding save failed');
    assert.equal(store.policy.fallback, 'strong');

    /* 保存成功后：先 PUT 再 POST enable，且各只一次。 */
    calls.length = 0;
    putSucceeds = true;
    assert.equal(await controller.enable(), true);
    assert.deepEqual(calls.map(call => call.method + ' ' + call.path),
      ['PUT /admin/policies/main', 'POST /admin/policies/main/enable']);
    assert.equal(store.resource.enabled, true, 'the toolbar state did not adopt enabled');
  } finally {
    api.req = realReq;
  }
});

/* ── tryrun：resetTryRun 必须释放在途试算留下的运行锁 ─────────────── */

test('resetTryRun releases a stuck try lock so the draft stays editable', () => {
  const store = new DraftStore();
  store.load(makeResource(), {}, { saved: true });
  /* 复现锁定路径：一次试算把 runSeq 记在案后，装载/新建让序号前进——
     在途请求的 finally 因 seq !== runSeq 永不释放，锁必须由 reset 交还。 */
  store.setTryRunning(true);
  assert.equal(store.change('behavior', draft => { draft.policy.name = 'locked'; }), false);

  let toolbars = 0;
  initTryRun({ store: store, toast: () => {}, updateToolbar: () => { toolbars += 1; } });
  try {
    resetTryRun();
    assert.equal(store.tryRunning, false, 'resetTryRun left the try lock held forever');
    assert.equal(store.change('behavior', draft => { draft.policy.name = 'unlocked'; }), true,
      'the editor stayed locked after the draft was replaced');
    assert.ok(toolbars >= 1, 'resetTryRun did not refresh the toolbar');
  } finally {
    initTryRun(null);
  }
});
