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
  BASELINE_RUNTIME,
  EXAMPLE,
  EXAMPLE_ROOT,
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
  clickCanvasAction,
  clickEdge,
  clickNode,
  danglingEdgeIds,
  draftStatus,
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
  mockBase,
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
  treeRuntimeFields,
  trySnapshot,
  waitCondPop,
  waitConfirmDialog,
  waitEdgeCondition,
  waitFor,
  waitInspectorBranch,
  waitToastIncludes
} from '../console_qa.mjs';

/* 严格自包含 v4 文档：导入/保存只接受带完整四字段运行快照的文档。
   以仓库示例文档（EXAMPLE）为基线构造，可按用例覆盖 name/sticky/root 等字段。 */
/* 内部运行快照 → 导入文档的扁平四字段（Node 侧 helper）。 */
function runtimeDocumentFields(runtime) {
  return {
    backends: JSON.parse(JSON.stringify(runtime.backends)),
    jev: JSON.parse(JSON.stringify(runtime.jev)),
    jev_window: JSON.parse(JSON.stringify(runtime.jev_window)),
    time_periods: JSON.parse(JSON.stringify(runtime.time_periods))
  };
}

function strictDocument(overrides) {
  const document = {
    kind: 'sr-routing-graph',
    version: 4,
    name: EXAMPLE.name,
    departments: JSON.parse(JSON.stringify(EXAMPLE.departments || [])),
    enabled: EXAMPLE.enabled === true,
    default: EXAMPLE.default === true,
    timezone: EXAMPLE.timezone,
    sticky: EXAMPLE.sticky,
    root: JSON.parse(JSON.stringify(EXAMPLE_ROOT)),
    backends: JSON.parse(JSON.stringify(EXAMPLE.backends)),
    jev: JSON.parse(JSON.stringify(EXAMPLE.jev)),
    jev_window: JSON.parse(JSON.stringify(EXAMPLE.jev_window)),
    time_periods: JSON.parse(JSON.stringify(EXAMPLE.time_periods))
  };
  if (EXAMPLE.canvas) document.canvas = JSON.parse(JSON.stringify(EXAMPLE.canvas));
  return Object.assign(document, overrides || {});
}

/* 导入被拒的 toast 必须带具体原因：解析/运行配置契约错误没有节点位置，只报
   「导入失败：」前缀或「N 个校验问题」计数会把真正的原因藏起来。这里断言完整
   的本地化拒绝文案；若实现退回泛化文案，waitToastIncludes 会超时而失败。 */
async function expectImportReason(reason) {
  assert.ok(reason && reason.trim(), 'expected import reason is empty');
  const generic = await page.evaluate(async () => {
    const { tf } = await import('/console/i18n.js');
    return {
      prefix: tf('cv.toastImportRejected', { err: '__REASON__' }).split('__REASON__')[0].trim(),
      count: tf('cv.toastImportIssues', { n: 1 }).trim()
    };
  });
  assert.notEqual(reason.trim(), generic.prefix, 'the asserted reason is only the generic prefix');
  const expected = await page.evaluate(async text => {
    const { tf } = await import('/console/i18n.js');
    return tf('cv.toastImportRejected', { err: text });
  }, reason);
  await waitToastIncludes(expected);
  const actual = (await toastText()).trim();
  assert.ok(actual.includes(reason), 'import rejection dropped the specific reason: ' + actual);
  assert.notEqual(actual, generic.count,
    'import rejection still showed only the generic issue count: ' + actual);
  assert.notEqual(actual, generic.prefix,
    'import rejection still showed only the generic prefix: ' + actual);
}

/* 与页面同一份 graph.js 计算「导入会给出的具体原因」：UI 必须把它显示出来，
   而不是丢掉原因只报问题数量（断言的判别点在 UI 装配，不在文案字面）。 */
async function parseImportReason(document) {
  const reason = await page.evaluate(async payload => {
    const RTGraph = await import('/console/routing/graph.js');
    const result = RTGraph.parseImport(JSON.stringify(payload));
    const issues = Array.isArray(result.issues) ? result.issues : [];
    const issue = issues.find(item => item.code === 'graph_parse_error') || issues[0] || null;
    return (issue && issue.zh) || result.error || '';
  }, document);
  assert.ok(reason, 'parseImport produced no reason for the rejected document');
  return reason;
}

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
  assert.equal(metrics.nodes, EXAMPLE_NODES);
  assert.equal(metrics.edges, EXAMPLE_EDGES);
  assert.equal(metrics.groups, 4);
  assert.ok(metrics.inspector && metrics.minimap && metrics.trybar);
  assert.match(metrics.zoom, /^\d+%$/);
  assert.equal(metrics.miniNodes, EXAMPLE_NODES);
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

test('P0 toolbar exposes every editing action inline, with no More menu', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 用户明确要求编辑动作全部平铺在顶栏（2026-11-23）：撤销 / 重做 / 自动整理 /
     保存草稿 / 新建 / 导入 / 导出都在首屏一键可达，不能再有「更多」折叠入口。 */
  assert.equal(await page.locator('#cvMore, #cvMoreBtn').count(), 0,
    'the overflow menu must be gone');
  const tools = await page.evaluate(() =>
    Array.from(document.querySelectorAll('.rtc-tools > button')).map(element => element.id));
  assert.deepEqual(tools, ['cvUndo', 'cvRedo', 'cvAutoLayout', 'cvSave', 'cvNewTree',
                           'cvImport', 'cvExport', 'cvDefault', 'cvDisable'],
    'the editing actions changed or lost their inline order');
  for (const id of ['cvUndo', 'cvRedo', 'cvAutoLayout', 'cvSave', 'cvNewTree',
                    'cvImport', 'cvExport', 'cvTryBtn', 'cvEnable', 'cvIssuesBtn']) {
    assert.equal(await page.locator('#' + id).isVisible(), true, id + ' must be visible');
  }
  /* 停用只在已启用的决策组上出现（基线组是已启用状态）。 */
  const lifecycle = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return {
      enabled: !!(app.tree && app.tree.enabled),
      isDefault: !!(app.tree && app.tree.default)
    };
  });
  assert.equal(await page.locator('#cvDisable').count(), 1, 'disable must stay in the toolbar');
  assert.equal(await page.locator('#cvDisable').isVisible(), lifecycle.enabled,
    'the disable action must follow the lifecycle state');
  /* 多图生命周期：默认图隐藏「设为默认」，非默认的启用图才显示；默认 pill 相反。 */
  assert.equal(lifecycle.isDefault, true, 'the baseline graph must be the default graph');
  assert.equal(await page.locator('#cvDefault').count(), 1, 'set-default must stay in the toolbar');
  assert.equal(await page.locator('#cvDefault').isVisible(), lifecycle.enabled && !lifecycle.isDefault,
    'the set-default action must follow the multi-graph lifecycle');
  assert.equal(await page.locator('#cvDefaultPill').count(), 1, 'the default pill is missing');
  assert.equal(await page.locator('#cvDefaultPill').isVisible(), lifecycle.isDefault,
    'the default pill must follow the multi-graph lifecycle');
  /* 决策组下拉框标出默认图（默认标记来自 enabled/default，不由唯一启用图推导）。 */
  const defaultMark = await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
    const option = Array.from(document.querySelectorAll('#cvTreeSelect option'))
      .find(item => item.value === document.querySelector('#cvTreeSelect').value);
    return { expect: t('cv.treeDefault'), label: option ? option.textContent : '' };
  });
  assert.ok(defaultMark.label.includes(defaultMark.expect),
    'the tree selector does not mark the default graph: ' + defaultMark.label);
  assert.equal(await page.locator('#cvLifecycle').count(), 1, 'the lifecycle badge is missing');
  assert.equal(await page.locator('#cvLifecycle').isVisible(), true, 'the lifecycle badge must be visible');
  assert.equal(await page.locator('#cvRevision, #cvEnabled, #cvClean, #cvDirty').count(), 0,
    'legacy status badges must be gone');
  /* 平铺后动作区不能溢出：1280×800 是部署文档承诺的最小基线。 */
  const overflow = await page.evaluate(() => {
    const actions = document.querySelector('.rtc-actions');
    return { actions: actions.scrollWidth - actions.clientWidth,
             page: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  });
  assert.ok(overflow.actions <= 1, 'the action row overflows horizontally: ' + overflow.actions);
  assert.ok(overflow.page <= 1, 'the page overflows horizontally: ' + overflow.page);

  await clickCanvasAction('cvAutoLayout');
  assert.ok(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return !!(app && app.graph);
  }), 'an inline action must work without opening a menu');
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
  /* 输入坐标取整：浏览器输入管线会量化小数坐标，锚点断言（1e-6）会因此假红。 */
  const point = { x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2) };
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
  const dirty = await draftStatus();
  assert.equal(dirty.visible, true);
  assert.equal(dirty.kind, 'layout');
  assert.equal(dirty.state, 'unpublished');
  let snapshot = await appSnapshot();
  assert.deepEqual(snapshot.history, { undo: true, redo: false });

  const expectPosition = async expected => {
    await waitFor(async value => {
    const { app, store } = await import('/console/routing/app.js');

      const node = app.graph.nodes.find(item => item.id === 'start');
      return node && node.position.x === value.x && node.position.y === value.y;

}, expected);
};

  await clickCanvasAction('cvUndo');
  await expectPosition(originalPosition);
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'clean');
  assert.deepEqual(snapshot.history, { undo: false, redo: true });

  await clickCanvasAction('cvRedo');
  await expectPosition(movedPosition);

  await page.keyboard.press(`${MOD}+z`);
  await expectPosition(originalPosition);
  await page.keyboard.press(`${MOD}+Shift+z`);
  await expectPosition(movedPosition);
});

test('7.4-03-node-library-four-groups', async () => {
  await openCanvas();
  assert.equal(await page.locator('[data-lib-kind="logic-not"]').count(), 0,
    'the node library still exposes a NOT entry');
  const actions = [
    { kind: 'question-choice', how: 'click', match: node => node.type === 'question' && node.question_type === 'choice' },
    { kind: 'question-noul', how: 'drag', match: node => node.type === 'question' && node.question_type === 'noul' },
    { kind: 'logic-and', how: 'click', match: node => node.type === 'logic' && node.operator === 'and' },
    { kind: 'logic-or', how: 'drag', match: node => node.type === 'logic' && node.operator === 'or' },
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

  await clickCanvasAction('cvUndo');
  let undone = await appSnapshot();
  assert.equal(undone.edges.length, before.edges.length, 'one undo did not remove the numeric edge');
  await clickCanvasAction('cvRedo');
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
  assert.equal(await startSlot.locator('[data-start-condition]').count(), 0,
    'the start slot still renders a condition chip');
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
  assert.equal(await startSlot.locator('[data-start-condition]').count(), 0,
    'the start slot still renders a condition chip');
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

  await clickCanvasAction('cvAutoLayout');
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
    lifecycle: {
      state: document.getElementById('cvLifecycle').dataset.state,
      visible: !!(document.getElementById('cvLifecycle').offsetWidth ||
        document.getElementById('cvLifecycle').offsetHeight ||
        document.getElementById('cvLifecycle').getClientRects().length)
    },
    history: { undo: store.canUndo, redo: store.canRedo }
}));
});
  const moved = Object.keys(laidOut.positions)
    .filter(id => laidOut.positions[id].x !== before[id].x || laidOut.positions[id].y !== before[id].y);
  assert.equal(moved.length, Object.keys(before).length, 'auto layout did not move every node');
  assert.equal(laidOut.kind, 'layout', 'auto layout must not dirty behavior');
  assert.deepEqual(laidOut.lifecycle, { state: 'unpublished', visible: true });
  assert.deepEqual(laidOut.history, { undo: true, redo: false });
  laidOut.edges.forEach(edge => {
    assert.ok(laidOut.positions[edge.target].x > laidOut.positions[edge.source].x,
      `${edge.source} -> ${edge.target} is not laid out left-to-right`);
});

  /* 第二次自动整理是 no-op：不得再入栈。若入了空历史，下面一次 undo 会退到同样的布局。 */
  await clickCanvasAction('cvAutoLayout');
  await clickCanvasAction('cvUndo');
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
    state: document.getElementById('cvLifecycle').dataset.state,
    history: { undo: store.canUndo, redo: store.canRedo }
}));
});
  assert.equal(undone.kind, 'clean', 'undo after a no-op layout did not restore the saved positions');
  assert.equal(undone.state, 'running');
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

  await clickCanvasAction('cvUndo');
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
  await clickCanvasAction('cvUndo');
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
  await clickCanvasAction('cvUndo');
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
  await clickCanvasAction('cvUndo');
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
  assert.equal(startView.label, '', 'the start edge must not describe a condition');
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
  await clickCanvasAction('cvUndo');
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

  await clickCanvasAction('cvUndo');
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

