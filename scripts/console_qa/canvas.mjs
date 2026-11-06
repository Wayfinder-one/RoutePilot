/* RoutePilot 控制台 QA · 画布编辑用例（Task 10/12/14：骨架、视口、节点/连线、条件、历史、导入导出、保存/启用、树切换、响应式）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实启动、mock、浏览器、
   管理面 API 与 teardown 全部由 ../console_qa.mjs 提供（同一进程内单页顺序执行）。
   入口：`npm run check:console`（全量）或 `QA_ONLY=<正则> node scripts/console_qa.mjs`；
   也可单独执行：`node scripts/console_qa/canvas.mjs`。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import path from 'node:path';
import {
  BASELINE,
  EXAMPLE,
  EXAMPLE_EDGES,
  EXAMPLE_NODES,
  EXAMPLE_START,
  EXAMPLE_TARGETS,
  HISTORY_LIMIT,
  MIN_K,
  MOD,
  S1_NOW,
  TREE_ID,
  api,
  appSnapshot,
  assertIncludes,
  beforeUnloadBlocked,
  blankPoint,
  clearToasts,
  clickEdge,
  clickNode,
  danglingEdgeIds,
  dirtyBadge,
  dragLibraryItem,
  dragNodeBy,
  dragPort,
  edgeOf,
  ensureBaselineTree,
  expectHttpStatusError,
  expectedHttpStatusErrors,
  expectNever,
  exportViaUi,
  issueCodes,
  openCanvas,
  openIssuesPanel,
  page,
  portPoint,
  prepareTryCanvas,
  resetExpectedHttpStatusErrors,
  routerBase,
  runIfMain,
  runTrySample,
  saveNow,
  screenshot,
  serverTree,
  setImportPayload,
  test,
  toastText,
  waitCondPop,
  waitConfirmDialog,
  waitEdgeCondition,
  waitFor,
  waitInspectorBranch,
  waitToastIncludes
} from '../console_qa.mjs';

test('Task 10 · canvas skeleton renders the graph with no page overflow', async () => {
  await openCanvas();
  const metrics = await page.evaluate(() => ({
    nodes: document.querySelectorAll('#cvStage [data-node-id]').length,
    edges: document.querySelectorAll('#cvEdges path.rtc-edge').length,
    groups: document.querySelectorAll('#cvPalBody .rtc-pgroup').length,
    inspector: !!document.getElementById('cvInspector'),
    minimap: !!document.getElementById('cvMinimap'),
    trybar: !!document.getElementById('cvTryBar'),
    zoom: document.getElementById('cvZoomLabel').textContent,
    miniNodes: document.querySelectorAll('#cvMinimapSvg .rtc-mini-node').length,
    miniView: document.querySelectorAll('#cvMinimapSvg .rtc-mini-view').length,
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
    scrollH: document.documentElement.scrollHeight,
    innerH: window.innerHeight
}));
  assert.equal(metrics.nodes, 15);
  assert.equal(metrics.edges, 15);
  assert.equal(metrics.groups, 4);
  assert.ok(metrics.inspector && metrics.minimap && metrics.trybar);
  assert.match(metrics.zoom, /^\d+%$/);
  assert.equal(metrics.miniNodes, 15);
  assert.equal(metrics.miniView, 1);
  assert.equal(HISTORY_LIMIT, 100, 'draft history limit changed');
  assert.ok(metrics.scrollW <= metrics.clientW + 1, `horizontal overflow: ${metrics.scrollW} > ${metrics.clientW}`);
  assert.ok(metrics.scrollH <= metrics.innerH + 1, `vertical page scroll: ${metrics.scrollH} > ${metrics.innerH}`);
  const emptySlot = await page.evaluate(() => {
    const row = document.querySelector('#cvNodes .rtc-slot.is-empty');
    if (!row) return null;
    const id = row.querySelector('.rtc-slot-id');
    return {
      id: id ? id.textContent.trim() : '',
      addChips: document.querySelectorAll('#cvNodes .rtc-slot-add').length,
      display: getComputedStyle(row).display,
      border: getComputedStyle(row).borderTopStyle,
      title: row.getAttribute('title') || ''
};
});
  assert.ok(emptySlot, 'empty slot placeholder missing');
  assert.match(emptySlot.id, /^in_\d+$/);
  assert.equal(emptySlot.addChips, 0, 'canvas must not offer slot-add chips (inspector owns adding)');
  assert.equal(emptySlot.display, 'none', 'empty slots must stay hidden at rest');
  assert.equal(emptySlot.border, 'dashed');
  assert.match(emptySlot.title, /检查器|inspector/);
  const english = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');

    setLang('en');
    const node = document.querySelector('[data-node-id="and-power"]');
    const chip = node.querySelector('[data-cond-edge]');
    const out = {
      badge: node.querySelector('.rtc-badge').textContent.trim(),
      chip: chip ? chip.textContent.trim() : ''
};
    setLang('zh');
    return out;

});
  assert.equal(english.badge, 'AND', 'logic operator name leaked graph.js Chinese into the EN card');
  assert.equal(english.chip, '> 0.65', 'numeric condition chip is not rendered from the port type');
  await screenshot('task10-canvas-1440.png');

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForFunction(() => window.innerWidth === 1280 &&
    document.getElementById('cvPalette').getBoundingClientRect().width > 0 &&
    document.getElementById('cvInspector').getBoundingClientRect().width > 0);
  const narrow = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
    scrollH: document.documentElement.scrollHeight,
    innerH: window.innerHeight,
    paletteVisible: document.getElementById('cvPalette').getBoundingClientRect().width > 0,
    inspectorVisible: document.getElementById('cvInspector').getBoundingClientRect().width > 0
}));
  assert.ok(narrow.scrollW <= narrow.clientW + 1, `1280 horizontal overflow: ${narrow.scrollW} > ${narrow.clientW}`);
  assert.ok(narrow.scrollH <= narrow.innerH + 1, `1280 page scroll: ${narrow.scrollH} > ${narrow.innerH}`);
  assert.ok(narrow.paletteVisible && narrow.inspectorVisible);
  await screenshot('task10-canvas-1280.png');
  await page.locator('[data-act="zoom-reset"]').click();
  await page.waitForFunction(() => document.querySelector('#cvZoomLabel').textContent === '100%' &&
    /scale\(1\)/.test(document.getElementById('cvWorld').style.transform));
  await screenshot('task10-canvas-100.png');
  await page.setViewportSize({ width: 1440, height: 960 });
});

test('Task 10 · blank drag, space+drag and middle-button drag all pan the viewport', async () => {
  await openCanvas();
  const start = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  let point = await blankPoint();
  assert.ok(point, 'no blank canvas point found');
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 60, point.y + 30, { steps: 6 });
  await page.mouse.up();
  let viewport = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  assert.ok(Math.abs((viewport.tx - start.tx) - 60) < 2, `blank pan tx ${viewport.tx - start.tx} != 60`);
  assert.ok(Math.abs((viewport.ty - start.ty) - 30) < 2, `blank pan ty ${viewport.ty - start.ty} != 30`);
  assert.equal(Math.round(viewport.k * 1000), Math.round(start.k * 1000));
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kind());
}), 'clean');

  const beforeSpace = viewport;
  point = await blankPoint();
  await page.keyboard.down('Space');
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x - 40, point.y + 25, { steps: 6 });
  await page.mouse.up();
  await page.keyboard.up('Space');
  viewport = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  assert.ok(Math.abs((viewport.tx - beforeSpace.tx) + 40) < 2, `space pan tx ${viewport.tx - beforeSpace.tx} != -40`);
  assert.ok(Math.abs((viewport.ty - beforeSpace.ty) - 25) < 2, `space pan ty ${viewport.ty - beforeSpace.ty} != 25`);

  const beforeMiddle = viewport;
  point = await blankPoint();
  await page.mouse.move(point.x, point.y);
  await page.mouse.down({ button: 'middle' });
  await page.mouse.move(point.x + 35, point.y - 20, { steps: 6 });
  await page.mouse.up({ button: 'middle' });
  viewport = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  assert.ok(Math.abs((viewport.tx - beforeMiddle.tx) - 35) < 2, `middle pan tx ${viewport.tx - beforeMiddle.tx} != 35`);
  assert.ok(Math.abs((viewport.ty - beforeMiddle.ty) + 20) < 2, `middle pan ty ${viewport.ty - beforeMiddle.ty} != -20`);

  /* 空格仍是按钮的原生激活键：焦点在顶栏按钮上时不能开始平移。 */
  await page.focus('#cvIssuesBtn');
  await page.keyboard.press('Space');
  await page.waitForFunction(() => !document.getElementById('cvIssues').hidden, null, { timeout: 2000 });
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
}), viewport,
    'Space on a focused toolbar button started space-pan instead of activating the button');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvIssues').isHidden(), true);
});

test('7.4-01-pan-zoom-fit-reset', async () => {
  await openCanvas();
  /* 平移（空白拖拽）也属于本条验收。 */
  const panStart = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  const blank = await blankPoint();
  assert.ok(blank, 'no blank canvas point for pan');
  await page.mouse.move(blank.x, blank.y);
  await page.mouse.down();
  await page.mouse.move(blank.x + 30, blank.y + 20, { steps: 5 });
  await page.mouse.up();
  const panned = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  assert.ok(Math.abs((panned.tx - panStart.tx) - 30) < 2, 'blank pan did not move X');
  assert.ok(Math.abs((panned.ty - panStart.ty) - 20) < 2, 'blank pan did not move Y');
  assert.equal(Math.round(panned.k * 1000), Math.round(panStart.k * 1000));

  const box = await page.locator('#cvStage').boundingBox();
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const labelBefore = await page.locator('#cvZoomLabel').textContent();
  const worldBefore = await page.evaluate(async ([x, y]) => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.screenToWorld(x, y));
}, [point.x, point.y]);
  await page.mouse.move(point.x, point.y);
  await page.mouse.wheel(0, -240);
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous, labelBefore);
  const worldAfter = await page.evaluate(async ([x, y]) => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.screenToWorld(x, y));
}, [point.x, point.y]);
  assert.ok(Math.abs(worldAfter.x - worldBefore.x) < 1e-6, 'wheel zoom X anchor moved');
  assert.ok(Math.abs(worldAfter.y - worldBefore.y) < 1e-6, 'wheel zoom Y anchor moved');
  const labelZoomed = await page.locator('#cvZoomLabel').textContent();
  assert.notEqual(labelZoomed, labelBefore);

  await page.locator('[data-act="zoom-out"]').click();
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous, labelZoomed);
  const labelOut = await page.locator('#cvZoomLabel').textContent();
  await page.locator('[data-act="zoom-in"]').click();
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous, labelOut);

  await page.locator('[data-act="zoom-reset"]').click();
  await page.waitForFunction(() => document.querySelector('#cvZoomLabel').textContent === '100%');
  await page.locator('[data-act="zoom-fit"]').click();
  const inside = await page.waitForFunction(() => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const nodes = Array.from(document.querySelectorAll('#cvStage [data-node-id]'));
    return nodes.length > 0 && nodes.every(el => {
      const rect = el.getBoundingClientRect();
      return rect.left >= stage.left - 1 && rect.right <= stage.right + 1 &&
        rect.top >= stage.top - 1 && rect.bottom <= stage.bottom + 1;
});
}, null, { timeout: 3000 });
  assert.equal(await inside.jsonValue(), true, 'fit left some nodes outside the stage');
});

test('Task 10 · Esc closes the quick-create overlay before clearing the selection', async () => {
  await openCanvas();
  await clickNode('and-power');
  const selected = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
});
  assert.deepEqual(selected, { kind: 'node', id: 'and-power' });
  const point = await blankPoint();
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvQuick').isHidden(), true);
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), selected);
  await page.keyboard.press('Escape');
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: null, id: null });
});

test('7.4-02-node-drag-live-layout-only', async () => {
  await openCanvas();
  const path = page.locator('#cvEdges path.rtc-edge[data-edge-id="e-start-capability"]');
  const originalD = await path.getAttribute('d');
  const originalPosition = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };

});
  const box = await page.locator('[data-node-id="start"]').boundingBox();
  const start = { x: box.x + box.width / 2, y: box.y + 11 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + 70, start.y + 50, { steps: 6 });
  const duringD = await path.getAttribute('d');
  assert.notEqual(duringD, originalD, 'edge path did not change during node drag');
  await page.mouse.up();
  const movedD = await path.getAttribute('d');
  assert.notEqual(movedD, originalD, 'edge path did not change after node drag');
  const movedPosition = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };

});
  assert.notDeepEqual(movedPosition, originalPosition);
  const dirty = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: document.getElementById('cvDirty').dataset.kind,
    hidden: document.getElementById('cvDirty').hidden,
    appKind: app.kind()
}));
});
  assert.equal(dirty.hidden, false);
  assert.equal(dirty.kind, 'layout');
  assert.equal(dirty.appKind, 'layout');
  let snapshot = await appSnapshot();
  assert.deepEqual(snapshot.history, { undo: true, redo: false });

  const expectPosition = async expected => {
    await waitFor(async value => {
    const { app, store } = await import('/console/routing/app.js');

      const node = app.graph.nodes.find(item => item.id === 'start');
      return node && node.position.x === value.x && node.position.y === value.y;

}, expected);
};

  await page.locator('#cvUndo').click();
  await expectPosition(originalPosition);
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'clean');
  assert.deepEqual(snapshot.history, { undo: false, redo: true });

  await page.locator('#cvRedo').click();
  await expectPosition(movedPosition);

  await page.keyboard.press(`${MOD}+z`);
  await expectPosition(originalPosition);
  await page.keyboard.press(`${MOD}+Shift+z`);
  await expectPosition(movedPosition);
});

test('7.4-03-node-library-four-groups', async () => {
  await openCanvas();
  const actions = [
    { kind: 'question-choice', how: 'click', match: node => node.type === 'question' && node.question_type === 'choice' },
    { kind: 'question-noul', how: 'drag', match: node => node.type === 'question' && node.question_type === 'noul' },
    { kind: 'logic-and', how: 'click', match: node => node.type === 'logic' && node.operator === 'and' },
    { kind: 'logic-not', how: 'drag', match: node => node.type === 'logic' && node.operator === 'not' },
    { kind: 'condition-text', how: 'click', match: node => node.type === 'condition' && node.source === 'text' },
    { kind: 'condition-periods', how: 'drag', match: node => node.type === 'condition' && node.source === 'periods' },
    { kind: 'target', how: 'click', match: node => node.type === 'target' },
    { kind: 'target', how: 'drag', match: node => node.type === 'target' }
  ];
  for (const action of actions) {
    const before = await appSnapshot();
    if (action.how === 'click') {
      await page.locator(`[data-lib-kind="${action.kind}"]`).click();
} else {
      const point = await blankPoint();
      assert.ok(point, 'no blank drop point for node library drag');
      await dragLibraryItem(action.kind, point);
}
    const after = await appSnapshot();
    const known = new Set(before.nodes.map(node => node.id));
    const added = after.nodes.filter(node => !known.has(node.id));
    assert.equal(added.length, 1, `${action.kind} (${action.how}) added ${added.length} nodes`);
    assert.ok(action.match(added[0]), `${action.kind} (${action.how}) created ${JSON.stringify(added[0])}`);
}
});

