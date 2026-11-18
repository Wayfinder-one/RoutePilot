/* DraftStore 纯状态契约测试（Task 8 重写：{resource, policy, layout} 草稿）。
 *
 * 运行：node --test scripts/console_draft_test.mjs
 * 约束：不加载 DOM、不访问 localStorage / fetch；store 只做内存状态。
 * policy/layout 双基线独立性的主断言（brief 逐字用例）在
 * console_policy_test.mjs；这里保留重定位后仍然成立的撤销/重做机制、
 * 历史上限、运行/保存锁、基线整体替换与订阅通知用例。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { DraftStore, HISTORY_LIMIT } from '../router/console/routing/draft.js';
import { clone, createPolicy } from '../router/console/routing/policy.js';

function makeResource(model) {
  return {
    id: 'main', enabled: false,
    document_sha256: 'a'.repeat(64), policy_revision: 'b'.repeat(64),
    updated_at: '2026-11-18T00:00:00+00:00', policy: createPolicy(model || 'qwen3-8b'),
  };
}

function storeWith(model) {
  const store = new DraftStore();
  store.load(makeResource(model), {}, { saved: true });
  return store;
}

test('change records one undo entry and redo is cleared', () => {
  const store = new DraftStore();
  store.load(makeResource());
  assert.equal(store.change('behavior', draft => { draft.policy.name = 'one'; }), true);
  assert.equal(store.undoStack.length, 1);
  assert.equal(store.undo(), true);
  assert.equal(store.redoStack.length, 1);
  assert.equal(store.change('behavior', draft => { draft.policy.name = 'two'; }), true);
  assert.equal(store.redoStack.length, 0, 'a new change did not clear the redo stack');
  assert.equal(store.undoStack.length, 1);
  assert.equal(store.policy.name, 'two');
});

test('change is a no-op without a loaded draft or while locked', () => {
  const empty = new DraftStore();
  assert.equal(empty.change('behavior', () => { throw new Error('must not mutate'); }), false);

  const running = storeWith();
  running.setTryRunning(true);
  assert.equal(running.change('behavior', draft => { draft.policy.name = 'x'; }), false);
  assert.equal(running.undo(), false);
  assert.equal(running.redo(), false);
  assert.equal(running.policy.name, '新策略');

  running.setTryRunning(false);
  const saving = storeWith();
  saving.setSaving(true);
  assert.equal(saving.change('behavior', draft => { draft.policy.name = 'y'; }), false);
  assert.equal(saving.policy.name, '新策略');
  assert.equal(saving.undoStack.length, 0);

  /* 保存锁同样拒绝 undo/redo，且不得消耗栈。 */
  const locked = storeWith();
  assert.equal(locked.change('behavior', draft => { draft.policy.name = 'one'; }), true);
  assert.equal(locked.change('behavior', draft => { draft.policy.name = 'two'; }), true);
  assert.equal(locked.undo(), true);
  assert.equal(locked.undoStack.length, 1);
  assert.equal(locked.redoStack.length, 1);
  const current = locked.draft;
  const name = locked.policy.name;

  locked.setSaving(true);
  assert.equal(locked.undo(), false, 'undo ran while a save was in flight');
  assert.equal(locked.redo(), false, 'redo ran while a save was in flight');
  assert.equal(locked.undoStack.length, 1, 'a blocked undo consumed the undo stack');
  assert.equal(locked.redoStack.length, 1, 'a blocked redo consumed the redo stack');
  assert.equal(locked.draft, current, 'a blocked undo/redo replaced the draft');
  assert.equal(locked.policy.name, name);

  locked.setSaving(false);
  assert.equal(locked.redo(), true);
  assert.equal(locked.policy.name, 'two');
});

test('change snapshots before/after so later mutations do not leak into history', () => {
  const store = storeWith();
  assert.equal(store.change('behavior', draft => {
    draft.policy.targets.strong.models = [{ model: 'qwen3-32b' }];
  }), true);
  const entry = store.undoStack[store.undoStack.length - 1];
  assert.equal(entry.kind, 'behavior');
  assert.equal(entry.before.policy.targets.strong.models[0].model, 'qwen3-8b');
  assert.equal(entry.after.policy.targets.strong.models[0].model, 'qwen3-32b');
  assert.deepEqual(entry.before.layout, {});
  store.policy.targets.strong.models[0].model = 'mutated-after-the-fact';
  assert.equal(entry.after.policy.targets.strong.models[0].model, 'qwen3-32b');
});

test('dirty classification covers behavior, layout and clean', () => {
  const store = storeWith();
  assert.equal(store.dirtyKind, 'clean');

  store.change('behavior', draft => {
    draft.policy.targets.strong.models = [{ model: 'qwen3-32b' }];
  });
  assert.equal(store.dirtyKind, 'behavior');

  store.undo();
  assert.equal(store.dirtyKind, 'clean');

  store.change('layout', draft => { draft.layout.collapsed = { policyRules: true }; });
  assert.equal(store.dirtyKind, 'layout');

  /* 布局改动叠在行为改动上：behavior 优先显示，但谁也不清谁。 */
  store.change('behavior', draft => { draft.policy.fallback = 'strong'; });
  assert.equal(store.dirtyKind, 'behavior');
  store.undo();
  assert.equal(store.dirtyKind, 'layout', 'an unrelated undo cleared the layout dirty state');

  store.undo();
  assert.equal(store.dirtyKind, 'clean');
});