test('Task 12 · moving a node saves layout-only: revision unchanged, coordinates persist, lifecycle unpublished', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 66, 38);
  const badge = await draftStatus();
  assert.equal(badge.visible, true);
  assert.equal(badge.kind, 'layout');
  assert.equal(badge.state, 'unpublished', 'a layout edit is still an unpublished change');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.status.unpublished'));
}));

  await saveNow();
  const saved = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    revision: app.tree.tree_revision,
    savedRevision: app.saved.tree_revision,
    state: document.getElementById('cvLifecycle').dataset.state
}));
});
  assert.equal(saved.kind, 'clean');
  assert.equal(saved.revision, baseline.tree_revision, 'layout-only save changed the local revision');
  assert.equal(saved.savedRevision, baseline.tree_revision);
  assert.equal(saved.state, 'running');

  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'a position-only save changed the server tree_revision');
  /* 画布坐标在文档顶层 canvas（节点对象不再带 position）。 */
  const start = server.canvas.start;
  assert.ok(start, 'the saved document is missing canvas.start');
  assert.ok(Math.abs(start.x - moved.x) < 0.01, 'saved X coordinate differs from the draft');
  assert.ok(Math.abs(start.y - moved.y) < 0.01, 'saved Y coordinate differs from the draft');
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
  const beforeSave = await draftStatus();
  assert.equal(beforeSave.visible, true);
  assert.equal(beforeSave.state, 'unpublished');
  assert.equal(beforeSave.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.status.unpublished'));
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
    state: document.getElementById('cvLifecycle').dataset.state
}));
});
  assert.equal(after.kind, 'clean');
  assert.equal(after.canUndo, true, 'save cleared the undo history');
  assert.equal(after.canRedo, false);
  assert.equal(after.state, 'running');
  assert.equal(after.revision, after.savedRevision);
  assert.equal((await serverTree(TREE_ID)).name, '保存后仍可撤销');

  await clickCanvasAction('cvUndo');
  await waitFor(async expected => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.name === expected);
}, BASELINE.name);
  const undone = await draftStatus();
  assert.equal(undone.kind, 'display', 'undoing a save did not mark the draft dirty again');
  assert.equal(undone.state, 'unpublished', 'undoing a save did not repaint the lifecycle badge');
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
    await clickCanvasAction('cvSave');
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
  const switchedDirty = await draftStatus();
  assert.equal(switchedDirty.kind, 'layout');
  assert.equal(switchedDirty.state, 'unpublished');

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
  await clickCanvasAction('cvNewTree');
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

  const primaryUi = () => page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
    const badge = document.getElementById('cvLifecycle');
    return {
      label: document.getElementById('cvEnableLabel').textContent.trim(),
      publish: t('cv.publish'),
      published: t('cv.published'),
      enable: t('cv.enable'),
      state: badge && badge.dataset.state,
      disabled: document.getElementById('cvEnable').disabled,
      disableHidden: document.getElementById('cvDisable').hidden
    };
});
  let ui = await primaryUi();
  assert.equal(ui.label, ui.publish, 'a new draft must offer to publish');
  assert.equal(ui.state, 'draft', 'an unsaved draft must be projected as draft');
  assert.equal(ui.disableHidden, true, 'a disabled draft must not offer disable');

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

  ui = await primaryUi();
  assert.equal(ui.label, ui.enable, 'a saved clean disabled draft must offer enable');
  assert.equal(ui.state, 'disabled', 'a saved clean disabled draft must be projected as disabled');
  assert.equal(ui.disableHidden, true, 'a disabled draft must not offer disable');

  await clickCanvasAction('cvEnable');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === true && app.changeKind === 'clean');
}, null, { timeout: 8000 });
  ui = await primaryUi();
  assert.equal(ui.label, ui.published, 'a clean enabled group must read as published');
  assert.equal(ui.state, 'running', 'an enabled clean group must be projected as running');
  assert.equal(ui.disabled, true, 'published is a state, not a repeatable action');
  /* 多图生命周期（Task 8）：启用新图只追加一张启用图，原基线图继续启用并保持
     默认。草稿是 enabled && !default，绝非「启用即默认」——若把 enabled 当
     default，下一次普通保存会被 409 graph_conflict 拒绝。 */
  const enabled = await serverTree(draft.id);
  assert.equal(enabled.enabled, true);
  assert.equal(enabled.default, false, 'a later-enabled graph must not steal default');
  const list = await api('GET', '/admin/trees');
  assert.equal(list.trees.find(tree => tree.id === draft.id).enabled, true);
  const baseline = list.trees.find(tree => tree.id === TREE_ID);
  assert.equal(baseline.enabled, true, 'enabling a draft must not disable the baseline graph');
  assert.equal(baseline.default, true, 'the baseline graph must stay the default graph');
  const multi = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return {
      enabled: !!(app.tree && app.tree.enabled),
      isDefault: !!(app.tree && app.tree.default),
      savedDefault: !!(app.saved && app.saved.default),
      savedHash: app.saved ? app.saved.document_sha256 : '',
      documentHash: app.tree ? app.tree.document_sha256 : ''
    };
  });
  assert.equal(multi.enabled, true);
  assert.equal(multi.isDefault, false, 'enabling a second graph must not mark it default');
  assert.equal(multi.savedDefault, false, 'the save baseline kept the wrong default flag');
  /* 生命周期写改了文档字节：保存基线必须从回包同步 hash，否则下一次保存 409。 */
  assert.equal(multi.savedHash, multi.documentHash,
    'the lifecycle response did not refresh the save baseline hash');
  assert.equal(multi.savedHash, enabled.document_sha256);
  assert.equal(await page.locator('#cvDefault').isVisible(), true,
    'a non-default enabled graph must offer "set as default"');
  assert.equal(await page.locator('#cvDefaultPill').isVisible(), false,
    'a non-default graph must not show the default pill');

  assert.equal(await page.locator('#cvDisable').isVisible(), true,
    'an enabled group must expose disable inline in the toolbar');
  await clickCanvasAction('cvDisable');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === false && app.changeKind === 'clean');
}, null, { timeout: 8000 });
  ui = await primaryUi();
  assert.equal(ui.label, ui.enable, 'a clean disabled group must offer enable again');
  assert.equal(ui.state, 'disabled', 'disabling a clean saved group must project disabled');
  assert.equal(ui.disableHidden, true, 'a disabled group must hide the disable action');
  assert.equal((await serverTree(draft.id)).enabled, false, 'disable did not reach the server');
  assert.equal(await page.locator('#cvDefault').isVisible(), false,
    'a disabled group must not offer "set as default"');
  await ensureBaselineTree();
});

