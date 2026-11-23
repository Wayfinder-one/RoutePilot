/* DraftStore 纯状态契约测试（Task 8，规格 Step 1/Step 3）。
 *
 * 运行：node --test scripts/console_draft_test.mjs
 * 约束：不加载 DOM、不访问 localStorage / fetch；store 只做内存状态。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { DraftStore, HISTORY_LIMIT } from '../router/console/routing/draft.js';
import { createMinimalTree, clone } from '../router/console/routing/graph.js';

/* 严格自包含：新草稿必须携带完整四字段运行快照（createMinimalTree 不再物化）。
   model 必须出现在 runtime.backends.models 里，否则 target 会引用快照外的模型。 */
function minimalRuntime(model, periods) {
  const modelId = model || 'qwen3-8b';
  return {
    backends: {
      providers: [{ name: 'local', base_url: 'http://upstream.test/v1', key_env: '', reasoning_format: 'openai' }],
      models: [{ id: modelId, upstream: modelId, provider: 'local', profile: {} }]
    },
    jev: null,
    jev_window: { input_tokens: 1024, context_tokens: 4096 },
    time_periods: periods || []
  };
}

function minimalTree(model, periods) {
  const modelId = model || 'qwen3-8b';
  return createMinimalTree(modelId, minimalRuntime(modelId, periods));
}

function storeWith(model) {
  const store = new DraftStore();
  store.load(minimalTree(model), { saved: true });
  return store;
}

test('change records one undo entry and redo is cleared', () => {
  const store = new DraftStore();
  store.load(minimalTree('qwen3-8b'));
  /* 不再手工塞一条假历史：若 change() 不记历史，下面的 undoStack 断言会直接失败。 */
  assert.equal(store.change('behavior', tree => { tree.name = 'one'; }), true);
  assert.equal(store.undoStack.length, 1);
  assert.equal(store.undo(), true);
  assert.equal(store.redoStack.length, 1);
  assert.equal(store.change('behavior', tree => { tree.name = 'two'; }), true);
  assert.equal(store.redoStack.length, 0, 'a new change did not clear the redo stack');
  assert.equal(store.undoStack.length, 1);
  assert.equal(store.tree.name, 'two');
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

  /* 保存锁同样拒绝 undo/redo（draft.js 的两处 return false），且不得消耗栈。 */
  const locked = storeWith();
  assert.equal(locked.change('behavior', tree => { tree.name = 'one'; }), true);
  assert.equal(locked.change('behavior', tree => { tree.name = 'two'; }), true);
  assert.equal(locked.undo(), true);
  assert.equal(locked.undoStack.length, 1);
  assert.equal(locked.redoStack.length, 1);
  const current = locked.tree;
  const name = locked.tree.name;

  locked.setSaving(true);
  assert.equal(locked.undo(), false, 'undo ran while a save was in flight');
  assert.equal(locked.redo(), false, 'redo ran while a save was in flight');
  assert.equal(locked.undoStack.length, 1, 'a blocked undo consumed the undo stack');
  assert.equal(locked.redoStack.length, 1, 'a blocked redo consumed the redo stack');
  assert.equal(locked.tree, current, 'a blocked undo/redo replaced the draft');
  assert.equal(locked.tree.name, name);

  locked.setSaving(false);
  assert.equal(locked.redo(), true);
  assert.equal(locked.tree.name, 'two');
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
  /* 基线按值保存：即使调用方传的就是当前草稿本身，之后改 tree 也不能回写 saved。
     如果 markSaved 只存引用，下面的 saved.name 会跟着变成“基线之后的修改”。 */
  store.tree.name = '再次本地修改';
  assert.equal(store.saved.name, '服务端名称');
  assert.equal(store.dirtyKind, 'display');

  assert.equal(store.markSaved(store.tree), true);
  assert.equal(store.saved.name, '再次本地修改');
  store.tree.name = '基线之后的修改';
  assert.equal(store.saved.name, '再次本地修改', 'markSaved kept a live reference to the draft');

  /* 替换而不是合并：新基线缺少 tree_revision 时，上一版基线的字段不能残留。 */
  const withRevision = clone(store.tree);
  withRevision.tree_revision = 'abc123';
  assert.equal(store.markSaved(withRevision), true);
  assert.equal(store.saved.tree_revision, 'abc123');
  const withoutRevision = clone(store.tree);
  delete withoutRevision.tree_revision;
  assert.equal(store.markSaved(withoutRevision), true);
  assert.equal('tree_revision' in store.saved, false,
    'markSaved merged into the previous baseline instead of replacing it');

  store.markSaved(clone(store.tree));
  assert.equal(store.dirtyKind, 'clean');
});

