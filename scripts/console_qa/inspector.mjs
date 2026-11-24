/* RoutePilot 控制台 QA · 检查器用例（Task 11/12：节点/边/图设置、条件双入口、选项重命名、校验定位与 API 超时）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/inspector.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import path from 'node:path';
import {
  BASELINE,
  EXAMPLE_EDGES,
  EXAMPLE_NODES,
  SECONDARY_JEV_ID,
  TREE_ID,
  api,
  appSnapshot,
  blankPoint,
  clickCanvasAction,
  clickEdge,
  clickNode,
  clearToasts,
  danglingEdgeIds,
  draftStatus,
  dragPort,
  edgeOf,
  ensureBaselineTree,
  expectHttpStatusError,
  expectNever,
  issueCodes,
  mockBase,
  openCanvas,
  openIssuesPanel,
  page,
  runIfMain,
  saveNow,
  screenshot,
  serverTree,
  test,
  toastText,
  waitCondPop,
  waitFor,
  waitInspectorBranch,
  waitToastIncludes
} from '../console_qa.mjs';

test('7.4-07-selected-edge-inspector', async () => {
  await openCanvas();
  const edgeId = 'e-cap-strong-and-power';
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  const text = await page.locator('#cvInspectorBody').innerText();
  assert.match(text, /单选题/);
  assert.match(text, /prob\.strong/);
  assert.match(text, /与门-2/);
  assert.match(text, /in_1/);
  assert.match(text, /> 0\.65/);
  const typeLabel = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');

    const rows = Array.from(document.querySelectorAll('#cvInspectorBody .rtc-ins-row'));
    const row = rows.find(item => item.querySelector('span') &&
      item.querySelector('span').textContent.trim() === t('cv.ins.type'));
    return row && row.querySelector('b') ? row.querySelector('b').textContent.trim() : '';

});
  assert.equal(typeLabel, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.ins.typeNumber'));
}),
    'numeric edge inspector did not label the edge type');

  await page.locator('#cvInspectorBody [data-act="ins-delete-edge"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 1);
  const dialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialog, /单选题/);
  assert.match(dialog, /prob\.strong/);
  assert.match(dialog, /与门-2/);
  assert.match(dialog, /in_1/);
  await page.locator('[data-act="delete-confirm"]').click();
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (!app.graph.edges.some(edge => edge.id === id));
}, edgeId);
  assert.equal(await page.locator(`#cvEdges path.rtc-edge[data-edge-id="${edgeId}"]`).count(), 0);
  assert.deepEqual(await danglingEdgeIds(), []);
  assert.equal((await issueCodes()).includes('edge_port_missing'), false);

  await clickCanvasAction('cvUndo');
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge => edge.id === id));
}, edgeId);
  const restored = await edgeOf(edgeId);
  assert.deepEqual(restored.condition, { op: 'gt', value: 0.65 });
  assert.equal(restored.target, 'and-power');
  assert.equal(restored.target_port, 'in_1');
  assert.equal((await appSnapshot()).kind, 'clean');
});

test('7.4-08-logic-slot-rows', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await page.locator('[data-lib-kind="logic-and"]').click();
  const andId = await page.evaluate(async known => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item =>
      item.type === 'logic' && item.operator === 'and' && known.indexOf(item.id) < 0);
    return node && node.id;

}, before.nodes.map(node => node.id));
  assert.ok(andId, 'AND node was not created');
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.some(issue =>
    issue.node_id === id && issue.code === 'logic_slot_count'));
}, andId);
  assert.equal(await page.locator(`#cvNodes [data-node-id="${andId}"] .rtc-slot`).count(), 2,
    'a new AND gate must render both input rows');
  assert.equal(await page.locator('#cvNodes [data-node-id="and-power"] .rtc-slot').count(), 3,
    'AND renders its two occupied rows plus the next free slot');
  assert.equal(await page.locator('#cvNodes [data-node-id="or-ctx"]').getAttribute('data-operator'), 'or');
  const orRows = page.locator('#cvNodes [data-node-id="or-ctx"] .rtc-slot');
  assert.equal(await orRows.count(), 3, 'OR renders its two occupied rows plus the next free slot');
  assert.match(await orRows.nth(0).innerText(), /文本条件/);
  assert.match(await orRows.nth(0).innerText(), /in_1/);
  assert.match(await orRows.nth(1).innerText(), /时段条件/);
  assert.match(await orRows.nth(1).innerText(), /in_2/);
  assert.equal(await orRows.nth(2).locator('.rtc-slot-add').count(), 0,
    'empty slot rows no longer render an add chip');
  assert.match(await orRows.nth(2).getAttribute('title'), /检查器|inspector/,
    'the third OR row is the next empty slot');

  /* The AND node was just created (selected + centered): clear selection and pan it
     far off-center so the locate click is the only reason it becomes selected/centered. */
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    app.kit.clearSelection();
    app.kit.zoomBy(0.5);

});
  const blank = await blankPoint();
  assert.ok(blank, 'no blank stage point available for panning');
  await page.mouse.move(blank.x, blank.y);
  await page.mouse.down();
  await page.mouse.move(blank.x - 320, blank.y + 190, { steps: 10 });
  await page.mouse.up();
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.clearSelection());
});
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: null, id: null });
  const offCenter = await page.evaluate(id => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const rect = document.querySelector(`[data-node-id="${id}"]`).getBoundingClientRect();
    return Math.hypot(
      rect.left + rect.width / 2 - (stage.left + stage.width / 2),
      rect.top + rect.height / 2 - (stage.top + stage.height / 2));
}, andId);
  assert.ok(offCenter > 120, `AND node was not moved off-center before locate: ${offCenter}px`);

  await openIssuesPanel();
  await page.locator(`#cvIssues [data-issue-node="${andId}"]`).first().click();
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: 'node', id: andId });
  const centered = await page.evaluate(id => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const rect = document.querySelector(`[data-node-id="${id}"]`).getBoundingClientRect();
    return Math.abs(rect.left + rect.width / 2 - (stage.left + stage.width / 2)) < 60 &&
      Math.abs(rect.top + rect.height / 2 - (stage.top + stage.height / 2)) < 60;
}, andId);
  assert.equal(centered, true, 'node issue locate did not center the node');

  await clickNode('and-power');
  await waitInspectorBranch('node', 'and-power');
  assert.deepEqual(await page.locator(
    '#cvInspectorBody [data-field="operator"] option').evaluateAll(
      options => options.map(option => option.value)), ['and', 'or']);
});