test('7.4-05-numeric-default-and-boolean-true', async () => {
  await openCanvas();
  const before = await appSnapshot();
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  const target = { node: 'and-power', port: 'in_3' };
  await dragPort('capability', 'prob.strong', target, {
    beforeUp: async () => {
      const classes = await page.evaluate(() => ({
        empty: document.querySelector('[data-node-id="and-power"] [data-port-id="in_3"]').classList.contains('is-compatible'),
        occupied: document.querySelector('[data-node-id="and-power"] [data-port-id="in_1"]').classList.contains('is-compatible')
}));
      assert.equal(classes.empty, true, 'empty target slot was not highlighted as compatible');
      assert.equal(classes.occupied, false, 'occupied slot was highlighted as compatible');
}
});
  const after = await appSnapshot();
  assert.equal(after.edges.length, before.edges.length + 1);
  const edge = after.edges.find(item => item.target === 'and-power' && item.target_port === 'in_3');
  assert.ok(edge, 'new edge not found on and-power.in_3');
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'prob.strong');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 });
  const chipText = await page.locator(`#cvNodes [data-cond-edge="${edge.id}"]`).textContent();
  assert.equal(chipText.trim(), '> 0.5');
  const pop = await page.evaluate(() => ({
    hidden: document.getElementById('cvCondPop').hidden,
    edgeId: document.getElementById('cvCondPop').dataset.edgeId
}));
  assert.equal(pop.hidden, false, 'condition editor did not open for a numeric edge');
  assert.equal(pop.edgeId, edge.id);

  await page.locator('#cvUndo').click();
  let undone = await appSnapshot();
  assert.equal(undone.edges.length, before.edges.length, 'one undo did not remove the numeric edge');
  await page.locator('#cvRedo').click();
  const redone = await appSnapshot();
  assert.equal(redone.edges.length, before.edges.length + 1, 'redo did not restore the numeric edge');

  /* 布尔线的默认条件：null（= true），且不打开条件浮层。 */
  await openCanvas();
  await dragPort('start', 'out', { node: 'capability', port: 'in_2' });
  const booleanEdge = (await appSnapshot()).edges.find(item =>
    item.source === 'start' && item.target === 'capability' && item.target_port === 'in_2');
  assert.ok(booleanEdge, 'boolean edge was not created');
  assert.equal(booleanEdge.condition, null, 'boolean edge did not default to null (passes through)');
  const startSlot = page.locator(`#cvNodes [data-edge="${booleanEdge.id}"]`);
  assert.equal(await startSlot.locator('[data-cond-edge]').count(), 0);
  assert.equal((await startSlot.locator('[data-start-condition]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.startAlways'));
}));
  assert.equal(await page.evaluate(async id => {
    const RTCondPop = await import('/console/routing/condpop.js');
    return (RTCondPop.open(id));
}, booleanEdge.id), false);
  assert.equal(await page.locator('#cvCondPop').isHidden(), true,
    'boolean edge opened the comparison editor');
});

test('Task 10 · a boolean edge defaults to condition null and does not open the condition editor', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await dragPort('start', 'out', { node: 'and-power', port: 'in_3' });
  const after = await appSnapshot();
  assert.equal(after.edges.length, before.edges.length + 1);
  const edge = after.edges.find(item => item.target === 'and-power' && item.target_port === 'in_3');
  assert.ok(edge, 'boolean edge not created');
  assert.equal(edge.condition, null);
  const startSlot = page.locator(`#cvNodes [data-edge="${edge.id}"]`);
  assert.equal(await startSlot.locator('[data-cond-edge]').count(), 0);
  assert.equal((await startSlot.locator('[data-start-condition]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.startAlways'));
}));
  assert.equal(await page.evaluate(async id => {
    const RTCondPop = await import('/console/routing/condpop.js');
    return (RTCondPop.open(id));
}, edge.id), false);
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
});

test('Task 10 · a noul question output connects to an AND gate slot', async () => {
  await openCanvas();
  /* 复现用户反馈：noul 的 yes/no 输出端口拖到与门空槽位必须能建立数值连线。 */
  const domainBox = await page.locator('[data-node-id="domain"]').boundingBox();
  assert.ok(domainBox, 'domain node not visible');
  const idsBefore = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.map(node => node.id));
});
  await dragLibraryItem('logic-and', {
    x: domainBox.x + domainBox.width + 220,
    y: domainBox.y + 120,
});
  const andId = await page.evaluate(async ids => {
    const { app, store } = await import('/console/routing/app.js');

    const created = app.graph.nodes.filter(node =>
      node.type === 'logic' && node.operator === 'and' && !ids.includes(node.id));
    return created.length ? created[created.length - 1].id : null;

}, idsBefore);
  assert.ok(andId, 'AND gate was not created from the library');

  await dragPort('domain', 'yes', { node: andId, port: 'in_1' });
  const edge = (await appSnapshot()).edges.find(item =>
    item.source === 'domain' && item.source_port === 'yes' &&
    item.target === andId && item.target_port === 'in_1');
  assert.ok(edge, 'noul yes → AND in_1 edge was not created');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 }, 'numeric edge did not default to gt 0.5');
  const chipText = await page.locator(`#cvNodes [data-cond-edge="${edge.id}"]`).textContent();
  assert.equal(chipText.replace(/\s+/g, ''), '>0.5');

  /* 几何回归：汇总行输出端口必须吸附卡片右缘（不能回到行内左侧被误读成开关）。 */
  const portGeo = await page.evaluate(() => {
    const card = document.querySelector('[data-node-id="domain"]').getBoundingClientRect();
    const port = document.querySelector(
      '[data-node-id="domain"] [data-port-id="yes"][data-port-dir="out"]').getBoundingClientRect();
    return { cardRight: card.right, portCx: port.x + port.width / 2 };
});
  assert.ok(Math.abs(portGeo.cardRight - portGeo.portCx) <= 9,
    `noul yes port is not on the card right edge (card ${portGeo.cardRight}, port ${portGeo.portCx})`);
  const optPortGeo = await page.evaluate(() => {
    const card = document.querySelector('[data-node-id="capability"]').getBoundingClientRect();
    const port = document.querySelector(
      '[data-node-id="capability"] [data-port-id="selected.easy"][data-port-dir="out"]').getBoundingClientRect();
    const cardCx = card.x + card.width / 2;
    return { cardRight: card.right, portCx: port.x + port.width / 2, cardCx };
});
  assert.ok(optPortGeo.portCx > optPortGeo.cardCx,
    'choice option output port is not in the right-edge zone');
});

test('Task 10 · a condition chip opens the condition overlay and Esc closes it', async () => {
  await openCanvas();
  /* Start-edge chips stay clickable until Task 2 makes them static; use an editable edge here. */
  const edgeId = 'e-cap-strong-and-power';
  const chip = page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`);
  await chip.click();
  const pop = await page.evaluate(() => ({
    hidden: document.getElementById('cvCondPop').hidden,
    edgeId: document.getElementById('cvCondPop').dataset.edgeId
}));
  assert.equal(pop.hidden, false);
  assert.equal(pop.edgeId, edgeId);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
});

test('7.4-04-port-type-and-reject-reasons', async () => {
  await openCanvas();
  const before = await appSnapshot();
  const cases = [
    { source: 'capability', port: 'prob.easy', node: 'and-power', slot: 'in_1', pattern: /占用/ },
    { source: 'capability', port: 'prob.easy', node: 'capability', slot: 'in_2', pattern: /自身/ },
    { source: 'and-power', port: 'out', node: 'capability', slot: 'in_2', pattern: /环/ },
    { source: 'start', port: 'out', node: 'capability', slot: 'in_1', pattern: /已存在/ }
  ];
  for (const item of cases) {
    await clearToasts();
    await dragPort(item.source, item.port, { node: item.node, port: item.slot }, {
      beforeUp: async () => {
        const compatible = await page.evaluate(([node, slot]) =>
          document.querySelector(`[data-node-id="${node}"] [data-port-id="${slot}"]`).classList.contains('is-compatible'),
        [item.node, item.slot]);
        assert.equal(compatible, false, `${item.node}.${item.slot} should not be compatible`);
}
});
    await page.waitForFunction(() => document.getElementById('cvToast').textContent.trim().length > 0, null, { timeout: 3000 });
    const text = await toastText();
    assert.match(text, item.pattern, `wrong toast for ${item.source}.${item.port} → ${item.node}.${item.slot}: ${text}`);
    const now = await appSnapshot();
    assert.equal(now.edges.length, before.edges.length, 'invalid drop changed the edge count');
}

  /* 类型/结构不兼容：条件节点没有输入槽位，拖到它上面不得高亮任何兼容端口。 */
  const condBox = await page.locator('[data-node-id="cond-text"]').boundingBox();
  assert.ok(condBox, 'condition node not visible');
  await dragPort('capability', 'prob.easy',
    { x: condBox.x + condBox.width / 2, y: condBox.y + 14 },
    {
      beforeUp: async () => {
        assert.equal(await page.evaluate(() =>
          document.querySelector('[data-node-id="cond-text"]')
            .querySelectorAll('.is-compatible, .is-incompatible').length), 0,
        'a node without input slots was highlighted as a drop target');
}
});
  assert.equal(await page.locator('#cvQuick').isHidden(), true,
    'dropping on an incompatible node opened quick-create');
  assert.equal((await appSnapshot()).edges.length, before.edges.length,
    'dropping on an incompatible node created an edge');
});

test('Task 10 · output dropped on blank opens quick-create; dropping on an illegal node only cancels', async () => {
  await openCanvas();
  const before = await appSnapshot();
  const point = await blankPoint();
  assert.ok(point);
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvQuick [data-qkind="logic-and"]').count(), 1);
  await page.locator('#cvQuick [data-qkind="logic-and"]').click();
  let after = await appSnapshot();
  assert.equal(after.nodes.length, before.nodes.length + 1);
  assert.equal(after.edges.length, before.edges.length + 1);
  const node = after.nodes.find(item => !before.nodes.some(previous => previous.id === item.id));
  assert.ok(node && node.type === 'logic' && node.operator === 'and');
  const edge = after.edges.find(item => item.target === node.id);
  assert.ok(edge, 'quick-created node has no edge');
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'prob.easy');
  assert.equal(edge.target_port, 'in_1');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 });

  await openCanvas();
  const beforeCancel = await appSnapshot();
  const nodeBox = await page.locator('[data-node-id="and-power"]').boundingBox();
  await dragPort('capability', 'prob.easy', { x: nodeBox.x + nodeBox.width / 2, y: nodeBox.y + 12 });
  await expectNever(() => !document.getElementById('cvQuick').hidden,
    'quick-create opened for a drop on an illegal node');
  assert.equal(await page.locator('#cvQuick').isHidden(), true);
  const afterCancel = await appSnapshot();
  assert.equal(afterCancel.nodes.length, beforeCancel.nodes.length);
  assert.equal(afterCancel.edges.length, beforeCancel.edges.length);
});

test('7.4-10-delete-confirm-and-slot-reuse', async () => {
  await openCanvas();
  await clickNode('and-power');
  let selection = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
});
  assert.deepEqual(selection, { kind: 'node', id: 'and-power' });
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 3);
  const dialogText = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialogText, /单选题/);
  assert.match(dialogText, /是否问题-1/);
  assert.match(dialogText, /模型目标-2/);
  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-node-id="and-power"]'));
  let snapshot = await appSnapshot();
  assert.equal(snapshot.nodes.some(node => node.id === 'and-power'), false);
  assert.equal(snapshot.edges.filter(edge => edge.source === 'and-power' || edge.target === 'and-power').length, 0);

  await openCanvas();
  await clickNode('cond-text');
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-node-id="cond-text"]'));
  snapshot = await appSnapshot();
  let orEdges = snapshot.edges.filter(edge => edge.target === 'or-ctx');
  assert.equal(orEdges.length, 1);
  assert.equal(orEdges[0].target_port, 'in_2');
  assert.equal(orEdges[0].source, 'cond-peak');

  await dragPort('cond-peak', 'out', { node: 'or-ctx', port: 'in_1' });
  snapshot = await appSnapshot();
  const reused = snapshot.edges.find(edge => edge.target === 'or-ctx' && edge.target_port === 'in_1');
  assert.ok(reused, 'new edge did not reuse the freed in_1 slot');
  assert.equal(reused.source, 'cond-peak');
  orEdges = snapshot.edges.filter(edge => edge.target === 'or-ctx');
  assert.equal(orEdges.length, 2);
  assert.ok(orEdges.some(edge => edge.target_port === 'in_2' && edge.source === 'cond-peak'), 'existing in_2 edge was renumbered');
});

test('Task 10 · auto layout is layout-only and a second run adds no history entry', async () => {
  await openCanvas();
  const before = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (Object.fromEntries(app.graph.nodes.map(node => [node.id, { x: node.position.x, y: node.position.y }])));
});
  const expected = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.layoutPositions('dag', app.graph));
});
  assert.equal(Object.keys(expected).length, Object.keys(before).length);

  await page.locator('#cvAutoLayout').click();
  await waitFor(async positions => {
    const { app, store } = await import('/console/routing/app.js');

    const nodes = app.graph.nodes;
    return nodes.length === Object.keys(positions).length &&
      nodes.every(node => node.position.x === positions[node.id].x && node.position.y === positions[node.id].y);

}, expected, { timeout: 3000 });

  const laidOut = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    positions: Object.fromEntries(app.graph.nodes.map(node => [node.id, { x: node.position.x, y: node.position.y }])),
    edges: app.graph.edges.map(edge => ({ source: edge.source, target: edge.target })),
    kind: app.kind(),
    badge: { kind: document.getElementById('cvDirty').dataset.kind, hidden: document.getElementById('cvDirty').hidden },
    history: { undo: store.canUndo, redo: store.canRedo }
}));
});
  const moved = Object.keys(laidOut.positions)
    .filter(id => laidOut.positions[id].x !== before[id].x || laidOut.positions[id].y !== before[id].y);
  assert.equal(moved.length, Object.keys(before).length, 'auto layout did not move every node');
  assert.equal(laidOut.kind, 'layout', 'auto layout must not dirty behavior');
  assert.deepEqual(laidOut.badge, { kind: 'layout', hidden: false });
  assert.deepEqual(laidOut.history, { undo: true, redo: false });
  laidOut.edges.forEach(edge => {
    assert.ok(laidOut.positions[edge.target].x > laidOut.positions[edge.source].x,
      `${edge.source} -> ${edge.target} is not laid out left-to-right`);
});

  /* 第二次自动整理是 no-op：不得再入栈。若入了空历史，下面一次 undo 会退到同样的布局。 */
  await page.locator('#cvAutoLayout').click();
  await page.locator('#cvUndo').click();
  await waitFor(async positions => {
    const { app, store } = await import('/console/routing/app.js');

    const nodes = app.graph.nodes;
    return nodes.length === Object.keys(positions).length &&
      nodes.every(node => node.position.x === positions[node.id].x && node.position.y === positions[node.id].y);

}, before, { timeout: 3000 });
  const undone = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    badgeHidden: document.getElementById('cvDirty').hidden,
    history: { undo: store.canUndo, redo: store.canRedo }
}));
});
  assert.equal(undone.kind, 'clean', 'undo after a no-op layout did not restore the saved positions');
  assert.equal(undone.badgeHidden, true);
  assert.deepEqual(undone.history, { undo: false, redo: true });
});

test('Task 10 · edge delete confirm lists the edge, removes it and undo restores it', async () => {
  await openCanvas();
  const edgeId = 'e-selected-strong-strong';
  const before = await appSnapshot();
  assert.equal(before.edges.some(edge => edge.id === edgeId), true);
  await clickEdge(edgeId);
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: 'edge', id: edgeId });
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 1);
  const dialogText = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialogText, /单选题/);
  assert.match(dialogText, /selected\.strong/);
  assert.match(dialogText, /模型目标-3/);
  assert.match(dialogText, /in_1/);

  await page.locator('[data-act="delete-confirm"]').click();
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (!app.graph.edges.some(edge => edge.id === id));
}, edgeId);
  assert.equal(await page.locator(`#cvEdges path.rtc-edge[data-edge-id="${edgeId}"]`).count(), 0);
  const removed = await appSnapshot();
  assert.equal(removed.edges.length, before.edges.length - 1);
  assert.equal(removed.edges.some(edge => edge.id === edgeId), false);

  await page.locator('#cvUndo').click();
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge => edge.id === id));
}, edgeId);
  const restored = await appSnapshot();
  const edge = restored.edges.find(item => item.id === edgeId);
  assert.ok(edge, 'undo did not restore the deleted edge');
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'selected.strong');
  assert.equal(edge.target, 'strong-target');
  assert.equal(edge.target_port, 'in_1');
  assert.equal(edge.condition, null);
  assert.equal(restored.kind, 'clean');
});

test('Task 10 · Delete/Backspace do nothing while an input is focused', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await clickNode('and-power');
  await page.locator('#cvTryInput').click();
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  await expectNever(() => !document.getElementById('cvDeleteModal').hidden,
    'Delete key opened the confirm modal while an input was focused');
  assert.equal(await page.locator('#cvDeleteModal').isHidden(), true);
  const after = await appSnapshot();
  assert.equal(after.nodes.length, before.nodes.length);
});