test('multi-graph lifecycle: default switch survives a save and disables into an empty route set', async () => {
  /* brief 的真实浏览器场景（Task 8）：多图同时启用、默认切换、切默认后立即保存
     不得 409 graph_conflict、停用非默认图成功、停用最后一张默认图后路由集为空。 */
  const MAIN = 'main';
  const FINANCE = 'finance';
  const listedTree = async id =>
    (await api('GET', '/admin/trees')).trees.find(tree => tree.id === id);
  try {
    await ensureBaselineTree();
    /* 从零启用图（合法空路由集）开始：先建两张关闭副本，再停用唯一启用图。
       这样第一张启用的图自动成为默认，正好复现 brief 的 main/finance 场景。 */
    const existing = (await api('GET', '/admin/trees')).trees;
    for (const [id, name] of [[MAIN, '主图'], [FINANCE, '财务图']]) {
      if (!existing.some(tree => tree.id === id)) {
        await api('POST', '/admin/trees/' + TREE_ID + '/copy', { id, name });
      }
    }
    await api('POST', '/admin/trees/' + TREE_ID + '/disable');
    const emptyStart = await api('GET', '/admin/trees');
    assert.deepEqual(emptyStart.graph_status.enabled_tree_ids, [],
      'disabling the last enabled graph must publish an empty route set');
    assert.equal(emptyStart.graph_status.default_tree_id, null);

    await openCanvas();
    const select = page.locator('#cvTreeSelect');

    /* 1) 启用 main：空路由集里的第一张启用图自动成为默认图。 */
    await select.selectOption(MAIN);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return (app.treeId === id && app.changeKind === 'clean');
}, MAIN, { timeout: 8000 });
    await clickCanvasAction('cvEnable');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return (app.tree && app.tree.enabled === true && app.tree.default === true);
}, null, { timeout: 8000 });
    assert.equal((await listedTree(MAIN)).default, true,
      'the first enabled graph must become the default graph');

    /* 2) 启用 finance：两者都保持启用，main 仍是默认。 */
    await select.selectOption(FINANCE);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return (app.treeId === id && app.changeKind === 'clean');
}, FINANCE, { timeout: 8000 });
    await clickCanvasAction('cvEnable');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return (app.tree && app.tree.enabled === true && app.tree.default === false);
}, null, { timeout: 8000 });
    const multi = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        enabled: !!(app.tree && app.tree.enabled),
        isDefault: !!(app.tree && app.tree.default),
        savedDefault: !!(app.saved && app.saved.default)
      };
    });
    assert.equal(multi.enabled, true);
    assert.equal(multi.isDefault, false, 'enabling the second graph claimed default');
    assert.equal(multi.savedDefault, false, 'the save baseline claimed default for a non-default graph');
    const mainTree = await listedTree(MAIN);
    const financeTree = await listedTree(FINANCE);
    assert.equal(mainTree.enabled, true, 'enabling finance disabled the already-enabled main graph');
    assert.equal(mainTree.default, true, 'enabling finance stole default from main');
    assert.equal(financeTree.enabled, true);
    assert.equal(financeTree.default, false);
    assert.equal(await page.locator('#cvDefault').isVisible(), true,
      'a non-default enabled graph must offer "set as default"');
    assert.equal(await page.locator('#cvDefaultPill').isVisible(), false,
      'a non-default graph must not show the default pill');

    /* 3) 点击 finance 的「设为默认」：finance 默认、main 降级，但两者仍启用。 */
    const hashBeforeDefault = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.saved.document_sha256;
    });
    await clearToasts();
    await clickCanvasAction('cvDefault');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return (app.tree && app.tree.enabled === true && app.tree.default === true);
}, null, { timeout: 8000 });
    await waitToastIncludes(await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return t('cv.toastDefault');
}));
    const switchedMain = await listedTree(MAIN);
    const switchedFinance = await listedTree(FINANCE);
    assert.equal(switchedFinance.default, true, 'the default switch did not reach the server');
    assert.equal(switchedFinance.enabled, true);
    assert.equal(switchedMain.default, false, 'the old default graph was not demoted');
    assert.equal(switchedMain.enabled, true, 'the old default graph must stay enabled');
    const multiAfterSwitch = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      const select = document.querySelector('#cvTreeSelect');
      const option = Array.from(select.options).find(item => item.value === select.value);
      const { t } = await import('/console/i18n.js');
      return {
        kind: app.kind(),
        isDefault: !!(app.tree && app.tree.default),
        savedDefault: !!(app.saved && app.saved.default),
        savedHash: app.saved.document_sha256,
        documentHash: app.tree.document_sha256,
        defaultMark: t('cv.treeDefault'),
        optionLabel: option ? option.textContent : ''
      };
    });
    assert.equal(multiAfterSwitch.kind, 'clean',
      'a lifecycle response must not dirty the draft');
    assert.equal(multiAfterSwitch.isDefault, true);
    assert.equal(multiAfterSwitch.savedDefault, true);
    /* default 切换会改写目标文档字节：保存基线必须从回包刷新 document_sha256。 */
    assert.notEqual(multiAfterSwitch.savedHash, hashBeforeDefault,
      'the default switch did not change the document hash');
    assert.equal(multiAfterSwitch.savedHash, multiAfterSwitch.documentHash,
      'the save baseline was not refreshed from the default response');
    assert.equal(multiAfterSwitch.savedHash, switchedFinance.document_sha256);
    assert.ok(multiAfterSwitch.optionLabel.includes(multiAfterSwitch.defaultMark),
      'the tree selector does not mark the new default graph');
    assert.equal(await page.locator('#cvDefault').isVisible(), false,
      'the default graph must hide "set as default"');
    assert.equal(await page.locator('#cvDefaultPill').isVisible(), true,
      'the default graph must show the default pill');

    /* 4) 立即再保存当前图：基线已刷新，不能出现 409 graph_conflict。 */
    await clearToasts();
    await saveNow();
    await waitToastIncludes(await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return t('cv.toastSaved');
}));
    const savedAgain = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      const { t } = await import('/console/i18n.js');
      return { kind: app.kind(), hash: app.saved.document_sha256, conflict: t('cv.toastSaveConflict') };
    });
    assert.equal(savedAgain.kind, 'clean');
    assert.equal((await toastText()).includes(savedAgain.conflict), false,
      'the save right after a default switch was rejected with graph_conflict');
    assert.equal(savedAgain.hash, (await serverTree(FINANCE)).document_sha256,
      'the save baseline drifted from the server document');

    /* 5) 启动选择默认图：受控列表把非默认的 main 放在 finance 前面。 */
    const listRoute = async route => {
      const real = await api('GET', '/admin/trees');
      const main = real.trees.find(tree => tree.id === MAIN);
      const finance = real.trees.find(tree => tree.id === FINANCE);
      const others = real.trees.filter(tree => tree.id !== MAIN && tree.id !== FINANCE);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...real, trees: [main, finance, ...others] })
});
};
    await page.route(/\/admin\/trees$/, listRoute);
    try {
      await openCanvas();
    } finally {
      await page.unroute(/\/admin\/trees$/, listRoute);
    }
    assert.equal(await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.treeId;
}), FINANCE, 'boot must select the default graph, not the first enabled graph');

    /* 6) 停用非默认的 main 成功；再停用默认的 finance（最后一张启用图）→ 空路由集。 */
    await select.selectOption(MAIN);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return (app.treeId === id && app.changeKind === 'clean' && app.tree.enabled === true);
}, MAIN, { timeout: 8000 });
    assert.equal(await page.locator('#cvDisable').isVisible(), true);
    await clickCanvasAction('cvDisable');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return (app.tree && app.tree.enabled === false && app.tree.default === false);
}, null, { timeout: 8000 });
    assert.equal((await listedTree(MAIN)).enabled, false, 'disabling a non-default graph failed');
    assert.equal((await listedTree(FINANCE)).enabled, true,
      'disabling the non-default graph also disabled the default graph');
    assert.equal((await listedTree(FINANCE)).default, true);

    await select.selectOption(FINANCE);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return (app.treeId === id && app.changeKind === 'clean' && app.tree.enabled === true);
}, FINANCE, { timeout: 8000 });
    await clickCanvasAction('cvDisable');
    await waitFor(async () => {
      const { app } = await import('/console/routing/app.js');
      return (app.tree && app.tree.enabled === false);
}, null, { timeout: 8000 });
    const empty = await api('GET', '/admin/trees');
    assert.ok(empty.trees.every(tree => tree.enabled === false),
      'disabling the last default graph left an enabled graph behind');
    assert.deepEqual(empty.graph_status.enabled_tree_ids, [],
      'the route set is not empty after disabling the last enabled graph');
    assert.equal(empty.graph_status.default_tree_id, null);
    const disabledUi = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        enabled: !!(app.tree && app.tree.enabled),
        isDefault: !!(app.tree && app.tree.default),
        state: document.getElementById('cvLifecycle').dataset.state,
        defaultHidden: document.getElementById('cvDefault').hidden,
        pillHidden: document.getElementById('cvDefaultPill').hidden
      };
    });
    assert.equal(disabledUi.enabled, false);
    assert.equal(disabledUi.isDefault, false);
    assert.equal(disabledUi.state, 'disabled');
    assert.equal(disabledUi.defaultHidden, true);
    assert.equal(disabledUi.pillHidden, true);
  } finally {
    await ensureBaselineTree();
  }
});
test('multi-graph lifecycle: disabling the default graph with other graphs enabled prompts for a default switch', async () => {
  /* Fix wave 1（设计 §6.2/§8）：默认图还有其它启用图时不能直接停用，必须先切默认图。
     两条路径都在真实浏览器里锁住：
     1) 前端拦截：页面列表已知两张图启用 → 点默认图的停用不发 /disable 请求，
        只弹本地化「先切换默认图」提示；
     2) 服务端竞态：页面加载后另一会话启用了第二张图（页面列表过期）→ 前端预检查
        放行 → 服务端 409 default_graph_required → 同一提示，绝不把结构化 detail
        当原始 JSON 显示；服务器状态与 snapshot_revision 均保持不变。 */
  const MAIN = 'main';
  const FINANCE = 'finance';
  const treesPayload = () => api('GET', '/admin/trees');
  const listedTree = async id => (await treesPayload()).trees.find(tree => tree.id === id);
  const publishedRevision = async () => (await treesPayload()).graph_status.snapshot_revision;
  try {
    await ensureBaselineTree();
    const existing = (await treesPayload()).trees;
    for (const [id, name] of [[MAIN, '主图'], [FINANCE, '财务图']]) {
      if (!existing.some(tree => tree.id === id)) {
        await api('POST', '/admin/trees/' + TREE_ID + '/copy', { id, name });
      }
    }
    /* 清空路由集后先启用 main（第一张启用图自动成为默认），再启用 finance。 */
    await api('POST', '/admin/trees/' + TREE_ID + '/disable');
    await api('POST', '/admin/trees/' + MAIN + '/enable');
    await api('POST', '/admin/trees/' + FINANCE + '/enable');
    assert.equal((await listedTree(MAIN)).default, true);
    assert.equal((await listedTree(FINANCE)).enabled, true);

    await openCanvas();
    const blockedCopy = await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return t('cv.toastDisableDefaultBlocked');
    });
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return (app.treeId === id && app.changeKind === 'clean' &&
        !!(app.tree && app.tree.enabled && app.tree.default));
    }, MAIN, { timeout: 8000 });
    assert.equal(await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.trees.filter(tree => tree.enabled).length;
    }), 2, 'the console must see both enabled graphs before the guard check');

    /* 1) 前端拦截：默认图 + 另一张启用图 → 不发写请求，只弹切换默认图提示。 */
    const writes = [];
    const onRequest = request => {
      if (request.method() === 'POST' &&
        new RegExp('/admin/trees/' + MAIN + '/(?:disable|default)$').test(request.url())) {
        writes.push(request.method() + ' ' + request.url());
      }
    };
    page.on('request', onRequest);
    let revisionBeforeGuard;
    try {
      revisionBeforeGuard = await publishedRevision();
      await clearToasts();
      await clickCanvasAction('cvDisable');
      await waitToastIncludes(blockedCopy);
    } finally {
      page.off('request', onRequest);
    }
    await page.waitForTimeout(120);
    assert.deepEqual(writes, [], 'the blocked disable still sent a write request');
    assert.equal(await toastText(), blockedCopy,
      'the blocked disable did not use the localized switch prompt');
    assert.equal((await toastText()).includes('default_graph_required'), false,
      'the raw backend JSON leaked into the toast');
    assert.equal(await publishedRevision(), revisionBeforeGuard,
      'the blocked disable changed the published route set');
    const mainAfterGuard = await listedTree(MAIN);
    assert.equal(mainAfterGuard.enabled, true, 'the blocked disable disabled the default graph');
    assert.equal(mainAfterGuard.default, true, 'the blocked disable dropped the default flag');
    const uiAfterGuard = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        enabled: !!(app.tree && app.tree.enabled),
        isDefault: !!(app.tree && app.tree.default),
        saving: !!app.saving,
        state: document.getElementById('cvLifecycle').dataset.state
      };
    });
    assert.equal(uiAfterGuard.enabled, true);
    assert.equal(uiAfterGuard.isDefault, true);
    assert.equal(uiAfterGuard.saving, false);
    assert.equal(uiAfterGuard.state, 'running');

    /* 2) 服务端竞态：页面加载时只有 main 启用，随后另一会话启用 finance。 */
    await api('POST', '/admin/trees/' + FINANCE + '/disable');
    await openCanvas();
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return (app.treeId === id && app.changeKind === 'clean');
    }, MAIN, { timeout: 8000 });
    assert.deepEqual(await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return app.trees.filter(tree => tree.enabled).map(tree => tree.id);
    }), [MAIN], 'the page must start from a one-enabled-graph view for the race');
    await api('POST', '/admin/trees/' + FINANCE + '/enable');
    const revisionBeforeRace = await publishedRevision();
    assert.equal((await listedTree(MAIN)).default, true,
      'the race setup must keep main as the default graph');
    expectHttpStatusError({ url: new RegExp('/admin/trees/' + MAIN + '/disable$'), status: /409/ });
    await clearToasts();
    await clickCanvasAction('cvDisable');
    await waitToastIncludes(blockedCopy);
    await page.waitForTimeout(300);
    assert.equal(expectedHttpStatusErrors.length, 0,
      'the 409 default_graph_required console error was not observed at the expected URL/status');
    assert.equal(await toastText(), blockedCopy,
      'the 409 rejection did not use the localized switch prompt');
    assert.equal((await toastText()).includes('default_graph_required'), false,
      'the structured 409 detail leaked into the toast as raw JSON');
    assert.equal(await publishedRevision(), revisionBeforeRace,
      'the rejected disable changed the published route set');
    const mainAfterRace = await listedTree(MAIN);
    assert.equal(mainAfterRace.enabled, true, 'the rejected disable disabled the default graph');
    assert.equal(mainAfterRace.default, true, 'the rejected disable dropped the default flag');
    assert.equal(await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return !!app.saving;
    }), false, 'a rejected disable left the console stuck in the saving state');
  } finally {
    resetExpectedHttpStatusErrors();
    await ensureBaselineTree();
  }
});
test('Task 2 fix · a saved disabled group with local edits reads disabled, not draft', async () => {
  /* 未启用优先：只要落过盘（store.saved），本地脏改动不再把生命周期降级成
     「草稿未保存」；脏状态由主按钮「保存并启用」表达。 */
  try {
    await ensureBaselineTree();
    await openCanvas();
    await clickCanvasAction('cvDisable');
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
      return (app.tree.enabled === false && app.changeKind === 'clean');
}, null, { timeout: 8000 });
    const saved = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
      return (app.saved ? { enabled: app.saved.enabled } : null);
});
    assert.equal(saved && saved.enabled, false,
      'disable must keep store.saved; the dirty-disabled regression needs that precondition');

    await dragNodeBy('start', 24, 14);
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
      return (app.changeKind !== 'clean');
});

    const state = await page.locator('#cvLifecycle').getAttribute('data-state');
    const label = (await page.locator('#cvLifecycleLabel').textContent()).trim();
    assert.equal(state, 'disabled',
      `a saved disabled group with local edits must read disabled, got ${state} / ${label}`);
    assert.equal(label, await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
      return (t('cv.status.disabled'));
}), 'a saved disabled group must use the disabled label even while dirty');
    assert.equal((await page.locator('#cvEnableLabel').textContent()).trim(),
      await page.evaluate(async () => {
    const { t } = await import('/console/i18n.js');
        return (t('cv.publish'));
}), 'a dirty disabled group must offer publish, not a stale published label');
} finally {
    await ensureBaselineTree();
}
});

test('Task 2 fix · language switch repaints the JS-owned lifecycle and primary labels', async () => {
  /* #cvEnableLabel 不再带 data-i18n：setLang 只能靠 onLangChange 重绘，
     这里保证切语言后标签仍与当前生命周期一致，而不是残留旧语言。
     期望 key 从 #cvLifecycle 的 data-state（以及主按钮当前 zh 文案）推导，
     基线 fixture 换状态时不会退化成一条误导的「语言切换没有重绘」失败。 */
  try {
    await ensureBaselineTree();
    await openCanvas();
    /* 先固定 zh，再从 #cvLifecycle 的 data-state 推导期望：
       running / unpublished / draft 一对一映射到主按钮 key；只有 disabled 的
       主按钮是 cv.publish 还是 cv.enable 取决于草稿是否脏，单看 state 不够，
       所以用同一 owner 画出的 zh 文案反查 key（两者中文不同，不会含糊）。 */
    const base = await page.evaluate(async () => {
      const { setLang, t } = await import('/console/i18n.js');
      setLang('zh');
      const state = document.getElementById('cvLifecycle').dataset.state;
      const lifecycleKey = 'cv.status.' + state;
      const primary = document.getElementById('cvEnableLabel').textContent.trim();
      const candidates = ['cv.published', 'cv.publishUpdate', 'cv.publish', 'cv.enable'];
      const primaryFromState = {
        running: 'cv.published',
        unpublished: 'cv.publishUpdate',
        draft: 'cv.publish'
      }[state] || null;
      return {
        state,
        lifecycleKey,
        lifecycle: document.getElementById('cvLifecycleLabel').textContent.trim(),
        lifecycleText: t(lifecycleKey),
        primary,
        primaryFromState,
        primaryKey: candidates.find(key => t(key) === primary) || null
      };
    });
    assert.equal(base.lifecycle, base.lifecycleText,
      `the lifecycle label does not match its own data-state (${base.state}) in zh`);
    assert.ok(base.primaryKey,
      `the primary label (${base.primary}) does not match any known key`);
    if (base.primaryFromState) {
      assert.equal(base.primaryKey, base.primaryFromState,
        `the ${base.state} state must offer ${base.primaryFromState}, got ${base.primaryKey}`);
    }

    const labels = () => page.evaluate(async ([lifecycleKey, primaryKey]) => {
      const { t } = await import('/console/i18n.js');
      return {
        lifecycle: document.getElementById('cvLifecycleLabel').textContent.trim(),
        primary: document.getElementById('cvEnableLabel').textContent.trim(),
        expectedLifecycle: t(lifecycleKey),
        expectedPrimary: t(primaryKey)
      };
    }, [base.lifecycleKey, base.primaryKey]);

    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('en');
    });
    let ui = await labels();
    assert.equal(ui.lifecycle, ui.expectedLifecycle,
      'language switch did not repaint the lifecycle label');
    assert.equal(ui.primary, ui.expectedPrimary,
      'language switch did not repaint the JS-owned primary label');

    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    });
    ui = await labels();
    assert.equal(ui.lifecycle, ui.expectedLifecycle,
      'switching back to zh did not repaint the lifecycle label');
    assert.equal(ui.primary, ui.expectedPrimary,
      'switching back to zh did not repaint the JS-owned primary label');
  } finally {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
    });
  }
});

