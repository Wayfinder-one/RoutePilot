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
  TREE_ID,
  api,
  appSnapshot,
  blankPoint,
  clickEdge,
  clickNode,
  danglingEdgeIds,
  dirtyBadge,
  dragPort,
  edgeOf,
  ensureBaselineTree,
  expectNever,
  issueCodes,
  openCanvas,
  openIssuesPanel,
  page,
  runIfMain,
  saveNow,
  screenshot,
  serverTree,
  test,
  waitCondPop,
  waitFor,
  waitInspectorBranch
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

  await page.locator('#cvUndo').click();
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

  await page.locator('[data-lib-kind="logic-not"]').click();
  const notId = await page.evaluate(async known => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item =>
      item.type === 'logic' && item.operator === 'not' && known.indexOf(item.id) < 0);
    return node && node.id;

}, before.nodes.map(node => node.id));
  assert.ok(notId, 'NOT node was not created');
  assert.equal(await page.locator(`#cvNodes [data-node-id="${notId}"] .rtc-slot`).count(), 1,
    'NOT must render exactly one input row');

  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    app.change('behavior', tree => {
      RTGraph.addEdge(tree.root, {
        source: 'cond-text', source_port: 'out', target: id, target_port: 'in_1', condition: null
});
      RTGraph.addEdge(tree.root, {
        source: 'cond-peak', source_port: 'out', target: id, target_port: 'in_2', condition: null
});
});

}, notId);
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.some(issue =>
    issue.node_id === id && issue.code === 'logic_slot_count' && /当前 2 个/.test(issue.zh)));
}, notId);
  assert.equal(await page.locator(`#cvNodes [data-node-id="${notId}"] .rtc-slot`).count(), 1,
    'NOT with two incoming edges still renders a single slot row');
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
  assert.equal((await rows.nth(0).locator('[data-start-condition]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.startAlways'));
}));
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

  await page.locator('#cvUndo').click();
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
  assert.equal(edgeById['e-cap-strong-not'].source_port, 'prob.light');
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

  await page.locator('#cvUndo').click();
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
  await page.locator('#cvUndo').click();
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
  await page.locator('#cvUndo').click();
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
  await page.locator('#cvUndo').click();
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
  await page.locator('#cvUndo').click();
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

test('Task 11 · logic and target inspectors edit operator, models, reasoning, priority and strategy with guards', async () => {
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
  await page.locator('#cvUndo').click();
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
  await page.locator('#cvInspectorBody [data-field="strategy"]').selectOption('least_inflight');
  await page.locator('#cvInspectorBody [data-field="capability"]').fill('强推理通用模型：复杂分析。');
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
  assert.equal(target.strategy, 'least_inflight');
  assert.equal(target.capability, '强推理通用模型：复杂分析。');
  await page.locator('#cvUndo').click();
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
  assert.equal(limitRows.edges, '15 / 400');
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
  await page.locator('#cvUndo').click();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === expected.name &&
    app.tree.timezone === expected.timezone && app.tree.sticky.enabled === true &&
    app.tree.sticky.ttl_seconds === expected.sticky.ttl_seconds &&
    app.graph.error_fallback === 'light-target');
}, BASELINE);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });
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

test('Task 12 · timezone/sticky are behavior while title/capability/name are display changes', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.timezone === 'UTC');
});
  let badge = await dirtyBadge();
  assert.equal(badge.kind, 'behavior', 'timezone change was not classified as behavior');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.dirty'));
}));
  await page.locator('#cvUndo').click();
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
  assert.equal((await dirtyBadge()).kind, 'display', 'question title was not classified as display');

  await clickNode('strong-target');
  await waitInspectorBranch('node', 'strong-target');
  await page.locator('#cvInspectorBody [data-field="capability"]').fill('强推理（展示说明）');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.find(node => node.id === 'strong-target').capability === '强推理（展示说明）');
});
  assert.equal((await dirtyBadge()).kind, 'display', 'target capability was not classified as display');

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
  badge = await dirtyBadge();
  assert.equal(badge.kind, 'display', 'tree name was not classified as display');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.dirty'));
}));

  await saveNow();
  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'a display-only save changed the server tree_revision');
  assert.equal(server.name, '展示字段决策组');
  assert.equal(server.root.nodes.find(node => node.id === 'capability').title, '能力分档（展示标题）');
  assert.equal(server.root.nodes.find(node => node.id === 'strong-target').capability, '强推理（展示说明）');
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
  const badge = await dirtyBadge();
  assert.equal(badge.hidden, false, 'inspector connection did not mark the draft dirty');
  assert.equal(badge.appKind, 'behavior');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (store.canUndo);
}), true,
    'inspector connection did not produce one history entry');
});

test('review-target-card-shows-name-model-fallback-roles-and-single-strategy', async () => {
  await ensureBaselineTree();
  await openCanvas();

  const otherwiseCard = page.locator('#cvNodes [data-node-id="standard-target"]');
  const otherwiseText = await otherwiseCard.innerText();
  assert.match(await otherwiseCard.locator('.rtc-tname').textContent(), /模型目标-\d+/,
    'target card header still shows only the model name');
  assert.match(await otherwiseCard.locator('.rtc-tbody').innerText(), /deepseek-v3/,
    'target card body does not show its model');
  assert.equal((otherwiseText.match(/最少在途请求/g) || []).length, 1,
    'target card renders the strategy more than once');
  assert.doesNotMatch(otherwiseText, /兜底模型/);

  const errorCard = page.locator('#cvNodes [data-node-id="light-target"]');
  assert.match(await errorCard.innerText(), /兜底模型/);

  await waitInspectorBranch('graph', 'root');
  assert.match(
    (await page.locator('#cvInspectorBody [data-field="fallback"] option:checked').textContent()).trim(),
    /模型目标-\d+ · qwen3-8b/);

  await clickNode('standard-target');
  await waitInspectorBranch('node', 'standard-target');
  assert.equal(
    (await page.locator('#cvInspectorBody [data-field="strategy"] option:checked').textContent()).trim(),
    '最少在途请求');
});

await runIfMain(import.meta.url);