test('7.4-06-condition-edit-dual-entry-dirty-undo', async () => {
  await openCanvas();
  const edgeId = 'e-cap-strong-and-power';
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);

  const operators = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvCondPop [data-cond-field="op"] option'))
    .map(option => [option.value, option.textContent.trim()]));
  assert.deepEqual(operators.map(item => item[0]), ['gt', 'gte', 'lt', 'lte', 'eq', 'neq'],
    'numeric operator dropdown does not expose the six canonical ops in order');
  assert.deepEqual(operators.map(item => item[1]), ['>', '≥', '<', '≤', '=', '≠']);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="op"]').inputValue(), 'gt');
  assert.equal(await page.locator('#cvCondPop [data-cond-field="value"]').inputValue(), '0.65');
  await screenshot('condition-editing-1440.png');

  /* NaN / Infinity / empty / |v| > 1e9 must stay in the editor and write nothing. */
  const historyBefore = (await appSnapshot()).history;
  for (const raw of ['', '   ', 'NaN', 'Infinity', '1e10', '-1000000001', 'abc']) {
    await page.locator('#cvCondPop [data-cond-field="op"]').selectOption('gt');
    await page.locator('#cvCondPop [data-cond-field="value"]').fill(raw);
    await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
    assert.equal(await page.locator('#cvCondPop').isHidden(), false, `bad threshold ${JSON.stringify(raw)} closed the editor`);
    const message = (await page.locator('#cvCondPop [data-cond-error]').textContent()).trim();
    assert.ok(message.length > 0, `bad threshold ${JSON.stringify(raw)} produced no inline error`);
    const current = (await edgeOf(edgeId)).condition;
    assert.deepEqual(current, { op: 'gt', value: 0.65 }, `bad threshold ${JSON.stringify(raw)} wrote the condition`);
    assert.deepEqual((await appSnapshot()).history, historyBefore, 'a rejected condition entered the history stack');
}

  /* Valid path through the slot chip entry. */
  await page.locator('#cvCondPop [data-cond-field="op"]').selectOption('lte');
  await page.locator('#cvCondPop [data-cond-field="value"]').fill('0.42');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');

    const edge = app.graph.edges.find(item => item.id === id);
    return edge && edge.condition && edge.condition.op === 'lte' && edge.condition.value === 0.42;

}, edgeId);
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  const focusedEdge = await page.evaluate(() => {
    const active = document.activeElement;
    return active && active.dataset ? (active.dataset.condEdge || '') : '';
});
  assert.equal(focusedEdge, edgeId, 'condition confirm did not return focus to the re-rendered condition chip');
  const chipText = (await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).textContent()).trim();
  assert.equal(chipText, '≤ 0.42');
  let snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior', 'condition confirm did not mark the draft behavior-dirty');
  assert.deepEqual(snapshot.history, { undo: true, redo: false });
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, { op: 'gt', value: 0.65 });
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'clean');
  assert.deepEqual(snapshot.history, { undo: false, redo: true }, 'one confirm must be exactly one history entry');

  /* The inspector edge branch uses the exact same form contract and commit path. */
  await openCanvas();
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  assert.equal(await page.locator('#cvInspectorBody [data-ins-branch="edge"]').getAttribute('data-edge-type'), 'number');
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
    'edge inspector did not label the rendered edge type');

  /* Bad numbers must be rejected through the inspector entry too (same contract). */
  const inspectorHistoryBefore = (await appSnapshot()).history;
  for (const raw of ['', '   ', 'NaN', 'Infinity', '1e10', '-1000000001', 'abc']) {
    await page.locator('#cvInspectorBody [data-cond-field="op"]').selectOption('gt');
    await page.locator('#cvInspectorBody [data-cond-field="value"]').fill(raw);
    await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
    const message = (await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim();
    const expected = await page.evaluate(async key => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t(key));
},
      raw.trim() === '' ? 'cv.cond.needValue' : 'cv.cond.badValue');
    assert.equal(message, expected, `inspector accepted/reworded bad threshold ${JSON.stringify(raw)}`);
    assert.deepEqual(await edgeOf(edgeId), {
      id: edgeId,
      source: 'capability',
      source_port: 'prob.strong',
      target: 'and-power',
      target_port: 'in_1',
      condition: { op: 'gt', value: 0.65 }
}, `inspector bad threshold ${JSON.stringify(raw)} wrote the condition`);
    assert.deepEqual((await appSnapshot()).history, inspectorHistoryBefore,
      'a rejected inspector condition entered the history stack');
}

  await page.locator('#cvInspectorBody [data-cond-field="op"]').selectOption('neq');
  await page.locator('#cvInspectorBody [data-cond-field="value"]').fill('0.7');
  await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
  await waitEdgeCondition(edgeId, { op: 'neq', value: 0.7 });
  assert.equal((await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).textContent()).trim(), '≠ 0.7');
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, { op: 'gt', value: 0.65 });
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });
});

test('Task 11 · boolean conditions use a negation switch from both entries', async () => {
  await openCanvas();
  const edgeId = 'e-selected-easy-light';
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="bool"]').count(), 0);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').count(), 1);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').isChecked(), false);

  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.locator('#cvCondPop [data-act="cond-cancel"]').click();
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  assert.equal((await edgeOf(edgeId)).condition, null);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: false });
  assert.equal((await appSnapshot()).kind, 'clean');

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  assert.equal((await edgeOf(edgeId)).condition, null);
  assert.equal((await appSnapshot()).kind, 'clean');
  assert.equal(await page.evaluate(() =>
    document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.condEdge || '' : ''
  ), edgeId, 'Esc did not return focus to the condition chip');

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="negate"]').check();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });
  assert.equal(await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTCondPop = await import('/console/routing/condpop.js');

    const edge = app.graph.edges.find(item => item.id === id);
    return RTCondPop.conditionText(edge);

}, edgeId), await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.negated'));
}));

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').isChecked(), true);
  await page.locator('#cvCondPop [data-cond-field="negate"]').uncheck();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(edgeId, null);
  assert.equal(await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTCondPop = await import('/console/routing/condpop.js');

    const edge = app.graph.edges.find(item => item.id === id);
    return RTCondPop.conditionText(edge);

}, edgeId), await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.direct'));
}));

  await openCanvas();
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  assert.equal(await page.locator(
    '#cvInspectorBody [data-ins-branch="edge"]'
  ).getAttribute('data-edge-type'), 'boolean');
  await page.locator('#cvInspectorBody [data-cond-field="negate"]').check();
  await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });
  assert.equal((await appSnapshot()).kind, 'behavior');
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, null);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });

  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  const startView = await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTCondPop = await import('/console/routing/condpop.js');

    const edge = app.graph.edges.find(item => item.id === id);
    return {
      canEdit: RTCondPop.canEditCondition(edge),
      label: RTCondPop.conditionText(edge),
      opened: RTCondPop.open(id)
};

}, startEdge.id);
  assert.equal(startView.canEdit, false);
  assert.equal(startView.label, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.startAlways'));
}));
  assert.equal(startView.opened, false);
});

test('Task 11 · validate-bar edge and graph issues locate and open the same condition editor / graph settings', async () => {
  await openCanvas();
  const edgeId = 'e-clarify-deep';
  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(edge => edge.id === id).condition = { op: 'bogus', value: 1 };
});

}, edgeId);
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.some(issue =>
    issue.edge_id === id && issue.code === 'numeric_edge_condition_invalid'));
}, edgeId);
  assert.equal((await issueCodes()).filter(code => code === 'numeric_edge_condition_invalid').length, 1);

  await openIssuesPanel();
  await page.locator('#cvIssues [data-issue-edge="e-clarify-deep"]').click();
  await waitCondPop(edgeId);
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: 'edge', id: edgeId });
  await page.locator('#cvCondPop [data-cond-field="op"]').selectOption('gt');
  await page.locator('#cvCondPop [data-cond-field="value"]').fill('0.61');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (!app.issues.some(issue => issue.code === 'numeric_edge_condition_invalid'));
});
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);

  /* Graph-level issue opens graph settings and can be fixed there. */
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => { tree.root.error_fallback = null; });

});
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.some(issue => issue.code === 'fallback_missing'));
});
  await page.locator('#cvIssuesBtn').click();
  if (await page.locator('#cvIssues').isHidden()) await page.locator('#cvIssuesBtn').click();
  await page.locator('#cvIssues [data-issue-graph="1"]').first().click();
  assert.equal(await page.evaluate(() =>
    document.getElementById('cvInspector').classList.contains('is-focus')), true,
  'graph issue locate did not light up the inspector');
  assert.equal(await page.evaluate(() => {
    const style = getComputedStyle(document.getElementById('cvInspector'));
    return style.outlineStyle + ' ' + style.outlineWidth;
}), 'solid 2px', 'is-focus has no visible token-based focus ring');
  await waitInspectorBranch('graph', 'root');
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: null, id: null });
  await page.locator('#cvInspectorBody [data-field="fallback"]').selectOption('standard-target');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (!app.issues.some(issue => issue.code === 'fallback_missing'));
});
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.error_fallback);
}), 'standard-target');
  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.error_fallback === null);
});
});

test('review-chinese-node-names', async () => {
  await openCanvas();
  const names = await page.evaluate(() => {
    const title = id => {
      const card = document.querySelector('#cvNodes [data-node-id="' + id + '"] .rtc-title');
      return card ? card.textContent.trim() : null;
};
    const firstSlot = document.querySelector('#cvNodes [data-node-id="capability"] .rtc-slot');
    return {
      capability: title('capability'),
      domain: title('domain'),
      clarify: title('clarify'),
      slotSource: firstSlot ? firstSlot.innerText : '',
      idTooltip: document.querySelector('#cvNodes [data-node-id="capability"] .rtc-title').getAttribute('title'),
};
});
  assert.equal(names.capability, '单选题', 'choice question card title is not Chinese');
  assert.equal(names.domain, '是否问题-1', 'first noul card title is not Chinese');
  assert.equal(names.clarify, '是否问题-2', 'second noul card title is not Chinese');
  assert.match(names.slotSource, /开始/, 'slot source label is not Chinese');
  assert.equal(names.idTooltip, 'capability', 'raw node id is not kept in the tooltip');

  await clickNode('capability');
  await page.waitForFunction(() =>
    document.getElementById('cvInspectorSub').textContent.trim() === '单选题');
});

test('option-delete-cascades-edges', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.locator('#cvInspectorBody [data-opt-row="strong"] [data-act="opt-delete"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 3);
  const dialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialog, /selected\.strong/);
  assert.match(dialog, /prob\.strong/);
  assert.match(dialog, /模型目标-3/);
  assert.match(dialog, /与门-2/);
  await page.locator('[data-act="delete-confirm"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (!app.graph.nodes.find(node => node.id === 'capability')
    .options.some(option => option.key === 'strong'));
});
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="strong"]').count(), 0);
  assert.deepEqual(await danglingEdgeIds(), []);
  assert.equal((await issueCodes()).includes('edge_port_missing'), false);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.some(edge =>
    edge.source === 'capability' && (edge.source_port === 'selected.strong' || edge.source_port === 'prob.strong')));
}), false);

  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.find(node => node.id === 'capability')
    .options.some(option => option.key === 'strong'));
});
  const restored = await appSnapshot();
  const restoredEdges = restored.edges.filter(edge => edge.source === 'capability' &&
    (edge.source_port === 'selected.strong' || edge.source_port === 'prob.strong'));
  assert.equal(restoredEdges.length, 3, 'one undo did not restore all option edges');
  assert.equal(restoredEdges.find(edge => edge.source_port === 'prob.strong').condition.value, 0.65);
  assert.equal(restoredEdges.find(edge => edge.source_port === 'selected.strong').condition, null);
  assert.deepEqual(await danglingEdgeIds(), []);
  assert.equal(restored.kind, 'clean');
});

test('Task 12 · moving a node saves layout-only: revision unchanged, coordinates persist, badge 仅布局', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 66, 38);
  const badge = await dirtyBadge();
  assert.equal(badge.hidden, false);
  assert.equal(badge.kind, 'layout');
  assert.equal(badge.appKind, 'layout');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.dirtyLayout'));
}));

  await saveNow();
  const saved = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    revision: app.tree.tree_revision,
    savedRevision: app.saved.tree_revision,
    badgeHidden: document.getElementById('cvDirty').hidden
}));
});
  assert.equal(saved.kind, 'clean');
  assert.equal(saved.revision, baseline.tree_revision, 'layout-only save changed the local revision');
  assert.equal(saved.savedRevision, baseline.tree_revision);
  assert.equal(saved.badgeHidden, true);

  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'a position-only save changed the server tree_revision');
  const start = server.root.nodes.find(node => node.id === 'start');
  assert.ok(Math.abs(start.position.x - moved.x) < 0.01, 'saved X coordinate differs from the draft');
  assert.ok(Math.abs(start.position.y - moved.y) < 0.01, 'saved Y coordinate differs from the draft');
});

test('Task 12 · save clears dirty while the undo history stays usable', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.clearSelection());
});
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="name"]').fill('保存后仍可撤销');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === '保存后仍可撤销' && app.kind() === 'display');
});
  const beforeSave = await dirtyBadge();
  assert.equal(beforeSave.hidden, false);
  assert.equal(beforeSave.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.dirty'));
}));

  await saveNow();
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    canUndo: store.canUndo,
    canRedo: store.canRedo,
    revision: app.tree.tree_revision,
    savedRevision: app.saved.tree_revision,
    badgeHidden: document.getElementById('cvDirty').hidden
}));
});
  assert.equal(after.kind, 'clean');
  assert.equal(after.canUndo, true, 'save cleared the undo history');
  assert.equal(after.canRedo, false);
  assert.equal(after.badgeHidden, true);
  assert.equal(after.revision, after.savedRevision);
  assert.equal((await serverTree(TREE_ID)).name, '保存后仍可撤销');

  await page.locator('#cvUndo').click();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === expected);
}, BASELINE.name);
  assert.equal((await dirtyBadge()).kind, 'display', 'undoing a save did not mark the draft dirty again');
});

test('shared graph · stale save keeps the local draft and reports a conflict', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await clearToasts();
  const baseline = await serverTree(TREE_ID);
  let sentBase = '';
  const matcher = '**/admin/trees/' + TREE_ID;
  await page.route(matcher, async route => {
    if (route.request().method() === 'PUT') {
      sentBase = route.request().postDataJSON().base_document_sha256;
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: { code: 'graph_conflict', actual_document_sha256: 'f'.repeat(64) } }),
});
      return;
}
    await route.continue();
});
  try {
    expectHttpStatusError({ url: new RegExp('/admin/trees/' + TREE_ID + '$'), status: /409/ });
    await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.change('display', tree => { tree.name = '保留这份本地草稿'; }));
});
    await page.locator('#cvSave').click();
    await page.waitForFunction(() => document.getElementById('cvToast').textContent.includes('其他修改'));
    assert.match(await toastText(), /其他修改/);
    assert.equal(sentBase, baseline.document_sha256);
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name);
}), '保留这份本地草稿');
    assert.notEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.changeKind);
}), 'clean');
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved.document_sha256);
}), baseline.document_sha256);
    assert.equal(expectedHttpStatusErrors.length, 0);
} finally {
    resetExpectedHttpStatusErrors();
    await page.unroute(matcher);
    await ensureBaselineTree();
}
});

test('Task 12 · unsaved switch-group asks one shared confirmation and beforeunload guards only dirty drafts', async () => {
  await ensureBaselineTree();
  const otherId = 'qa-switch-group';
  const existing = await api('GET', '/admin/trees');
  if (!existing.trees.some(tree => tree.id === otherId)) {
    await api('POST', '/admin/trees/' + TREE_ID + '/copy', { id: otherId, name: 'QA 切换组' });
}
  await openCanvas();
  /* clean 草稿不得 preventDefault；dirty 草稿必须 preventDefault（且只在 dirty 时）。 */
  let blocked = await beforeUnloadBlocked();
  assert.equal(blocked, false, 'a clean tree blocked beforeunload');

  await dragNodeBy('start', 40, 30);
  blocked = await beforeUnloadBlocked();
  assert.equal(blocked, true, 'a dirty draft did not prevent the beforeunload default');

  const select = page.locator('#cvTreeSelect');
  await select.selectOption(otherId);
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmSwitchTitle'));
})),
    'switch confirmation is not the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
}), TREE_ID);
  assert.equal(await select.inputValue(), TREE_ID);
  assert.equal((await dirtyBadge()).kind, 'layout');

  await select.selectOption(otherId);
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId === id && app.changeKind === 'clean');
},
    otherId, { timeout: 8000 });
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name);
}), 'QA 切换组');
  blocked = await beforeUnloadBlocked();
  assert.equal(blocked, false, 'the clean switched tree still prevented beforeunload');
  await ensureBaselineTree();
});