test('Task 2 fix · an English session never paints the primary label in Chinese', async () => {
  /* 首屏主按钮不再有中文静态种子：app.js 在模块加载时同步 paintLifecycle()。
     从 document-start 起记录 #cvEnableLabel 的每一个非空值，证明 en 会话
     哪怕一帧都不会出现中文，且 pre-boot 值本身已是英文 cv.publish。 */
  try {
    await ensureBaselineTree();
    /* 首条用例单跑时页面还是 about:blank，写不了 session/localStorage；
       先落到同源页面，再设置 en 预设并武装探针（真正的带探针加载在 openCanvas）。 */
    await page.goto(routerBase + '/console/routing.html');
    await page.addInitScript(() => {
      let armed = false;
      try { armed = sessionStorage.getItem('qa-cv-en-probe') === '1'; } catch (error) { armed = false; }
      if (!armed) return;
      window.__cvEnableLabelSeen = [];
      const record = () => {
        const el = document.getElementById('cvEnableLabel');
        if (!el) return;
        const seen = window.__cvEnableLabelSeen;
        const text = el.textContent.trim();
        if (seen[seen.length - 1] !== text) seen.push(text);
      };
      new MutationObserver(record).observe(document, { childList: true, subtree: true, characterData: true });
      document.addEventListener('DOMContentLoaded', record);
    });
    /* 语言在 i18n 模块求值前就落到 localStorage；init script 只在本用例武装。 */
    await page.evaluate(() => {
      sessionStorage.setItem('qa-cv-en-probe', '1');
      localStorage.setItem('router_lang', 'en');
    });
    await openCanvas();
    const observed = await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return {
        lang: document.documentElement.lang,
        publish: t('cv.publish'),
        seen: (window.__cvEnableLabelSeen || []).slice()
      };
    });
    assert.equal(observed.lang, 'en', 'the en preset did not reach the page');
    assert.notEqual(observed.publish, 'cv.publish', 'missing en translation for cv.publish');
    const seen = observed.seen.filter(Boolean);
    assert.ok(seen.length > 0, 'the probe never observed a non-empty primary label');
    seen.forEach(text => assert.doesNotMatch(text, /[\u4e00-\u9fff]/,
      'an English session rendered a Chinese primary label: ' + text));
    assert.equal(seen[0], observed.publish,
      'the pre-boot primary label is not the localized cv.publish');
  } finally {
    await page.evaluate(async () => {
      const { setLang } = await import('/console/i18n.js');
      setLang('zh');
      sessionStorage.removeItem('qa-cv-en-probe');
    });
  }
});

test('7.4-13-save-enable-unsaved-badge', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 未保存提示：布局改动仍是未发布修改；保存后生命周期回到运行中。 */
  await dragNodeBy('start', 26, 16);
  let badge = await draftStatus();
  assert.equal(badge.visible, true, 'a dirty draft did not show the lifecycle badge');
  assert.equal(badge.kind, 'layout');
  assert.equal(badge.state, 'unpublished');
  assert.equal(badge.text, await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.status.unpublished'));
}));
  assert.equal((await page.locator('#cvEnableLabel').textContent()).trim(),
    await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.publishUpdate'));
}));
  await saveNow();
  badge = await draftStatus();
  assert.equal(badge.state, 'running', 'save left the draft unpublished');
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
    await clickCanvasAction('cvNewTree');
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null);
});
    await clickCanvasAction('cvEnable');
    await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === true && app.changeKind === 'clean');
},
      null, { timeout: 10000 });
    assert.deepEqual(requests, ['create', 'enable'], 'enable did not auto-save the dirty new tree first');
    assert.equal((await draftStatus()).state, 'running');
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
    await clickCanvasAction('cvEnable');
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
    assert.equal((await draftStatus()).state, 'running');
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
      nodes: app.graph.nodes.length,
      runtime: app.tree.runtime
}));
});
    assert.equal(draft.saved, null);
    assert.equal(draft.treeId, draft.tree, 'boot fallback envelope id differs from the draft tree id');
    assert.match(draft.treeId, /^[0-9a-f]{12}$/, 'boot fallback id is not a frontend UUID slice: ' + draft.treeId);
    assert.notEqual(draft.treeId, 'local');
    assert.equal(draft.nodes, 2);
    assert.equal(draft.kind, 'behavior');
    /* 严格自包含：boot 兜底草稿从数据库目录 createRuntime() 得到完整四字段快照。 */
    assert.deepEqual(Object.keys(draft.runtime).sort(),
      ['backends', 'jev', 'jev_window', 'time_periods']);
    assert.ok(draft.runtime.backends.models.length >= 1,
      'the fallback draft runtime has no model to route to');

    const posted = page.waitForRequest(request =>
      request.method() === 'POST' && new URL(request.url()).pathname === '/admin/trees');
    await clickCanvasAction('cvSave');
    const request = await posted;
    const payload = request.postDataJSON();
    assert.match(payload.id, /^[0-9a-f]{12}$/, 'POST /admin/trees did not use the UUID-slice id');
    assert.equal(payload.id, draft.treeId);
    /* 保存无条件携带四字段完整快照：绝不发送 runtime:null，也不省略运行字段。 */
    assert.equal(Object.prototype.hasOwnProperty.call(payload, 'runtime'), false,
      'save payload sent the removed runtime control field');
    ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
      assert.equal(Object.prototype.hasOwnProperty.call(payload, field), true,
        'a strict draft save dropped the ' + field + ' runtime field');
});
    assert.deepEqual(payload.backends, draft.runtime.backends);
    assert.deepEqual(payload.jev_window, draft.runtime.jev_window);
    await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.changeKind === 'clean' && app.treeId === id);
},
      draft.treeId, { timeout: 8000 });
    const listed = (await api('GET', '/admin/trees')).trees.find(tree => tree.id === draft.treeId);
    assert.ok(listed, 'boot fallback save did not create a tree with the draft id');
    assert.equal(listed.enabled, false);
    /* 保存回包的快照同步进草稿与基线：刚保存就必须 clean。 */
    const saved = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return (({ kind: app.kind(), runtime: app.tree.runtime, savedRuntime: app.saved.jev_window }));
});
    assert.equal(saved.kind, 'clean', 'the saved snapshot was not synced into the draft');
    assert.deepEqual(saved.runtime.backends, draft.runtime.backends);
    assert.deepEqual(saved.savedRuntime, draft.runtime.jev_window);
} finally {
    await page.unroute(treeListRoute, emptyTreeList);
    await ensureBaselineTree();
}
});

test('Task 12 · export downloads the complete v4 envelope including timezone and sticky', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const download = await exportViaUi();
  assert.equal(download.name, TREE_ID + '.sr-routing-graph-v4.json');
  const parsed = JSON.parse(download.text);
  assert.equal(parsed.kind, 'sr-routing-graph');
  assert.equal(parsed.version, 4);
  assert.deepEqual(parsed.departments, []);
  /* 生命周期随服务端：基线组是启用图，导出必须保留 enabled/default=true。 */
  assert.equal(parsed.enabled, baseline.enabled === true);
  assert.equal(parsed.default, baseline.default === true);
  assert.equal(parsed.default, parsed.enabled);
  assert.equal(parsed.name, baseline.name);
  assert.equal(parsed.timezone, BASELINE.timezone);
  assert.deepEqual(parsed.sticky, BASELINE.sticky);
  assert.equal(parsed.root.nodes.length, EXAMPLE_NODES);
  assert.equal(parsed.root.edges.length, EXAMPLE_EDGES);
  assert.equal(parsed.root.otherwise_target, undefined);
  assert.equal(parsed.root.error_fallback, EXAMPLE_TARGETS.fallback);
  /* 自包含文档：导出的 v4 文件必须带四运行字段（与保存 payload 同一形状），
     且服务端密钥/密钥状态不得出现在文件里。 */
  ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
    assert.ok(Object.prototype.hasOwnProperty.call(parsed, field),
      'exported document is missing the ' + field + ' runtime field');
});
  assert.equal(parsed.backends.providers.length >= 1, true, 'exported runtime has no providers');
  assert.equal(parsed.time_periods.map(period => period.name).join(','), 'peak,normal,offpeak');
  assert.equal(JSON.stringify(parsed).includes('"api_key"'), false,
    'exported document leaked a server-side api_key field');
  assert.equal(JSON.stringify(parsed).includes('"key_present"'), false,
    'exported document leaked a server-side key_present field');
  const exported = await draftStatus();
  assert.equal(exported.kind, 'clean', 'export mutated the draft');
  assert.equal(exported.state, 'running', 'export left the lifecycle badge dirty');
});

test('envelope-roundtrip-preserves-revision', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const source = await exportViaUi();
  /* 源文档的 canonical 必须走 parseImport 的整树投影：手工拼 {name,...,root}
     会丢掉四运行字段，导入后的整树 canonical 就永远不相等。 */
  const sourceCanonical = await page.evaluate(async text => {
    const RTGraph = await import('/console/routing/graph.js');

    const parsed = RTGraph.parseImport(text);
    return parsed.ok ? RTGraph.behaviorCanonical(parsed.tree) : null;

}, source.text);
  assert.ok(sourceCanonical, 'the exported document no longer parses as a self-contained v4 document');
  const parsedSource = JSON.parse(source.text);

  await clickCanvasAction('cvNewTree');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  const newDraft = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return ({ id: app.treeId, runtime: app.tree.runtime });
});
  const newId = newDraft.id;
  /* 严格契约：新建草稿自带完整四字段快照（撤销导入后回到这份快照，而不是 null）。 */
  assert.deepEqual(Object.keys(newDraft.runtime).sort(),
    ['backends', 'jev', 'jev_window', 'time_periods']);
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
    runtime: app.tree.runtime,
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
  assert.ok(imported.runtime, 'import did not restore the self-contained runtime');
  assert.deepEqual(imported.runtime.time_periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
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
  /* 导入文档自带的 runtime 必须原样进保存 payload/服务端文档，不能退回活动树的配置。 */
  assert.deepEqual(roundTripped.time_periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.deepEqual(roundTripped.backends.providers.map(provider => [provider.name, provider.base_url]),
    parsedSource.backends.providers.map(provider => [provider.name, provider.base_url]));
  assert.equal(Object.prototype.hasOwnProperty.call(roundTripped, 'jev'), true);

  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.graph.nodes.length === 2);
});
  const undone = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return { edges: app.graph.edges.length, runtime: app.tree.runtime };
});
  assert.equal(undone.edges, 1, 'one undo did not restore the pre-import draft');
  assert.deepEqual(undone.runtime, newDraft.runtime,
    'undo did not restore the pre-import draft runtime snapshot');
});