test('7.4-09-question-target-rows', async () => {
  await openCanvas();
  await dragPort('cond-text', 'out', { node: 'capability', port: 'in_2' });
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge =>
    edge.target === 'capability' && edge.target_port === 'in_2'));
});

  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] .rtc-slot').count(), 3);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] .rtc-slots-note').count(), 0,
    'the OR explanation note is still rendered on the card');
  const rows = page.locator('#cvNodes [data-node-id="capability"] .rtc-slot');
  assert.match(await rows.nth(0).innerText(), /开始/);
  assert.match(await rows.nth(0).innerText(), /in_1/);
  assert.equal(await rows.nth(0).locator('[data-start-condition]').count(), 0,
    'the start slot still renders a condition chip');
  assert.doesNotMatch(await rows.nth(0).innerText(), /总是通过|Always passes/);
  assert.match(await rows.nth(1).innerText(), /文本条件/);
  assert.equal((await rows.nth(1).locator('[data-cond-edge]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.direct'));
}));

  await rows.nth(1).locator('[data-cond-edge]').click();
  await waitCondPop(await rows.nth(1).locator('[data-cond-edge]').getAttribute('data-cond-edge'));
  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitFor(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');

    const row = document.querySelectorAll('#cvNodes [data-node-id="capability"] .rtc-slot')[1];
    return row && row.querySelector('[data-cond-edge]').textContent.trim() === t('cv.cond.negated');

});

  await dragPort('cond-text', 'out', { node: 'strong-target', port: 'in_2' });
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge =>
    edge.target === 'strong-target' && edge.target_port === 'in_2'));
});
  assert.equal(await page.locator('#cvNodes [data-node-id="strong-target"] .rtc-slot').count(), 3);
  assert.equal(await page.locator('#cvNodes [data-node-id="strong-target"] .rtc-slots-note').count(), 0,
    'the OR explanation note is still rendered on the target card');
  const targetRows = page.locator('#cvNodes [data-node-id="strong-target"] .rtc-slot');
  assert.match(await targetRows.nth(0).innerText(), /单选题|置信度/);
  assert.match(await targetRows.nth(1).innerText(), /文本条件/);
});

test('Task 11 · option key rename syncs selected/prob source ports and conflicts stay in the editor', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const historyBefore = (await appSnapshot()).history;

  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="key"]').fill('strong');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
});
  const conflict = (await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim();
  assert.match(conflict, /已存在|already exists/i);
  assert.match(conflict, /strong/, 'conflict message does not name the blocking option key');
  let snapshot = await appSnapshot();
  assert.deepEqual(snapshot.history, historyBefore, 'conflicting rename pushed history');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (!!RTGraph.findNode(app.graph, 'capability')
    .options.find(option => option.key === 'easy'));
}), true);
  assert.equal(snapshot.edges.find(edge => edge.id === 'e-selected-easy-light').source_port, 'selected.easy');

  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="key"]').fill('light');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge => edge.source_port === 'selected.light'));
});
  snapshot = await appSnapshot();
  assert.equal(snapshot.edges.find(edge => edge.id === 'e-selected-easy-light').source_port, 'selected.light');
  assert.equal(snapshot.edges.find(edge => edge.id === 'e-selected-easy-light').condition, null);
  const ports = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.portsOf(
    RTGraph.findNode(app.graph, 'capability'), app.graph).map(port => port.id));
});
  assert.ok(ports.includes('selected.light') && ports.includes('prob.light'));
  assert.equal(ports.includes('selected.easy'), false);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="light"]').count(), 1);

  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge => edge.source_port === 'selected.easy'));
});
  snapshot = await appSnapshot();
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'capability')
    .options.some(option => option.key === 'easy'));
}), true);
  assert.equal(snapshot.edges.some(edge => edge.source_port === 'selected.light'), false);
});

test('Task 11 · swapping two option keys in one save renames every row and follows source ports', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');

  /* easy→strong and strong→light in the same save: every final key is unique, but
     row 1's new key is still held by row 2 until it moves. Two-phase rename must win. */
  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="key"]').fill('strong');
  await page.locator('#cvInspectorBody [data-opt-row="strong"] [data-opt-field="key"]').fill('light');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'capability');
    return node && node.options[0].key === 'strong' && node.options[2].key === 'light';

}, null, { timeout: 4000 });
  assert.equal(await page.locator('#cvInspectorBody [data-ins-error]').isHidden(), true,
    'valid two-key swap was rejected');

  const snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  assert.equal(JSON.stringify(snapshot).includes('__opt_tmp'), false,
    'temporary rename keys leaked into the graph');
  const edgeById = Object.fromEntries(snapshot.edges.map(edge => [edge.id, edge]));
  assert.equal(edgeById['e-selected-easy-light'].source_port, 'selected.strong',
    'renamed easy row did not follow to selected.strong');
  assert.equal(edgeById['e-cap-strong-and-power'].source_port, 'prob.light',
    'renamed strong row did not follow to prob.light');
  assert.equal(edgeById['e-cap-strong-and-power'].condition.value, 0.65,
    'swap changed an unrelated condition');
  assert.equal(edgeById['e-cap-below-strong-follow'].source_port, 'prob.light');
  assert.equal(edgeById['e-selected-strong-strong'].source_port, 'selected.light');
  assert.equal(edgeById['e-selected-standard-standard'].source_port, 'selected.standard');

  const options = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'capability').options)));
});
  assert.deepEqual(options.map(option => option.key), ['strong', 'standard', 'light']);
  assert.equal(options[0].label, '轻量模型', 'label did not stay with the renamed easy row');
  assert.equal(options[2].label, '增强模型', 'label did not stay with the renamed strong row');
  const ports = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.portsOf(
    RTGraph.findNode(app.graph, 'capability'), app.graph).map(port => port.id));
});
  assert.ok(ports.includes('selected.strong') && ports.includes('prob.strong'));
  assert.ok(ports.includes('selected.light') && ports.includes('prob.light'));
  assert.equal(ports.includes('selected.easy'), false);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="strong"]').count(), 1);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="light"]').count(), 1);
  assert.deepEqual((await appSnapshot()).history, { undo: true, redo: false },
    'two-key swap must be exactly one history entry');

  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'capability');
    return node && node.options[0].key === 'easy' && node.options[2].key === 'strong';

});
  const restored = await appSnapshot();
  assert.equal(restored.edges.find(edge => edge.id === 'e-selected-easy-light').source_port, 'selected.easy');
  assert.equal(restored.edges.find(edge => edge.id === 'e-selected-strong-strong').source_port, 'selected.strong');
  assert.equal(restored.edges.find(edge => edge.id === 'e-cap-strong-and-power').source_port, 'prob.strong');
  assert.deepEqual(restored.history, { undo: false, redo: true });
});