test('minimal-new-group-template', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await page.locator('#cvNewTree').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.kind() !== 'clean');
});
  const draft = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (({
    id: app.tree.id,
    treeId: app.treeId,
    kind: app.kind(),
    types: app.graph.nodes.map(node => node.type).sort(),
    edges: app.graph.edges.map(edge => [edge.source, edge.source_port, edge.target, edge.target_port]),
    fallback: app.graph.error_fallback,
    target: (app.graph.nodes.find(node => node.type === 'target') || {}).id,
    finite: app.graph.nodes.every(node =>
      RTGraph && node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y))
}));
});
  assert.equal(draft.kind, 'behavior');
  assert.match(draft.id, /^[a-z0-9]{12}$/, 'new tree id is not the 12-char UUID slice');
  assert.deepEqual(draft.types, ['start', 'target']);
  assert.equal(draft.edges.length, 1);
  assert.deepEqual(draft.edges[0], ['start', 'out', draft.target, 'in_1']);
  assert.equal(draft.fallback, draft.target, 'fallback model does not point at the target');
  assert.equal(draft.finite, true, 'new tree nodes have no finite coordinates');

  await saveNow();
  const saved = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    id: app.treeId,
    savedId: app.saved.id,
    enabled: app.tree.enabled,
    kind: app.kind()
}));
});
  assert.equal(saved.id, draft.id);
  assert.equal(saved.savedId, draft.id);
  assert.equal(saved.enabled, false);
  assert.equal(saved.kind, 'clean');
  const server = await serverTree(draft.id);
  assert.equal(server.enabled, false);
  assert.equal(server.root.nodes.length, 2);
  assert.equal(server.root.edges.length, 1);

  await page.locator('#cvEnable').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === true);
}, null, { timeout: 8000 });
  assert.equal((await page.locator('#cvEnableLabel').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.enabled'));
}));
  const enabled = await serverTree(draft.id);
  assert.equal(enabled.enabled, true);
  const list = await api('GET', '/admin/trees');
  assert.equal(list.trees.find(tree => tree.id === draft.id).enabled, true);
  assert.equal(list.trees.find(tree => tree.id === TREE_ID).enabled, false);
  await ensureBaselineTree();
});

test('7.4-13-save-enable-unsaved-badge', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 未保存提示：布局改动显示「仅布局」徽标；保存后徽标消失。 */
  await dragNodeBy('start', 26, 16);
  let badge = await dirtyBadge();
  assert.equal(badge.hidden, false, 'a dirty draft did not show the unsaved badge');
  assert.equal(badge.kind, 'layout');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.dirtyLayout'));
}));
  await saveNow();
  assert.equal((await dirtyBadge()).hidden, true, 'save left the unsaved badge visible');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kind());
}), 'clean');
  const requests = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname === '/admin/trees') requests.push('create');
    if (request.method() === 'POST' && /\/admin\/trees\/[^/]+\/enable$/.test(url.pathname)) requests.push('enable');
};
  page.on('request', onRequest);
  try {
    await page.locator('#cvNewTree').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null);
});
    await page.locator('#cvEnable').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === true && app.changeKind === 'clean');
},
      null, { timeout: 10000 });
    assert.deepEqual(requests, ['create', 'enable'], 'enable did not auto-save the dirty new tree first');
    assert.equal((await dirtyBadge()).hidden, true);
    const server = await serverTree(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
}));
    assert.equal(server.enabled, true);
} finally {
    page.off('request', onRequest);
    await ensureBaselineTree();
}
});

test('Task 12 · enabling an already-enabled dirty draft saves it first and never re-enables', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 30, 20);
  const requests = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/trees/' + TREE_ID) requests.push('save');
    if (request.method() === 'POST' && /\/admin\/trees\/[^/]+\/enable$/.test(url.pathname)) requests.push('enable');
};
  page.on('request', onRequest);
  try {
    await page.locator('#cvEnable').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.changeKind === 'clean');
});
    await waitToastIncludes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.toastAlreadyEnabled'));
}));
    await page.waitForTimeout(250);
    assert.deepEqual(requests, ['save'],
      'enable short-circuited on an enabled tree before saving its dirty draft');
    assert.equal((await dirtyBadge()).hidden, true);
    assert.equal((await serverTree(TREE_ID)).enabled, true);
} finally {
    page.off('request', onRequest);
    await ensureBaselineTree();
}
});

test('Task 12 · a boot fallback draft saves as a new UUID-slice tree, not "local"', async () => {
  const emptyTreeList = async route => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ trees: [], snapshot_revision: '' })
});
};
  const treeListRoute = url => url.pathname === '/admin/trees';
  await page.route(treeListRoute, emptyTreeList);
  try {
    await openCanvas();
    const draft = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
      treeId: app.treeId,
      tree: app.tree.id,
      saved: app.saved,
      kind: app.kind(),
      nodes: app.graph.nodes.length
}));
});
    assert.equal(draft.saved, null);
    assert.equal(draft.treeId, draft.tree, 'boot fallback envelope id differs from the draft tree id');
    assert.match(draft.treeId, /^[0-9a-f]{12}$/, 'boot fallback id is not a frontend UUID slice: ' + draft.treeId);
    assert.notEqual(draft.treeId, 'local');
    assert.equal(draft.nodes, 2);
    assert.equal(draft.kind, 'behavior');

    const posted = page.waitForRequest(request =>
      request.method() === 'POST' && new URL(request.url()).pathname === '/admin/trees');
    await page.locator('#cvSave').click();
    const request = await posted;
    const payload = request.postDataJSON();
    assert.match(payload.id, /^[0-9a-f]{12}$/, 'POST /admin/trees did not use the UUID-slice id');
    assert.equal(payload.id, draft.treeId);
    await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.changeKind === 'clean' && app.treeId === id);
},
      draft.treeId, { timeout: 8000 });
    const listed = (await api('GET', '/admin/trees')).trees.find(tree => tree.id === draft.treeId);
    assert.ok(listed, 'boot fallback save did not create a tree with the draft id');
    assert.equal(listed.enabled, false);
} finally {
    await page.unroute(treeListRoute, emptyTreeList);
    await ensureBaselineTree();
}
});

test('Task 12 · loading the example asks for confirmation and replaces the graph as one undo step', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 48, 34);

  await page.locator('#cvLoadExample').click();
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmExampleTitle'));
})),
    'example load did not reuse the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  const kept = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y, kind: app.kind() };

});
  assert.deepEqual({ x: kept.x, y: kept.y }, { x: moved.x, y: moved.y });
  assert.equal(kept.kind, 'layout');

  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  /* 示例是异步 fetch 的：等待它真正替换草稿（示例 start 坐标），不能只看节点数（本来就是 15）。 */
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return app.graph.nodes.length === expected.nodes &&
      app.graph.edges.length === expected.edges &&
      node && node.position.x === expected.start.x && node.position.y === expected.start.y &&
      app.kind() === 'clean';

}, { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES, start: EXAMPLE_START }, { timeout: 8000 });
  const loaded = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    canUndo: store.canUndo,
    start: (() => {
      const node = app.graph.nodes.find(item => item.id === 'start');
      return { x: node.position.x, y: node.position.y };
})()
}));
});
  assert.equal(loaded.kind, 'clean');
  assert.equal(loaded.canUndo, true);
  assert.notDeepEqual(loaded.start, { x: moved.x, y: moved.y });

  /* 一次撤销必须回到载入前的完整草稿（拖拽后的坐标），而不是半张示例图。 */
  await page.locator('#cvUndo').click();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === expected && app.kind() === 'layout');
}, EXAMPLE_NODES);
  const undone = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };

});
  assert.deepEqual(undone, { x: moved.x, y: moved.y },
    'loading the example was not a single replace-entire-graph history operation');
});

test('Task 12 · loading the example from a clean saved draft still confirms before replacing it', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* clean 但内容已和示例不同（拖动后保存），让“载入示例”成为一次可见的整图替换。 */
  const moved = await dragNodeBy('start', 52, 36);
  await saveNow();
  assert.equal((await dirtyBadge()).kind, 'clean');

  await page.locator('#cvLoadExample').click();
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmLoadExample'));
})),
    'clean example load did not use the clean confirmation title');
  assert.ok(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmLoadExampleClean'));
})),
    'clean example load did not explain that the whole graph is replaced');
  assert.equal(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmDirtyBody'));
})), false,
    'clean example load reused the discard-draft copy');

  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  const kept = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return {
      start: { x: node.position.x, y: node.position.y },
      kind: app.kind(),
      nodes: app.graph.nodes.length
};

});
  assert.deepEqual(kept.start, moved, 'cancelling the clean example load changed the draft');
  assert.equal(kept.kind, 'clean');
  assert.equal(kept.nodes, EXAMPLE_NODES);

  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return app.graph.nodes.length === expected.nodes &&
      app.graph.edges.length === expected.edges &&
      node && node.position.x === expected.start.x && node.position.y === expected.start.y;

}, { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES, start: EXAMPLE_START }, { timeout: 8000 });
  const loaded = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    canUndo: store.canUndo,
    kind: app.kind()
}));
});
  assert.equal(loaded.canUndo, true, 'confirming the example load did not add a history entry');
  /* 保存后的草稿与示例只差节点坐标，所以内容分类是 layout（整图替换仍算一个历史项）。 */
  assert.equal(loaded.kind, 'layout',
    'replacing the saved layout with the example was not classified as a layout change');

  /* 一次撤销回到保存后的拖拽坐标，证明示例载入只产生一个历史项。 */
  await page.locator('#cvUndo').click();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return node && node.position.x === expected.x && node.position.y === expected.y &&
      app.kind() === 'clean';

}, moved, { timeout: 4000 });
  await ensureBaselineTree();
});

test('Task 12 · export downloads the complete v3 envelope including timezone and sticky', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const download = await exportViaUi();
  assert.equal(download.name, TREE_ID + '.sr-routing-graph-v3.json');
  const parsed = JSON.parse(download.text);
  assert.equal(parsed.kind, 'sr-routing-graph');
  assert.equal(parsed.version, 3);
  assert.equal(parsed.name, baseline.name);
  assert.equal(parsed.timezone, BASELINE.timezone);
  assert.deepEqual(parsed.sticky, BASELINE.sticky);
  assert.equal(parsed.root.nodes.length, EXAMPLE_NODES);
  assert.equal(parsed.root.edges.length, EXAMPLE_EDGES);
  assert.equal(parsed.root.otherwise_target, undefined);
  assert.equal(parsed.root.error_fallback, EXAMPLE_TARGETS.fallback);
  assert.equal((await dirtyBadge()).kind, 'clean', 'export mutated the draft');
});

test('envelope-roundtrip-preserves-revision', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const source = await exportViaUi();
  const sourceCanonical = await page.evaluate(async text => {
    const RTGraph = await import('/console/routing/graph.js');

    const parsed = JSON.parse(text);
    return RTGraph.behaviorCanonical({
      name: parsed.name, timezone: parsed.timezone, sticky: parsed.sticky, root: parsed.root
});

}, source.text);

  await page.locator('#cvNewTree').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  const newId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  await setImportPayload(source.text);
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === expected.nodes && app.graph.edges.length === expected.edges);
},
  { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES }, { timeout: 8000 });
  const imported = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (({
    id: app.treeId,
    treeId: app.tree.id,
    name: app.tree.name,
    timezone: app.tree.timezone,
    sticky: app.tree.sticky,
    kind: app.kind(),
    canonical: RTGraph.behaviorCanonical(app.tree),
    canUndo: store.canUndo
}));
});
  assert.equal(imported.id, newId, 'import replaced the target group id');
  assert.equal(imported.treeId, newId);
  assert.equal(imported.name, JSON.parse(source.text).name);
  assert.equal(imported.timezone, BASELINE.timezone);
  assert.deepEqual(imported.sticky, BASELINE.sticky);
  assert.equal(imported.canonical, sourceCanonical, 'import round-trip changed the behavior canonical');
  assert.equal(imported.kind, 'behavior');
  assert.equal(imported.canUndo, true);

  /* 保存回服务端：行为 canonical 相同 ⇒ 服务端 tree_revision 必须与导出源一致。 */
  await saveNow();
  const roundTripped = await serverTree(newId);
  assert.equal(roundTripped.tree_revision, baseline.tree_revision,
    'export → import → save changed the behavior revision');
  assert.equal(roundTripped.root.nodes.length, EXAMPLE_NODES);
  assert.equal(roundTripped.root.edges.length, EXAMPLE_EDGES);

  await page.locator('#cvUndo').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === 2);
});
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.edges.length);
}), 1,
    'one undo did not restore the pre-import draft');
});

test('7.4-14-import-export-v3-only', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 导出只产出 sr-routing-graph v3；导入下面只接受同一格式。 */
  const exported = await exportViaUi();
  const exportedDoc = JSON.parse(exported.text);
  assert.equal(exportedDoc.kind, 'sr-routing-graph');
  assert.equal(exportedDoc.version, 3);
  assert.equal(exportedDoc.root.type, 'graph');
  await dragNodeBy('start', 36, 24);

  /* 未保存时先弹共享确认框；取消后不选文件、草稿保持。 */
  await page.locator('#cvImport').click();
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmImportTitle'));
})),
    'import did not reuse the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  assert.equal((await dirtyBadge()).kind, 'layout');

  const rejection = await page.evaluate(async () => {
    const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.REJECT_MESSAGE);
});
  const keptBefore = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };

});
  await setImportPayload({ root: { type: 'jev', nodes: [], edges: [] }, version: 1 }, 'legacy-jev.json');
  await waitToastIncludes(rejection);
  const kept = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y, kind: app.kind() };

});
  assert.deepEqual({ x: kept.x, y: kept.y }, keptBefore, 'a rejected legacy import replaced the draft');
  assert.equal(kept.kind, 'layout');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length);
}), EXAMPLE_NODES);

  /* 正确 kind、version=2：同一固定拒绝文案。 */
  await clearToasts();
  const source = await exportViaUi();
  const version2 = JSON.parse(source.text);
  version2.version = 2;
  await clearToasts();
  await setImportPayload(version2, 'version-2.json');
  await waitToastIncludes(rejection);
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };

}), keptBefore, 'a rejected version-2 import replaced the draft');
});

test('shared graph · missing required envelope field keeps the draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const draft = () => page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    name: app.tree.name,
    timezone: app.tree.timezone,
    sticky: app.tree.sticky,
    kind: app.kind(),
    nodes: app.graph.nodes.length,
    edges: app.graph.edges.length
}));
});
  const before = await draft();
  const document = JSON.parse((await exportViaUi()).text);
  delete document.sticky;

  await setImportPayload(document, 'missing-sticky.json');
  await waitToastIncludes(await page.evaluate(async n => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (tf('cv.toastImportIssues', { n }));
}, 1));

  assert.deepEqual(await draft(), before, 'a document missing sticky replaced the local draft');
});

test('Task 12 · a v3 document with a malformed root reports graph_parse_error and keeps the draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 34, 22);
  const draftOf = () => page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return {
      start: { x: node.position.x, y: node.position.y },
      kind: app.kind(),
      nodes: app.graph.nodes.length,
      edges: app.graph.edges.length
};

});
  const kept = await draftOf();

  await clearToasts();
  await setImportPayload({
    kind: 'sr-routing-graph',
    version: 3,
    name: 'v3 但 root 非法',
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: { type: 'jev', branches: [] }
}, 'malformed-v3.json');

  await waitToastIncludes(await page.evaluate(async n => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (tf('cv.toastImportIssues', { n }));
}, 1));
  const issues = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.map(issue => ({
    code: issue.code, level: issue.level, zh: issue.zh, en: issue.en,
    node_id: issue.node_id, edge_id: issue.edge_id, port: issue.port
})));
});
  /* 被拒文件的 Issue 不能留在校验条：否则「定位」会指向草稿里不存在的节点。
     校验条恢复为当前草稿的 4 条提示（§5.3）。 */
  assert.deepEqual(issues.map(issue => issue.code), ['hint', 'hint', 'hint', 'hint'],
    'a malformed v3 import leaked its parse issue into the draft validation bar');
  assert.equal(issues.every(issue => issue.level === 'hint'), true);
  assert.deepEqual(issues.map(issue => [issue.node_id, issue.edge_id]),
    [['', ''], ['', ''], ['', ''], ['', '']]);
  assert.equal(await page.locator('#cvIssues .rtc-issue.is-hard').count(), 0,
    'the validation bar rendered the rejected file as a hard issue');
  assert.equal(await page.locator('#cvIssues [data-locate]').count(), 0,
    'the rejected file left locatable issues pointing at draft nodes');
  assert.deepEqual(await draftOf(), kept, 'a malformed v3 import changed the draft');
});

