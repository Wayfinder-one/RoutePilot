/* DraftStore 纯状态契约测试（Task 8，规格 Step 1/Step 3）。
 *
 * 运行：node --test scripts/console_draft_test.mjs
 * 约束：不加载 DOM、不访问 localStorage / fetch；store 只做内存状态。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { DraftStore, HISTORY_LIMIT } from '../router/console/routing/draft.js';
import { createMinimalTree, clone } from '../router/console/routing/graph.js';

function storeWith(model) {
  const store = new DraftStore();
  store.load(createMinimalTree(model || 'qwen3-8b'), { saved: true });
  return store;
}

test('change records one undo entry and redo is cleared', () => {
  const store = new DraftStore();
  store.load(createMinimalTree('qwen3-8b'));
  store.undoStack.push({ before: clone(store.tree), after: clone(store.tree), kind: 'display' });
  assert.equal(store.change('behavior', tree => { tree.name = 'changed'; }), true);
  assert.equal(store.tree.name, 'changed');
  assert.equal(store.redoStack.length, 0);
  assert.equal(store.undo(), true);
  assert.notEqual(store.tree.name, 'changed');
});

test('change is a no-op without a loaded tree or while locked', () => {
  const empty = new DraftStore();
  assert.equal(empty.change('behavior', () => { throw new Error('must not mutate'); }), false);

  const running = storeWith();
  running.setTryRunning(true);
  assert.equal(running.change('behavior', tree => { tree.name = 'x'; }), false);
  assert.equal(running.undo(), false);
  assert.equal(running.redo(), false);
  assert.equal(running.tree.name, '新决策组');

  running.setTryRunning(false);
  const saving = storeWith();
  saving.setSaving(true);
  assert.equal(saving.change('behavior', tree => { tree.name = 'y'; }), false);
  assert.equal(saving.tree.name, '新决策组');
  assert.equal(saving.undoStack.length, 0);
});

test('change snapshots before/after so later mutations do not leak into history', () => {
  const store = storeWith();
  assert.equal(store.change('behavior', tree => {
    tree.root.nodes[1].models = [{ model: 'qwen3-32b' }];
  }), true);
  const entry = store.undoStack[store.undoStack.length - 1];
  assert.equal(entry.kind, 'behavior');
  assert.equal(entry.before.root.nodes[1].models[0].model, 'qwen3-8b');
  assert.equal(entry.after.root.nodes[1].models[0].model, 'qwen3-32b');
  store.tree.root.nodes[1].models[0].model = 'mutated-after-the-fact';
  assert.equal(entry.after.root.nodes[1].models[0].model, 'qwen3-32b');
});

test('dirty classification covers behavior, display, layout and clean', () => {
  const store = storeWith();
  assert.equal(store.dirtyKind, 'clean');

  store.change('behavior', tree => {
    tree.root.nodes[1].models = [{ model: 'qwen3-32b' }];
  });
  assert.equal(store.dirtyKind, 'behavior');

  store.undo();
  assert.equal(store.dirtyKind, 'clean');

  store.change('display', tree => { tree.name = '仅显示名变化'; });
  assert.equal(store.dirtyKind, 'display');

  store.undo();
  store.change('layout', tree => {
    tree.root.nodes[0].position = { x: 123, y: 456 };
  });
  assert.equal(store.dirtyKind, 'layout');

  store.undo();
  assert.equal(store.dirtyKind, 'clean');
});

test('history caps at 100 entries and drops the oldest', () => {
  const store = storeWith();
  for (let index = 0; index < HISTORY_LIMIT + 1; index += 1) {
    assert.equal(store.change('behavior', tree => { tree.name = 'draft-' + index; }), true);
  }
  assert.equal(store.undoStack.length, HISTORY_LIMIT);
  assert.equal(store.undoStack[0].after.name, 'draft-1');
  assert.equal(store.undoStack[store.undoStack.length - 1].after.name, 'draft-100');

  let undone = 0;
  while (store.undo()) undone += 1;
  assert.equal(undone, HISTORY_LIMIT);
  /* 第 1 条历史被上限丢弃：撤到底只能回到 draft-0（被丢弃那条的 after）。 */
  assert.equal(store.tree.name, 'draft-0');
  assert.equal(store.redoStack.length, HISTORY_LIMIT);
});

test('undo and redo walk the stack without clearing the other direction', () => {
  const store = storeWith();
  store.change('behavior', tree => { tree.name = 'one'; });
  store.change('behavior', tree => { tree.name = 'two'; });

  assert.equal(store.undo(), true);
  assert.equal(store.tree.name, 'one');
  assert.equal(store.canRedo, true);
  assert.equal(store.redo(), true);
  assert.equal(store.tree.name, 'two');
  assert.equal(store.canRedo, false);
  assert.equal(store.canUndo, true);
  assert.equal(store.undo(), true);
  assert.equal(store.undo(), true);
  assert.equal(store.tree.name, '新决策组');
  assert.equal(store.undo(), false);
  assert.equal(store.redo(), true);
  assert.equal(store.tree.name, 'one');
});

test('markSaved replaces the baseline and resets the dirty kind', () => {
  const store = storeWith();
  store.change('behavior', tree => { tree.name = '本地草稿'; });
  assert.equal(store.dirtyKind, 'display');

  const server = clone(store.tree);
  server.name = '服务端名称';
  server.tree_revision = 'abc123';
  assert.equal(store.markSaved(server), true);
  assert.equal(store.saved.name, '服务端名称');
  assert.equal(store.saved.tree_revision, 'abc123');
  /* 基线替换是整体替换：调用方随后再改 tree 不应回写到 saved。 */
  store.tree.name = '再次本地修改';
  assert.equal(store.saved.name, '服务端名称');
  assert.equal(store.dirtyKind, 'display');

  store.markSaved(clone(store.tree));
  assert.equal(store.dirtyKind, 'clean');
});

test('load resets both history stacks and the saved baseline', () => {
  const store = storeWith();
  store.change('behavior', tree => { tree.name = 'dirty'; });
  store.load(createMinimalTree('qwen3-32b'), { saved: true });
  assert.equal(store.undoStack.length, 0);
  assert.equal(store.redoStack.length, 0);
  assert.equal(store.dirtyKind, 'clean');
  assert.equal(store.saved.root.nodes[1].models[0].model, 'qwen3-32b');

  store.load(createMinimalTree('qwen3-32b'));
  assert.equal(store.saved, null);
  assert.equal(store.dirtyKind, 'behavior');
});

test('subscribers observe load/change/undo/redo/saved events', () => {
  const store = new DraftStore();
  const seen = [];
  const off = store.subscribe(event => seen.push(event.type));
  store.load(createMinimalTree('qwen3-8b'), { saved: true });
  store.change('behavior', tree => { tree.name = 'x'; });
  store.undo();
  store.redo();
  store.markSaved(clone(store.tree));
  off();
  store.change('behavior', tree => { tree.name = 'y'; });
  assert.deepEqual(seen, ['load', 'change', 'undo', 'redo', 'saved']);
});