test('Task 11 · inspector save and delete bind to the rendered branch, not a silently moved kit selection', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  /* Move the kit selection without re-rendering the inspector: the rendered form
     still belongs to capability, so save/delete must use capability's id. */
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.select('node', 'cond-text', true));
});
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: 'node', id: 'cond-text' });
  const condTextBefore = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'cond-text'))));
});
  const draft = '只写入渲染时绑定的 capability 节点。';
  await page.locator('#cvInspectorBody [data-field="instructions"]').fill(draft);
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async value => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'capability');
    return node && node.instructions.indexOf(value) >= 0;

}, draft);
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'cond-text'))));
}), condTextBefore,
  'inspector save wrote to the silently moved kit selection');
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'capability')
    .instructions.indexOf('只写入') < 0);
});

  /* Option delete resolves the node id from the rendered branch as well. */
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.select('node', 'cond-text', true));
});
  await page.locator('#cvInspectorBody [data-opt-row="strong"] [data-act="opt-delete"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  const optionDialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(optionDialog, /单选题/);
  assert.match(optionDialog, /selected\.strong/);
  assert.match(optionDialog, /prob\.strong/);
  await page.locator('[data-act="delete-cancel"]').click();

  /* Edge delete lists the rendered edge even when the kit selection points at another. */
  await clickEdge('e-cap-strong-and-power');
  await waitInspectorBranch('edge', 'e-cap-strong-and-power');
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.select('edge', 'e-selected-easy-light', true));
});
  await page.locator('#cvInspectorBody [data-act="ins-delete-edge"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  const edgeDialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(edgeDialog, /与门-2/);
  assert.match(edgeDialog, /in_1/);
  await page.locator('[data-act="delete-cancel"]').click();
  assert.equal(await page.locator('#cvDeleteModal').isHidden(), true);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges
    .filter(edge => edge.id === 'e-cap-strong-and-power').length);
}), 1);
});

test('Task 11 · question and condition inspectors edit instructions, options, keywords, periods and criteria', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const original = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'capability'))));
});
  const instructions = '这条请求需要哪个能力档位？请结合复杂度与输出长度判断。';
  await page.locator('#cvInspectorBody [data-field="instructions"]').fill(instructions);
  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="label"]').fill('轻量档');
  await page.locator('#cvInspectorBody [data-act="opt-add"]').click();
  const newRow = page.locator('#cvInspectorBody [data-opt-row]').last();
  await newRow.locator('[data-opt-field="key"]').fill('medium');
  await newRow.locator('[data-opt-field="label"]').fill('中档模型');
  await newRow.locator('[data-opt-field="capability"]').fill('中等复杂度写作与总结。');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'capability');
    return node && node.options.length === 4 && node.instructions.indexOf('复杂度') >= 0;

});
  let snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (!!RTGraph.findNode(app.graph, 'capability')
    .options.find(option => option.key === 'easy'));
}), true);
  const saved = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'capability'))));
});
  assert.equal(saved.options.find(option => option.key === 'easy').label, '轻量档');
  assert.deepEqual(saved.options.find(option => option.key === 'medium'),
    { key: 'medium', label: '中档模型', capability: '中等复杂度写作与总结。' });
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'capability').options.length === 3);
});
  snapshot = await appSnapshot();
  const undone = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'capability'))));
});
  assert.equal(undone.instructions, original.instructions);
  assert.equal(undone.options.find(option => option.key === 'easy').label, original.options[0].label);

  await clickNode('cond-text');
  await waitInspectorBranch('node', 'cond-text');
  await page.locator('#cvInspectorBody [data-field="keywords"]').fill('抢修, 停电, 电梯, 故障');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'cond-text');
    return node && node.contains_any.indexOf('电梯') >= 0;

});
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'cond-text').contains_any);
}),
    ['抢修', '停电', '电梯', '故障']);
  assert.equal(await page.locator('#cvNodes [data-node-id="cond-text"] [data-kw="电梯"]').count(), 1);
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'cond-text').contains_any.indexOf('电梯') < 0);
});

  await clickNode('cond-peak');
  await waitInspectorBranch('node', 'cond-peak');
  await page.locator('#cvInspectorBody [data-field="periods"] input[value="normal"]').check();
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'cond-peak');
    return node && node.periods.indexOf('normal') >= 0;

});
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'cond-peak').periods);
}),
    ['peak', 'normal']);
  assert.equal(await page.locator('#cvNodes [data-node-id="cond-peak"] [data-period="normal"]').count(), 1);
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'cond-peak').periods.length === 1);
});

  const beforeScore = await appSnapshot();
  await page.locator('[data-lib-kind="question-score"]').click();
  const scoreIds = await page.evaluate(async known => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes
    .filter(node => node.type === 'question' && node.question_type === 'score' && known.indexOf(node.id) < 0)
    .map(node => node.id));
}, beforeScore.nodes.map(node => node.id));
  assert.equal(scoreIds.length, 1);
  await waitInspectorBranch('node', scoreIds[0]);
  await page.locator('#cvInspectorBody [data-field="criteria"]').fill('2, 5, 9');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, id);
    return node && JSON.stringify(node.criteria) === '[2,5,9]';

}, scoreIds[0]);
});