test('Task 12 · a valid v3 import without positions lays every node out with finite coordinates', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const stripped = JSON.parse(JSON.stringify(EXAMPLE));
  stripped.nodes.forEach(node => { delete node.position; });
  await setImportPayload({
    kind: 'sr-routing-graph',
    version: 3,
    name: BASELINE.name,
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: stripped
}, 'missing-positions.json');
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === expected && app.graph.nodes.every(node =>
      node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)));
},
  EXAMPLE_NODES, { timeout: 8000 });
  const result = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (({
    kind: app.kind(),
    issues: app.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
    nodes: app.graph.nodes.map(node => ({ id: node.id, x: node.position.x, y: node.position.y })),
    expected: RTGraph.layoutPositions('dag', {
      nodes: app.graph.nodes, edges: app.graph.edges
})
}));
});
  assert.deepEqual(result.issues, [], 'position-less valid v3 import failed structure validation');
  result.nodes.forEach(node => {
    assert.ok(Number.isFinite(node.x) && Number.isFinite(node.y), `${node.id} has no finite position`);
    assert.deepEqual({ x: node.x, y: node.y }, result.expected[node.id],
      `${node.id} was not placed by the shared DAG layout`);
});
  assert.equal(result.kind, 'layout', 'filling missing positions must stay layout-only');
});

test('7.4-12-auto-layout-layout-only', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 70, 45);
  const expected = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.layoutPositions('dag', app.graph));
});
  await page.locator('#cvAutoLayout').click();
  await waitFor(async positions => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.every(node => {
    const next = positions[node.id];
    return next && node.position.x === next.x && node.position.y === next.y;
}));
}, expected, { timeout: 4000 });
  const laidOut = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    badge: document.getElementById('cvDirty').dataset.kind,
    positions: Object.fromEntries(app.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
}));
});
  assert.equal(laidOut.kind, 'layout');
  assert.equal(laidOut.badge, 'layout');

  await saveNow();
  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'auto layout leaked into the server behavior revision');
  server.root.nodes.forEach(node => {
    const position = laidOut.positions[node.id];
    assert.ok(position, `${node.id} missing from the client layout`);
    assert.ok(Math.abs(node.position.x - position.x) < 0.01, `${node.id} saved X differs from the DAG layout`);
    assert.ok(Math.abs(node.position.y - position.y) < 0.01, `${node.id} saved Y differs from the DAG layout`);
});
});

test('7.4-15-validation-bar-and-locate', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 用禁用中的新组（enable 才会先保存）；载入示例，让 422 Issue 有可定位的节点/边。 */
  await page.locator('#cvNewTree').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  const targetId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === expected.nodes &&
    app.graph.edges.length === expected.edges &&
    app.graph.nodes.some(node => node.id === 'deep-target'));
},
  { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES }, { timeout: 8000 });
  await saveNow();
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled);
}), false,
    'the fixture tree must start disabled so enable really runs');
  await dragNodeBy('start', 40, 30);
  const issuesPayload = [
    { code: 'invalid_timezone', level: 'hard', zh: '时区无效（QA）', en: 'Invalid timezone (QA)', node_id: '', edge_id: '', port: '' },
    { code: 'question_incomplete', level: 'hard', zh: '问题节点不完整（QA）', en: 'Incomplete question (QA)', node_id: 'capability', edge_id: '', port: '' },
    { code: 'numeric_edge_condition_invalid', level: 'hard', zh: '槽位条件非法（QA）', en: 'Invalid slot condition (QA)', node_id: 'deep-target', edge_id: 'e-clarify-deep', port: 'in_1' },
    { code: 'fallback_missing', level: 'hard', zh: '兜底缺失（QA）', en: 'Missing fallback (QA)', node_id: '', edge_id: '', port: 'error_fallback' }
  ];
  const enableRequests = [];
  const onRequest = request => {
    if (request.method() === 'POST' && /\/admin\/trees\/[^/]+\/enable$/.test(new URL(request.url()).pathname)) {
      enableRequests.push(request.url());
}
};
  const putHandler = async route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'PUT' && url.pathname === '/admin/trees/' + targetId) {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: issuesPayload })
});
      return;
}
    await route.continue();
};
  page.on('request', onRequest);
  await page.route('**/admin/trees/**', putHandler);
  try {
    /* 只允许这一条故意 422：URL 精确到本用例的新组，状态必须是 422。 */
    expectHttpStatusError({ url: new RegExp('/admin/trees/' + targetId + '$'), status: /422/ });
    await page.locator('#cvEnable').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.length === 8 &&
      !document.getElementById('cvIssues').hidden);
}, null, { timeout: 8000 });
    await page.waitForTimeout(300);
    assert.equal(expectedHttpStatusErrors.length, 0,
      'the scoped 422 console error was not observed at the expected URL/status');
    assert.equal(enableRequests.length, 0, 'enable was sent even though the save failed');
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled);
}), false);
    assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.filter(issue => issue.level !== 'hint')
      .map(issue => issue.code));
}),
      ['invalid_timezone', 'question_incomplete', 'numeric_edge_condition_invalid', 'fallback_missing']);
    /* §5.3：服务端 4 个错误必须与当前草稿的 4 条提示合并显示。 */
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.filter(issue => issue.level === 'hint').length);
}),
      4, 'the 422 issues dropped the 4 RTGraph hints');
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.validate(app.tree.root)
      .filter(issue => issue.level === 'hint').length);
}), 4);
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.every(issue =>
      issue.level === 'hint' || issue.level === 'hard'));
}), true);

    /* 节点定位：选中并居中 */
    await page.locator('#cvIssues [data-issue-node="capability"]').click();
    assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}),
      { kind: 'node', id: 'capability' });
    assert.equal(await page.evaluate(() => {
      const stage = document.getElementById('cvStage').getBoundingClientRect();
      const rect = document.querySelector('[data-node-id="capability"]').getBoundingClientRect();
      return Math.abs(rect.left + rect.width / 2 - (stage.left + stage.width / 2)) < 60 &&
        Math.abs(rect.top + rect.height / 2 - (stage.top + stage.height / 2)) < 60;
}), true, 'node issue locate did not center the node');
    /* 等保存失败 toast 自然消失，截一张干净的「校验错误 + 已定位节点」证据图。 */
    await page.waitForTimeout(4300);
    await screenshot('validation-error-locating-1440.png');

    /* 边 / 槽位定位：Issue 保留 slot 字段，选中边并打开同一条件浮层 */
    const slotIssue = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.find(issue => issue.code === 'numeric_edge_condition_invalid'));
});
    assert.equal(slotIssue.port, 'in_1', 'the slot issue lost its port');
    await page.locator('#cvIssues [data-issue-edge="e-clarify-deep"]').click();
    await waitCondPop('e-clarify-deep');
    assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}),
      { kind: 'edge', id: 'e-clarify-deep' });

    /* 图级定位：条件浮层此刻仍开着；定位必须自己关掉它并聚焦检查器图设置。 */
    await page.locator('#cvIssues [data-issue-graph="1"]').first().click();
    assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), true,
      'graph-level locate left the condition popup open');
    assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: null, id: null });
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvInspector').classList.contains('is-focus')), true);
    await waitInspectorBranch('graph', 'root');

    /* Task 14：试算/收起会把检查器折叠起来；图级定位必须重新展开图设置，
       否则「定位」只把焦点送进一个不可见的面板。 */
    await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.collapsePanels());
});
    await page.waitForFunction(() =>
      document.getElementById('cvInspector').classList.contains('is-collapsed'));
    await page.locator('#cvIssues [data-issue-graph="1"]').first().click();
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvInspector').classList.contains('is-collapsed')), false,
    'graph-level locate left the collapsed inspector hidden');
    await waitInspectorBranch('graph', 'root');
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvInspector').classList.contains('is-focus')), true);
} finally {
    resetExpectedHttpStatusErrors();
    page.off('request', onRequest);
    await page.unroute('**/admin/trees/**', putHandler);
    await ensureBaselineTree();
}
});

test('Task 12 · example graph saves, enables and survives a page refresh with a consistent revision', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await page.locator('#cvNewTree').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  const newId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  /* 新建草稿是 2 个节点：必须等示例 root 真正替换后再保存。 */
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === expected.nodes &&
    app.graph.edges.length === expected.edges &&
    app.graph.nodes.some(node => node.id === 'deep-target'));
},
  { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES }, { timeout: 8000 });
  await saveNow();

  const savedServer = await serverTree(newId);
  assert.equal(savedServer.enabled, false);
  assert.equal(savedServer.root.nodes.length, EXAMPLE_NODES);
  assert.equal(savedServer.root.edges.length, EXAMPLE_EDGES);
  const revision = savedServer.tree_revision;

  await page.locator('#cvEnable').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === true);
}, null, { timeout: 8000 });
  const enabledServer = await serverTree(newId);
  assert.equal(enabledServer.enabled, true);
  assert.equal(enabledServer.tree_revision, revision, 'enabling the example graph changed its revision');

  await openCanvas();
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    treeId: app.treeId,
    revision: app.tree.tree_revision,
    enabled: app.tree.enabled,
    nodes: app.graph.nodes.length,
    edges: app.graph.edges.length,
    kind: app.kind(),
    revisionLabel: document.getElementById('cvRevision').textContent
}));
});
  assert.equal(after.treeId, newId, 'refresh booted a different tree than the enabled one');
  assert.equal(after.revision, revision, 'refreshed tree revision differs from the GET revision');
  assert.equal(after.enabled, true);
  assert.equal(after.nodes, EXAMPLE_NODES);
  assert.equal(after.edges, EXAMPLE_EDGES);
  assert.equal(after.kind, 'clean');
  assert.ok(after.revisionLabel.includes(revision.slice(0, 10)),
    'toolbar revision label does not show the refreshed revision');
  assert.equal((await serverTree(newId)).tree_revision, revision);
  await ensureBaselineTree();
});

test('7.4-11-undo-redo-buttons-and-keyboard', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const original = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };

});
  const moved = await dragNodeBy('start', 46, 26);
  assert.notDeepEqual(moved, original);
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (store.canUndo);
}), true);
  const expectStart = position => waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return node && node.position.x === expected.x && node.position.y === expected.y;

}, position, { timeout: 4000 });

  await page.locator('#cvUndo').click();
  await expectStart(original);
  await page.locator('#cvRedo').click();
  await expectStart(moved);
  await page.keyboard.press(`${MOD}+z`);
  await expectStart(original);
  await page.keyboard.press(`${MOD}+Shift+z`);
  await expectStart(moved);
});

test('quick-create-compatible-node', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const before = await appSnapshot();
  const point = await blankPoint();
  assert.ok(point, 'no blank canvas point for quick create');
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  const menu = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kinds: Array.from(document.querySelectorAll('#cvQuick [data-qkind]')).map(el => el.dataset.qkind),
    head: document.querySelector('#cvQuick .rtc-quick-h').textContent,
    quick: app.quick
      ? { source: app.quick.sourceNodeId, port: app.quick.sourcePort }
      : null
}));
});
  /* 快速创建只列出「有输入槽位、且当前输出类型可连」的节点；条件/起点不在列。 */
  assert.deepEqual(menu.kinds, [
    'question-choice', 'question-noul', 'question-score',
    'logic-and', 'logic-or', 'logic-not', 'target'
  ]);
  assert.deepEqual(menu.quick, { source: 'capability', port: 'prob.easy' });
  assertIncludes(menu.head, 'capability');

  await page.locator('#cvQuick [data-qkind="target"]').click();
  const after = await appSnapshot();
  assert.equal(after.nodes.length, before.nodes.length + 1);
  assert.equal(after.edges.length, before.edges.length + 1);
  const created = after.nodes.find(node => !before.nodes.some(previous => previous.id === node.id));
  assert.equal(created.type, 'target');
  assert.equal(await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.findNode(app.graph, id).priority);
}, created.id), 6,
  'a new target must take max(priority) + 1 under higher-wins');
  const edge = after.edges.find(item => item.target === created.id);
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'prob.easy');
  assert.equal(edge.target_port, 'in_1');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 });
  assert.equal(await page.locator('#cvQuick').isHidden(), true);
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection());
}), { kind: 'edge', id: edge.id });
  assert.equal(await page.locator('#cvCondPop').isHidden(), false,
    'quick-creating from a numeric port did not open the condition editor');
  await page.keyboard.press('Escape');

  /* 布尔源：同一份兼容清单；新边默认 = true 且不弹比较编辑器。 */
  await openCanvas();
  const boolBefore = await appSnapshot();
  const boolPoint = await blankPoint();
  await dragPort('start', 'out', boolPoint);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  assert.deepEqual(await page.evaluate(() =>
    Array.from(document.querySelectorAll('#cvQuick [data-qkind]')).map(el => el.dataset.qkind)), [
    'question-choice', 'question-noul', 'question-score',
    'logic-and', 'logic-or', 'logic-not', 'target'
  ]);
  await page.locator('#cvQuick [data-qkind="question-noul"]').click();
  const boolAfter = await appSnapshot();
  const boolNode = boolAfter.nodes.find(node => !boolBefore.nodes.some(previous => previous.id === node.id));
  assert.equal(boolNode.type, 'question');
  assert.equal(boolNode.question_type, 'noul');
  const boolEdge = boolAfter.edges.find(item => item.target === boolNode.id);
  assert.ok(boolEdge, 'boolean quick create did not connect the new node');
  assert.equal(boolEdge.condition, null);
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
});