test('Task C2 · save carries the runtime and the saved response becomes the draft baseline', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const draftRuntime = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.tree.runtime;
  });
  assert.ok(draftRuntime, 'the server tree loaded into the draft has no runtime');

  /* 制造一次 display 改动再保存：PUT payload 必须同时带四运行字段与文档指纹。 */
  await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.change('display', tree => { tree.name = '保存携带运行配置'; }));
});
  const captured = page.waitForRequest(request =>
    request.method() === 'PUT' && new URL(request.url()).pathname === '/admin/trees/' + TREE_ID);
  await saveNow();
  const payload = (await captured).postDataJSON();
  ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
    assert.ok(Object.prototype.hasOwnProperty.call(payload, field),
      'save payload dropped the ' + field + ' runtime field');
});
  assert.deepEqual(payload.time_periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.deepEqual(payload.backends.models.map(model => model.id).sort(),
    draftRuntime.backends.models.map(model => model.id).sort(),
    'save payload sent a different runtime model catalog than the draft');
  assert.equal(payload.base_document_sha256, baseline.document_sha256);

  /* 回包里服务端物化的 runtime 必须写回草稿与基线：刚保存就必须 clean。 */
  const afterSave = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    runtime: app.tree.runtime,
    savedBackends: app.saved.backends,
    savedJev: app.saved.jev,
    savedWindow: app.saved.jev_window,
    savedPeriods: app.saved.time_periods
}));
});
  assert.equal(afterSave.kind, 'clean', 'a save with runtime left the draft dirty');
  assert.ok(afterSave.runtime, 'the saved response runtime was not synced into the draft');
  assert.deepEqual(afterSave.runtime.time_periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.deepEqual(afterSave.savedBackends.models.map(model => model.id).sort(),
    afterSave.runtime.backends.models.map(model => model.id).sort());
  assert.deepEqual(afterSave.savedPeriods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.ok(afterSave.savedJev && typeof afterSave.savedJev.base_url === 'string');
  assert.ok(afterSave.savedWindow && Number.isInteger(afterSave.savedWindow.input_tokens));

  const savedServer = await serverTree(TREE_ID);
  assert.deepEqual(savedServer.time_periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.equal(savedServer.backends.providers[0].base_url,
    draftRuntime.backends.providers[0].base_url,
    'saving changed the runtime provider endpoint');
  await ensureBaselineTree();
});

test('Task C2 · import replaces the runtime and the imported runtime reaches save, enable and export', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const source = JSON.parse((await exportViaUi()).text);
  assert.ok(source.jev, 'the exported document has no jev runtime section');

  /* 自包含文档：导入必须整组替换 runtime（jev.model 是可观测差异）。 */
  const patched = JSON.parse(JSON.stringify(source));
  patched.name = '导入带自定义运行配置';
  patched.jev.model = 'qa-imported-runtime';
  await setImportPayload(patched, 'self-contained-runtime.json');
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return !!(app.tree.runtime && app.tree.runtime.jev &&
      app.tree.runtime.jev.model === 'qa-imported-runtime');
}, null, { timeout: 8000 });
  const imported = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    kind: app.kind(),
    canUndo: store.canUndo,
    runtime: app.tree.runtime
}));
});
  assert.equal(imported.kind, 'behavior', 'importing a document with a different runtime is not a behavior change');
  assert.equal(imported.canUndo, true, 'import did not become a single history entry');
  assert.deepEqual(imported.runtime.time_periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.deepEqual(imported.runtime.backends.models.map(model => model.id).sort(),
    source.backends.models.map(model => model.id).sort(),
    'import dropped or reshaped the document runtime models');

  /* 保存：导入的 runtime 必须原样进 payload 与服务端文档。 */
  const captured = page.waitForRequest(request =>
    request.method() === 'PUT' && new URL(request.url()).pathname === '/admin/trees/' + TREE_ID);
  await saveNow();
  const payload = (await captured).postDataJSON();
  assert.equal(payload.jev.model, 'qa-imported-runtime', 'the imported runtime did not reach the save payload');
  assert.equal(Array.isArray(payload.backends.models), true);
  const savedServer = await serverTree(TREE_ID);
  assert.equal(savedServer.jev.model, 'qa-imported-runtime');
  assert.equal(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.kind());
}), 'clean');

  /* 启用：停用后重新启用，服务端用刚保存的 runtime 走完整启用校验，再导出。
     停用后 boot 会回退到别的组（或列表首个组），必须显式切回 TREE_ID 再启用。 */
  await api('POST', '/admin/trees/' + TREE_ID + '/disable');
  await openCanvas();
  const enableSelect = page.locator('#cvTreeSelect');
  if ((await enableSelect.inputValue()) !== TREE_ID) {
    await enableSelect.selectOption(TREE_ID);
    await waitFor(async id => {
    const { app } = await import('/console/routing/app.js');
    return (app.treeId === id && app.changeKind === 'clean');
},
      TREE_ID, { timeout: 8000 });
}
  await clickCanvasAction('cvEnable');
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.tree.enabled === true);
}, null, { timeout: 8000 });
  const enabledServer = await serverTree(TREE_ID);
  assert.equal(enabledServer.enabled, true);
  assert.equal(enabledServer.jev.model, 'qa-imported-runtime');
  const selfContainedExport = JSON.parse((await exportViaUi()).text);
  ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
    assert.ok(Object.prototype.hasOwnProperty.call(selfContainedExport, field),
      'the enabled tree export dropped the ' + field + ' runtime field');
  });
  assert.equal(selfContainedExport.jev.model, 'qa-imported-runtime');

  /* 旧文档（无四运行字段）：严格契约下必须被拒绝，草稿保持上一份自包含快照。 */
  const legacy = JSON.parse(JSON.stringify(source));
  ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => { delete legacy[field]; });
  legacy.name = '导入旧文档';
  const beforeLegacy = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return ({ name: app.tree.name, runtime: app.tree.runtime });
});
  await clearToasts();
  await setImportPayload(legacy, 'legacy-document.json');
  /* 拒绝原因必须点名缺哪组字段：只显示「导入失败：」前缀或「1 个校验问题」不算数。 */
  await expectImportReason('运行配置字段必须成组出现，缺少：backends, jev, jev_window, time_periods');
  const legacyState = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return ({
    kind: app.kind(),
    runtime: app.tree.runtime,
    name: app.tree.name
});
});
  /* 被拒的旧文档不得替换草稿：name/runtime 都保持导入前的值。 */
  assert.equal(legacyState.name, beforeLegacy.name,
    'a rejected legacy import replaced the draft name');
  assert.equal(legacyState.kind, 'clean',
    'a rejected legacy import left the draft dirty');
  assert.ok(legacyState.runtime, 'a rejected legacy import cleared the runtime snapshot');
  assert.deepEqual(legacyState.runtime.backends, beforeLegacy.runtime.backends);
  assert.equal(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return app.issues.every(issue => issue.level === 'hint');
}), true, 'a rejected legacy import leaked hard issues into the draft validation bar');

  /* 保存仍然只发送完整四字段：不存在 runtime:null 这条旧分支。 */
  const legacySave = page.waitForRequest(request =>
    request.method() === 'PUT' && new URL(request.url()).pathname === '/admin/trees/' + TREE_ID);
  await saveNow();
  const legacyPayload = (await legacySave).postDataJSON();
  assert.equal(Object.prototype.hasOwnProperty.call(legacyPayload, 'runtime'), false,
    'saving sent the removed runtime control field');
  ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
    assert.equal(Object.prototype.hasOwnProperty.call(legacyPayload, field), true,
      'saving dropped the ' + field + ' runtime field');
});
  const legacyServer = await serverTree(TREE_ID);
  const expectedRuntimeSnapshot = {};
  ['backends', 'jev', 'jev_window', 'time_periods'].forEach(field => {
    expectedRuntimeSnapshot[field] = beforeLegacy.runtime[field];
  });
  assert.deepEqual(treeRuntimeFields(legacyServer), expectedRuntimeSnapshot,
    'the stored canvas runtime drifted from the imported self-contained snapshot');
  await ensureBaselineTree();
});

test('Task C2 · inspector models come from the DB catalog, periods from the canvas runtime', async () => {
  await ensureBaselineTree();
  const baseline = await serverTree(TREE_ID);
  /* 自包含树：runtime 里的模型/时段与数据库目录故意不一致（目录漂移）。 */
  const runtime = {
    backends: {
      providers: JSON.parse(JSON.stringify(baseline.backends.providers)),
      models: JSON.parse(JSON.stringify(baseline.backends.models))
        .filter(model => model.id !== 'tierless-model')
        .concat([{
          id: 'runtime-only-model',
          upstream: 'runtime-only-model',
          provider: 'local',
          profile: {
            domains: [],
            tier: 'small',
            capability: '只存在于这份文档 runtime 里的模型',
            reasoning: { efforts: { low: 'low', medium: 'medium', high: 'high' }, default: 'low' }
}
}])
},
    jev: JSON.parse(JSON.stringify(baseline.jev)),
    jev_window: JSON.parse(JSON.stringify(baseline.jev_window)),
    time_periods: JSON.parse(JSON.stringify(baseline.time_periods)).filter(period => period.name === 'peak')
};
  const RUNTIME_TREE_ID = 'qa-runtime-catalog';
  await api('POST', '/admin/trees', {
    id: RUNTIME_TREE_ID,
    name: '自带运行配置的决策组',
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: EXAMPLE_ROOT,
    canvas: EXAMPLE.canvas,
    ...runtime
});
  await openCanvas();
  const switched = await page.evaluate(async id => {
    const { app } = await import('/console/routing/app.js');
    return (app.selectTree(id));
}, RUNTIME_TREE_ID);
  assert.equal(switched, true, 'switching to the self-contained tree was rejected');
  await waitFor(async id => {
    const { app } = await import('/console/routing/app.js');
    return (app.treeId === id && app.booted && app.kind() === 'clean');
}, RUNTIME_TREE_ID, { timeout: 8000 });

  /* 目标节点：模型下拉来自数据库模型目录（spec「模型下拉框来自数据库模型目录」）。
     画布快照里数据库已不存在的 runtime-only-model 不作为下拉选项出现（当前值兜底）。 */
  await clickNode('power-target');
  await waitInspectorBranch('node', 'power-target');
  const modelOptions = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="model"] option')).map(option => option.value));
  assert.ok(modelOptions.includes('tierless-model'),
    'inspector models did not come from the DB catalog: ' + modelOptions.join(','));
  assert.equal(modelOptions.includes('runtime-only-model'), false,
    'inspector models still came from the canvas runtime snapshot');
  /* 推理档位来自数据库模型 profile：deepseek-v3 声明 low/medium/high。 */
  const reasoningOptions = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="reasoning"] option')).map(option => option.value));
  ['low', 'medium', 'high'].forEach(level => {
    assert.ok(reasoningOptions.includes(level),
      'profile.reasoning.efforts map was not projected into the inspector array: ' + reasoningOptions.join(','));
});

  /* 时段节点：复选框只列画布 runtime 的时段（全局目录还有 normal/offpeak）。 */
  await clickNode('cond-peak');
  await waitInspectorBranch('node', 'cond-peak');
  assert.deepEqual(await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-field="periods"] input')).map(input => input.value)),
    ['peak'], 'inspector periods did not come from the canvas runtime');

  /* 选择数据库模型 = 把该模型及 provider 的非密钥目录信息补进画布快照（同一历史项）。 */
  await clickNode('power-target');
  await waitInspectorBranch('node', 'power-target');
  const beforeApply = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return {
      models: app.tree.runtime.backends.models.map(model => model.id),
      upstreams: app.tree.runtime.backends.models.map(model => model.upstream),
      kind: app.kind()
};
});
  assert.equal(beforeApply.models.includes('tierless-model'), false,
    'the baseline snapshot unexpectedly already declared the DB-only model');
  await page.locator('#cvInspectorBody [data-model-field="model"]').first().selectOption('tierless-model');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.tree.runtime.backends.models.some(model => model.id === 'tierless-model'));
}, null, { timeout: 8000 });
  const applied = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return {
      kind: app.kind(),
      models: app.tree.runtime.backends.models.map(model => model.id),
      upstreams: app.tree.runtime.backends.models.map(model => model.id + '=' + model.upstream),
      targetModels: app.graph.nodes.find(node => node.id === 'power-target').models
};
});
  assert.equal(applied.kind, 'behavior', 'embedding a DB model was not a behavior change');
  assert.ok(applied.models.includes('tierless-model'),
    'target model submission did not embed the DB model into the canvas snapshot');
  assert.deepEqual(applied.models.slice(0, beforeApply.models.length), beforeApply.models,
    'embedding a new model reordered the existing snapshot entries');
  assert.deepEqual(applied.upstreams.slice(0, beforeApply.upstreams.length),
    beforeApply.models.map((id, index) => id + '=' + beforeApply.upstreams[index]),
    'embedding a new model overwrote an existing same-id snapshot entry');
  assert.equal(applied.targetModels[0].model, 'tierless-model');
  await clickCanvasAction('cvUndo');
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return (!app.tree.runtime.backends.models.some(model => model.id === 'tierless-model'));
});
  assert.equal(await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.kind());
}), 'clean', 'undo did not restore the pre-embed snapshot');

  /* 新建草稿同样从数据库目录生成快照：目录模型（tierless-model）与全部时段可见。 */
  await clickCanvasAction('cvNewTree');
  await waitFor(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  await clickNode('target-1');
  await waitInspectorBranch('node', 'target-1');
  const fallbackModels = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="model"] option')).map(option => option.value));
  assert.ok(fallbackModels.includes('tierless-model'),
    'a new draft did not take its model options from the database catalog');
  const fallbackPeriods = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return (app.tree.runtime.time_periods.map(period => period.name));
});
  assert.deepEqual(fallbackPeriods, ['peak', 'normal', 'offpeak'],
    'a new draft runtime did not take the periods from the database catalog');
  await ensureBaselineTree();
});

test('7.4-14-import-export-v4-only', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  /* 导出只产出 sr-routing-graph v4；导入下面只接受同一格式。 */
  const exported = await exportViaUi();
  const exportedDoc = JSON.parse(exported.text);
  assert.equal(exportedDoc.kind, 'sr-routing-graph');
  assert.equal(exportedDoc.version, 4);
  assert.deepEqual(exportedDoc.departments, []);
  assert.equal(exportedDoc.enabled, baseline.enabled === true);
  assert.equal(exportedDoc.default, baseline.default === true);
  assert.equal(exportedDoc.root.type, 'graph');
  await dragNodeBy('start', 36, 24);

  /* 未保存时先弹共享确认框；取消后不选文件、草稿保持。 */
  await clickCanvasAction('cvImport');
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(async () => {
    const { setLang, t, tf } = await import('/console/i18n.js');
    return (t('cv.confirmImportTitle'));
})),
    'import did not reuse the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  const keptDirty = await draftStatus();
  assert.equal(keptDirty.kind, 'layout');
  assert.equal(keptDirty.state, 'unpublished');

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
  /* 缺必需字段是解析级拒绝：原因必须点名缺失字段，而不是只报校验问题数量。 */
  await expectImportReason('图解析失败：导入 envelope 缺少必需字段：sticky');

  assert.deepEqual(await draft(), before, 'a document missing sticky replaced the local draft');
});

test('Task 12 · a v4 document with a malformed root reports graph_parse_error and keeps the draft', async () => {
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
  await setImportPayload(strictDocument({
    name: 'v4 但 root 非法',
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: { type: 'jev', branches: [] }
}), 'malformed-v4.json');

  /* root 形状非法没有节点位置：必须显示解析原因本身，而不是「N 个校验问题」。 */
  await expectImportReason('图解析失败：root 不符合 sr-routing-graph v4 结构（未知字段或类型非法）');
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
    'a malformed v4 import leaked its parse issue into the draft validation bar');
  assert.equal(issues.every(issue => issue.level === 'hint'), true);
  assert.deepEqual(issues.map(issue => [issue.node_id, issue.edge_id]),
    [['', ''], ['', ''], ['', ''], ['', '']]);
  assert.equal(await page.locator('#cvIssues .rtc-issue.is-hard').count(), 0,
    'the validation bar rendered the rejected file as a hard issue');
  assert.equal(await page.locator('#cvIssues [data-locate]').count(), 0,
    'the rejected file left locatable issues pointing at draft nodes');
  assert.deepEqual(await draftOf(), kept, 'a malformed v4 import changed the draft');
});