test('Task 11 · logic and target inspectors edit operator, models, reasoning and priority with guards', async () => {
  await openCanvas();
  await clickNode('and-power');
  await waitInspectorBranch('node', 'and-power');
  const noopHistory = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForTimeout(50);
  assert.deepEqual((await appSnapshot()).history, noopHistory, 'no-op node save pushed history');
  await page.locator('#cvInspectorBody [data-field="operator"]').selectOption('or');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'and-power').operator === 'or');
});
  assert.equal(await page.locator('#cvNodes [data-node-id="and-power"]').getAttribute('data-operator'), 'or');
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'and-power').operator === 'and');
});

  await clickNode('strong-target');
  await waitInspectorBranch('node', 'strong-target');
  const modelIds = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="model"] option')).map(option => option.value));
  assert.ok(modelIds.includes('deepseek-v3') && modelIds.includes('qwen3-32b') && modelIds.includes('qwen3-8b'),
    'model options do not come from /admin/models: ' + modelIds.join(','));
  const reasoning = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="reasoning"] option')).map(option => option.value));
  assert.ok(reasoning.includes('low') && reasoning.includes('medium') && reasoning.includes('high'),
    'reasoning options do not come from the model profile: ' + reasoning.join(','));

  const historyBefore = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-field="priority"]').fill('1');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
});
  assert.match((await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim(), /priority/i);
  assert.deepEqual((await appSnapshot()).history, historyBefore, 'duplicate priority pushed history');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, 'strong-target').priority);
}), 3);

  await page.locator('#cvInspectorBody [data-field="priority"]').fill('7');
  await page.locator('#cvInspectorBody [data-model-field="model"]').first().selectOption('deepseek-v3');
  await page.locator('#cvInspectorBody [data-model-field="reasoning"]').first().selectOption('high');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'strong-target');
    return node && node.priority === 7 && node.models.length === 1 &&
      node.models[0].model === 'deepseek-v3' && node.models[0].reasoning === 'high';

});
  const target = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (JSON.parse(JSON.stringify(
    RTGraph.findNode(app.graph, 'strong-target'))));
});
  /* 调度策略与目标能力描述已下线：提交后不应再写回这两个字段。 */
  assert.equal('strategy' in target, false);
  assert.equal('capability' in target, false);
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const node = RTGraph.findNode(app.graph, 'strong-target');
    return node && node.priority === 3 && node.models[0].model === 'qwen3-32b' && node.models[0].reasoning === 'medium';

});
});

test('Task 11 · graph settings edit name/timezone/sticky/fallbacks, show limits and reject an invalid timezone', async () => {
  await openCanvas();
  await waitInspectorBranch('graph', 'root');
  assert.equal(await page.locator('#cvInspectorBody [data-field="name"]').inputValue(), BASELINE.name);
  assert.equal(await page.locator('#cvInspectorBody [data-field="timezone"]').inputValue(), BASELINE.timezone);
  assert.equal(await page.locator('#cvInspectorBody [data-field="sticky"]').isChecked(), true);
  assert.equal(await page.locator('#cvInspectorBody [data-field="stickyTtl"]').inputValue(), '1800');
  assert.equal(await page.locator('#cvInspectorBody [data-field="fallback"]').inputValue(), 'light-target');
  const limitRows = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const read = name => {
      const row = document.querySelector('#cvInspectorBody [data-limit="' + name + '"] b');
      return row ? row.textContent.trim() : null;
};
    const graph = app.graph;
    return {
      nodes: read('nodes'), edges: read('edges'),
      questions: read('questions'), waves: read('waves'),
      counts: {
        nodes: graph.nodes.length,
        edges: graph.edges.length,
        questions: graph.nodes.filter(node => node.type === 'question').length
}
};

});
  assert.deepEqual(limitRows.counts, { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES, questions: 3 });
  assert.equal(limitRows.nodes, EXAMPLE_NODES + ' / 200');
  assert.equal(limitRows.edges, EXAMPLE_EDGES + ' / 400');
  assert.equal(limitRows.questions, '3 / 32');
  assert.equal(limitRows.waves, '≤ 8');

  const noopHistory = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForTimeout(50);
  assert.deepEqual((await appSnapshot()).history, noopHistory, 'no-op graph save pushed history');

  const historyBefore = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('Mars/Phobos');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
});
  assert.match((await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim(), /时区|timezone/i);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.timezone);
}), BASELINE.timezone);
  assert.deepEqual((await appSnapshot()).history, historyBefore);

  await page.locator('#cvInspectorBody [data-field="name"]').fill('电力优先决策组 · v2');
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
  await page.locator('#cvInspectorBody [data-field="sticky"]').uncheck();
  await page.locator('#cvInspectorBody [data-field="stickyTtl"]').fill('900');
  await page.locator('#cvInspectorBody [data-field="fallback"]').selectOption('power-target');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === '电力优先决策组 · v2' &&
    app.tree.timezone === 'UTC' && app.tree.sticky.enabled === false &&
    app.tree.sticky.ttl_seconds === 900 && app.graph.error_fallback === 'power-target');
});
  const snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.error_fallback);
}), 'power-target');
  await clickCanvasAction('cvUndo');
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === expected.name &&
    app.tree.timezone === expected.timezone && app.tree.sticky.enabled === true &&
    app.tree.sticky.ttl_seconds === expected.sticky.ttl_seconds &&
    app.graph.error_fallback === 'light-target');
}, BASELINE);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });
});