test('history caps at 100 entries and drops the oldest', () => {
  const store = storeWith();
  for (let index = 0; index < HISTORY_LIMIT + 1; index += 1) {
    assert.equal(store.change('behavior', draft => { draft.policy.name = 'draft-' + index; }), true);
  }
  assert.equal(store.undoStack.length, HISTORY_LIMIT);
  assert.equal(store.undoStack[0].after.policy.name, 'draft-1');
  assert.equal(store.undoStack[store.undoStack.length - 1].after.policy.name, 'draft-100');

  let undone = 0;
  while (store.undo()) undone += 1;
  assert.equal(undone, HISTORY_LIMIT);
  /* 第 1 条历史被上限丢弃：撤到底只能回到 draft-0。 */
  assert.equal(store.policy.name, 'draft-0');
  assert.equal(store.redoStack.length, HISTORY_LIMIT);
});

test('undo and redo walk the stack without clearing the other direction', () => {
  const store = storeWith();
  store.change('behavior', draft => { draft.policy.name = 'one'; });
  store.change('behavior', draft => { draft.policy.name = 'two'; });

  assert.equal(store.undo(), true);
  assert.equal(store.policy.name, 'one');
  assert.equal(store.canRedo, true);
  assert.equal(store.redo(), true);
  assert.equal(store.policy.name, 'two');
  assert.equal(store.canRedo, false);
  assert.equal(store.canUndo, true);
  assert.equal(store.undo(), true);
  assert.equal(store.undo(), true);
  assert.equal(store.policy.name, '新策略');
  assert.equal(store.undo(), false);
  assert.equal(store.redo(), true);
  assert.equal(store.policy.name, 'one');
});

test('undo and redo never roll back server-owned resource metadata', () => {
  const store = storeWith();
  store.change('behavior', draft => { draft.policy.name = 'one'; });
  /* 模拟 setEnabled 的直写：草稿与基线的元数据视图同步置位。 */
  store.draft.resource.enabled = true;
  store.saved.resource.enabled = true;
  store.undo();
  assert.equal(store.policy.name, '新策略');
  /* id/enabled/revision 属于服务端：历史快照不得把它们带回旧值。 */
  assert.equal(store.resource.enabled, true, 'undo rolled back the server-owned enabled state');
  assert.equal(store.resource.id, 'main');
  assert.equal(store.dirtyKind, 'clean');
});

test('markSaved replaces the baselines wholesale and by value', () => {
  const store = storeWith();
  store.change('behavior', draft => { draft.policy.name = '本地草稿'; });
  assert.equal(store.dirtyKind, 'clean', 'name edits are not behaviour by canonical');

  const server = clone(store.draft.resource);
  server.policy_revision = 'abc123';
  server.policy = clone(store.policy);
  assert.equal(store.markSaved(server), true);
  assert.equal(store.saved.resource.policy_revision, 'abc123');
  assert.equal(store.saved.policy.name, '本地草稿');

  /* 基线按值保存：之后改草稿不能回写 saved。 */
  store.policy.name = '再次本地修改';
  assert.equal(store.saved.policy.name, '本地草稿');

  /* 替换而不是合并：新基线缺 policy_revision 时旧字段不能残留。 */
  const withoutRevision = clone(server);
  delete withoutRevision.policy_revision;
  assert.equal(store.markSaved(withoutRevision), true);
  assert.equal('policy_revision' in store.saved.resource, false,
    'markSaved merged into the previous baseline instead of replacing it');

  const again = clone(store.draft.resource);
  again.policy = clone(store.policy);
  assert.equal(store.markSaved(again), true);
  assert.equal(store.dirtyKind, 'clean');
});

test('load resets both history stacks and the saved baseline', () => {
  const store = storeWith();
  store.change('behavior', draft => { draft.policy.name = 'dirty'; });
  store.load(makeResource('qwen3-32b'), { panels: { left: 9 } }, { saved: true });
  assert.equal(store.undoStack.length, 0);
  assert.equal(store.redoStack.length, 0);
  assert.equal(store.dirtyKind, 'clean');
  assert.equal(store.saved.policy.targets.strong.models[0].model, 'qwen3-32b');
  assert.equal(store.saved.layout.panels.left, 9);
  assert.equal(store.layout.panels.left, 9);

  store.load(makeResource('qwen3-32b'), {});
  assert.equal(store.saved, null);
  assert.equal(store.dirtyKind, 'behavior');
});

test('subscribers observe load/change/undo/redo/saved/layoutSaved events', () => {
  const store = new DraftStore();
  const seen = [];
  const off = store.subscribe(event => seen.push(event.type));
  store.load(makeResource(), {}, { saved: true });
  store.change('behavior', draft => { draft.policy.name = 'x'; });
  store.undo();
  store.redo();
  store.markSaved(makeResource());
  store.markLayoutSaved({ panels: { left: 3 } });
  off();
  store.change('behavior', draft => { draft.policy.name = 'y'; });
  assert.deepEqual(seen, ['load', 'change', 'undo', 'redo', 'saved', 'layoutSaved']);
});