test('Task 12 · a valid v4 import without positions lays every node out with finite coordinates', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const stripped = JSON.parse(JSON.stringify(EXAMPLE_ROOT));
  stripped.nodes.forEach(node => { delete node.position; });
  await setImportPayload(strictDocument({
    name: BASELINE.name,
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: stripped,
    /* 没有顶层 canvas：节点坐标必须由共享 DAG 布局补齐。 */
    canvas: undefined
}), 'missing-positions.json');
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
    runtime: app.tree.runtime,
    issues: app.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
    nodes: app.graph.nodes.map(node => ({ id: node.id, x: node.position.x, y: node.position.y })),
    expected: RTGraph.layoutPositions('dag', {
      nodes: app.graph.nodes, edges: app.graph.edges
})
}));
});
  assert.deepEqual(result.issues, [], 'position-less valid v4 import failed structure validation');
  result.nodes.forEach(node => {
    assert.ok(Number.isFinite(node.x) && Number.isFinite(node.y), `${node.id} has no finite position`);
    assert.deepEqual({ x: node.x, y: node.y }, result.expected[node.id],
      `${node.id} was not placed by the shared DAG layout`);
});
  /* 完整自包含文档的 runtime 必须原样进入草稿；示例文档端点与 mock 基线不同，
     所以相对已保存基线是 behavior 变更，缺坐标的自动补位本身仍是 layout 级别的。 */
  assert.deepEqual(Object.keys(result.runtime || {}).sort(),
    ['backends', 'jev', 'jev_window', 'time_periods']);
  assert.equal(result.runtime.jev.id, EXAMPLE.jev.id);
  assert.deepEqual(result.runtime.backends.providers,
    JSON.parse(JSON.stringify(EXAMPLE.backends.providers)));
  assert.equal(result.kind, 'behavior', 'importing a self-contained document must replace the runtime');
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
  await clickCanvasAction('cvAutoLayout');
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
    state: document.getElementById('cvLifecycle').dataset.state,
    positions: Object.fromEntries(app.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
}));
});
  assert.equal(laidOut.kind, 'layout');
  assert.equal(laidOut.state, 'unpublished');

  await saveNow();
  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'auto layout leaked into the server behavior revision');
  server.root.nodes.forEach(node => {
    const position = laidOut.positions[node.id];
    assert.ok(position, `${node.id} missing from the client layout`);
    const saved = server.canvas[node.id];
    assert.ok(saved, `${node.id} missing from the saved canvas`);
    assert.ok(Math.abs(saved.x - position.x) < 0.01, `${node.id} saved X differs from the DAG layout`);
    assert.ok(Math.abs(saved.y - position.y) < 0.01, `${node.id} saved Y differs from the DAG layout`);
});
});

test('7.4-15-validation-bar-and-locate', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 用禁用中的新组（enable 才会先保存）；导入示例文档，让 422 Issue 有可定位的节点/边。 */
  await clickCanvasAction('cvNewTree');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  const targetId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  /* 新建草稿未保存（behavior dirty）：导入先弹共享丢弃确认，确认后再选文件。 */
  await clickCanvasAction('cvImport');
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await setImportPayload(EXAMPLE, 'example-graph.json');
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
    await clickCanvasAction('cvEnable');
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

    /* 保存被 422 驳回时，画布上的问题节点 / 边 / 槽位同步标红（不等用户点「定位」）。 */
    assert.equal(await page.locator('#cvNodes [data-node-id="capability"].is-issue-target').count(), 1,
      'the 422 node issue was not painted red on the canvas');
    assert.equal(await page.locator(
      '#cvNodes [data-node-id="deep-target"] .rtc-slot[data-slot="in_1"].is-issue-target').count(), 1,
      'the slot behind the invalid edge issue was not painted red');
    assert.equal(await page.evaluate(() => {
      const path = document.querySelector('#cvEdges path.rtc-edge[data-edge-id="e-clarify-deep"]');
      return !!path && path.classList.contains('is-issue-target') &&
        getComputedStyle(path).stroke === 'rgb(185, 28, 28)';
}), true, 'the invalid edge was not stroked red');
    assert.equal(await page.evaluate(() => {
      const node = document.querySelector('#cvNodes [data-node-id="capability"]');
      return getComputedStyle(node).borderColor;
}), 'rgb(185, 28, 28)', 'the invalid node border is not red');

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

test('Task 12 · an imported graph saves, enables and survives a page refresh with a consistent revision', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await clickCanvasAction('cvNewTree');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.saved === null && app.graph.nodes.length === 2);
});
  const newId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  /* 新建草稿未保存（behavior dirty）：导入先弹共享丢弃确认，确认后再选文件。
     导入的文档就是示例 fixture（含顶层 canvas），整图替换后的图与旧的「载入示例」一致。 */
  await clickCanvasAction('cvImport');
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await setImportPayload(EXAMPLE, 'example-graph.json');
  /* 新建草稿是 2 个节点：必须等导入文档真正替换后再保存。 */
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

  await clickCanvasAction('cvEnable');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.tree.enabled === true);
}, null, { timeout: 8000 });
  const enabledServer = await serverTree(newId);
  assert.equal(enabledServer.enabled, true);
  assert.equal(enabledServer.tree_revision, revision, 'enabling the imported graph changed its revision');

  /* Task 8 多图语义：启用不再停用其它图（基线图仍启用且仍是默认图），boot 优先
     打开默认图。把刚启用的导入图设为默认后再刷新，才应该回到它 —— 旧断言
     「启用后它就是唯一的启用图」已过时。 */
  await api('POST', '/admin/trees/' + encodeURIComponent(newId) + '/default');
  await openCanvas();
  const after = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    treeId: app.treeId,
    revision: app.tree.tree_revision,
    enabled: app.tree.enabled,
    nodes: app.graph.nodes.length,
    edges: app.graph.edges.length,
    kind: app.kind()
}));
});
  const status = await draftStatus();
  assert.equal(after.treeId, newId, 'refresh booted a different tree than the enabled one');
  assert.equal(after.revision, revision, 'refreshed tree revision differs from the GET revision');
  assert.equal(after.enabled, true);
  assert.equal(after.nodes, EXAMPLE_NODES);
  assert.equal(after.edges, EXAMPLE_EDGES);
  assert.equal(after.kind, 'clean');
  assert.equal(status.state, 'running');
  assert.ok(status.revision.includes(revision.slice(0, 10)),
    'lifecycle badge title does not show the refreshed revision');
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

  await clickCanvasAction('cvUndo');
  await expectStart(original);
  await clickCanvasAction('cvRedo');
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
    'logic-and', 'logic-or', 'target'
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
    'logic-and', 'logic-or', 'target'
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
    const { app } = await import('/console/routing/app.js');

    const G = RTGraph;
    /* 严格自包含：用基线画布的快照作为文档 runtime（模型 deepseek-v3 在快照内）。 */
    const runtime = JSON.parse(JSON.stringify(app.tree.runtime));
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
      kind: 'sr-routing-graph', version: 4, name: '极限图 200 节点 / 400 边',
      departments: [], enabled: false, default: false,
      timezone: 'Asia/Shanghai', sticky: { enabled: false, ttl_seconds: 1800 }, root: graph,
      backends: runtime.backends,
      jev: runtime.jev,
      jev_window: runtime.jev_window,
      time_periods: runtime.time_periods
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
  assert.ok(BASELINE_RUNTIME, 'bootstrap did not capture the baseline runtime');
  /* 严格自包含：导入文档必须自带四字段；这份图会真的启用并 /admin/try，
     所以用 QA 基线的 mock 快照（而不是仓库示例的 127.0.0.1 端点）。 */
  const doc = Object.assign({
    kind: 'sr-routing-graph',
    version: 4,
    departments: [], enabled: false, default: false,
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
}, JSON.parse(JSON.stringify(BASELINE_RUNTIME)));
  /* 新建未启用的组再导入：保存走 POST /admin/trees，不会覆盖已启用的基线组。 */
  await clickCanvasAction('cvNewTree');
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
    state: document.getElementById('cvLifecycle').dataset.state,
    positions: Object.fromEntries(app.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
}));
});
  assert.equal(after.revision, saved.revision, 'reload changed the tree revision');
  assert.equal(after.kind, 'clean');
  assert.equal(after.state, 'running');
  Object.keys(saved.positions).forEach(id => {
    assert.ok(after.positions[id], `${id} disappeared after reload`);
    assert.ok(Math.abs(after.positions[id].x - saved.positions[id].x) < 0.01, `${id} X moved after reload`);
    assert.ok(Math.abs(after.positions[id].y - saved.positions[id].y) < 0.01, `${id} Y moved after reload`);
});
  assert.ok(Math.abs(after.positions.start.x - moved.x) < 0.01, 'the dragged layout was not persisted');
});

test('Task 14 · a malformed v4 import with non-array fields is rejected before applying', async () => {
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
  const malformed = JSON.parse(JSON.stringify(EXAMPLE_ROOT));
  malformed.nodes.find(node => node.type === 'target').models = 5;
  const probe = await page.evaluate(async payload => {
    const RTGraph = await import('/console/routing/graph.js');

    const result = RTGraph.parseImport(JSON.stringify(payload));
    return {
      ok: result.ok,
      error: result.error,
      codes: (result.issues || []).map(issue => issue.code),
      zh: (result.issues || []).map(issue => issue.zh).join(' '),
      reason: ((result.issues || []).find(issue => issue.code === 'graph_parse_error') || {}).zh || ''
};

}, strictDocument({
    name: '非法 models',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root: malformed
}));
  assert.equal(probe.ok, false, 'parseImport accepted a non-array models field');
  assert.ok(probe.codes.includes('graph_parse_error'), 'missing graph_parse_error issue');
  assert.match(probe.zh, /models/);

  await clearToasts();
  await setImportPayload(strictDocument({
    name: '非法 models',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root: malformed
}), 'malformed-models.json');
  /* 被拒文件不得只报「1 个校验问题」：必须点名非数组的 models 字段。 */
  await expectImportReason(probe.reason);
  assert.deepEqual(await draftOf(), kept, 'a rejected malformed v4 import changed the draft');
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
    await clickCanvasAction('cvSave');
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
      importDisabled: document.getElementById('cvImport').disabled,
      primaryBusy: document.getElementById('cvEnable').getAttribute('aria-busy'),
      primaryDisabled: document.getElementById('cvEnable').disabled,
      primaryLabel: document.getElementById('cvEnableLabel').textContent,
      expectedSaving: t('cv.saving')
}));
});
    assert.equal(busy.saving, true);
    assert.equal(busy.disabled, true, 'the save button stayed enabled while saving');
    assert.equal(busy.ariaBusy, 'true', 'the save button did not expose aria-busy');
    assert.equal(busy.label, busy.expectedLabel, 'the save button did not show the busy label');
    assert.equal(busy.newDisabled, true, 'New stayed enabled while saving');
    assert.equal(busy.importDisabled, true, 'Import stayed enabled while saving');
    assert.equal(busy.primaryBusy, 'true', 'the primary action did not expose aria-busy while saving');
    assert.equal(busy.primaryDisabled, true, 'the primary action stayed enabled while saving');
    assert.equal(busy.primaryLabel, busy.expectedSaving, 'the primary action did not show the busy label');

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

      const other = RTGraph.createMinimalTree('qwen3-8b', app.tree.runtime);
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

test('Task 8 · a tree switch that loses to an import of the example document is discarded', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const baseline = await serverTree(TREE_ID);
  const STALE_ID = 'qa-example-import-stale';
  const matcher = url => url.pathname === '/admin/trees/' + STALE_ID;
  let pending = null;
  await page.route(matcher, route => {
    if (route.request().method() !== 'GET') return route.continue();
    pending = route;
  });
  try {
    await page.evaluate(async id => {
      const { app } = await import('/console/routing/app.js');
      window.__exampleImportRace = app.selectTree(id);
    }, STALE_ID);
    const deadline = Date.now() + 5000;
    while (!pending && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(pending, 'the tree switch GET was not issued');

    /* 基线草稿是 clean 的：导入不弹丢弃确认，直接在文件选择器里放示例文档。
       导入同样整图替换并在应用前 bump loadSeq（applyImport）。 */
    const chooser = page.waitForEvent('filechooser');
    await clickCanvasAction('cvImport');
    await (await chooser).setFiles({
      name: 'example-graph.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify(EXAMPLE))
    });
    await waitFor(async expected => {
      const { app } = await import('/console/routing/app.js');
      const node = app.graph && app.graph.nodes.find(item => item.id === 'start');
      /* 示例文档自带 local runtime：相对已保存的 mock 基线是 behavior dirty。 */
      return app.treeId === expected.id && app.tree.name === expected.name &&
        app.kind() === 'behavior' && !!node &&
        node.position.x === expected.start.x && node.position.y === expected.start.y;
    }, { id: TREE_ID, name: EXAMPLE.name, start: EXAMPLE_START }, { timeout: 8000 });

    /* 导入已落地，旧 switch 的回包现在才到：必须被 loadSeq 丢弃。 */
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
      const value = await window.__exampleImportRace;
      delete window.__exampleImportRace;
      return [value];
    });
    assert.deepEqual(results, [false],
      'a switch that lost to an example document import did not report itself as discarded');
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
      name: EXAMPLE.name,
      kind: 'behavior',
      nodes: EXAMPLE_NODES,
      edges: EXAMPLE_EDGES,
      start: EXAMPLE_START
    }, 'a stale tree switch overwrote the imported example graph');
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
    await clickCanvasAction('cvNewTree');
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