test('Task 9 · graph settings choose a JEV and refresh the runtime from the database catalog', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  await waitInspectorBranch('graph', 'root');

  /* JEV 下拉来自数据库 JEV 目录；首项是「不使用 JEV」，当前快照的 default 选中。 */
  const options = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-field="jev"] option'))
    .map(option => ({ value: option.value, text: option.textContent.trim() })));
  assert.equal(options[0].value, '', 'the JEV select has no "no JEV" option');
  assert.equal(options[0].text, await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
    return t('cv.ins.jevNone');
}));
  const defaultOption = options.find(option => option.value === 'default');
  assert.ok(defaultOption, 'the database JEV default service is missing from the select');
  assert.match(defaultOption.text, /default · d1-2b/, 'JEV option does not show id · model');
  const secondaryOption = options.find(option => option.value === SECONDARY_JEV_ID);
  assert.ok(secondaryOption, 'the second database JEV service is missing from the select');
  assert.match(secondaryOption.text, new RegExp(SECONDARY_JEV_ID + ' · ' + 'd1-2B'),
    'JEV option label does not include the model');
  assert.equal(await page.locator('#cvInspectorBody [data-field="jev"]').inputValue(), 'default');
  assert.match(await page.locator('#cvInspectorBody [data-runtime-summary]').textContent(), /1024\/4096/,
    'the runtime summary does not show the canvas JEV window');

  /* 应用 JEV 选择 = 同一个历史项里替换 runtime.jev 与 runtime.jev_window（原子）。 */
  const historyBefore = await page.evaluate(async () => {
    const { store } = await import('/console/routing/app.js');
    return store.undoStack.length;
});
  await page.locator('#cvInspectorBody [data-field="jev"]').selectOption(SECONDARY_JEV_ID);
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return !!(app.tree.runtime && app.tree.runtime.jev &&
      app.tree.runtime.jev.id === 'jev-secondary');
}, null, { timeout: 8000 });
  const applied = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return ({
    jev: app.tree.runtime.jev,
    window: app.tree.runtime.jev_window,
    kind: app.kind(),
    history: store.undoStack.length
});
});
  assert.deepEqual(applied.window, { input_tokens: 2048, context_tokens: 8192 },
    'the JEV selection did not replace the window from the same service');
  assert.equal(applied.kind, 'behavior');
  assert.equal(applied.history, historyBefore + 1,
    'JEV selection + graph settings were not a single history entry');
  assert.equal(applied.jev.base_url, mockBase);

  /* 撤销必须同时恢复服务与窗口（不允许混用两个服务的配置）。 */
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return !!(app.tree.runtime && app.tree.runtime.jev &&
      app.tree.runtime.jev.id === 'default' &&
      app.tree.runtime.jev_window.input_tokens === 1024 &&
      app.tree.runtime.jev_window.context_tokens === 4096);
}, null, { timeout: 8000 });

  /* 含问题节点时选择「不使用 JEV」是本地硬错误：不写历史、不改草稿。 */
  const beforeReject = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return ({ history: store.undoStack.length, jev: app.tree.runtime.jev });
});
  await page.locator('#cvInspectorBody [data-field="jev"]').selectOption('');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
});
  assert.equal((await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim(),
    await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return t('cv.ins.errJevRequired');
}), 'a question canvas accepted "no JEV"');
  const afterReject = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return ({ history: store.undoStack.length, jev: app.tree.runtime.jev, kind: app.kind() });
});
  assert.deepEqual(afterReject, Object.assign({}, beforeReject, { kind: 'clean' }),
    'a rejected JEV selection mutated the draft or history');

  /* 「从数据库刷新运行配置」：基线快照与目录逐字段一致 → 无变更提示、不写历史。 */
  const beforeRefresh = await page.evaluate(async () => {
    const { store } = await import('/console/routing/app.js');
    return store.undoStack.length;
});
  await page.locator('#cvInspectorBody [data-act="ins-refresh-runtime"]').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    const { t } = await import('/console/i18n.js');
    return app.lastToast === t('cv.ins.runtimeUnchanged');
}, null, { timeout: 8000 });
  assert.equal(await page.evaluate(async () => {
    const { store } = await import('/console/routing/app.js');
    return store.undoStack.length;
}), beforeRefresh, 'a no-op runtime refresh pushed history');

  /* 目录漂移的画布：快照 provider/JEV 端点与数据库不同，刷新必须从目录重建快照。 */
  const drifted = JSON.parse(JSON.stringify(baseline));
  const driftedId = 'qa-runtime-refresh';
  drifted.backends.providers = drifted.backends.providers.map(provider =>
    Object.assign({}, provider, { base_url: 'http://stale-provider.test/v1' }));
  drifted.jev = Object.assign({}, drifted.jev, { base_url: 'http://stale-jev.test' });
  await api('POST', '/admin/trees', {
    id: driftedId,
    name: '目录漂移的画布',
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: drifted.root,
    canvas: drifted.canvas,
    backends: drifted.backends,
    jev: drifted.jev,
    jev_window: drifted.jev_window,
    time_periods: drifted.time_periods
});
  await page.evaluate(async id => {
    const { app } = await import('/console/routing/app.js');
    return app.selectTree(id);
}, driftedId);
  await waitFor(async id => {
    const { app } = await import('/console/routing/app.js');
    return (app.treeId === id && app.booted && app.kind() === 'clean');
}, driftedId, { timeout: 8000 });
  await waitInspectorBranch('graph', 'root');
  const refreshHistory = await page.evaluate(async () => {
    const { store } = await import('/console/routing/app.js');
    return store.undoStack.length;
});
  await page.locator('#cvInspectorBody [data-act="ins-refresh-runtime"]').click();
  const refreshedProviderUrl = mockBase + '/v1';
  await waitFor(async expected => {
    const { app } = await import('/console/routing/app.js');
    return app.tree.runtime.backends.providers[0].base_url === expected;
}, refreshedProviderUrl, { timeout: 8000 });
  const refreshed = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return ({
    provider: app.tree.runtime.backends.providers[0].base_url,
    jev: app.tree.runtime.jev.base_url,
    window: app.tree.runtime.jev_window,
    kind: app.kind(),
    history: store.undoStack.length
});
});
  assert.equal(refreshed.jev, mockBase, 'refresh did not replace the canvas JEV endpoint from the catalog');
  assert.deepEqual(refreshed.window, { input_tokens: 1024, context_tokens: 4096 });
  assert.equal(refreshed.kind, 'behavior');
  assert.equal(refreshed.history, refreshHistory + 1, 'refresh was not a single history entry');
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.tree.runtime.backends.providers[0].base_url === 'http://stale-provider.test/v1';
}, null, { timeout: 8000 });
  assert.equal(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.tree.runtime.jev.base_url;
}), 'http://stale-jev.test', 'undo did not restore the pre-refresh JEV snapshot');
  await ensureBaselineTree();
  await openCanvas();
});