test('200-node-400-edge-smoke', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const doc = await page.evaluate(async () => {
    const RTGraph = await import('/console/routing/graph.js');

    const G = RTGraph;
    const graph = {
      id: 'root', type: 'graph', nodes: [], edges: [],
      error_fallback: 't67'
};
    graph.nodes.push({ id: 'start', type: 'start' });
    graph.nodes.push({ id: 'cond', type: 'condition', source: 'text', contains_any: ['故障'] });
    for (let i = 0; i < 32; i += 1) {
      graph.nodes.push({
        id: 'q' + i, type: 'question', question_type: 'choice',
        instructions: '极限图问题 ' + i + '：请选择最合适的能力档位。',
        options: [
          { key: 'easy', label: '轻量', capability: '简单问答' },
          { key: 'strong', label: '增强', capability: '复杂分析' }
        ]
});
}
    for (let i = 0; i < 98; i += 1) {
      graph.nodes.push({ id: 'l' + i, type: 'logic', operator: 'or' });
}
    for (let i = 0; i < 68; i += 1) {
      graph.nodes.push({
        id: 't' + i, type: 'target',
        models: [{ model: 'deepseek-v3', reasoning: 'high' }], priority: i + 1
});
}
    let seq = 0;
    const addEdge = (source, sourcePort, target, targetPort) => {
      seq += 1;
      graph.edges.push({
        id: 'e' + seq, source, source_port: sourcePort,
        target, target_port: targetPort, condition: null
});
};
    /* 32：start → 每个问题；196：每个 OR 门由两个问题的 selected 端口各接一条。 */
    for (let i = 0; i < 32; i += 1) addEdge('start', 'out', 'q' + i, G.nextSlot(graph, 'q' + i));
    for (let i = 0; i < 98; i += 1) {
      const logic = 'l' + i;
      addEdge('q' + ((2 * i) % 32), 'selected.easy', logic, G.nextSlot(graph, logic));
      addEdge('q' + ((2 * i + 1) % 32), 'selected.strong', logic, G.nextSlot(graph, logic));
}
    /* 68：前 68 个门 → target[i]；104：再按固定公式补齐到 400 边。 */
    for (let i = 0; i < 68; i += 1) {
      addEdge('l' + i, 'out', 't' + i, G.nextSlot(graph, 't' + i));
}
    for (let i = 0; i < 104; i += 1) {
      const target = 't' + ((7 * i + 1) % 68);
      addEdge('l' + (i % 98), 'out', target, G.nextSlot(graph, target));
}
    return {
      kind: 'sr-routing-graph', version: 3, name: '极限图 200 节点 / 400 边',
      timezone: 'Asia/Shanghai', sticky: { enabled: false, ttl_seconds: 1800 }, root: graph
};

});
  assert.equal(doc.root.nodes.length, 200, 'generator did not make 200 nodes');
  assert.equal(doc.root.edges.length, 400, 'generator did not make 400 edges');

  await setImportPayload(doc, 'extreme-200-400.json');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === 200 &&
    app.graph.edges.length === 400);
}, null, { timeout: 20000 });
  await page.waitForFunction(() =>
    document.querySelectorAll('#cvStage [data-node-id]').length === 200 &&
    document.querySelectorAll('#cvEdges path.rtc-edge').length === 400, null, { timeout: 20000 });

  const counts = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const graph = app.graph;
    const byType = {};
    graph.nodes.forEach(node => { byType[node.type] = (byType[node.type] || 0) + 1; });
    return {
      byType,
      nodes: graph.nodes.length,
      edges: graph.edges.length,
      startOut: graph.edges.filter(edge => edge.source === 'start').length,
      questionIn: graph.edges.filter(edge => edge.target.charAt(0) === 'q').length,
      logicIn: graph.edges.filter(edge => edge.target.charAt(0) === 'l').length,
      targetIn: graph.edges.filter(edge => edge.target.charAt(0) === 't').length,
      domNodes: document.querySelectorAll('#cvStage [data-node-id]').length,
      domEdges: document.querySelectorAll('#cvEdges path.rtc-edge').length,
      logicInputs: graph.nodes.filter(node => node.type === 'logic').map(node => ({
        id: node.id,
        inputs: graph.edges.filter(edge => edge.target === node.id).length
})),
      hard: app.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
      limit: app.issues.filter(issue => issue.code === 'limit_exceeded').length,
      priorities: graph.nodes.filter(node => node.type === 'target')
        .map(node => node.priority).sort((a, b) => a - b),
      fallbacks: [graph.error_fallback]
};

});
  assert.deepEqual(counts.byType, { start: 1, condition: 1, question: 32, logic: 98, target: 68 });
  assert.equal(counts.nodes, 200);
  assert.equal(counts.edges, 400);
  assert.equal(counts.startOut, 32);
  assert.equal(counts.questionIn, 32);
  assert.equal(counts.logicIn, 196);
  /* 不只看总数：每个 OR 门必须恰好 2 条输入，生成器的取模错误不会被 196 掩盖。 */
  assert.equal(counts.logicInputs.length, 98);
  const wrongGates = counts.logicInputs.filter(gate => gate.inputs !== 2);
  assert.deepEqual(wrongGates, [],
    'OR gates with a slot count other than 2: ' + JSON.stringify(wrongGates.slice(0, 5)));
  assert.equal(counts.targetIn, 172);
  assert.equal(counts.domNodes, 200, 'the DOM did not render 200 node cards');
  assert.equal(counts.domEdges, 400, 'the DOM did not render 400 edge paths');
  assert.equal(counts.limit, 0, 'the 200/400 graph reported limit_exceeded');
  assert.deepEqual(counts.hard, [], 'the extreme graph failed structural validation');
  assert.deepEqual(counts.priorities, Array.from({ length: 68 }, (_, index) => index + 1));
  assert.deepEqual(counts.fallbacks, ['t67']);

  /* 平移 / 滚轮缩放 / fit 仍有响应；不设置任何未经验证的帧率指标。 */
  const center = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const area = app.kit.safeArea(20);
    return { x: stage.left + area.x + area.w / 2, y: stage.top + area.y + area.h / 2 };

});
  const viewport = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  await page.mouse.move(center.x, center.y);
  await page.mouse.down({ button: 'middle' });
  await page.mouse.move(center.x + 44, center.y + 26, { steps: 6 });
  await page.mouse.up({ button: 'middle' });
  const panned = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  assert.ok(Math.abs((panned.tx - viewport.tx) - 44) < 2, 'extreme graph ignored a pan');
  assert.ok(Math.abs((panned.ty - viewport.ty) - 26) < 2, 'extreme graph ignored a pan');
  const labelBefore = await page.locator('#cvZoomLabel').textContent();
  await page.mouse.move(center.x, center.y);
  await page.mouse.wheel(0, -240);
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous,
    labelBefore, { timeout: 3000 });
  const zoomed = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getViewport());
});
  assert.ok(zoomed.k > panned.k, 'wheel zoom did not react on the extreme graph');
  await page.locator('[data-act="zoom-fit"]').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const current = app.kit.getViewport();
    return Number.isFinite(current.tx) && Number.isFinite(current.ty) && current.k > 0;

}, null, { timeout: 3000 });
  const fitted = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    viewport: app.kit.getViewport(),
}));
});
  assert.equal(Math.round(fitted.viewport.k * 1000), Math.round(MIN_K * 1000),
    'the 200-node graph should clamp fit at the minimum zoom');
  await page.locator('[data-act="zoom-reset"]').click();
  await page.waitForFunction(() => document.querySelector('#cvZoomLabel').textContent === '100%');
  await screenshot('extreme-200-400.png');
});

test('condition-only-question-is-valid', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const doc = {
    kind: 'sr-routing-graph',
    version: 3,
    name: '条件可达性图',
    timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root',
      type: 'graph',
      nodes: [
        { id: 'start', type: 'start' },
        { id: 'cond-gate', type: 'condition', source: 'text', contains_any: ['故障'] },
        {
          id: 'cond-q', type: 'question', question_type: 'choice',
          instructions: '仅由第 0 阶段条件节点触发的问题。',
          options: [
            { key: 'easy', label: '轻量', capability: '简单问答' },
            { key: 'strong', label: '增强', capability: '复杂分析' }
          ]
},
        { id: 'target-cond', type: 'target', models: [{ model: 'qwen3-8b' }], priority: 1 }
      ],
      edges: [
        {
          id: 'e-cond-q', source: 'cond-gate', source_port: 'out',
          target: 'cond-q', target_port: 'in_1', condition: null
},
        {
          id: 'e-q-target', source: 'cond-q', source_port: 'selected.easy',
          target: 'target-cond', target_port: 'in_1', condition: null
}
      ],
      error_fallback: 'target-cond'
}
};
  /* 新建未启用的组再导入：保存走 POST /admin/trees，不会覆盖已启用的基线组。 */
  await page.locator('#cvNewTree').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  await setImportPayload(doc, 'condition-only.json');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === 4 && app.graph.edges.length === 2);
});
  const imported = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    hard: app.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
    questionSources: app.graph.edges
      .filter(edge => edge.target === 'cond-q').map(edge => edge.source),
    startOut: app.graph.edges.filter(edge => edge.source === 'start').length
}));
});
  assert.deepEqual(imported.hard, [], 'condition-only reachability was rejected by the client validator');
  assert.deepEqual(imported.questionSources, ['cond-gate'],
    'the fixture question must be fed by the condition node only');
  assert.equal(imported.startOut, 0,
    'the fixture start node must be isolated, so the question is only reachable from the condition');

  await saveNow();
  const treeId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  const saved = await serverTree(treeId);
  assert.equal(saved.enabled, false);
  assert.equal(saved.root.nodes.length, 4);
  assert.equal(saved.root.edges.length, 2);
  assert.match(saved.tree_revision, /^[0-9a-f]{64}$/);
  assert.equal(saved.root.error_fallback, 'target-cond');

  /* 只由第 0 阶段条件触发的问题也必须能被真链路执行：启用后走 /admin/try。 */
  await api('POST', '/admin/trees/' + encodeURIComponent(treeId) + '/enable');
  const tried = await api('POST', '/admin/try', {
    tree_id: treeId,
    text: '10kV 线路故障，请安排抢修。',
    now: S1_NOW
});
  assert.equal(tried.outcome.type, 'hit', 'a condition-only question did not route');
  assert.equal(tried.outcome.target_id, 'target-cond',
    'the condition-only question did not reach its selected.easy target');
  assert.equal(tried.outcome.waves.length, 1, 'the condition-only graph needed more than one wave');
  await ensureBaselineTree();
});

test('layout-persists-across-save-and-reload', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 54, 32);
  await saveNow();
  const saved = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    revision: app.tree.tree_revision,
    positions: Object.fromEntries(app.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
}));
});
  assert.equal(saved.revision, baseline.tree_revision, 'a layout-only save changed the revision');

  await page.reload();
  await page.waitForSelector('#cvStage [data-node-id]', { timeout: 20000 });
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app && app.booted && app.kit);
});
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.changeKind === 'clean');
});
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    revision: app.tree.tree_revision,
    kind: app.kind(),
    badgeHidden: document.getElementById('cvDirty').hidden,
    positions: Object.fromEntries(app.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
}));
});
  assert.equal(after.revision, saved.revision, 'reload changed the tree revision');
  assert.equal(after.kind, 'clean');
  assert.equal(after.badgeHidden, true);
  Object.keys(saved.positions).forEach(id => {
    assert.ok(after.positions[id], `${id} disappeared after reload`);
    assert.ok(Math.abs(after.positions[id].x - saved.positions[id].x) < 0.01, `${id} X moved after reload`);
    assert.ok(Math.abs(after.positions[id].y - saved.positions[id].y) < 0.01, `${id} Y moved after reload`);
});
  assert.ok(Math.abs(after.positions.start.x - moved.x) < 0.01, 'the dragged layout was not persisted');
});

test('Task 14 · a malformed v3 import with non-array fields is rejected before applying', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const draftOf = () => page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    nodes: app.graph.nodes.map(node => node.id),
    edges: app.graph.edges.map(edge => edge.id),
    models: app.graph.nodes.find(node => node.type === 'target').models,
    kind: app.kind()
}));
});
  const kept = await draftOf();
  await clearToasts();

  /* models: 5 曾在 cards.js 里直接 .map 崩溃：parseImport 必须在应用前拒绝。 */
  const malformed = JSON.parse(JSON.stringify(EXAMPLE));
  malformed.nodes.find(node => node.type === 'target').models = 5;
  const probe = await page.evaluate(async payload => {
    const RTGraph = await import('/console/routing/graph.js');

    const result = RTGraph.parseImport(JSON.stringify(payload));
    return {
      ok: result.ok,
      error: result.error,
      codes: (result.issues || []).map(issue => issue.code),
      zh: (result.issues || []).map(issue => issue.zh).join(' ')
};

}, {
    kind: 'sr-routing-graph', version: 3, name: '非法 models',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root: malformed
});
  assert.equal(probe.ok, false, 'parseImport accepted a non-array models field');
  assert.ok(probe.codes.includes('graph_parse_error'), 'missing graph_parse_error issue');
  assert.match(probe.zh, /models/);

  await setImportPayload({
    kind: 'sr-routing-graph', version: 3, name: '非法 models',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root: malformed
}, 'malformed-models.json');
  await waitToastIncludes(await page.evaluate(async n => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (tf('cv.toastImportIssues', { n }));
}, 1));
  assert.deepEqual(await draftOf(), kept, 'a rejected malformed v3 import changed the draft');
  assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.every(issue => issue.level === 'hint'));
}), true,
    'the rejected file leaked hard issues into the current draft validation bar');
});

test('Task 14 · the only start node cannot be deleted', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await clearToasts();
  await clickNode('start');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.getSelection().kind === 'node' &&
    app.kit.getSelection().id === 'start');
});
  const before = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    nodes: app.graph.nodes.length,
    undo: store.canUndo
}));
});
  await page.keyboard.press('Delete');
  await waitToastIncludes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.deleteStartBlocked'));
}));
  assert.equal(await page.evaluate(() => document.getElementById('cvDeleteModal').hidden), true,
    'the delete dialog opened for the start node');
  assert.deepEqual(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    nodes: app.graph.nodes.length,
    hasStart: app.graph.nodes.some(node => node.id === 'start'),
    undo: store.canUndo
}));
}), { nodes: before.nodes, hasStart: true, undo: before.undo },
  'a start-node delete mutated the draft or pushed history');

  /* 检查器删除按钮走同一个 requestDelete 守卫。 */
  await clearToasts();
  await waitInspectorBranch('node', 'start');
  await page.locator('#cvInspectorBody [data-act="ins-delete-node"]').click();
  await waitToastIncludes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.deleteStartBlocked'));
}));
  assert.equal(await page.evaluate(() => document.getElementById('cvDeleteModal').hidden), true,
    'the inspector delete button opened the dialog for the start node');
});

test('Task 14 · an in-flight save is busy, blocks switching and discards a stale envelope', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 22, 12);
  let release = null;
  const gate = new Promise(resolve => { release = resolve; });
  const matcher = url => url.pathname === '/admin/trees/' + TREE_ID;
  await page.route(matcher, async route => { await gate; await route.continue(); });
  try {
    await page.locator('#cvSave').click();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saving === true);
}, null, { timeout: 5000 });
    const busy = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const { setLang, t, tf } = await import('/console/i18n.js');
    return (({
      saving: app.saving,
      disabled: document.getElementById('cvSave').disabled,
      ariaBusy: document.getElementById('cvSave').getAttribute('aria-busy'),
      label: document.querySelector('#cvSave span[data-i18n]').textContent,
      expectedLabel: t('cv.saving'),
      newDisabled: document.getElementById('cvNewTree').disabled,
      importDisabled: document.getElementById('cvImport').disabled
}));
});
    assert.equal(busy.saving, true);
    assert.equal(busy.disabled, true, 'the save button stayed enabled while saving');
    assert.equal(busy.ariaBusy, 'true', 'the save button did not expose aria-busy');
    assert.equal(busy.label, busy.expectedLabel, 'the save button did not show the busy label');
    assert.equal(busy.newDisabled, true, 'New stayed enabled while saving');
    assert.equal(busy.importDisabled, true, 'Import stayed enabled while saving');

    /* 保存回包落地前切换草稿必须被拒绝。 */
    const switched = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.selectTree('other-tree'));
});
    assert.equal(switched, false, 'selectTree allowed a switch while saving');
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.lastToast);
}),
      await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.toastSavingBlocked'));
}));

    /* 即使有代码绕过 selectTree 直接 loadTree，旧回包也不能写进新草稿。 */
    const raced = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

      const other = RTGraph.createMinimalTree('qwen3-8b');
      other.id = 'stale-race';
      other.name = 'raced tree';
      app.loadTree(other, true);
      return { id: app.treeId, name: app.tree.name };

});
    assert.deepEqual(raced, { id: 'stale-race', name: 'raced tree' });
    release();
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saving === false);
}, null, { timeout: 8000 });
    const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
      id: app.treeId,
      name: app.tree.name,
      revision: app.tree.tree_revision || '',
      savedId: app.saved && app.saved.id
}));
});
    assert.deepEqual(after, { id: 'stale-race', name: 'raced tree', revision: '', savedId: 'stale-race' },
      'a stale save envelope was applied to the freshly loaded draft');
} finally {
    release();
}
  await page.unroute(matcher);
  await ensureBaselineTree();
});

test('Task 8 · out-of-order tree switches keep the newest selection', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const baseline = await serverTree(TREE_ID);
  const treeFor = (id, name) => Object.assign({}, JSON.parse(JSON.stringify(baseline)), { id, name });
  const OLD_ID = 'qa-switch-old';
  const NEW_ID = 'qa-switch-new';
  const matcher = url => url.pathname === '/admin/trees/' + OLD_ID ||
    url.pathname === '/admin/trees/' + NEW_ID;
  const pending = [];
  await page.route(matcher, async route => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    pending.push({
      id: decodeURIComponent(route.request().url().split('/').pop()),
      route: route,
      done: false
    });
  });
  try {
    await page.evaluate(async ids => {
      const { app } = await import('/console/routing/app.js');
      /* 不 await：两次选择必须同时在途，才能制造逆序回包。 */
      window.__switchRace = [app.selectTree(ids[0]), app.selectTree(ids[1])];
    }, [OLD_ID, NEW_ID]);
    const deadline = Date.now() + 5000;
    while (pending.length < 2 && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    assert.deepEqual(pending.map(item => item.id), [OLD_ID, NEW_ID],
      'the two tree switches were not issued in order');

    /* 新选择先回包，旧选择后回包：旧回包必须被 loadSeq 丢弃。 */
    await pending[1].route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tree: treeFor(NEW_ID, 'newer selection') })
    });
    pending[1].done = true;
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.treeId === id;
    }, NEW_ID, { timeout: 5000 });
    await pending[0].route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tree: treeFor(OLD_ID, 'older selection') })
    });
    pending[0].done = true;

    const results = await page.evaluate(async () => {
      const values = await Promise.all(window.__switchRace);
      delete window.__switchRace;
      return values;
    });
    const after = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return { id: app.treeId, name: app.tree.name };
    });
    assert.deepEqual(results, [false, true],
      'a stale tree switch did not report itself as discarded');
    assert.deepEqual(after, { id: NEW_ID, name: 'newer selection' },
      'a slower older tree response overwrote the newer selection');
  } finally {
    for (const item of pending) {
      if (item.done) continue;
      item.done = true;
      try { await item.route.abort(); } catch (error) { /* already gone */ }
    }
    await page.unroute(matcher);
    await ensureBaselineTree();
    await openCanvas();
  }
});