test('Task 14 · New and Import stay inert until the canvas is booted', async () => {
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
    for (const id of ['cvNewTree', 'cvImport']) {
      await clearToasts();
      await clickCanvasAction(id);
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
  const root = JSON.parse(JSON.stringify(EXAMPLE_ROOT));
  root.edges[0].target_port = 'in_9999';
  const invalidSlotDocument = strictDocument({
    name: '非法槽位',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root,
  });
  await clearToasts();
  await setImportPayload(invalidSlotDocument, 'invalid-slot-in-9999.json');
  /* 被拒原因必须点名不存在的槽位，而不是只报校验问题数量。 */
  await expectImportReason(await parseImportReason(invalidSlotDocument));
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
    const { app } = await import('/console/routing/app.js');
    return (RTGraph.createMinimalTree('qwen3.8-flash-sft', app.tree.runtime));
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
    const document = Object.assign({
      kind: 'sr-routing-graph', version: 4, name: tree.name,
      departments: [], enabled: false, default: false,
      timezone: tree.timezone, sticky: tree.sticky, root: tree.root,
}, runtimeDocumentFields(base.runtime));
    await setImportPayload(document, 'invalid-contract.json');
    /* 每种非法契约都要显示它自己的具体原因（未知类型/未知字段/非法时区）。 */
    await expectImportReason(await parseImportReason(document));
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
    const { app } = await import('/console/routing/app.js');
    return (RTGraph.createMinimalTree('qwen3.8-flash-sft', app.tree.runtime));
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
    const document = Object.assign({
      kind: 'sr-routing-graph', version: 4, name: tree.name,
      departments: [], enabled: false, default: false,
      timezone: tree.timezone, sticky: tree.sticky, root: tree.root,
}, runtimeDocumentFields(base.runtime));
    await setImportPayload(document, 'invalid-envelope-value.json');
    /* envelope 字段类型非法同样要显示具体原因（sticky.enabled / timezone）。 */
    await expectImportReason(await parseImportReason(document));
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
  assert.equal(await slot.locator('[data-start-condition]').count(), 0,
    'the read-only start slot still renders a condition chip');
  assert.equal(await slot.getAttribute('tabindex'), '-1',
    'the condition-less start slot is not reachable for focus restore');
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
  assert.equal(await slot.locator('[data-start-condition]').count(), 0,
    'clearing the negation brought the start condition chip back');
  assert.equal(await slot.getAttribute('tabindex'), '-1',
    'the condition-less start slot is not reachable for focus restore');
  await clickCanvasAction('cvUndo');
  await waitEdgeCondition(startEdge.id, { op: 'is', value: false });
});

test('the start slot carries no condition chip and no hover feedback', async () => {
  await openCanvas();
  const startEdge = (await appSnapshot()).edges.find(edge => edge.source === 'start');
  const slot = page.locator(`#cvNodes [data-edge="${startEdge.id}"]`);
  assert.equal(await slot.locator('[data-start-condition], [data-cond-edge]').count(), 0,
    'the start slot still exposes a condition control');
  const style = () => slot.evaluate(element => {
    const value = getComputedStyle(element);
    return { backgroundColor: value.backgroundColor, cursor: value.cursor };
});

  await page.mouse.move(0, 0);
  const resting = await style();
  assert.notEqual(resting.cursor, 'pointer', 'the start slot looks clickable');
  await slot.hover();
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

  const focused = await page.evaluate(() => {
    const element = document.activeElement;
    return {
      tag: element ? element.tagName : '',
      edge: element && element.dataset ? element.dataset.edge || '' : '',
      tabIndex: element ? element.tabIndex : null
};
});
  assert.deepEqual(focused, { tag: 'DIV', edge: startEdge.id, tabIndex: -1 });
  assert.equal(await page.locator(`#cvNodes [data-cond-edge="${startEdge.id}"]`).count(), 0);

  await clickCanvasAction('cvUndo');
  await waitEdgeCondition(startEdge.id, { op: 'is', value: false });
});

test('replay condition labels omit the start edge and keep direct/negated text', async () => {
  await prepareTryCanvas();
  const edges = (await appSnapshot()).edges;
  const startEdge = edges.find(edge => edge.source === 'start');
  const conditionEdge = edges.find(edge => edge.source === 'cond-text');
  await runTrySample(0);
  const startTitle = await page.locator(`#cvNodes [data-edge="${startEdge.id}"]`).getAttribute('title');
  assert.doesNotMatch(startTitle, /总是通过|Always passes/,
    'the start slot still describes an always-pass condition');
  assert.doesNotMatch(startTitle, /= true/);
  assert.doesNotMatch(startTitle, / {2,}/,
    'the start slot title keeps a gap where the condition label used to be');

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
      await page.locator(`#cvNodes [data-edge="${startEdge.id}"] [data-start-condition]`).count(),
      0,
      'the start slot still renders an English condition chip'
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



test('P0 route-check copy is consistent in Chinese and English', async () => {
  await ensureBaselineTree();
  await openCanvas();
  for (const lang of ['zh', 'en']) {
    await page.evaluate(async next => {
      const { setLang } = await import('/console/i18n.js');
      setLang(next);
    }, lang);
    const copy = await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return {
        top: document.getElementById('cvTryBtn').textContent.trim(),
        run: document.getElementById('cvTryRun').textContent.trim(),
        placeholder: document.getElementById('cvTryInput').placeholder,
        expectedTop: t('cv.try'),
        expectedRun: t('cv.tryRun'),
        bar: document.getElementById('cvTryBar').textContent.replace(/\s+/g, ' ').trim()
      };
    });
    assert.equal(copy.top, copy.expectedTop);
    assert.equal(copy.run, copy.expectedRun);
    if (lang === 'zh') {
      assert.equal(copy.top, '检查路由');
      assert.equal(copy.run, '运行检查');
      assert.match(copy.placeholder, /检查路由/);
      assert.doesNotMatch(copy.bar, /试算/);
    } else {
      assert.equal(copy.top, 'Check routing');
      assert.equal(copy.run, 'Run check');
      assert.match(copy.placeholder, /check routing/i);
      assert.doesNotMatch(copy.bar, /Try run|Run try/);
    }
  }
  await page.evaluate(async () => {
    const { setLang } = await import('/console/i18n.js');
    setLang('zh');
  });
});


test('Task 9 · departments: route-set try-run picks the department graph and draft keeps the canvas', async () => {
  /* Task 9 的真实浏览器场景：默认图 main(fast) + 部门图 finance(strong, 财务部) 由
     真实管理 API 建立（自包含运行快照，无问题节点因此不触发 JEV）。
     route_set 模式必须只发 text/session_id/可选 department；draft 模式必须继续
     测当前画布（此处是 finance），而不是默认图。
     两张图使用不同的开始/目标/边 ID（<id>-start / <id>-target / <id>-edge）：
     路由集回放一旦被投影到当前草稿画布，错误的高亮、节点重排和 dirty 才可观察。 */
  const MAIN = 'main';
  const FINANCE = 'finance';
  const directDocument = (id, model, departments) => {
    const start = id + '-start';
    const target = id + '-target';
    const edge = id + '-edge';
    return {
      id: id,
      name: id === MAIN ? '默认图' : '财务图',
      departments: departments.slice(),
      timezone: 'Asia/Shanghai',
      sticky: { enabled: false, ttl_seconds: 1800 },
      root: {
        id: 'root', type: 'graph',
        nodes: [
          { id: start, type: 'start', title: id + ' 起始节点' },
          { id: target, type: 'target', title: id + ' 独立目标', priority: 1,
            models: [{ model: model, reasoning: null }] }
        ],
        edges: [{
          id: edge, source: start, source_port: 'out',
          target: target, target_port: 'in_1', condition: null
        }],
        error_fallback: target
      },
      backends: {
        providers: [{
          name: 'local', base_url: mockBase + '/v1', key_env: '', reasoning_format: 'openai'
        }],
        models: [{ id: model, upstream: model, provider: 'local', profile: {} }]
      },
      jev: null,
      jev_window: { input_tokens: 1024, context_tokens: 4096 },
      time_periods: []
    };
  };
  const ensureDirectGraph = async (id, model, departments) => {
    /* 早前的多图生命周期用例会用同一 id 复制示例图（并留下停用状态）：这里把文档
       整体改写成自包含的直连图（fast/strong + 财务部），而不是因为 id 已存在就跳过。 */
    const doc = directDocument(id, model, departments);
    const listed = (await api('GET', '/admin/trees')).trees;
    if (!listed.some(tree => tree.id === id)) {
      await api('POST', '/admin/trees', doc);
      return;
    }
    const existing = await serverTree(id);
    await api('PUT', '/admin/trees/' + id, {
      ...doc,
      base_document_sha256: existing.document_sha256
    });
  };
  const current = async () => page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    return ({
      treeId: app.treeId,
      kind: app.changeKind,
      enabled: !!(app.tree && app.tree.enabled),
      departments: (app.tree && app.tree.departments || []).slice(),
      running: !!app.tryRunning,
      replayText: app.replay ? app.replay.text : ''
    });
  });
  /* 画布状态快照：graph.js 没有导出 editableCanonical，这里用整棵内部树（含节点
     position）的稳定序列化做更严格的等价物——任何重排、增删或 dirty 都会变。 */
  const canvasState = () => page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const RTGraph = await import('/console/routing/graph.js');
    return {
      canonical: RTGraph.stableStringify(app.tree),
      dirty: app.changeKind,
      replayNodes: document.querySelectorAll('#cvNodes .is-replay').length,
      finalEdges: document.querySelectorAll('#cvEdges .is-final-edge').length
    };
  });
  const resultFinalLabel = () => page.evaluate(() => {
    const el = document.querySelector('#cvTryResult .rtc-try-final');
    return el ? el.textContent.trim() : '';
  });
  const resultActions = () => page.evaluate(() => {
    const waves = document.getElementById('cvTryWavesBtn');
    return {
      rerun: !!document.getElementById('cvTryRerun'),
      clear: !!document.getElementById('cvTryClear'),
      waves: !!waves,
      wavesVisible: !!(waves && (waves.offsetWidth || waves.offsetHeight ||
        waves.getClientRects().length))
    };
  });
  try {
    await ensureBaselineTree();
    await ensureDirectGraph(MAIN, 'fast', []);
    await ensureDirectGraph(FINANCE, 'strong', ['财务部']);

    /* 路由集 = main(默认) + finance(财务部)；先停用基线图得到合法空路由集，
       再按「第一张启用图自动默认」启用 main，最后启用 finance。 */
    await api('POST', '/admin/trees/' + TREE_ID + '/disable');
    await api('POST', '/admin/trees/' + MAIN + '/enable');
    await api('POST', '/admin/trees/' + FINANCE + '/enable');
    const routeSet = await api('GET', '/admin/trees');
    assert.equal(routeSet.graph_status.default_tree_id, MAIN);
    assert.deepEqual(routeSet.graph_status.enabled_tree_ids.slice().sort(), [FINANCE, MAIN]);

    /* boot 打开默认图 main；切到 finance，让 draft 模式测「当前画布 ≠ 默认图」。 */
    await openCanvas();
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.treeId === id && app.changeKind === 'clean';
    }, MAIN, { timeout: 10000 });
    await page.locator('#cvTreeSelect').selectOption(FINANCE);
    await waitFor(async id => {
      const { app } = await import('/console/routing/app.js');
      return app.treeId === id && app.changeKind === 'clean' && !!(app.tree && app.tree.enabled);
    }, FINANCE, { timeout: 10000 });
    assert.deepEqual((await current()).departments, ['财务部']);

    /* 范围选择默认 draft，且部门输入在 draft 模式隐藏。 */
    assert.equal(await page.locator('#cvTryScope').inputValue(), 'draft',
      'the try scope must default to the current draft');
    assert.equal(await page.locator('#cvTryDepartment').isVisible(), false,
      'the department input must be hidden in draft mode');

    /* 打开 finance 草稿后的干净画布快照：路由集结果绝不能投影或修改它。 */
    const cleanCanvas = await canvasState();
    assert.equal(cleanCanvas.dirty, 'clean');
    assert.equal(cleanCanvas.replayNodes, 0);
    assert.equal(cleanCanvas.finalEdges, 0);

    /* 1) route_set + 财务部 → 部门图 finance / graph_selection=department / 模型 strong，
       请求体只含 text/session_id/department（无 root/tree_id/canvas/运行字段）。 */
    const captured = [];
    const matcher = url => url.pathname === '/admin/try';
    const handler = async route => {
      captured.push(route.request().postDataJSON());
      await route.continue();
    };
    await page.route(matcher, handler);
    try {
      await page.locator('#cvTryScope').selectOption('route_set');
      assert.equal(await page.locator('#cvTryDepartment').isVisible(), true,
        'route_set mode must reveal the department input');
      await page.locator('#cvTryDepartment').fill('财务部');
      await page.locator('#cvTryInput').fill('部门路由试算');
      await page.locator('#cvTryRun').click();
      await waitFor(async text => {
        const { app } = await import('/console/routing/app.js');
        return !app.tryRunning && app.replay && app.replay.text === text;
      }, '部门路由试算', { timeout: 15000 });
    } finally {
      await page.unroute(matcher, handler);
    }
    let snap = await trySnapshot();
    assertIncludes(snap.resultText, FINANCE,
      'the route-set result does not report the selected tree_id');
    assertIncludes(snap.resultText, 'department',
      'the route-set result does not report graph_selection=department');
    assertIncludes(snap.resultText, 'strong',
      'the department graph model is not part of the try outcome');
    /* 路由集结果只显示原始图/节点 ID：当前画布 finance 的标题不得混进来。 */
    assert.equal(await resultFinalLabel(), FINANCE + '-target',
      'route-set result rendered the current canvas title instead of the raw target id');
    assert.equal(snap.resultText.includes('独立目标'), false,
      'route-set result read target/candidate names from the current canvas');
    const departmentActions = await resultActions();
    assert.equal(departmentActions.rerun, true, 'route-set result lost the rerun action');
    assert.equal(departmentActions.clear, true, 'route-set result lost the clear action');
    assert.equal(departmentActions.wavesVisible, false,
      'route-set result must not offer by-waves layout for the current canvas');
    const afterDepartment = await canvasState();
    assert.equal(afterDepartment.replayNodes, 0,
      'route-set replay painted the current finance canvas (department trace)');
    assert.equal(afterDepartment.finalEdges, 0,
      'route-set replay painted final edges on the current finance canvas (department trace)');
    assert.equal(afterDepartment.canonical, cleanCanvas.canonical,
      'route-set try-run mutated the open draft canvas');
    assert.equal(afterDepartment.dirty, 'clean',
      'route-set try-run dirtied the open draft');
    const departmentPayload = captured[captured.length - 1];
    assert.deepEqual(Object.keys(departmentPayload).sort(), ['department', 'session_id', 'text'],
      'route-set try payload leaked draft/run fields instead of sending only text+session_id+department');
    for (const field of ['root', 'tree_id', 'canvas', 'timezone', 'sticky',
      'backends', 'jev', 'jev_window', 'time_periods', 'now']) {
      assert.equal(Object.prototype.hasOwnProperty.call(departmentPayload, field), false,
        'route-set try payload must not carry ' + field);
    }

    /* 2) 未知部门 → 默认图 main / graph_selection=default / 模型 fast。
       当前画布是 finance，回放却属于 main：结果必须如实指 main，画布毫无动静。 */
    const beforeDefault = await canvasState();
    await page.locator('#cvTryDepartment').fill('未知部门');
    await page.locator('#cvTryInput').fill('未知部门试算');
    await page.locator('#cvTryRun').click();
    await waitFor(async text => {
      const { app } = await import('/console/routing/app.js');
      return !app.tryRunning && app.replay && app.replay.text === text;
    }, '未知部门试算', { timeout: 15000 });
    snap = await trySnapshot();
    assertIncludes(snap.resultText, MAIN, 'unknown department did not fall back to the default graph');
    assertIncludes(snap.resultText, 'default', 'unknown department did not report graph_selection=default');
    assertIncludes(snap.resultText, 'fast', 'the default graph model is not part of the try outcome');
    assertIncludes(snap.resultText, MAIN + '-target',
      'the default-graph result did not report the main target');
    assert.equal(snap.resultText.includes(FINANCE + '-target'), false,
      'the default-graph result leaked the finance canvas target');
    const afterDefault = await canvasState();
    assert.equal(afterDefault.replayNodes, 0,
      'the main trace painted the finance canvas nodes');
    assert.equal(afterDefault.finalEdges, 0,
      'the main trace painted final edges on the finance canvas');
    assert.equal(afterDefault.canonical, beforeDefault.canonical,
      'the default-graph route-set result moved or mutated the finance canvas');
    assert.equal(afterDefault.dirty, 'clean',
      'the default-graph route-set result dirtied the finance canvas');
    assert.equal((await resultActions()).wavesVisible, false,
      'default-graph route-set result offered by-waves layout on the finance canvas');

    /* 2b) 防御守卫：即使绕过隐藏按钮直接调用模块，也不能按 main 的波次重排 finance。 */
    const blocked = await page.evaluate(async () => {
      const RTTryRun = await import('/console/routing/tryrun.js');
      const { app } = await import('/console/routing/app.js');
      const { t } = await import('/console/i18n.js');
      const originalToast = app.toast;
      const toasts = [];
      app.toast = (message, tone) => {
        toasts.push({ message: String(message), tone: String(tone || '') });
      };
      let result = null;
      try {
        result = RTTryRun.layoutByWaves();
      } finally {
        app.toast = originalToast;
      }
      return { result: result, toasts: toasts, expected: t('cv.tryRouteSetLayoutBlocked') };
    });
    assert.equal(blocked.result, false, 'layoutByWaves accepted a route-set replay');
    assert.ok(blocked.toasts.some(item => item.message === blocked.expected && item.tone === 'warn'),
      'layoutByWaves on a route-set replay must warn with cv.tryRouteSetLayoutBlocked');
    const afterBlocked = await canvasState();
    assert.equal(afterBlocked.canonical, beforeDefault.canonical,
      'the blocked by-waves layout still moved the finance canvas');

    /* 3) route_set 空部门 → 同样走默认图（空部门等价于默认图）。 */
    await page.locator('#cvTryDepartment').fill('');
    await page.locator('#cvTryInput').fill('空部门试算');
    await page.locator('#cvTryRun').click();
    await waitFor(async text => {
      const { app } = await import('/console/routing/app.js');
      return !app.tryRunning && app.replay && app.replay.text === text;
    }, '空部门试算', { timeout: 15000 });
    snap = await trySnapshot();
    assertIncludes(snap.resultText, MAIN, 'an empty department must use the default graph');
    assertIncludes(snap.resultText, 'default', 'an empty department must report graph_selection=default');
    assert.equal((await resultActions()).wavesVisible, false,
      'an empty-department route-set result offered by-waves layout on the finance canvas');
    assert.equal((await canvasState()).canonical, beforeDefault.canonical,
      'the empty-department route-set result mutated the finance canvas');

    /* 4) 切回 draft：测当前画布 finance（模型 strong），不是默认图 main(fast)；
       请求体重新携带草稿 root/runtime，且不再携带 department。
       草稿回放必须逐字保留：画布高亮、画布标题、按波次整理入口都还在。 */
    const draftCaptured = [];
    const draftHandler = async route => {
      draftCaptured.push(route.request().postDataJSON());
      await route.continue();
    };
    await page.route(matcher, draftHandler);
    try {
      await page.locator('#cvTryScope').selectOption('draft');
      assert.equal(await page.locator('#cvTryDepartment').isVisible(), false,
        'the department input must hide again in draft mode');
      await page.locator('#cvTryInput').fill('草稿画布试算');
      await page.locator('#cvTryRun').click();
      await waitFor(async text => {
        const { app } = await import('/console/routing/app.js');
        return !app.tryRunning && app.replay && app.replay.text === text;
      }, '草稿画布试算', { timeout: 15000 });
    } finally {
      await page.unroute(matcher, draftHandler);
    }
    snap = await trySnapshot();
    assertIncludes(snap.resultText, 'draft',
      'draft mode must return the canvas snapshot id (draft), not a route-set tree_id');
    assertIncludes(snap.resultText, 'strong',
      'draft mode did not exercise the current canvas (finance/strong)');
    assert.equal(snap.resultText.includes('fast'), false,
      'draft mode fell back to the default graph main(fast) instead of the current canvas');
    const draftCanvas = await canvasState();
    assert.ok(draftCanvas.replayNodes > 0,
      'draft replay stopped highlighting the current canvas nodes');
    assert.ok(draftCanvas.finalEdges > 0,
      'draft replay stopped highlighting the final edge on the current canvas');
    assert.equal(await resultFinalLabel(), FINANCE + ' 独立目标',
      'draft replay must keep rendering node names from the current canvas');
    assert.equal(draftCanvas.dirty, 'clean', 'draft replay dirtied the current canvas');
    const draftActions = await resultActions();
    assert.equal(draftActions.wavesVisible, true,
      'draft replay lost the by-waves layout action');

    /* 5) 草稿回放还在画布上时切到 route_set 再跑：前一次草稿高亮必须被清掉，
       画布只剩静态图，路由集结果也不提供按波次整理。 */
    await page.locator('#cvTryScope').selectOption('route_set');
    await page.locator('#cvTryDepartment').fill('财务部');
    await page.locator('#cvTryInput').fill('清理草稿高亮');
    await page.locator('#cvTryRun').click();
    await waitFor(async text => {
      const { app } = await import('/console/routing/app.js');
      return !app.tryRunning && app.replay && app.replay.text === text;
    }, '清理草稿高亮', { timeout: 15000 });
    const cleared = await canvasState();
    assert.equal(cleared.replayNodes, 0,
      'a stale draft replay highlight survived the route-set try-run');
    assert.equal(cleared.finalEdges, 0,
      'a stale draft replay final edge survived the route-set try-run');
    assert.equal(cleared.dirty, 'clean',
      'the route-set try-run dirtied the canvas after a draft replay');
    assert.equal((await resultActions()).wavesVisible, false,
      'route-set result offered by-waves layout after a draft replay');

    /* 6) 再切回 draft：草稿回放与按波次整理仍然逐字保留。 */
    await page.locator('#cvTryScope').selectOption('draft');
    await page.locator('#cvTryInput').fill('草稿画布复测');
    await page.locator('#cvTryRun').click();
    await waitFor(async text => {
      const { app } = await import('/console/routing/app.js');
      return !app.tryRunning && app.replay && app.replay.text === text;
    }, '草稿画布复测', { timeout: 15000 });
    const repainted = await canvasState();
    assert.ok(repainted.replayNodes > 0,
      'draft replay did not repaint the canvas after a route-set run');
    assert.ok(repainted.finalEdges > 0,
      'draft replay did not repaint the final edge after a route-set run');

    /* 按波次整理仍然工作：只改布局、把节点排进波次列，并清掉陈旧回放。 */
    const positionsBefore = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return Object.fromEntries(app.graph.nodes.map(node => [node.id,
        { x: node.position.x, y: node.position.y }]));
    });
    const waveToasts = await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return { done: t('cv.tryWavesLayoutDone'), same: t('cv.tryWavesLayoutSame') };
    });
    await page.locator('#cvTryWavesBtn').click();
    await page.waitForFunction(expected => {
      const text = document.getElementById('cvToast').textContent;
      return text.indexOf(expected.done) >= 0 || text.indexOf(expected.same) >= 0;
    }, waveToasts, { timeout: 8000 });
    const waveToast = await toastText();
    assert.ok(waveToast.includes(waveToasts.done) || waveToast.includes(waveToasts.same),
      'by-waves layout no longer reports success for a draft replay: ' + waveToast);
    const afterWaves = await page.evaluate(async () => {
      const { app } = await import('/console/routing/app.js');
      return {
        positions: Object.fromEntries(app.graph.nodes.map(node => [node.id,
          { x: node.position.x, y: node.position.y }])),
        kind: app.changeKind,
        replay: app.replay,
        resultHidden: document.getElementById('cvTryResult').hidden
      };
    });
    assert.equal(afterWaves.kind, 'layout', 'by-waves layout was not a layout-only edit');
    assert.equal(afterWaves.replay, null, 'by-waves layout left a stale draft replay');
    assert.equal(afterWaves.resultHidden, true, 'by-waves layout left the draft result panel open');
    const startAfter = afterWaves.positions[FINANCE + '-start'];
    const targetAfter = afterWaves.positions[FINANCE + '-target'];
    assert.ok(startAfter && targetAfter, 'by-waves layout dropped a canvas node');
    assert.equal(startAfter.x, targetAfter.x,
      'by-waves layout did not put wave-0 nodes in the same column');
    assert.notEqual(startAfter.y, targetAfter.y,
      'by-waves layout stacked wave-0 nodes on top of each other');
    const startBefore = positionsBefore[FINANCE + '-start'];
    const targetBefore = positionsBefore[FINANCE + '-target'];
    assert.ok(startAfter.x !== startBefore.x || startAfter.y !== startBefore.y ||
      targetAfter.x !== targetBefore.x || targetAfter.y !== targetBefore.y,
      'by-waves layout did not move any node');
    const draftPayload = draftCaptured[draftCaptured.length - 1];
    assert.ok(draftPayload && draftPayload.root, 'draft try payload lost the canvas root');
    assert.equal(Object.prototype.hasOwnProperty.call(draftPayload, 'department'), false,
      'draft try payload must not send a department');
    assert.equal(Object.prototype.hasOwnProperty.call(draftPayload, 'tree_id'), false,
      'draft try payload must stay a self-contained canvas envelope');
  } finally {
    await ensureBaselineTree();
  }
});