test('Task 11 · Backspace inside editor inputs is safe, all overlays close with Esc and labels are locatable', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.locator('#cvInspectorBody [data-field="instructions"]').click();
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  await expectNever(() => !document.getElementById('cvDeleteModal').hidden,
    'Delete/Backspace in an inspector input opened the delete confirmation');
  assert.equal((await appSnapshot()).nodes.length, before.nodes.length);

  const chip = page.locator('#cvNodes [data-cond-edge="e-cap-strong-and-power"]');
  const chipAria = await chip.getAttribute('aria-label');
  assert.ok(chipAria && chipAria.includes('in_1'), 'condition chip has no locatable aria-label');
  await chip.click();
  await waitCondPop('e-cap-strong-and-power');
  const popAria = await page.evaluate(() => ({
    role: document.getElementById('cvCondPop').getAttribute('role'),
    label: document.getElementById('cvCondPop').getAttribute('aria-label')
}));
  assert.equal(popAria.role, 'dialog');
  assert.equal(popAria.label, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.condTitle'));
}));
  assert.equal(await page.locator('#cvInspector').getAttribute('aria-label'),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.inspector'));
}));
  await page.locator('#cvCondPop [data-cond-field="value"]').click();
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);

  const point = await blankPoint();
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvQuick').isHidden(), true);

  await clickNode('and-power');
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvDeleteModal').isHidden(), true);

  await openIssuesPanel();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvIssues').isHidden(), true);
});

test('Task 11 · admin API timeout rejects a hung request with a clear localized error', async () => {
  await openCanvas();
  let held = null;
  const hung = async route => { held = route; };
  const timeoutProbe = url => url.pathname === '/admin/time-periods' && url.searchParams.has('timeout-probe');
  await page.route(timeoutProbe, hung);
  try {
    const result = await page.evaluate(async () => {
      const { api } = await import('/console/api.js');
      const { tf } = await import('/console/i18n.js');
      const path = '/admin/time-periods?timeout-probe=1';
      try {
        await api.req('GET', path, undefined, { timeoutMs: 1000 });
        return { threw: false };
} catch (error) {
        return {
          threw: true,
          timeout: error.timeout === true,
          status: error.status,
          message: error.message,
          expected: tf('api.timeout', { seconds: 1, path: path })
};
}
});
    assert.equal(result.threw, true, 'hung admin request did not reject');
    assert.equal(result.timeout, true, 'timeout error is not marked: ' + result.message);
    assert.equal(result.status, 0);
    assert.equal(result.message, result.expected, 'timeout error message is not the localized clear message');
} finally {
    if (held) { try { await held.abort(); } catch { /* already gone */ } }
    await page.unroute(timeoutProbe);
}
});

test('Task 12 · timezone/sticky are behavior while title/name are display changes', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.timezone === 'UTC');
});
  let badge = await draftStatus();
  assert.equal(badge.kind, 'behavior', 'timezone change was not classified as behavior');
  assert.equal(badge.state, 'unpublished');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.status.unpublished'));
}));
  await clickCanvasAction('cvUndo');
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.timezone === expected && app.kind() === 'clean');
}, BASELINE.timezone);

  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.locator('#cvInspectorBody [data-field="title"]').fill('能力分档（展示标题）');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.find(node => node.id === 'capability').title === '能力分档（展示标题）');
});
  badge = await draftStatus();
  assert.equal(badge.kind, 'display', 'question title was not classified as display');
  assert.equal(badge.state, 'unpublished');

  await clickNode('strong-target');
  await waitInspectorBranch('node', 'strong-target');
  await page.locator('#cvInspectorBody [data-field="title"]').fill('强推理（展示标题）');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.find(node => node.id === 'strong-target').title === '强推理（展示标题）');
});
  badge = await draftStatus();
  assert.equal(badge.kind, 'display', 'target title was not classified as display');
  assert.equal(badge.state, 'unpublished');

  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.clearSelection());
});
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="name"]').fill('展示字段决策组');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === '展示字段决策组');
});
  badge = await draftStatus();
  assert.equal(badge.kind, 'display', 'tree name was not classified as display');
  assert.equal(badge.state, 'unpublished');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.status.unpublished'));
}));

  await saveNow();
  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'a display-only save changed the server tree_revision');
  assert.equal(server.name, '展示字段决策组');
  assert.equal(server.root.nodes.find(node => node.id === 'capability').title, '能力分档（展示标题）');
  assert.equal(server.root.nodes.find(node => node.id === 'strong-target').title, '强推理（展示标题）');
});

test('P0 inspector applies to draft and lifecycle explains publish state', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const apply = page.locator('#cvInspectorBody [data-act="ins-save-node"]');
  assert.equal((await apply.textContent()).trim(), await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
    return t('cv.ins.apply');
  }));
  const writes = [];
  const onRequest = request => {
    if (request.method() !== 'PUT' && request.method() !== 'POST') return;
    const path = new URL(request.url()).pathname;
    if (path === '/admin/trees/' + TREE_ID || path === '/admin/trees/' + TREE_ID + '/enable') {
      writes.push(request.method() + ' ' + path);
    }
  };
  page.on('request', onRequest);
  try {
    await page.locator('#cvInspectorBody [data-field="instructions"]').fill('P0 只应用到草稿');
    await apply.click();
    await waitFor(async () => {
      const badge = document.getElementById('cvLifecycle');
      return badge && badge.dataset.state === 'unpublished';
    });
    /* Inspector Apply is local-only: no tree write may leave the page. */
    await page.waitForTimeout(120);
    assert.deepEqual(writes, [], 'inspector Apply wrote the draft to the server');
    assert.equal((await page.locator('#cvEnableLabel').textContent()).trim(),
      await page.evaluate(async () => {
        const { t } = await import('/console/i18n.js');
        return t('cv.publishUpdate');
      }), 'a dirty enabled group must offer Save and update');
    /* The toolbar primary is the one action that publishes the draft. */
    await clickCanvasAction('cvEnable');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.kind() === 'clean' &&
        document.getElementById('cvLifecycle').dataset.state === 'running';
    }, null, { timeout: 8000 });
    assert.deepEqual(writes, ['PUT /admin/trees/' + TREE_ID],
      'the toolbar publish did not issue exactly one server save');
    assert.equal(await page.locator('#cvRevision').count(), 0);
    assert.equal(await page.locator('#cvClean').count(), 0);
    assert.equal(await page.locator('#cvDirty').count(), 0);
    assert.equal(await page.locator('#cvEnabled').count(), 0);
  } finally {
    page.off('request', onRequest);
  }
});