test('Task 8 · a tree switch that loses to Load example is discarded', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const baseline = await serverTree(TREE_ID);
  const STALE_ID = 'qa-example-stale';
  const matcher = url => url.pathname === '/admin/trees/' + STALE_ID;
  let pending = null;
  await page.route(matcher, route => {
    if (route.request().method() !== 'GET') return route.continue();
    pending = route;
  });
  try {
    await page.evaluate(async id => {
      const { app } = await import('/console/routing/app.js');
      window.__exampleRace = app.selectTree(id);
    }, STALE_ID);
    const deadline = Date.now() + 5000;
    while (!pending && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(pending, 'the tree switch GET was not issued');

    await page.locator('#cvLoadExample').click();
    await waitConfirmDialog();
    await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
    await waitFor(async expected => {
      const { app } = await import('/console/routing/app.js');
      const node = app.graph && app.graph.nodes.find(item => item.id === 'start');
      return app.treeId === expected.id && app.kind() === 'clean' && !!node &&
        node.position.x === expected.start.x && node.position.y === expected.start.y;
    }, { id: TREE_ID, start: EXAMPLE_START }, { timeout: 8000 });

    /* 示例已落地，旧 switch 的回包现在才到：必须被 loadSeq 丢弃。 */
    const stale = Object.assign(JSON.parse(JSON.stringify(baseline)), {
      id: STALE_ID,
      name: 'stale example response'
    });
    await pending.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tree: stale })
    });
    pending = null;
    const results = await page.evaluate(async () => {
      const value = await window.__exampleRace;
      delete window.__exampleRace;
      return [value];
    });
    assert.deepEqual(results, [false],
      'a switch that lost to an example load did not report itself as discarded');
    const after = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      const node = app.graph.nodes.find(item => item.id === 'start');
      return {
        id: app.treeId,
        name: app.tree.name,
        kind: app.kind(),
        nodes: app.graph.nodes.length,
        edges: app.graph.edges.length,
        start: { x: node.position.x, y: node.position.y }
      };
    });
    assert.deepEqual(after, {
      id: TREE_ID,
      name: BASELINE.name,
      kind: 'clean',
      nodes: EXAMPLE_NODES,
      edges: EXAMPLE_EDGES,
      start: EXAMPLE_START
    }, 'a stale tree switch overwrote the example graph');
  } finally {
    if (pending) {
      try { await pending.abort(); } catch (error) { /* already gone */ }
    }
    await page.unroute(matcher);
    await ensureBaselineTree();
    await openCanvas();
  }
});

test('Task 8 · a tree switch that loses to Import is discarded', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const baseline = await serverTree(TREE_ID);
  const source = JSON.parse((await exportViaUi()).text);
  source.name = 'import beats stale switch';
  const STALE_ID = 'qa-import-stale';
  const matcher = url => url.pathname === '/admin/trees/' + STALE_ID;
  let pending = null;
  await page.route(matcher, route => {
    if (route.request().method() !== 'GET') return route.continue();
    pending = route;
  });
  try {
    await page.evaluate(async id => {
      const { app } = await import('/console/routing/app.js');
      window.__importRace = app.selectTree(id);
    }, STALE_ID);
    const deadline = Date.now() + 5000;
    while (!pending && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(pending, 'the tree switch GET was not issued');

    await setImportPayload(source, 'import-race.json');
    await waitFor(async name => {
      const { app } = await import('/console/routing/app.js');
      return app.tree.name === name;
    }, source.name, { timeout: 8000 });

    const stale = Object.assign(JSON.parse(JSON.stringify(baseline)), {
      id: STALE_ID,
      name: 'stale import response'
    });
    await pending.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tree: stale })
    });
    pending = null;
    const results = await page.evaluate(async () => {
      const value = await window.__importRace;
      delete window.__importRace;
      return [value];
    });
    assert.deepEqual(results, [false],
      'a switch that lost to an import did not report itself as discarded');
    const after = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        id: app.treeId,
        name: app.tree.name,
        nodes: app.graph.nodes.length,
        edges: app.graph.edges.length
      };
    });
    assert.deepEqual(after, {
      id: TREE_ID,
      name: source.name,
      nodes: EXAMPLE_NODES,
      edges: EXAMPLE_EDGES
    }, 'a stale tree switch overwrote the imported graph');
  } finally {
    if (pending) {
      try { await pending.abort(); } catch (error) { /* already gone */ }
    }
    await page.unroute(matcher);
    await ensureBaselineTree();
    await openCanvas();
  }
});

test('Task 8 · a stale tree switch failure stays silent', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const source = JSON.parse((await exportViaUi()).text);
  source.name = 'import wins over stale failure';
  const STALE_ID = 'qa-stale-fail';
  const matcher = url => url.pathname === '/admin/trees/' + STALE_ID;
  let pending = null;
  await page.route(matcher, route => {
    if (route.request().method() !== 'GET') return route.continue();
    pending = route;
  });
  const loadFailPrefix = await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
    return t('cv.toastLoadFail').split('{err}')[0];
  });
  expectHttpStatusError({ url: new RegExp('/admin/trees/' + STALE_ID + '$'), status: /500/ });
  try {
    await page.evaluate(async id => {
      const { app } = await import('/console/routing/app.js');
      window.__staleFail = app.selectTree(id);
    }, STALE_ID);
    const deadline = Date.now() + 5000;
    while (!pending && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(pending, 'the tree switch GET was not issued');

    await setImportPayload(source, 'import-stale-fail.json');
    await waitFor(async name => {
      const { app } = await import('/console/routing/app.js');
      return app.tree.name === name;
    }, source.name, { timeout: 8000 });

    await clearToasts();
    await pending.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'stale switch must stay silent' })
    });
    pending = null;
    const results = await page.evaluate(async () => {
      const value = await window.__staleFail;
      delete window.__staleFail;
      return [value];
    });
    assert.deepEqual(results, [false],
      'a stale tree switch failure did not report itself as discarded');
    assert.equal((await toastText()).includes(loadFailPrefix), false,
      'a stale tree switch failure still showed a load-error toast');
    assert.equal(await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.tree.name;
    }), source.name, 'a stale tree switch failure mutated the imported draft');
  } finally {
    if (pending) {
      try { await pending.abort(); } catch (error) { /* already gone */ }
    }
    await page.unroute(matcher);
    resetExpectedHttpStatusErrors();
    await ensureBaselineTree();
    await openCanvas();
  }
});

test('Task 8 · cancelling New keeps an in-flight tree switch', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 需要 dirty 草稿，New 才会弹丢弃确认框。 */
  await dragNodeBy('start', 24, 14);
  const baseline = await serverTree(TREE_ID);
  const RACE_ID = 'qa-new-cancel';
  const matcher = url => url.pathname === '/admin/trees/' + RACE_ID;
  let pending = null;
  await page.route(matcher, route => {
    if (route.request().method() !== 'GET') return route.continue();
    pending = route;
  });
  try {
    await page.evaluate(async id => {
      const { app } = await import('/console/routing/app.js');
      window.__newCancelRace = app.selectTree(id);
    }, RACE_ID);
    await waitConfirmDialog();
    await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
    const deadline = Date.now() + 5000;
    while (!pending && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(pending, 'the tree switch GET was not issued');

    /* 取消 New 不能动 loadSeq：在途 switch 仍是用户最后一次有效选择。 */
    await page.locator('#cvNewTree').click();
    const dialog = await waitConfirmDialog();
    assert.ok(dialog.length > 0, 'New did not ask before replacing the dirty draft');
    await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
    await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);

    const selection = Object.assign(JSON.parse(JSON.stringify(baseline)), {
      id: RACE_ID,
      name: 'cancel-safe selection'
    });
    await pending.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tree: selection })
    });
    pending = null;
    const results = await page.evaluate(async () => {
      const value = await window.__newCancelRace;
      delete window.__newCancelRace;
      return [value];
    });
    assert.deepEqual(results, [true],
      'cancelling New invalidated an in-flight tree switch');
    const after = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return { id: app.treeId, name: app.tree.name };
    });
    assert.deepEqual(after, { id: RACE_ID, name: 'cancel-safe selection' },
      'the in-flight switch did not win after New was cancelled');
  } finally {
    if (pending) {
      try { await pending.abort(); } catch (error) { /* already gone */ }
    }
    await page.unroute(matcher);
    await ensureBaselineTree();
    await openCanvas();
  }
});

test('Task 14 · New, Import and Load example stay inert until the canvas is booted', async () => {
  let release = () => {};
  const gate = new Promise(resolve => { release = resolve; });
  const hold = async route => {
    await gate;
    try { await route.continue(); } catch (error) { /* the test already unrouted it */ }
};
  await page.route('**/admin/trees', hold);
  try {
    await page.goto(routerBase + '/console/routing.html');
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app && app.kit && !app.booted);
},
      null, { timeout: 10000 });
    const notReady = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.toastNotReady'));
});
    for (const id of ['cvNewTree', 'cvImport', 'cvLoadExample']) {
      await clearToasts();
      await page.locator('#' + id).click();
      await waitToastIncludes(notReady);
      assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree);
}), null,
        `#${id} mutated the draft before boot`);
      assert.equal(await page.evaluate(() => document.getElementById('cvConfirmModal').hidden), true,
        `#${id} opened a confirm dialog before boot`);
}
    /* 早到的 change() 是安全的 false，不 clone(null)、不抛异常。 */
    const changeResult = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.change('behavior', () => { throw new Error('mutate must not run before boot'); }));
});
    assert.equal(changeResult, false, 'change() before boot did not return false');
} finally {
    release();
}
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.booted && app.tree);
}, null, { timeout: 15000 });
  await page.unroute('**/admin/trees');
});

test('7.4-20-responsive-keyboard-and-visual', async () => {
  await ensureBaselineTree();
  await page.setViewportSize({ width: 1440, height: 960 });
  await openCanvas();
  const metrics = () => page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight
}));
  let size = await metrics();
  assert.ok(size.scrollWidth <= size.clientWidth + 1,
    `1440 horizontal overflow: ${size.scrollWidth} > ${size.clientWidth}`);
  assert.ok(size.scrollHeight <= size.clientHeight + 1,
    `1440 vertical overflow: ${size.scrollHeight} > ${size.clientHeight}`);
  await screenshot('canvas-default-1440.png');

  /* fit 使用安全区：选中节点不能被节点库/检查器/缩放条/试算台/小地图盖住。 */
  await clickNode('deep-target');
  const fitInfo = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    app.kit.fit({ padding: 24 });
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const area = app.kit.safeArea(12);
    const rect = document.querySelector('[data-node-id="deep-target"]').getBoundingClientRect();
    const node = {
      left: rect.left - stage.left,
      top: rect.top - stage.top,
      right: rect.right - stage.left,
      bottom: rect.bottom - stage.top
};
    const overlays = ['cvPalette', 'cvInspector', 'cvZoom', 'cvTryBar', 'cvMinimap']
      .map(id => {
        const el = document.getElementById(id);
        if (!el || el.hidden) return null;
        const box = el.getBoundingClientRect();
        return {
          id, left: box.left, top: box.top,
          right: box.right, bottom: box.bottom
};
}).filter(Boolean);
    return { area, node, selection: app.kit.getSelection(), overlays, stage: { left: stage.left, top: stage.top } };

});
  assert.deepEqual(fitInfo.selection, { kind: 'node', id: 'deep-target' });
  assert.ok(fitInfo.node.left >= fitInfo.area.x - 1 &&
    fitInfo.node.right <= fitInfo.area.x + fitInfo.area.w + 1,
  'fit placed the selected node outside the safe area horizontally');
  assert.ok(fitInfo.node.top >= fitInfo.area.y - 1 &&
    fitInfo.node.bottom <= fitInfo.area.y + fitInfo.area.h + 1,
  'fit placed the selected node outside the safe area vertically');
  const covered = fitInfo.overlays.some(box => {
    const rect = {
      left: fitInfo.stage.left + fitInfo.node.left,
      top: fitInfo.stage.top + fitInfo.node.top,
      right: fitInfo.stage.left + fitInfo.node.right,
      bottom: fitInfo.stage.top + fitInfo.node.bottom
};
    return rect.left < box.right && rect.right > box.left &&
      rect.top < box.bottom && rect.bottom > box.top;
});
  assert.equal(covered, false, 'the selected node is covered by an overlay after fit');

  /* Tab 导航：每个落点都有可见 focus 态。 */
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
});
  const focusStates = [];
  for (let index = 0; index < 6; index += 1) {
    await page.keyboard.press('Tab');
    focusStates.push(await page.evaluate(() => {
      const el = document.activeElement;
      const style = getComputedStyle(el);
      return {
        tag: el.tagName,
        id: el.id,
        focusVisible: el.matches(':focus-visible'),
        outline: style.outlineStyle !== 'none' && style.outlineWidth !== '0px',
        shadow: style.boxShadow !== 'none'
};
}));
}
  focusStates.forEach((state, index) => {
    assert.equal(state.focusVisible, true, `Tab stop ${index} (${state.tag}#${state.id}) is not focus-visible`);
    assert.ok(state.outline || state.shadow,
      `Tab stop ${index} (${state.tag}#${state.id}) has no visible focus state`);
});

  /* 删除确认：焦点入场、Tab 圈定、Esc 关闭并把焦点还给触发按钮。 */
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const trigger = '#cvInspectorBody [data-act="ins-delete-node"]';
  await page.locator(trigger).click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden);
  const initial = await page.evaluate(() => ({
    inside: document.getElementById('cvDeleteBody').contains(document.activeElement),
    act: document.activeElement.dataset ? document.activeElement.dataset.act : ''
}));
  assert.equal(initial.inside, true, 'the delete dialog did not move focus inside');
  assert.equal(initial.act, 'delete-cancel');
  /* 真正的环绕：从 confirm 按 Tab 回到 cancel，从 cancel 按 Shift+Tab 回到
     confirm。只来回按 Tab/Shift+Tab 而不跨过首尾，证明不了焦点陷阱。 */
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-confirm',
    'Tab from the cancel button did not reach the confirm button');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-cancel',
    'Tab from the last control did not wrap forward to the first');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-confirm',
    'Shift+Tab from the first control did not wrap backward to the last');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-cancel');
  for (let index = 0; index < 5; index += 1) {
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvDeleteBody').contains(document.activeElement)), true,
    'Tab escaped the delete dialog');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvDeleteBody').contains(document.activeElement)), true,
    'Shift+Tab escaped the delete dialog');
}
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('cvDeleteModal').hidden);
  const restored = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    act: document.activeElement.dataset ? document.activeElement.dataset.act : '',
    insideInspector: document.getElementById('cvInspectorBody').contains(document.activeElement),
    selection: app.kit.getSelection()
}));
});
  assert.equal(restored.act, 'ins-delete-node', 'Esc did not return focus to the delete trigger');
  assert.equal(restored.insideInspector, true);
  assert.deepEqual(restored.selection, { kind: 'node', id: 'capability' });

  /* 1280×800：同一页无水平/垂直溢出，两个面板仍可见。 */
  await page.setViewportSize({ width: 1280, height: 800 });
  await openCanvas();
  size = await metrics();
  assert.ok(size.scrollWidth <= size.clientWidth + 1,
    `1280 horizontal overflow: ${size.scrollWidth} > ${size.clientWidth}`);
  assert.ok(size.scrollHeight <= size.clientHeight + 1,
    `1280 vertical overflow: ${size.scrollHeight} > ${size.clientHeight}`);
  const narrow = await page.evaluate(() => ({
    palette: document.getElementById('cvPalette').getBoundingClientRect().width > 0,
    inspector: document.getElementById('cvInspector').getBoundingClientRect().width > 0,
    zoom: /^\d+%$/.test(document.getElementById('cvZoomLabel').textContent)
}));
  assert.deepEqual(narrow, { palette: true, inspector: true, zoom: true });
  await screenshot('canvas-1280.png');
  await page.setViewportSize({ width: 1440, height: 960 });
});

test('7.4-21-chinese-product-and-actionable-errors', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const language = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    save: document.getElementById('cvSave').textContent.trim(),
    palette: document.querySelector('#cvPalette .rtc-pal-t span').textContent.trim(),
    tryRun: document.getElementById('cvTryRun').textContent.trim(),
    rawKey: /cv\.[a-zA-Z]/.test(document.body.textContent)
}));
  assert.equal(language.lang, 'zh-CN');
  assert.equal(language.rawKey, false, 'the Chinese page leaked raw i18n keys');
  assert.match(language.save, /保存/);
  assert.match(language.palette, /节点/);
  assert.match(language.tryRun, /试算|运行/);

  /* 具体可操作：被占用槽位的拒绝点名「空槽位 / 下一个 in_N」，且不落边。 */
  await clearToasts();
  const before = (await appSnapshot()).edges.length;
  await dragPort('capability', 'prob.easy', { node: 'and-power', port: 'in_1' });
  await waitToastIncludes('空槽位');
  const occupied = await toastText();
  assert.match(occupied, /占用|已连接/);
  assert.match(occupied, /in_N/);
  assert.equal((await appSnapshot()).edges.length, before);

  /* 槽位条件的错误同样具体：文案来自词典、指出阈值非法，且不写回。 */
  const edgeId = 'e-cap-strong-and-power';
  const original = (await edgeOf(edgeId)).condition;
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="value"]').fill('NaN');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  assert.equal((await page.locator('#cvCondPop [data-cond-error]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.badValue'));
}));
  assert.deepEqual((await edgeOf(edgeId)).condition, original);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);

  /* 校验错误带节点/边/槽位定位字段，可直接跳到出问题的位置。 */
  await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(edge => edge.id === 'e-clarify-deep').condition = { op: 'bogus', value: 1 };
});

});
  const issue = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const found = app.issues.find(item => item.code === 'numeric_edge_condition_invalid');
    return found ? {
      zh: found.zh, edge: found.edge_id, node: found.node_id, port: found.port
} : null;

});
  assert.ok(issue, 'invalid slot condition produced no located issue');
  assert.match(issue.zh, /算子|阈值|条件/);
  assert.equal(issue.edge, 'e-clarify-deep');
  assert.equal(issue.node, 'deep-target');
  assert.equal(issue.port, 'in_1');
});