test('load resets both history stacks and the saved baseline', () => {
  const store = storeWith();
  store.change('behavior', tree => { tree.name = 'dirty'; });
  store.load(minimalTree('qwen3-32b'), { saved: true });
  assert.equal(store.undoStack.length, 0);
  assert.equal(store.redoStack.length, 0);
  assert.equal(store.dirtyKind, 'clean');
  assert.equal(store.saved.root.nodes[1].models[0].model, 'qwen3-32b');

  store.load(minimalTree('qwen3-32b'));
  assert.equal(store.saved, null);
  assert.equal(store.dirtyKind, 'behavior');
});

test('runtime travels with the draft and undo/redo keep deep copies', () => {
  const store = new DraftStore();
  const periods = [{ name: 'peak', days: [0, 1, 2, 3, 4, 5, 6], start: '08:00', end: '11:00' }];
  const tree = minimalTree('qwen3-8b', periods);
  /* 严格契约：新草稿直接携带完整四字段运行快照（没有「待保存再物化」的中间态）。 */
  assert.deepEqual(Object.keys(tree.runtime).sort(),
    ['backends', 'jev', 'jev_window', 'time_periods']);
  assert.deepEqual(tree.runtime.time_periods.map(period => period.name), ['peak']);
  assert.equal(tree.runtime.backends.models[0].id, 'qwen3-8b');
  store.load(tree, { saved: true });
  assert.equal(store.dirtyKind, 'clean');

  assert.equal(store.change('behavior', draft => {
    draft.runtime.time_periods = [{ name: 'normal', days: [0], start: '11:00', end: '18:00' }];
  }), true);
  assert.equal(store.dirtyKind, 'behavior');

  /* 历史快照是深拷贝：撤销后改当前树不能回写到 before 快照。 */
  const before = store.undoStack[0].before;
  assert.equal(store.undo(), true);
  assert.equal(store.tree.runtime.time_periods[0].name, 'peak');
  store.tree.runtime.time_periods[0].name = 'mutated-after-undo';
  assert.equal(before.runtime.time_periods[0].name, 'peak', 'undo history kept a live runtime reference');

  assert.equal(store.redo(), true);
  assert.equal(store.tree.runtime.time_periods[0].name, 'normal');
  assert.equal(store.dirtyKind, 'behavior');

  /* 窗口数值随 runtime 一起参与 dirty 判定。 */
  assert.equal(store.change('behavior', draft => { draft.runtime.jev_window.input_tokens = 256; }), true);
  assert.equal(store.undo(), true);
  assert.equal(store.tree.runtime.jev_window.input_tokens, 1024);

  /* 整组替换运行快照（例如应用 JEV 选择/导入）是可撤销的 behavior 变更。 */
  const replacement = clone(store.tree.runtime);
  replacement.jev = {
    id: 'jev-b', base_url: 'http://jev-b.test', model: 'd1-b', key_env: '',
    timeout_seconds: 12, instructions: '选择', state_template: '{request}'
  };
  replacement.jev_window = { input_tokens: 2048, context_tokens: 8192 };
  assert.equal(store.change('behavior', draft => { draft.runtime = clone(replacement); }), true);
  assert.equal(store.tree.runtime.jev.id, 'jev-b');
  assert.deepEqual(store.tree.runtime.jev_window, { input_tokens: 2048, context_tokens: 8192 });
  assert.equal(store.undo(), true);
  assert.equal(store.tree.runtime.jev, null);
  assert.equal(store.tree.runtime.jev_window.input_tokens, 1024);
  assert.equal(store.tree.runtime.time_periods[0].name, 'normal');
});

test('subscribers observe load/change/undo/redo/saved events', () => {
  const store = new DraftStore();
  const seen = [];
  const off = store.subscribe(event => seen.push(event.type));
  store.load(minimalTree('qwen3-8b'), { saved: true });
  store.change('behavior', tree => { tree.name = 'x'; });
  store.undo();
  store.redo();
  store.markSaved(clone(store.tree));
  off();
  store.change('behavior', tree => { tree.name = 'y'; });
  assert.deepEqual(seen, ['load', 'change', 'undo', 'redo', 'saved']);
});