test('review-inspector-adds-input-connection', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 画布不再提供「+ 槽位」入口；新增输入槽位（= 新增入边）在检查器完成。 */
  assert.equal(await page.locator('#cvNodes .rtc-slot-add').count(), 0,
    'canvas still renders a slot-add affordance');
  await clickNode('and-power');
  const picker = '[data-ins-branch="node"] [data-slot-wrap="in_3"]';
  await page.locator(picker + ' [data-act="ins-slot-add"]').click();
  const select = page.locator(picker + ' [data-slot-source]');
  assert.equal(await select.isVisible(), true, 'inspector slot picker did not open');
  await screenshot('inspector-slots-1440.png');
  await select.selectOption({ index: 0 });
  await page.locator(picker + ' [data-act="ins-slot-create"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge =>
    edge.target === 'and-power' && edge.target_port === 'in_3'));
});
  const edge = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.find(item =>
    item.target === 'and-power' && item.target_port === 'in_3'));
});
  assert.ok(edge, 'inspector did not create the input connection');
  assert.equal(await page.locator('#cvNodes [data-node-id="and-power"] .rtc-slot[data-slot="in_3"].is-empty').count(), 0,
    'canvas slot row did not become occupied');
  assert.match(await page.locator('#cvNodes [data-node-id="and-power"] .rtc-slot[data-slot="in_3"]').innerText(), /开始/,
    'canvas slot row does not show the new source');
  const badge = await draftStatus();
  assert.equal(badge.visible, true, 'inspector connection did not mark the draft dirty');
  assert.equal(badge.kind, 'behavior');
  assert.equal(badge.state, 'unpublished');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (store.canUndo);
}), true,
    'inspector connection did not produce one history entry');
});

test('review-target-card-shows-name-model-and-fallback-roles', async () => {
  await ensureBaselineTree();
  await openCanvas();

  const otherwiseCard = page.locator('#cvNodes [data-node-id="standard-target"]');
  const otherwiseText = await otherwiseCard.innerText();
  assert.match(await otherwiseCard.locator('.rtc-tname').textContent(), /模型目标-\d+/,
    'target card header still shows only the model name');
  assert.match(await otherwiseCard.locator('.rtc-tbody').innerText(), /deepseek-v3/,
    'target card body does not show its model');
  assert.doesNotMatch(otherwiseText, /最少在途请求|调度策略/);
  assert.doesNotMatch(otherwiseText, /兜底模型/);

  const errorCard = page.locator('#cvNodes [data-node-id="light-target"]');
  assert.match(await errorCard.innerText(), /兜底模型/);

  await waitInspectorBranch('graph', 'root');
  assert.match(
    (await page.locator('#cvInspectorBody [data-field="fallback"] option:checked').textContent()).trim(),
    /模型目标-\d+ · qwen3-8b/);

  await clickNode('standard-target');
  await waitInspectorBranch('node', 'standard-target');
  assert.equal(await page.locator('#cvInspectorBody [data-field="strategy"]').count(), 0,
    'target inspector still exposes the removed scheduling strategy');
  assert.equal(await page.locator('#cvInspectorBody [data-field="capability"]').count(), 0,
    'target inspector still exposes the removed capability description');
});

test('Task 9 · runtime refresh refuses to drop a period still referenced by a condition node', async () => {
  const baseline = await ensureBaselineTree();
  assert.ok(Array.isArray(baseline.time_periods), 'the baseline tree has no runtime periods');
  /* 模拟 /admin/time-periods 失败/漂移后的目录：少了条件节点 cond-peak 引用的
     peak（app.js loadCatalog 会吞掉失败并把 periods 置空，后果相同）。 */
  const catalogPeriods = baseline.time_periods.filter(period => period.name !== 'peak');
  assert.deepEqual(catalogPeriods.map(period => period.name), ['normal', 'offpeak'],
    'the baseline catalog periods changed; this case needs peak as a referenced period');
  const periodsRoute = url => url.pathname === '/admin/time-periods';
  await page.route(periodsRoute, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ periods: catalogPeriods })
  }));
  try {
    await openCanvas();
    await waitInspectorBranch('graph', 'root');
    const snapshot = () => page.evaluate(async () => {
      const { app, store } = await import('/console/routing/app.js');
      const condition = app.graph.nodes.find(node => node.id === 'cond-peak');
      return {
        kind: app.kind(),
        history: store.undoStack.length,
        runtimePeriods: app.tree.runtime.time_periods.map(period => period.name),
        conditionPeriods: condition.periods.slice(),
        enabled: app.tree.enabled
      };
    });
    const before = await snapshot();
    assert.deepEqual(before.runtimePeriods, ['peak', 'normal', 'offpeak']);
    assert.deepEqual(before.conditionPeriods, ['peak']);
    const expected = await page.evaluate(async () => {
      const { tf } = await import('/console/i18n.js');
      return tf('cv.runtime.errPeriodsMissing', { periods: 'peak' });
    });
    await page.locator('#cvInspectorBody [data-act="ins-refresh-runtime"]').click();
    /* 刷新必须点名拒绝：静默把 time_periods 刷成 [] 会在服务端 enable 时报
       unknown_period（本地 validate 不查时段目录）。 */
    await page.waitForFunction(message => {
      const box = document.querySelector('#cvInspectorBody [data-ins-error]');
      return !!box && !box.hidden && box.textContent.includes(message);
    }, expected, { timeout: 6000 });
    assert.equal((await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim(),
      expected, 'the refresh error did not name the missing period');
    assert.ok(expected.includes('peak'), 'the refresh error did not name "peak": ' + expected);
    assert.deepEqual(await snapshot(), before,
      'a refused refresh mutated the draft runtime or pushed history');
  } finally {
    await page.unroute(periodsRoute);
    await ensureBaselineTree();
    await openCanvas();
  }
});