test('review-long-instruction-is-clamped-without-overlap', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const long = '你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明选择最合适的模型。'.repeat(4);
  await page.evaluate(async text => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    app.change('behavior', tree => {
      RTGraph.findNode(tree.root, 'capability').instructions = text;
});

}, long);
  const metrics = await page.evaluate(() => {
    const card = document.querySelector('[data-node-id="capability"]');
    const instr = card.querySelector('.rtc-instr');
    const inner = instr.querySelector('.rtc-instr-t');
    const slot = card.querySelector('.rtc-slot');
    const cs = getComputedStyle(inner);
    const instrBox = instr.getBoundingClientRect();
    const innerBox = inner.getBoundingClientRect();
    const instrStyle = getComputedStyle(instr);
    return {
      clamp: String(cs.webkitLineClamp || cs.lineClamp || ''),
      overflow: cs.overflow,
      title: instr.getAttribute('title') || '',
      innerH: innerBox.height,
      innerScrollH: inner.scrollHeight,
      lineH: parseFloat(instrStyle.lineHeight),
      instrBottom: instrBox.bottom,
      slotTop: slot ? slot.getBoundingClientRect().top : null
};
});
  assert.equal(metrics.clamp, '2', 'instruction clamp is not two lines');
  assert.equal(metrics.overflow, 'hidden');
  assert.ok(metrics.innerH <= metrics.lineH * 2 + 1,
    `instruction exceeds two lines (${metrics.innerH}px > ${metrics.lineH * 2}px)`);
  assert.ok(metrics.innerScrollH > metrics.innerH,
    'instruction was not actually truncated (no clipped content)');
  assert.ok(metrics.title.length > 66, 'full instruction is not reachable via the title tooltip');
  if (metrics.slotTop != null) {
    assert.ok(metrics.instrBottom <= metrics.slotTop + 0.5,
      'instruction text overlaps the slot row');
}
});

test('review-empty-slots-hidden-until-drag', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const hidden = await page.evaluate(() => {
    const ghosts = Array.from(document.querySelectorAll('#cvNodes .rtc-slot--ghost'));
    return {
      count: ghosts.length,
      visible: ghosts.filter(row => getComputedStyle(row).display !== 'none').length
};
});
  assert.ok(hidden.count > 0, 'test precondition: expected at least one empty slot in the graph');
  assert.equal(hidden.visible, 0, 'empty slots must be hidden at rest');
  const from = await portPoint('capability', 'prob.easy', 'out');
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + 24, from.y + 12, { steps: 4 });
  const during = await page.evaluate(() => {
    const ghosts = Array.from(document.querySelectorAll('#cvNodes .rtc-slot--ghost'));
    return {
      visible: ghosts.filter(row => getComputedStyle(row).display !== 'none').length,
      stage: document.getElementById('cvStage').classList.contains('is-edge-drag')
};
});
  assert.ok(during.stage, 'edge drag state was not set');
  assert.ok(during.visible > 0, 'empty slots were not revealed while dragging an edge');
  await page.mouse.up();
  const after = await page.evaluate(() => ({
    visible: Array.from(document.querySelectorAll('#cvNodes .rtc-slot--ghost'))
      .filter(row => getComputedStyle(row).display !== 'none').length,
    stage: document.getElementById('cvStage').classList.contains('is-edge-drag')
}));
  assert.equal(after.visible, 0, 'empty slots stayed visible after the drag ended');
  assert.equal(after.stage, false, 'edge drag state was not cleared');
});

test('review-slot-limit-import-preserves-draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const before = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    nodes: app.graph.nodes.map(node => node.id),
    edges: app.graph.edges.map(edge => [edge.id, edge.target_port]),
    slots: document.querySelectorAll('#cvNodes .rtc-slot').length,
}));
});
  const root = JSON.parse(JSON.stringify(EXAMPLE));
  root.edges[0].target_port = 'in_9999';
  await clearToasts();
  await setImportPayload({
    kind: 'sr-routing-graph', version: 3, name: '非法槽位',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root,
}, 'invalid-slot-in-9999.json');
  await waitToastIncludes(await page.evaluate(async n => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (tf('cv.toastImportIssues', { n }));
}, 1));
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    nodes: app.graph.nodes.map(node => node.id),
    edges: app.graph.edges.map(edge => [edge.id, edge.target_port]),
    slots: document.querySelectorAll('#cvNodes .rtc-slot').length,
}));
});
  assert.deepEqual(after, before, 'invalid slot import replaced the draft or allocated new slot rows');
});

test('review-import-contract-rejects-before-draft-replacement', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const base = await page.evaluate(async () => {
    const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.createMinimalTree('qwen3.8-flash-sft'));
});
  const kept = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (JSON.stringify(app.tree));
});
  const invalid = [];
  const badType = JSON.parse(JSON.stringify(base));
  badType.root.nodes.push({ id: 'q-review', type: 'question', question_type: 'unsupported', instructions: '非法类型' });
  badType.root.edges.push({
    id: 'e-q-review', source: 'start', source_port: 'out',
    target: 'q-review', target_port: 'in_1', condition: null,
});
  invalid.push(badType);
  const unknown = JSON.parse(JSON.stringify(base));
  unknown.root.legacy = true;
  invalid.push(unknown);
  const badZone = JSON.parse(JSON.stringify(base));
  badZone.timezone = 'Mars/Olympus';
  invalid.push(badZone);

  for (const tree of invalid) {
    await clearToasts();
    await setImportPayload({
      kind: 'sr-routing-graph', version: 3, name: tree.name,
      timezone: tree.timezone, sticky: tree.sticky, root: tree.root,
}, 'invalid-contract.json');
    await waitToastIncludes(await page.evaluate(async n => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (tf('cv.toastImportIssues', { n }));
}, 1));
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (JSON.stringify(app.tree));
}), kept);
}
});

test('review-import-envelope-types-preserve-draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const base = await page.evaluate(async () => {
    const RTGraph = await import('/console/routing/graph.js');
    return (RTGraph.createMinimalTree('qwen3.8-flash-sft'));
});
  const kept = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (JSON.stringify(app.tree));
});
  const invalid = [];
  const badSticky = JSON.parse(JSON.stringify(base));
  badSticky.sticky.enabled = 'yes';
  invalid.push(badSticky);
  const badTimezone = JSON.parse(JSON.stringify(base));
  badTimezone.timezone = 123;
  invalid.push(badTimezone);

  for (const tree of invalid) {
    await clearToasts();
    await setImportPayload({
      kind: 'sr-routing-graph', version: 3, name: tree.name,
      timezone: tree.timezone, sticky: tree.sticky, root: tree.root,
}, 'invalid-envelope-value.json');
    await waitToastIncludes(await page.evaluate(async n => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (tf('cv.toastImportIssues', { n }));
}, 1));
    assert.equal(await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (JSON.stringify(app.tree));
}), kept);
}
});

test('review-pointer-cancel-restores-node-without-history', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const original = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y, kind: app.kind() };

});
  const box = await page.locator('[data-node-id="start"]').boundingBox();
  const point = { x: box.x + box.width / 2, y: box.y + 11 };
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 70, point.y + 50, { steps: 6 });
  await page.locator('#cvStage').dispatchEvent('pointercancel', {
    pointerId: 1, pointerType: 'mouse', clientX: point.x + 70, clientY: point.y + 50,
});
  await page.mouse.up();
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === 'start');
    return node.position.x === expected.x && node.position.y === expected.y;

}, original);
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(), undo: store.canUndo, redo: store.canRedo,
}));
});
  assert.equal(original.kind, 'clean');
  assert.deepEqual(after, { kind: 'clean', undo: false, redo: false });
});

test('boolean condition guidance is bilingual and has no expected-value syntax', async () => {
  await openCanvas();
  await openIssuesPanel();

  async function guidance() {
    return page.evaluate(() => ({
      legend: document.querySelector('[data-i18n="cv.legendBool"]').textContent.trim(),
      issues: document.getElementById('cvIssues').textContent.replace(/\s+/g, ' ').trim(),
      slotTitle: document.querySelector(
        '#cvNodes [data-node-id="capability"] [data-port][data-port-dir="in"][data-port-id="in_1"]'
      ).getAttribute('title')
}));
}

  const zh = await guidance();
  assert.equal(zh.legend, '布尔线 · 默认直接通过');
  assert.match(zh.issues, /直接通过/);
  assert.match(zh.issues, /取反/);
  assert.doesNotMatch([zh.legend, zh.issues, zh.slotTitle].join('\n'), /= (?:true|false)/);

  await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('en'));
});
  try {
    const en = await guidance();
    assert.equal(en.legend, 'Boolean edge · passes through by default');
    assert.match(en.issues, /pass(?:es)? through/i);
    assert.match(en.issues, /negat/i);
    assert.doesNotMatch([en.legend, en.issues, en.slotTitle].join('\n'), /= (?:true|false)/);
    await screenshot('boolean-condition-guidance-en-1440.png');
} finally {
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('zh'));
});
}
});

test('boolean start is read-only and legacy negation needs an explicit clear', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  let slot = page.locator(`#cvNodes [data-edge="${startEdge.id}"]`);
  assert.equal(await slot.locator('[data-cond-edge]').count(), 0);
  assert.equal(await slot.locator('[data-start-condition]').count(), 1);
  await screenshot('boolean-start-readonly-1440.png');

  await clickEdge(startEdge.id);
  await waitInspectorBranch('edge', startEdge.id);
  assert.equal(await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').count(), 0);
  assert.equal(await page.locator('#cvInspectorBody [data-cond-field="negate"]').count(), 0);

  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      const edge = tree.root.edges.find(item => item.id === id);
      edge.condition = { op: 'is', value: false };
});

}, startEdge.id);
  slot = page.locator(`#cvNodes [data-edge="${startEdge.id}"]`);
  assert.equal((await slot.locator('[data-cond-edge]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.negated'));
}));
  await slot.locator('[data-cond-edge]').click();
  await waitCondPop(startEdge.id);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="negate"]').isChecked(), true);
  await page.locator('#cvCondPop [data-cond-field="negate"]').uncheck();
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(startEdge.id, null);
  assert.equal(await slot.locator('[data-cond-edge]').count(), 0);
  assert.equal((await slot.locator('[data-start-condition]').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.startAlways'));
}));
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(startEdge.id, { op: 'is', value: false });
});

test('static start condition has no editable hover feedback', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  const condition = page.locator(`#cvNodes [data-edge="${startEdge.id}"] [data-start-condition]`);
  const style = () => condition.evaluate(element => {
    const value = getComputedStyle(element);
    return {
      color: value.color,
      backgroundColor: value.backgroundColor,
      borderColor: value.borderColor,
      cursor: value.cursor
};
});

  await page.mouse.move(0, 0);
  const resting = await style();
  assert.equal(resting.cursor, 'default');
  await condition.hover();
  assert.deepEqual(await style(), resting);
});

test('clearing legacy start negation restores keyboard focus', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = { op: 'is', value: false };
});

}, startEdge.id);

  const chip = page.locator(`#cvNodes [data-cond-edge="${startEdge.id}"]`);
  await chip.focus();
  await page.keyboard.press('Enter');
  await waitCondPop(startEdge.id);
  const negate = page.locator('#cvCondPop [data-cond-field="negate"]');
  assert.equal(await negate.isChecked(), true);
  await negate.focus();
  await page.keyboard.press('Space');
  assert.equal(await negate.isChecked(), false);
  await page.locator('#cvCondPop [data-act="cond-confirm"]').focus();
  await page.keyboard.press('Enter');
  await waitEdgeCondition(startEdge.id, null);

  const focused = await page.evaluate(() => ({
    tag: document.activeElement && document.activeElement.tagName,
    startCondition: document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.startCondition || '' : '',
    tabIndex: document.activeElement ? document.activeElement.tabIndex : null
}));
  assert.deepEqual(focused, { tag: 'SPAN', startCondition: startEdge.id, tabIndex: -1 });
  assert.equal(await page.locator(`#cvNodes [data-cond-edge="${startEdge.id}"]`).count(), 0);

  await page.locator('#cvUndo').click();
  await waitEdgeCondition(startEdge.id, { op: 'is', value: false });
});

test('replay condition labels use direct, negated and start-only text', async () => {
  await prepareTryCanvas();
  const edges = (await appSnapshot()).edges;
  const startEdge = edges.find(edge => edge.source === 'start');
  const conditionEdge = edges.find(edge => edge.source === 'cond-text');
  await runTrySample(0);
  assert.match(await page.locator(`#cvNodes [data-edge="${startEdge.id}"]`).getAttribute('title'),
    /总是通过/);
  assert.doesNotMatch(await page.locator(`#cvNodes [data-edge="${startEdge.id}"]`).getAttribute('title'),
    /= true/);

  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = { op: 'is', value: false };
});

}, conditionEdge.id);
  await runTrySample(0);
  const negatedTitle = await page.locator(`#cvNodes [data-edge="${conditionEdge.id}"]`).getAttribute('title');
  assert.match(negatedTitle, /取反/);
  assert.doesNotMatch(negatedTitle, /= false/);

  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = null;
});

}, conditionEdge.id);
  await runTrySample(0);
  const directTitle = await page.locator(`#cvNodes [data-edge="${conditionEdge.id}"]`).getAttribute('title');
  assert.match(directTitle, /直接通过/);
  assert.doesNotMatch(directTitle, /= true/);
});

test('boolean deletion and connection feedback use the new labels', async () => {
  await openCanvas();
  const edgeId = 'e-selected-easy-light';
  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = { op: 'is', value: false };
});

}, edgeId);
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  await page.locator('#cvInspectorBody [data-act="ins-delete-edge"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden);
  const deletion = await page.locator('#cvDeleteBody').innerText();
  assert.match(deletion, /取反/);
  assert.doesNotMatch(deletion, /= (?:true|false)/);
  await page.locator('#cvDeleteBody [data-act="delete-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvDeleteModal').hidden);

  await openCanvas();
  await clearToasts();
  await dragPort('cond-text', 'out', { node: 'capability', port: 'in_2' });
  const expected = await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.toastBoolDefault'));
});
  await waitToastIncludes(expected);
  assert.doesNotMatch(await toastText(), /= (?:true|false)/);
});

test('numeric unset and English boolean condition labels stay explicit', async () => {
  await openCanvas();
  const numericEdge = 'e-clarify-deep';
  await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    app.change('behavior', tree => {
      tree.root.edges.find(item => item.id === id).condition = null;
});

}, numericEdge);
  assert.equal(
    (await page.locator(`#cvNodes [data-cond-edge="${numericEdge}"]`).textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.cond.unset'));
})
  );

  await openCanvas();
  await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('en'));
});
  try {
    const edges = (await appSnapshot()).edges;
    const startEdge = edges.find(edge => edge.source === 'start');
    const directEdge = edges.find(edge => edge.id === 'e-cond-text-or');
    assert.equal(
      (await page.locator(`#cvNodes [data-edge="${startEdge.id}"] [data-start-condition]`).textContent()).trim(),
      'Always passes'
    );
    const chip = page.locator(`#cvNodes [data-cond-edge="${directEdge.id}"]`);
    assert.equal((await chip.textContent()).trim(), 'Pass through');
    await chip.click();
    await waitCondPop(directEdge.id);
    assert.equal(
      (await page.locator('#cvCondPop .rtc-cond-negate span').textContent()).trim(),
      'Negate'
    );
    assert.equal(await page.locator('#cvCondPop [data-cond-field="bool"]').count(), 0);
    await screenshot('en-boolean-condition-1440.png');
} finally {
    await page.keyboard.press('Escape');
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (setLang('zh'));
});
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
}
});

await runIfMain(import.meta.url);