await runIfMain(import.meta.url);

test('inspector · target model row hints when the model declares no reasoning tiers', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await clickNode('power-target');
  await waitInspectorBranch('node', 'power-target');
  const reasoningSelect = page.locator('#cvInspectorBody [data-model-row] [data-model-field="reasoning"]').first();
  const modelSelect = page.locator('#cvInspectorBody [data-model-row] [data-model-field="model"]').first();
  assert.equal(await page.locator('#cvInspectorBody [data-reasoning-hint]').count(), 0,
    'a declared model row must not show the undeclared-reasoning hint');

  /* 切换所选模型即时生效：档位只剩「不指定」，并出现可操作提示（无需保存）。 */
  await modelSelect.selectOption('tierless-model');
  await page.waitForSelector('#cvInspectorBody [data-reasoning-hint]', { timeout: 4000 });
  const hint = page.locator('#cvInspectorBody [data-reasoning-hint]').first();
  assert.equal(await hint.count(), 1, 'an undeclared-reasoning model row shows no hint');
  const hintText = (await hint.innerText()).trim();
  assert.ok(hintText.length > 0, 'the undeclared-reasoning hint is empty');
  const options = await reasoningSelect.locator('option').allInnerTexts();
  assert.equal(options.length, 1, 'an undeclared model must only offer the unspecified tier');

  /* 切回已声明模型：提示消失、档位可选。 */
  const declared = await page.evaluate(async () => {
    const { app } = await import('/console/routing/app.js');
    const model = app.models.find(item => item && item.id !== 'tierless-model');
    return model ? model.id : null;
});
  await modelSelect.selectOption(declared);
  await page.waitForFunction(
    () => document.getElementById('cvInspectorBody').querySelectorAll('[data-reasoning-hint]').length === 0,
    null, { timeout: 4000 });
  const declaredOptions = await reasoningSelect.locator('option').allInnerTexts();
  assert.ok(declaredOptions.length > 1, 'a declared model must offer reasoning tiers');
});