test('Task 9 · departments: graph settings edit/sort, duplicate hint and cross-graph conflict', async () => {
  /* Task 9 的真实浏览器场景：部门编辑在「图设置」里完成，保存仍走 controller.save()；
     服务端负责排序与跨启用图唯一性。本地重复只提示；跨图冲突必须显示服务端
     department_conflict Issue（含部门名与涉事图 id），且第一张图的服务端部门不变。
     场景末尾顺带锁住 dirty 草稿在启停时保留未保存部门编辑（syncLifecycleBaseline）。 */
  const OTHER = 'dept-conflict';
  const listedTree = async id =>
    (await api('GET', '/admin/trees')).trees.find(tree => tree.id === id);
  try {
    await ensureBaselineTree();
    /* 先建第二张图并启用：副本继承源图当时的部门（基线为空），因此后续给源图
       配部门不会先在副本里制造冲突。 */
    if (!(await listedTree(OTHER))) {
      await api('POST', '/admin/trees/' + TREE_ID + '/copy', { id: OTHER, name: '部门冲突图' });
    }
    await api('POST', '/admin/trees/' + OTHER + '/enable');

    await openCanvas();
    await waitInspectorBranch('graph', 'root');
    assert.equal(await page.locator('#cvDepartments').count(), 1,
      'graph settings must offer a departments input');
    assert.equal(await page.locator('#cvDepartments').inputValue(), '',
      'a fresh baseline graph must start with no departments');
    assert.equal(await page.locator('#cvDepartments').getAttribute('data-field'), 'departments');

    /* 1) 输入含重复 + 未排序：本地去重并立即给本地化提示；提交后草稿已排序。 */
    const duplicateHint = await page.evaluate(async () => {
      const { tf } = await import('/console/i18n.js');
      return tf('cv.ins.departmentsDupHint', { departments: '财务部' });
    });
    await clearToasts();
    await page.locator('#cvDepartments').fill('财务部, 财务部, 法务部');
    await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
    await waitToastIncludes(duplicateHint);
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return JSON.stringify(app.tree.departments) === JSON.stringify(['法务部', '财务部']);
    }, null, { timeout: 5000 });
    assert.equal(await page.locator('#cvDepartments').inputValue(), '法务部, 财务部',
      'the graph settings input must round-trip the sorted departments');
    assert.equal((await appSnapshot()).kind, 'behavior',
      'editing departments must be a behavior change');

    /* 2) 保存：服务端排序后的部门原样落盘（服务端是排序的事实源）。 */
    await saveNow();
    const source = await serverTree(TREE_ID);
    assert.deepEqual(source.departments, ['法务部', '财务部'],
      'saved departments are not sorted by the server');
    assert.deepEqual((await listedTree(OTHER)).departments, [],
      'the other enabled graph must not inherit the source departments');

    /* 3) 在另一张启用图配置同一部门：保存被 422 department_conflict 拒绝，
        Issues 条展示部门名与冲突图 id，第一张图服务端部门保持不变。 */
    await page.locator('#cvTreeSelect').selectOption(OTHER);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.treeId === id && app.changeKind === 'clean';
    }, OTHER, { timeout: 8000 });
    await waitInspectorBranch('graph', 'root');
    await page.locator('#cvDepartments').fill('财务部');
    await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return JSON.stringify(app.tree.departments) === JSON.stringify(['财务部']);
    }, null, { timeout: 5000 });
    expectHttpStatusError({ url: new RegExp('/admin/trees/' + OTHER + '$'), status: /422/ });
    await clickCanvasAction('cvSave');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return (app.issues || []).some(issue => issue.code === 'department_conflict');
    }, null, { timeout: 10000 });
    assert.deepEqual(await issueCodes(), ['department_conflict'],
      'the conflict was not surfaced as a single hard department_conflict issue');
    await waitFor(() => !document.getElementById('cvIssues').hidden);
    const issueText = (await page.locator('#cvIssues').innerText()).replace(/\s+/g, ' ');
    assert.ok(issueText.includes('财务部'), 'the conflict issue does not name the department');
    assert.ok(issueText.includes(TREE_ID), 'the conflict issue does not name the conflicting graph id');
    assert.deepEqual((await serverTree(TREE_ID)).departments, ['法务部', '财务部'],
      'a rejected save mutated the first graph departments');
    assert.deepEqual((await serverTree(OTHER)).departments, [],
      'a rejected save wrote the conflicting departments');

    /* 4) 已知问题的回归（Task 9）：脏草稿的部门编辑不得被启停回包覆盖。
       当前图仍是脏的（财务部 未保存）——直接停用，本地部门必须原样保留，
       服务端仍是空部门（未保存的编辑不落盘）。 */
    await clearToasts();
    await clickCanvasAction('cvDisable');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.tree && app.tree.enabled === false;
    }, null, { timeout: 8000 });
    const preserved = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        departments: (app.tree.departments || []).slice(),
        savedDepartments: (app.saved && app.saved.departments || []).slice(),
        kind: app.changeKind,
        input: document.getElementById('cvDepartments').value,
        enabled: !!app.tree.enabled
      };
    });
    assert.equal(preserved.enabled, false);
    assert.deepEqual(preserved.departments, ['财务部'],
      'a lifecycle call silently discarded unsaved department edits');
    assert.deepEqual(preserved.savedDepartments, [],
      'the save baseline claimed departments that were never saved');
    assert.notEqual(preserved.kind, 'clean', 'the dirty department edit was marked saved by the lifecycle call');
    assert.equal(preserved.input, '财务部', 'the graph settings input lost the unsaved edit');
    assert.deepEqual((await serverTree(OTHER)).departments, [],
      'disabling the graph saved the unsaved departments unexpectedly');
  } finally {
    /* 清理：源图部门恢复为空 + 基线（启用/默认）恢复；副本保持停用即可。 */
    const current = await serverTree(TREE_ID).catch(() => null);
    if (current && Array.isArray(current.departments) && current.departments.length) {
      await api('PUT', '/admin/trees/' + TREE_ID + '/departments', {
        departments: [],
        base_document_sha256: current.document_sha256
      });
    }
    await ensureBaselineTree();
  }
});

await runIfMain(import.meta.url);
