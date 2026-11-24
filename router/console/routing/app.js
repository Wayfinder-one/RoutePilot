/* RTApp · 决策组画布页装配（规格 §6.1–§6.3 / §6.10–§6.12）
   ------------------------------------------------------------------
   本文件只做 DOM 装配、事件绑定与渲染：
     · DraftStore 持有草稿、保存基线与撤销历史；
     · RoutingController 持有保存/启停/切换/导入导出的 API 编排与唯一 saving；
     · canvas / cards / inspector / condpop / tryrun 各自成模块，通过
       configure*() 接收 store 与显式函数依赖（不使用 window 桥接）。
   所有编辑仍走一次 store.change(kind, mutate) = 一个历史项。
   ------------------------------------------------------------------ */
import '../nav.js';
import { api, apiErrText } from '../api.js';
import { LANG, onLangChange, t, tf } from '../i18n.js';
import * as RTGraph from './graph.js';
import { DraftStore } from './draft.js';
import { RoutingController } from './controller.js';
import { addRuntimeModel } from './runtime.js';
import { escape as esc, nodeName, render as renderCards } from './cards.js';
import { create as createCanvas } from './canvas.js';
import {
  configureCondPop,
  close as closeCondPop,
  conditionText,
  isOpen as isCondPopOpen,
  open as openCondPop,
  refresh as refreshCondPop,
} from './condpop.js';
import { configureInspector, render as renderInspectorBranch } from './inspector.js';
import {
  apply as applyTryRun,
  configureTryRun,
  current as currentReplay,
  init as initTryRun,
  refresh as refreshTryRun,
  rebuild as rebuildTryRun,
  reset as resetTryRun,
} from './tryrun.js';

const $ = (sel, scope) => (scope || document).querySelector(sel);
/* 日志快照：URL 带 ?request=<id> 时进入只读回放，不装配任何编辑入口。 */
const SNAPSHOT_REQUEST = new URLSearchParams(window.location.search).get('request') || '';
const isLogSnapshot = () => SNAPSHOT_REQUEST !== '';
const clone = value => JSON.parse(JSON.stringify(value));
const icon = (name, cls) =>
  '<svg class="rtc-ic' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#ri-' + name + '"/></svg>';

/* ── 节点目录（节点库四组 + 快速创建共用） ─────────────────────── */
const CATALOG = [
  {
    id: 'question', tone: 'question', labelKey: 'cv.group.question',
    items: [
      { kind: 'question-choice', icon: 'question', labelKey: 'cv.node.question-choice', descKey: 'cv.desc.question-choice' },
      { kind: 'question-noul', icon: 'question', labelKey: 'cv.node.question-noul', descKey: 'cv.desc.question-noul' },
      { kind: 'question-score', icon: 'question', labelKey: 'cv.node.question-score', descKey: 'cv.desc.question-score' }
    ]
  },
  {
    id: 'logic', tone: 'logic', labelKey: 'cv.group.logic',
    items: [
      { kind: 'logic-and', icon: 'logic', labelKey: 'cv.node.logic-and', descKey: 'cv.desc.logic-and' },
      { kind: 'logic-or', icon: 'logic', labelKey: 'cv.node.logic-or', descKey: 'cv.desc.logic-or' }
    ]
  },
  {
    id: 'condition', tone: 'condition', labelKey: 'cv.group.condition',
    items: [
      { kind: 'condition-text', icon: 'condition', labelKey: 'cv.node.condition-text', descKey: 'cv.desc.condition-text' },
      { kind: 'condition-periods', icon: 'condition', labelKey: 'cv.node.condition-periods', descKey: 'cv.desc.condition-periods' }
    ]
  },
  {
    id: 'target', tone: 'target', labelKey: 'cv.group.target',
    items: [
      { kind: 'target', icon: 'target', labelKey: 'cv.node.target', descKey: 'cv.desc.target' }
    ]
  }
];
/* 快速创建 = 任何能从输出端口连入 in_1 的节点类型（所有入槽都是布尔槽位）。 */
const QUICK_KINDS = CATALOG.reduce((all, group) => all.concat(group.items), [])
  .filter(item => item.kind.indexOf('condition-') !== 0 && item.kind !== 'start');

function catalogItem(kind) {
  for (let groupIndex = 0; groupIndex < CATALOG.length; groupIndex += 1) {
    const found = CATALOG[groupIndex].items.find(item => item.kind === kind);
    if (found) return found;
  }
  return null;
}

function kindLabel(kind) {
  const item = catalogItem(kind);
  return item ? t(item.labelKey) : kind;
}

/* ── 目录：数据库目录（/admin/*）与当前树内嵌运行快照 ─────────────────
   严格自包含画布的编辑语义：
     · 检查器「模型」下拉来自数据库模型目录（选择 target 模型时把目录项补进
       画布快照；同 ID 已存在时不覆盖，数据库差异只作为 /admin/try 的检查提示）；
     · 「JEV」下拉来自数据库 JEV 目录（应用时原子替换 runtime.jev/jev_window）；
     · 时段复选框以画布 runtime.time_periods 为准 —— 画布引用未声明的时段会被
       服务端拒绝，所以不能拿全局时段目录当选项源。
   目录在 boot 时并行加载（app.catalog），新画布创建前必须 await。 */
function currentRuntime() {
  if (!store.tree || typeof RTGraph.runtimeBehaviorView !== 'function') return null;
  return RTGraph.runtimeBehaviorView(store.tree);
}

function providerFormatOf(runtime, providerName) {
  const providers = (runtime && runtime.backends && runtime.backends.providers) || [];
  const provider = providers.find(item => item && item.name === providerName);
  const format = provider && provider.reasoning_format;
  return typeof format === 'string' && format ? format : 'openai';
}

/* runtime 的 provider.reasoning_format 决定未显式声明 format 的模型协议；
   profile.reasoning.efforts 是「档位 → 协议拼写」map，检查器消费的是
   /admin/models 归一化后的数组形状（efforts: [{id, wire}]）。 */
function runtimeModelOption(model, providerFormat) {
  const source = model && typeof model === 'object' ? model : {};
  const profile = source.profile && typeof source.profile === 'object' ? source.profile : {};
  const format = typeof providerFormat === 'string' && providerFormat ? providerFormat : 'openai';
  const declared = profile.reasoning;
  let reasoning;
  if (declared === false) {
    reasoning = { declared: true, supported: false, format: format, efforts: [], default: null };
  } else if (declared && typeof declared === 'object' && !Array.isArray(declared)) {
    const efforts = declared.efforts && typeof declared.efforts === 'object' &&
      !Array.isArray(declared.efforts) ? declared.efforts : {};
    reasoning = {
      declared: true,
      supported: true,
      format: typeof declared.format === 'string' && declared.format ? declared.format : format,
      efforts: Object.keys(efforts).map(level => ({ id: level, wire: efforts[level] })),
      default: typeof declared.default === 'string' ? declared.default : null
    };
  } else {
    reasoning = { declared: false, supported: null, format: format, efforts: [], default: null };
  }
  return {
    id: source.id,
    upstream: source.upstream,
    provider: source.provider,
    profile: profile,
    capability: typeof profile.capability === 'string' ? profile.capability.trim() : '',
    reasoning: reasoning
  };
}

function runtimeModels() {
  const runtime = currentRuntime();
  if (!runtime) return null;
  const models = (runtime.backends && Array.isArray(runtime.backends.models))
    ? runtime.backends.models : [];
  return models.map(model =>
    runtimeModelOption(model, providerFormatOf(runtime, model && model.provider)));
}

/* 数据库模型目录 → 检查器形状；provider 协议取数据库 provider 声明。 */
function catalogModels() {
  const providers = (app.catalog && app.catalog.providers) || [];
  return ((app.catalog && app.catalog.models) || []).map(model => {
    const provider = providers.find(item => item && item.name === model.provider);
    return runtimeModelOption(model, provider ? provider.reasoning_format : 'openai');
  });
}

/* 检查器模型下拉 = 数据库模型目录（spec「模型下拉框来自数据库模型目录」）。
   画布快照里数据库已不存在的模型由 modelRowHTML 的「当前值」兜底选项展示。 */
function inspectorModels() {
  return catalogModels();
}

/* 时段选项只认画布运行快照：引用未声明时段会被服务端校验拒绝。 */
function effectivePeriods() {
  const runtime = currentRuntime();
  if (!runtime) return [];
  return (Array.isArray(runtime.time_periods) ? runtime.time_periods : [])
    .map(period => (period && period.name) || '')
    .filter(Boolean);
}

/* 新建 target 的默认模型：优先画布快照里已有的模型（不改快照即可执行），
   否则用数据库目录第一个（由调用方在同一次 change 里补进快照）。 */
function defaultModelId() {
  const runtimeModelsList = runtimeModels() || [];
  if (runtimeModelsList.length) return runtimeModelsList[0].id;
  const models = inspectorModels();
  return models.length ? models[0].id : '';
}

function reasoningFor(modelId) {
  const runtimeModel = (runtimeModels() || []).find(item => item && item.id === modelId);
  const model = runtimeModel || inspectorModels().find(item => item && item.id === modelId);
  if (!model) return null;
  const reasoning = model.reasoning ||
    (model.profile && typeof model.profile === 'object' ? model.profile.reasoning : null);
  if (!reasoning || reasoning.declared === false || reasoning.supported === false) return null;
  return reasoning.default || null;
}

/* 把 target 引用的模型及其 provider 的非密钥目录信息补进画布快照。
   同 ID 已存在时 addRuntimeModel 不覆盖旧快照（数据库漂移只做检查提示）。 */
function targetModelIds(node) {
  if (!node || node.type !== 'target' || !Array.isArray(node.models)) return [];
  return node.models.map(item => item && item.model).filter(Boolean);
}

function embedRuntimeModels(tree, modelIds) {
  if (!tree || !tree.runtime || !modelIds || !modelIds.length) return;
  const models = (app.catalog && app.catalog.models) || [];
  const providers = (app.catalog && app.catalog.providers) || [];
  modelIds.forEach(id => {
    const model = models.find(item => item && item.id === id);
    if (!model) return;
    const provider = providers.find(item => item && item.name === model.provider);
    if (!provider) return;
    tree.runtime = addRuntimeModel(tree.runtime, model, provider);
  });
}

function nextPriority(graph) {
  let priority = 1;
  (graph.nodes || []).forEach(node => {
    if (node.type === 'target' && Number.isInteger(node.priority) && node.priority >= priority) {
      priority = node.priority + 1;
    }
  });
  return priority;
}

function uniqueNodeId(prefix, graph, forced) {
  if (forced) return forced;
  let index = 1;
  while ((graph.nodes || []).some(node => node.id === prefix + '-' + index)) index += 1;
  return prefix + '-' + index;
}

function makeNode(kind, position, graph, forcedId) {
  const point = {
    x: Math.max(0, Math.round(position && position.x ? position.x : 40)),
    y: Math.max(0, Math.round(position && position.y ? position.y : 40))
  };
  let node;
  if (kind === 'question-choice') {
    node = {
      id: uniqueNodeId('question-choice', graph, forcedId),
      type: 'question', question_type: 'choice',
      instructions: t('cv.default.questionChoice'),
      options: [
        { key: 'a', label: t('cv.default.optionA'), capability: t('cv.default.capabilityA') },
        { key: 'b', label: t('cv.default.optionB'), capability: t('cv.default.capabilityB') }
      ],
      position: point
    };
  } else if (kind === 'question-noul') {
    node = {
      id: uniqueNodeId('question-noul', graph, forcedId),
      type: 'question', question_type: 'noul',
      instructions: t('cv.default.questionNoul'),
      position: point
    };
  } else if (kind === 'question-score') {
    node = {
      id: uniqueNodeId('question-score', graph, forcedId),
      type: 'question', question_type: 'score',
      instructions: t('cv.default.questionScore'),
      criteria: [1, 5, 7.5, 10],
      position: point
    };
  } else if (kind === 'logic-and' || kind === 'logic-or') {
    node = {
      id: uniqueNodeId(kind, graph, forcedId),
      type: 'logic', operator: kind.slice('logic-'.length),
      position: point
    };
  } else if (kind === 'condition-text') {
    node = {
      id: uniqueNodeId('condition-text', graph, forcedId),
      type: 'condition', source: 'text',
      contains_any: [t('cv.default.keyword')],
      position: point
    };
  } else if (kind === 'condition-periods') {
    /* 新时段的默认值来自当前目录（runtime 的 time_periods 或全局目录），
       避免新节点引用当前树根本不存在的时段。 */
    const firstPeriod = effectivePeriods()[0] || 'peak';
    node = {
      id: uniqueNodeId('condition-periods', graph, forcedId),
      type: 'condition', source: 'periods',
      periods: [firstPeriod],
      position: point
    };
  } else if (kind === 'target') {
    const model = defaultModelId();
    const reasoning = reasoningFor(model);
    const targetModel = { model: model };
    if (reasoning) targetModel.reasoning = reasoning;
    node = {
      id: uniqueNodeId('target', graph, forcedId),
      type: 'target',
      models: [targetModel],
      priority: nextPriority(graph),
      position: point
    };
  } else {
    return null;
  }
  return node;
}

/* ── 草稿 Store / 视图状态 ─────────────────────────────────────── */
const store = new DraftStore();

let stage = null;
let world = null;
let nodesLayer = null;
let svg = null;
let libDrag = null;
let ghost = null;
let toastSeq = 0;
/* 检查器里有未提交输入：焦点仍在表单控件内，或本次渲染后发生过 input/change。 */
let inspectorDirty = false;
/* 保存/启用被 422 驳回后，画布上的红色标注要闪一次再常驻。 */
let issueFlashPending = false;
/* 共享的「丢弃草稿」确认框；null = 未打开。 */
let confirmState = null;

/* 装配层暴露给模块与测试观察的只读视图；没有 window 全局。 */
const app = {
  kit: null,
  /* 数据库目录（/admin/providers、/admin/models、/admin/jev、/admin/time-periods）。
     catalogReady 是 boot 里创建的 Promise：新画布创建前必须 await。 */
  catalog: { providers: [], models: [], services: [], periods: [] },
  catalogReady: null,
  models: [],
  periods: [],
  issues: [],
  issueRows: [],
  selection: { kind: null, id: null },
  quick: null,
  viewport: { k: 1, tx: 0, ty: 0 },
  booted: false,
  lastToast: '',
  palCollapsed: false,
  inspCollapsed: false,
  palSearch: '',
  mini: null,
  controller: null,
  get tree() { return store.tree; },
  get graph() { return store.graph; },
  get treeId() { return app.controller ? app.controller.treeId : ''; },
  get trees() { return app.controller ? app.controller.trees : []; },
  get saved() { return store.saved; },
  get tryRunning() { return store.tryRunning; },
  get saving() { return store.saving; },
  get changeKind() { return store.dirtyKind; },
  get replay() { return currentReplay(); },
  kind() { return store.dirtyKind; },
  change: (kind, mutate) => change(kind, mutate),
  render: () => renderAll(),
  toast: (message, tone) => toast(message, tone),
  collapsePanels: () => collapsePanels(),
  updateToolbar: () => updateToolbar(),
  locateIssue: issue => locateIssue(issue),
  requestDelete: selection => requestDelete(selection),
  selectTree: id => app.controller.selectTree(id),
  loadTree: (raw, saved) => app.controller.loadTree(raw, saved),
};

const hooks = {
  toast: (message, tone) => toast(message, tone),
  askDiscard: (reason, copy) => askDiscard(reason, copy),
  render: () => renderAll(),
  renderIssues: () => renderIssues(),
  revealIssues: () => revealIssues(),
  refreshDirty: () => refreshDirty(),
  syncSaving: () => updateToolbar(),
  setIssues: issues => { app.issues = issues; },
  isInspectorEditing: () => inspectorEditing(),
  fitAfterReplace: () => fitAfterReplace(),
  renderTreeSelect: () => renderTreeSelect(),
  getCatalog: () => app.catalog,
  change: (kind, mutate) => app.change(kind, mutate),
};

const controller = new RoutingController({ store, hooks });
app.controller = controller;

/* ── 草稿订阅：Store 是唯一写入路径，装配层只负责重绘 ─────────── */
function refreshIssues() {
  app.issues = store.tree ? RTGraph.validate(store.tree.root) : [];
}

function handleStoreEvent(event) {
  if (event.type === 'saved') {
    refreshDirty();
    updateToolbar();
    return;
  }
  if (event.type === 'load') {
    app.quick = null;
    if (app.kit) app.kit.attachGraph(store.graph);
    resetTryRun();
    refreshIssues();
    renderAll();
    refreshDirty();
    /* 卡片渲染完成后才能测量尺寸：和旧 loadTree 一样在下一帧 fit。 */
    requestAnimationFrame(() => {
      if (!store.tree || !app.kit) return;
      app.kit.fit();
      renderMinimap();
    });
    return;
  }
  if (event.type === 'undo' || event.type === 'redo') {
    if (app.kit) app.kit.attachGraph(store.graph);
    refreshIssues();
    dropReplay();
    renderAll();
    refreshDirty();
    toast(event.type === 'undo' ? t('cv.toastUndo') : t('cv.toastRedo'));
    return;
  }
  refreshIssues();
  dropReplay();
  renderAll();
  refreshDirty();
}

store.subscribe(handleStoreEvent);

function inspectorEditing() {
  if (inspectorDirty) return true;
  const body = document.getElementById('cvInspectorBody');
  const active = document.activeElement;
  return !!(body && active && body.contains(active) &&
    /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || ''));
}

function bindInspectorEditState() {
  const body = document.getElementById('cvInspectorBody');
  if (!body || body.dataset.editBound === '1') return;
  body.dataset.editBound = '1';
  const mark = () => { inspectorDirty = true; };
  body.addEventListener('input', mark);
  body.addEventListener('change', mark);
}

/* ── 渲染：节点库 ─────────────────────────────────────────────── */
function renderPalette() {
  const body = $('#cvPalBody');
  const palette = $('#cvPalette');
  if (!body || !palette) return;
  const keyword = app.palSearch.trim().toLowerCase();
  const search = $('#cvPalSearch');
  if (search) search.placeholder = t('cv.paletteSearch');
  const html = CATALOG.map(group => {
    const items = group.items.filter(item => {
      if (!keyword) return true;
      return (t(group.labelKey) + ' ' + t(item.labelKey) + ' ' + t(item.descKey)).toLowerCase().indexOf(keyword) >= 0;
    });
    if (!items.length) return '';
    return '<div class="rtc-pgroup" data-group="' + esc(group.id) + '">' +
      '<div class="rtc-pg-h"><span class="rtc-pg-dot rtc-tone--' + esc(group.tone) + '"></span>' +
      esc(t(group.labelKey)) + '</div>' +
      items.map(item =>
        '<button type="button" class="rtc-pitem" draggable="false" data-lib-kind="' + esc(item.kind) + '"' +
        ' data-lib-group="' + esc(group.id) + '">' +
        '<span class="rtc-pico rtc-tone--' + esc(group.tone) + '">' + icon(item.icon) + '</span>' +
        '<span class="rtc-pitem-l"><span class="rtc-pnm">' + esc(t(item.labelKey)) + '</span>' +
        '<span class="rtc-pdesc">' + esc(t(item.descKey)) + '</span></span></button>').join('') +
      '</div>';
  }).join('');
  body.innerHTML = html || '<div class="rtc-pal-empty">' + esc(t('cv.paletteEmpty')) + '</div>';
  palette.classList.toggle('is-collapsed', app.palCollapsed);
  const mini = $('#cvPalMini');
  if (mini) {
    mini.innerHTML = CATALOG.map(group =>
      '<button type="button" data-pal-group="' + esc(group.id) + '" title="' + esc(t(group.labelKey)) + '">' +
      icon(group.items[0].icon) + '</button>').join('');
  }
  body.scrollTop = 0;
}

/* ── 渲染：检查器（三分支由 inspector.js 生成） ──────────────── */
function renderInspector() {
  /* 快照只读：检查器不生成表单，避免出现任何可提交的编辑控件。 */
  if (isLogSnapshot()) {
    const body = document.getElementById('cvInspectorBody');
    if (body) body.innerHTML = '';
    inspectorDirty = false;
    return;
  }
  const selection = app.kit ? app.kit.getSelection() : app.selection;
  renderInspectorBranch(selection);
  /* 新 DOM 里没有未提交输入；只有下一次 input/change 才会重新置脏。 */
  inspectorDirty = false;
}

/* ── 渲染：校验条 ─────────────────────────────────────────────── */
function issueCounts() {
  const issues = app.issues || [];
  return {
    hard: issues.filter(issue => issue.level !== 'hint'),
    hints: issues.filter(issue => issue.level === 'hint')
  };
}

function renderIssues() {
  const counts = issueCounts();
  const text = tf('cv.issuesCount', { errors: counts.hard.length, hints: counts.hints.length });
  const label = $('#cvIssueText');
  if (label) label.textContent = text;
  const button = $('#cvIssuesBtn');
  if (button) {
    button.classList.toggle('bad', counts.hard.length > 0);
    button.classList.toggle('warn', counts.hard.length === 0 && counts.hints.length > 0);
    button.setAttribute('aria-expanded', String(!$('#cvIssues').hidden));
  }
  const panel = $('#cvIssues');
  if (!panel) return;
  const rows = counts.hard.concat(counts.hints);
  /* 校验条点击按行号取回原始 Issue（服务端 422 与本地 validate 共用同一份定位规则）。 */
  app.issueRows = rows;
  /* 画布上的红色标注和校验条同一份数据：面板收起时标记仍然保留。 */
  paintIssueTargets(rows, { flash: issueFlashPending });
  issueFlashPending = false;
  if (panel.hidden) { panel.innerHTML = ''; return; }
  const lang = (typeof LANG === 'string' && LANG === 'en') ? 'en' : 'zh';
  panel.innerHTML = rows.map((issue, index) => {
    const title = (lang === 'en' && issue.en)
      ? issue.en
      : (issue.zh || issue.msg || issue.en || issue.code || '');
    const target = issueTarget(issue);
    const attrs = target
      ? (target.kind === 'graph'
        ? ' data-issue-graph="1"'
        : target.kind === 'edge'
          ? ' data-issue-edge="' + esc(target.id) + '"'
          : ' data-issue-node="' + esc(target.id) + '"') + ' data-issue-idx="' + index + '"'
      : '';
    return '<div class="rtc-issue ' + (issue.level === 'hint' ? 'is-hint' : 'is-hard') + '">' +
      '<span class="rtc-idot"></span><span class="rtc-issue-text">' + esc(title) + '</span>' +
      (attrs ? '<button type="button" class="rtc-issue-locate" data-locate="1"' + attrs + '>' +
        esc(t('cv.issueLocate')) + '</button>' : '') +
      '</div>';
  }).join('') || '<div class="rtc-issue is-ok">' + esc(t('cv.issueNone')) + '</div>';
}

/* ── 校验定位：edge_id + port → node_id → 图设置（§5.1 / §6.10） ── */
/* 这些码的修复入口在检查器「图设置」，即使服务端顺手带了 node_id（如兜底指向）也不选节点。 */
const GRAPH_LEVEL_CODES = {
  invalid_timezone: true,
  fallback_missing: true,
  graph_parse_error: true,
  limit_exceeded: true,
  decision_service_unavailable: true,
  start_count: true
};

function issueTarget(issue) {
  if (!issue || issue.level === 'hint') return null;
  if (issue.edge_id) return { kind: 'edge', id: String(issue.edge_id), port: String(issue.port || '') };
  if (!GRAPH_LEVEL_CODES[issue.code] && issue.node_id) return { kind: 'node', id: String(issue.node_id) };
  return { kind: 'graph' };
}

function focusInspector() {
  const inspector = $('#cvInspector');
  if (!inspector) return;
  inspector.classList.add('is-focus');
  setTimeout(() => inspector.classList.remove('is-focus'), 900);
}

/* 试算运行前自动收起节点库与检查器；结束后保持收起，用户可手动展开（§0-10）。 */
function applyInspectorCollapsed() {
  const inspector = $('#cvInspector');
  if (!inspector) return;
  inspector.classList.toggle('is-collapsed', !!app.inspCollapsed);
  const toggle = $('#cvInspToggle');
  if (toggle) toggle.setAttribute('aria-expanded', String(!app.inspCollapsed));
}

function collapsePanels() {
  app.palCollapsed = true;
  renderPalette();
  const search = $('#cvPalSearch');
  if (search) search.disabled = true;
  app.inspCollapsed = true;
  applyInspectorCollapsed();
}

function locateIssue(issue) {
  const target = issueTarget(issue);
  if (!target) return false;
  if (target.kind === 'edge') {
    const edge = ((store.graph && store.graph.edges) || []).find(item => item && item.id === target.id);
    if (!edge) return false;
    app.kit.focusEdge(target.id);
    openCondPop(target.id);
    return true;
  }
  if (target.kind === 'node') {
    if (!RTGraph.findNode(store.graph, target.id)) return false;
    app.kit.focusNode(target.id);
    return true;
  }
  /* 图级定位要聚焦检查器图设置：先关掉可能还开着的输入槽条件浮层，
     否则浮层会盖住检查器。 */
  if (isCondPopOpen()) closeCondPop();
  /* 试算/收起会把检查器折叠起来：图级定位必须重新展开，否则焦点落在
     一个不可见的「图设置」上，用户看不到修复入口。 */
  app.inspCollapsed = false;
  applyInspectorCollapsed();
  app.kit.clearSelection();
  focusInspector();
  return true;
}

function revealIssues() {
  const panel = $('#cvIssues');
  if (!panel) return;
  panel.hidden = false;
  issueFlashPending = true;
  renderIssues();
  const button = $('#cvIssuesBtn');
  if (button) button.setAttribute('aria-expanded', 'true');
}

/* ── 校验命中：把 issue 目标涂成红色（节点 / 边 / 槽位） ───────── */
function cssEscape(value) {
  if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(String(value));
  return String(value).replace(/["\\]/g, '\\$&');
}

function clearIssueTargets() {
  const marked = document.querySelectorAll('.is-issue-target');
  for (let index = 0; index < marked.length; index += 1) {
    marked[index].classList.remove('is-issue-target', 'is-issue-flash');
  }
}

function issueTargetElements(target) {
  const elements = [];
  if (!target) return elements;
  if (target.kind === 'node') {
    const node = document.querySelector('#cvNodes [data-node-id="' + cssEscape(target.id) + '"]');
    if (node) elements.push(node);
    return elements;
  }
  if (target.kind !== 'edge') return elements;
  const path = app.kit && app.kit.edgeEl ? app.kit.edgeEl(target.id) : null;
  if (path) elements.push(path);
  const edge = ((store.graph && store.graph.edges) || []).find(item => item && item.id === target.id);
  if (!edge) return elements;
  const node = document.querySelector('#cvNodes [data-node-id="' + cssEscape(edge.target) + '"]');
  const slot = node && node.querySelector('.rtc-slot[data-slot="' + cssEscape(edge.target_port) + '"]');
  if (slot) elements.push(slot);
  return elements;
}

function paintIssueTargets(rows, options) {
  if (!app.kit) return;
  clearIssueTargets();
  const flash = !!(options && options.flash);
  (rows || []).forEach(issue => {
    const target = issueTarget(issue);
    if (!target) return;
    issueTargetElements(target).forEach(el => {
      el.classList.add('is-issue-target');
      if (!flash) return;
      el.classList.add('is-issue-flash');
      setTimeout(() => el.classList.remove('is-issue-flash'), 2200);
    });
  });
}

/* ── 渲染：小地图（只读 graph positions/viewport，不写 dirty） ── */
const MINI_W = 168;
const MINI_H = 104;
const MINI_PAD = 8;

function nodeBoxSize(node) {
  const el = app.kit ? app.kit.nodeEl(node.id) : null;
  return {
    w: el ? el.offsetWidth : 220,
    h: el ? el.offsetHeight : 100
  };
}

function renderMinimap() {
  const svgEl = $('#cvMinimapSvg');
  if (!svgEl) return;
  const graph = store.graph;
  if (!graph || !graph.nodes || !graph.nodes.length) {
    svgEl.innerHTML = '';
    app.mini = null;
    return;
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  graph.nodes.forEach(node => {
    const size = nodeBoxSize(node);
    minX = Math.min(minX, node.position.x);
    minY = Math.min(minY, node.position.y);
    maxX = Math.max(maxX, node.position.x + size.w);
    maxY = Math.max(maxY, node.position.y + size.h);
  });
  const width = Math.max(1, maxX - minX);
  const height = Math.max(1, maxY - minY);
  const scale = Math.min((MINI_W - MINI_PAD * 2) / width, (MINI_H - MINI_PAD * 2) / height);
  const ox = MINI_PAD + ((MINI_W - MINI_PAD * 2) - width * scale) / 2;
  const oy = MINI_PAD + ((MINI_H - MINI_PAD * 2) - height * scale) / 2;
  app.mini = { scale: scale, ox: ox, oy: oy, minX: minX, minY: minY };
  const mapX = x => ox + (x - minX) * scale;
  const mapY = y => oy + (y - minY) * scale;

  let html = graph.nodes.map(node => {
    const size = nodeBoxSize(node);
    return '<rect class="rtc-mini-node" data-type="' + esc(node.type) + '" x="' + mapX(node.position.x).toFixed(1) +
      '" y="' + mapY(node.position.y).toFixed(1) + '" width="' + Math.max(3, size.w * scale).toFixed(1) +
      '" height="' + Math.max(2, size.h * scale).toFixed(1) + '" rx="1.5"></rect>';
  }).join('');

  if (app.kit && stage) {
    const rect = stage.getBoundingClientRect();
    const a = app.kit.screenToWorld(rect.left, rect.top);
    const b = app.kit.screenToWorld(rect.right, rect.bottom);
    const x = mapX(Math.max(a.x, minX));
    const y = mapY(Math.max(a.y, minY));
    const w = Math.max(2, (Math.min(b.x, maxX) - Math.max(a.x, minX)) * scale);
    const h = Math.max(2, (Math.min(b.y, maxY) - Math.max(a.y, minY)) * scale);
    html += '<rect class="rtc-mini-view" x="' + x.toFixed(1) + '" y="' + y.toFixed(1) +
      '" width="' + w.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="2"></rect>';
  }
  svgEl.innerHTML = html;
}

function onMinimapClick(event) {
  if (!app.mini) return;
  const rect = $('#cvMinimapSvg').getBoundingClientRect();
  const px = event.clientX - rect.left;
  const py = event.clientY - rect.top;
  const worldX = app.mini.minX + (px - app.mini.ox) / app.mini.scale;
  const worldY = app.mini.minY + (py - app.mini.oy) / app.mini.scale;
  let best = null;
  let bestDistance = Infinity;
  store.graph.nodes.forEach(node => {
    const size = nodeBoxSize(node);
    const dx = node.position.x + size.w / 2 - worldX;
    const dy = node.position.y + size.h / 2 - worldY;
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) { bestDistance = distance; best = node; }
  });
  if (best) app.kit.focusNode(best.id, { scale: app.viewport.k });
}

/* ── 渲染：全部 ───────────────────────────────────────────────── */
function renderAll() {
  if (!store.tree || !app.kit) return;
  app.kit.attachGraph(store.graph);
  renderCards(store.graph, nodesLayer);
  app.kit.sync();
  renderIssues();
  renderInspector();
  renderMinimap();
  updateToolbar();
  if (isCondPopOpen()) refreshCondPop();
  /* 语言切换/重绘后重新点亮回放；apply 只读 trace，不重新求值。 */
  if (currentReplay()) applyTryRun(currentReplay());
}

function updateToolbar() {
  const undo = $('#cvUndo');
  const redo = $('#cvRedo');
  const busy = !!store.tryRunning || !!store.saving;
  if (undo) {
    undo.disabled = busy || !store.canUndo;
    undo.title = t('cv.undoTitle');
  }
  if (redo) {
    redo.disabled = busy || !store.canRedo;
    redo.title = t('cv.redoTitle');
  }
  ['cvAutoLayout', 'cvSave', 'cvEnable', 'cvNewTree', 'cvImport', 'cvExport', 'cvTryBtn',
    'cvDisable', 'cvDefault']
    .forEach(id => {
      const button = document.getElementById(id);
      if (button) button.disabled = busy;
    });
  /* 保存/启用请求在途：顶栏的 #cvSave 仍要显示忙碌文案，
     主按钮的 aria-busy 由 paintLifecycle() 统一暴露。 */
  const save = $('#cvSave');
  if (save) {
    save.setAttribute('aria-busy', String(!!store.saving));
    save.classList.toggle('is-saving', !!store.saving);
    const label = save.querySelector('span[data-i18n]');
    if (label) label.textContent = store.saving ? t('cv.saving') : t('cv.saveDraft');
  }
  const disable = $('#cvDisable');
  if (disable) disable.hidden = !(store.tree && store.tree.enabled);
  /* 多图生命周期：默认可切换，但默认图本身不再提供「设为默认」；设默认隐含
     启用，所以只在已启用且非默认的图上显示（禁用图先走主按钮启用）。
     默认 pill 与按钮互补：当前图是默认图时显示。 */
  const defaultButton = $('#cvDefault');
  if (defaultButton) {
    defaultButton.hidden = !(store.tree && store.tree.enabled && !store.tree.default);
  }
  const defaultPill = $('#cvDefaultPill');
  if (defaultPill) {
    /* 只读日志回放不宣传实时生命周期（与隐藏 #cvLifecycle 同一原则）。 */
    defaultPill.hidden = isLogSnapshot() || !(store.tree && store.tree.default);
  }
  paintLifecycle();
  applyTryScope();
}

/* 试算范围（Task 9）：draft 保持既有全草稿请求；route_set 只按部门/默认图从
   已发布路由集选图，因此部门输入只在 route_set 模式下显示并可用。运行/保存在途
   时范围与部门一起禁用（由 updateToolbar 统一调用，tryrun.setBusy 会再次触发）。 */
function applyTryScope() {
  const scope = $('#cvTryScope');
  if (!scope) return;
  const department = $('#cvTryDepartment');
  const label = $('#cvTryDepartmentLabel');
  const routeSet = scope.value === 'route_set';
  const busy = !!(store.tryRunning || store.saving);
  scope.disabled = busy;
  if (label) label.hidden = !routeSet;
  if (department) {
    department.hidden = !routeSet;
    department.disabled = !routeSet || busy;
  }
}

function bindTryScope() {
  const scope = $('#cvTryScope');
  if (!scope) return;
  scope.addEventListener('change', applyTryScope);
  applyTryScope();
}

/* 顶栏状态只由 store 的持久化视图投影：启用与否 + 草稿是否干净 + 是否落过盘。
   dirtyKind 仍保留 behavior/layout/display 分类，供保存语义与 beforeunload 使用；
   这里只把它折叠成用户需要看的四种生命周期，不再另设第五种“仅布局”状态。
   未启用优先：只要落过盘（store.saved）就显示 disabled，无论草稿是否有本地
   改动（脏状态由主按钮「保存并启用 / 保存并更新」表达）；draft 只留给从未
   保存过的新草稿。 */
function lifecycleState() {
  const enabled = !!(store.tree && store.tree.enabled);
  const clean = store.dirtyKind === 'clean';
  if (enabled) return clean ? 'running' : 'unpublished';
  return store.saved ? 'disabled' : 'draft';
}

function paintLifecycle() {
  const state = lifecycleState();
  const badge = $('#cvLifecycle');
  const label = $('#cvLifecycleLabel');
  if (label) label.textContent = t('cv.status.' + state);
  if (badge) {
    badge.dataset.state = state;
    badge.classList.toggle('ok', state === 'running');
    badge.classList.toggle('warn', state === 'unpublished' || state === 'draft');
    const revision = store.tree && store.tree.tree_revision;
    badge.title = revision ? tf('cv.revision', { rev: String(revision).slice(0, 10) }) : t('cv.unpublished');
  }
  const enabled = !!(store.tree && store.tree.enabled);
  const dirty = store.dirtyKind !== 'clean' || !store.saved;
  const primaryKey = store.saving ? 'cv.saving'
    : enabled ? (dirty ? 'cv.publishUpdate' : 'cv.published')
    : (dirty ? 'cv.publish' : 'cv.enable');
  const primary = $('#cvEnable');
  const primaryLabel = $('#cvEnableLabel');
  if (primaryLabel) primaryLabel.textContent = t(primaryKey);
  if (primary) {
    primary.setAttribute('aria-busy', String(!!store.saving));
    primary.disabled = !!store.tryRunning || !!store.saving || (enabled && !dirty);
  }
}

function refreshDirty() {
  if (!store.tree) return;
  updateToolbar();
}

/* ── 历史 / 编辑入口 ─────────────────────────────────────────── */
/* 任何一次编辑/恢复都先清掉回放：陈旧高亮不能再指向旧结果（§6.8）。 */
function dropReplay() {
  resetTryRun();
}

function change(kind, mutate) {
  /* 画布还没装配好（boot 早期 / 服务端树缺失）时不能改：clone(null)/mutate
     会抛错，调用方也拿不到明确的 false。 */
  if (!store.tree) return false;
  if (store.tryRunning) {
    toast(t('cv.tryEditBlocked'), 'warn');
    return false;
  }
  return store.change(kind, mutate);
}

/* ── 渲染：toast ──────────────────────────────────────────────── */
function toast(message, tone) {
  const host = $('#cvToast');
  if (!host) return;
  toastSeq += 1;
  const item = document.createElement('div');
  item.className = 'rtc-toast' + (tone ? ' is-' + tone : '');
  item.dataset.toast = String(toastSeq);
  item.innerHTML = icon(tone === 'warn' || tone === 'error' ? 'alert' : 'check') + '<span></span>';
  item.querySelector('span').textContent = String(message == null ? '' : message);
  host.appendChild(item);
  app.lastToast = String(message == null ? '' : message);
  host.dataset.last = app.lastToast;
  setTimeout(() => { if (item.parentNode) item.parentNode.removeChild(item); }, 4200);
}

/* ── 删除确认（节点 / 边 / choice 选项共用级联清单） ──────────── */
/* 焦点管理：打开时记录触发按钮并把焦点移入对话框，Tab 在框内循环；
   Esc / 取消关闭后把焦点还给触发按钮（键盘可达性，规格 §6.11）。 */
let deleteTrigger = null;

function deleteFocusables() {
  const body = $('#cvDeleteBody');
  if (!body) return [];
  return Array.prototype.slice.call(
    body.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
  ).filter(el => !el.disabled && el.getClientRects().length > 0);
}

function restoreDeleteFocus() {
  const trigger = deleteTrigger;
  deleteTrigger = null;
  if (trigger && document.contains(trigger) && typeof trigger.focus === 'function') {
    trigger.focus();
    return;
  }
  const fallback = $('#cvEnable');
  /* 主按钮在「已启用且无改动」时是禁用的（Enabled 只是一个状态），focus()
     落在禁用按钮上是空操作、焦点会掉回 body，所以再退到常驻可聚焦的控件。 */
  if (fallback && !fallback.disabled) { fallback.focus(); return; }
  const alwaysFocusable = $('#cvIssuesBtn');
  if (alwaysFocusable) alwaysFocusable.focus();
}

function onDeleteKeydown(event) {
  const modal = $('#cvDeleteModal');
  if (!modal || modal.hidden) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    hideDelete();
    return;
  }
  if (event.key !== 'Tab') return;
  const items = deleteFocusables();
  if (!items.length) { event.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  const inside = !!active && $('#cvDeleteBody').contains(active);
  if (event.shiftKey) {
    if (!inside || active === first) { event.preventDefault(); last.focus(); }
  } else if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}

function requestDelete(selection) {
  if (store.tryRunning) { toast(t('cv.tryEditBlocked'), 'warn'); return; }
  if (!selection || !selection.kind) return;
  const modal = $('#cvDeleteModal');
  if (!modal) return;
  const graph = store.graph;
  let title = '';
  let edges = [];
  let nodeId = '';
  let key = '';
  if (selection.kind === 'node') {
    const node = RTGraph.findNode(graph, selection.id);
    if (!node) return;
    /* start 是图入口且节点库不能再创建：允许删除会让草稿永久无法启用。 */
    if (node.type === 'start') {
      toast(t('cv.deleteStartBlocked'), 'warn');
      return;
    }
    edges = (graph.edges || []).filter(edge => edge.source === selection.id || edge.target === selection.id);
    title = tf('cv.deleteNode', { id: nodeName(node, graph) });
  } else if (selection.kind === 'edge') {
    const edge = (graph.edges || []).find(item => item.id === selection.id);
    if (!edge) return;
    edges = [edge];
    title = t('cv.deleteEdgeTitle');
  } else if (selection.kind === 'option') {
    const node = RTGraph.findNode(graph, selection.nodeId);
    const option = node && (node.options || []).find(item => item.key === selection.key);
    if (!node || !option) return;
    const ports = ['selected.' + selection.key, 'prob.' + selection.key];
    edges = (graph.edges || []).filter(edge => edge.source === selection.nodeId && ports.indexOf(edge.source_port) >= 0);
    title = tf('cv.deleteOption', { label: option.label, key: selection.key });
    nodeId = selection.nodeId;
    key = selection.key;
  } else {
    return;
  }
  const rows = edges.map(edge => {
    const type = RTGraph.edgeType(edge, graph);
    const condition = type === 'number' || (edge.condition && edge.condition.op === 'is')
      ? ' · ' + conditionText(edge, graph)
      : '';
    const sourceNode = RTGraph.findNode(graph, edge.source);
    const targetNode = RTGraph.findNode(graph, edge.target);
    const sourceName = sourceNode ? nodeName(sourceNode, graph) : edge.source;
    const targetName = targetNode ? nodeName(targetNode, graph) : edge.target;
    return '<li>' + esc(sourceName + ' · ' + edge.source_port + ' → ' +
      targetName + ' · ' + edge.target_port + condition) + '</li>';
  }).join('');
  const body = $('#cvDeleteBody');
  body.innerHTML = '<h3>' + esc(t('cv.deleteTitle')) + '</h3>' +
    '<p class="rtc-dialog-lead">' + esc(title) + '</p>' +
    (rows ? '<p class="rtc-dialog-lead">' + esc(tf('cv.deleteAffected', { n: edges.length })) + '</p>' +
      '<ul class="rtc-delete-list">' + rows + '</ul>' : '') +
    '<p class="rtc-dialog-note">' + esc(t('cv.deleteSlotNote')) + '</p>' +
    '<div class="rtc-dialog-acts">' +
    '<button type="button" class="btn sm" data-act="delete-cancel">' + esc(t('cv.deleteCancel')) + '</button>' +
    '<button type="button" class="btn sm danger" data-act="delete-confirm">' + esc(t('cv.deleteOk')) + '</button>' +
    '</div>';
  deleteTrigger = document.activeElement && document.activeElement !== document.body
    ? document.activeElement
    : null;
  modal.hidden = false;
  modal.dataset.kind = selection.kind;
  modal.dataset.id = selection.id || '';
  modal.dataset.nodeId = nodeId;
  modal.dataset.key = key;
  body.setAttribute('aria-label', t('cv.deleteTitle'));
  const cancel = body.querySelector('[data-act="delete-cancel"]');
  if (cancel) cancel.focus();
}

function hideDelete(options) {
  const modal = $('#cvDeleteModal');
  if (modal) modal.hidden = true;
  if (!options || options.restore !== false) restoreDeleteFocus();
  else deleteTrigger = null;
}

function confirmDelete() {
  const modal = $('#cvDeleteModal');
  if (!modal || modal.hidden) return;
  const kind = modal.dataset.kind;
  const id = modal.dataset.id;
  const nodeId = modal.dataset.nodeId;
  const key = modal.dataset.key;
  hideDelete({ restore: false });
  if (!kind) return;
  /* 弹窗打开后目标可能已被撤销/重做移除：先在快照上试删，避免推入空历史。 */
  const probe = store.tree ? clone(store.tree) : null;
  const check = kind === 'node'
    ? RTGraph.removeNode(probe && probe.root, id)
    : kind === 'edge'
      ? RTGraph.removeEdge(probe && probe.root, id)
      : RTGraph.removeOption(probe && probe.root, nodeId, key);
  if (!check.ok) return;
  let cleared = [];
  let removed = false;
  change('behavior', tree => {
    let result;
    if (kind === 'node') result = RTGraph.removeNode(tree.root, id);
    else if (kind === 'edge') result = RTGraph.removeEdge(tree.root, id);
    else result = RTGraph.removeOption(tree.root, nodeId, key);
    if (result.ok) {
      removed = true;
      cleared = result.fallbackCleared || [];
    }
  });
  if (!removed) return;
  if (kind === 'option') {
    const selection = app.kit.getSelection();
    if (selection.kind !== 'node' || selection.id !== nodeId) app.kit.select('node', nodeId, true);
  } else {
    app.kit.clearSelection();
  }
  if (cleared.length) toast(t('cv.toastFallbackCleared'), 'warn');
  else toast(t('cv.toastDeleted'));
  /* 确认删除后触发按钮可能随卡片重绘消失：回到顶栏的稳定焦点位。 */
  const enable = $('#cvEnable');
  if (enable && document.contains(enable)) enable.focus();
}

/* ── 快速创建（输出端口拖到空白） ─────────────────────────────── */
function quickCandidate(kind, context) {
  const probe = makeNode(kind, { x: 0, y: 0 }, store.graph, '__quick_probe');
  if (!probe) return false;
  const tmp = {
    id: store.graph.id || 'root', type: 'graph',
    nodes: (store.graph.nodes || []).concat([probe]),
    edges: store.graph.edges || [],
    error_fallback: store.graph.error_fallback
  };
  const targetPort = RTGraph.nextSlot(tmp, '__quick_probe');
  const hasPort = RTGraph.portsOf(probe, tmp).some(port => port.dir === 'in' && port.id === targetPort);
  if (!hasPort) return false;
  return RTGraph.canConnect(tmp, {
    source: context.sourceNodeId,
    source_port: context.sourcePort,
    target: '__quick_probe',
    target_port: targetPort
  }).ok;
}

function openQuickMenu(context) {
  const menu = $('#cvQuick');
  if (!menu) return false;
  app.quick = context;
  const sourceNode = RTGraph.findNode(store.graph, context.sourceNodeId);
  const sourcePort = sourceNode ? RTGraph.portOf(sourceNode, context.sourcePort, store.graph) : null;
  const candidates = QUICK_KINDS.filter(item => quickCandidate(item.kind, context));
  const head = '<div class="rtc-quick-h">' + icon('plus') + '<span>' + esc(t('cv.quickTitle')) + '</span>' +
    '<small>' + esc(context.sourceNodeId + ' · ' + (sourcePort ? sourcePort.label : context.sourcePort)) + '</small></div>';
  const list = candidates.length
    ? candidates.map(item => {
      const group = CATALOG.find(entry => entry.items.indexOf(item) >= 0);
      return '<button type="button" class="rtc-qitem" data-qkind="' + esc(item.kind) + '">' +
        '<span class="rtc-qico rtc-tone--' + esc(group ? group.tone : 'question') + '">' + icon(item.icon) + '</span>' +
        '<span class="rtc-qitem-l"><span class="rtc-qnm">' + esc(t(item.labelKey)) + '</span>' +
        '<span class="rtc-qdesc">' + esc(t(item.descKey)) + '</span></span></button>';
    }).join('')
    : '<div class="rtc-quick-empty">' + esc(t('cv.quickEmpty')) + '</div>';
  menu.innerHTML = head + list + '<div class="rtc-quick-f">' + esc(t('cv.quickHint')) + '</div>';
  menu.hidden = false;
  menu.style.visibility = 'hidden';
  const width = menu.offsetWidth || 272;
  const height = menu.offsetHeight || 220;
  menu.style.left = Math.max(8, Math.min(window.innerWidth - width - 10, context.clientX + 12)) + 'px';
  menu.style.top = Math.max(8, Math.min(window.innerHeight - height - 10, context.clientY + 12)) + 'px';
  menu.style.visibility = '';
  return true;
}

function closeQuick() {
  const menu = $('#cvQuick');
  if (menu) menu.hidden = true;
  app.quick = null;
}

function createQuick(kind) {
  const context = app.quick;
  closeQuick();
  if (!context) return;
  let createdNode = null;
  let createdEdge = null;
  change('behavior', tree => {
    createdNode = makeNode(kind, { x: context.x + 24, y: context.y - 40 }, tree.root);
    if (!createdNode) return;
    tree.root.nodes.push(createdNode);
    embedRuntimeModels(tree, targetModelIds(createdNode));
    const result = RTGraph.addEdge(tree.root, {
      source: context.sourceNodeId,
      source_port: context.sourcePort,
      target: createdNode.id,
      target_port: 'in_1'
    });
    if (result.ok) createdEdge = result.edge;
  });
  if (!createdNode) return;
  if (createdEdge) {
    app.kit.select('edge', createdEdge.id);
    if (RTGraph.edgeType(createdEdge, store.graph) === 'number') openCondPop(createdEdge.id);
    toast(tf('cv.toastQuickCreated', { label: kindLabel(kind) }));
  } else {
    toast(t('cv.toastConnectFail'), 'warn');
  }
}

/* ── 节点库创建（点击 / 拖入） ───────────────────────────────── */
function createFromLibrary(kind, position) {
  if (!app.booted || store.tryRunning) return;
  let created = null;
  change('behavior', tree => {
    created = makeNode(kind, position, tree.root);
    if (created) {
      tree.root.nodes.push(created);
      /* 新 target 引用的模型（及其 provider）必须在同一次历史项里补进画布快照。 */
      embedRuntimeModels(tree, targetModelIds(created));
    }
  });
  if (!created) return;
  app.kit.select('node', created.id);
  toast(tf('cv.toastNodeCreated', { label: kindLabel(kind) }));
}

function pointCreatable(clientX, clientY) {
  if (!stage) return false;
  const rect = stage.getBoundingClientRect();
  if (clientX < rect.left || clientX > rect.right || clientY < rect.top || clientY > rect.bottom) return false;
  const el = document.elementFromPoint(clientX, clientY);
  if (el && el.closest && el.closest('.rtc-overlay')) return false;
  return true;
}

function showGhost(label) {
  hideGhost();
  ghost = document.createElement('div');
  ghost.className = 'rtc-drag-ghost';
  ghost.textContent = label;
  document.body.appendChild(ghost);
}

function moveGhost(clientX, clientY) {
  if (!ghost) return;
  ghost.style.left = (clientX + 14) + 'px';
  ghost.style.top = (clientY + 14) + 'px';
}

function hideGhost() {
  if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
  ghost = null;
}

function endLibraryDrag() {
  document.removeEventListener('pointermove', onLibraryMove);
  document.removeEventListener('pointerup', onLibraryUp);
  document.removeEventListener('pointercancel', onLibraryCancel);
}

function onLibraryMove(event) {
  if (!libDrag) return;
  const dx = event.clientX - libDrag.startX;
  const dy = event.clientY - libDrag.startY;
  if (!libDrag.moved && Math.sqrt(dx * dx + dy * dy) > 4) {
    libDrag.moved = true;
    showGhost(libDrag.label);
  }
  if (libDrag.moved) {
    moveGhost(event.clientX, event.clientY);
    if (stage) stage.classList.toggle('is-drop-target', pointCreatable(event.clientX, event.clientY));
  }
}

function onLibraryUp(event) {
  endLibraryDrag();
  const drag = libDrag;
  libDrag = null;
  hideGhost();
  if (stage) stage.classList.remove('is-drop-target');
  if (!drag) return;
  if (drag.moved) {
    if (!pointCreatable(event.clientX, event.clientY)) return;
    const point = app.kit.screenToWorld(event.clientX, event.clientY);
    createFromLibrary(drag.kind, { x: point.x - 110, y: point.y - 50 });
    return;
  }
  const rect = stage.getBoundingClientRect();
  const point = app.kit.screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  const offset = ((store.graph && store.graph.nodes.length) || 0) % 5 * 26;
  createFromLibrary(drag.kind, { x: point.x - 110 + offset, y: point.y - 50 + offset });
}

function onLibraryCancel() {
  endLibraryDrag();
  libDrag = null;
  hideGhost();
  if (stage) stage.classList.remove('is-drop-target');
}

function onLibraryDown(event) {
  const item = event.target.closest('[data-lib-kind]');
  if (!item || event.button !== 0) return;
  if (store.tryRunning) return;
  if (!app.booted) {
    toast(t('cv.toastNotReady'), 'warn');
    return;
  }
  event.preventDefault();
  const labelNode = item.querySelector('.rtc-pnm');
  libDrag = {
    kind: item.dataset.libKind,
    label: labelNode ? labelNode.textContent : item.dataset.libKind,
    startX: event.clientX,
    startY: event.clientY,
    moved: false
  };
  document.addEventListener('pointermove', onLibraryMove);
  document.addEventListener('pointerup', onLibraryUp);
  document.addEventListener('pointercancel', onLibraryCancel);
}

/* ── 视口 / 选中回调 ──────────────────────────────────────────── */
function onViewport(viewport) {
  app.viewport = viewport;
  const label = $('#cvZoomLabel');
  if (label) label.textContent = Math.round(viewport.k * 100) + '%';
  renderMinimap();
}

function onSelect(selection) {
  app.selection = selection || { kind: null, id: null };
  renderInspector();
}

function onInvalid(reason) {
  const mapping = [
    { re: /自身/, key: 'cv.errSelf' },
    { re: /环/, key: 'cv.errCycle' },
    { re: /已存在/, key: 'cv.errDuplicate' },
    { re: /已连接一条线|已连接/, key: 'cv.errOccupied' },
    { re: /端口不存在/, key: 'cv.errPort' },
    { re: /连线已取消|Connection cancelled/, key: 'cv.cancelConnect' }
  ];
  let key = 'cv.errGeneric';
  for (let index = 0; index < mapping.length; index += 1) {
    if (mapping[index].re.test(reason)) { key = mapping[index].key; break; }
  }
  toast(key === 'cv.errGeneric' ? tf(key, { reason: reason }) : t(key), 'warn');
}

function onEdgeCreated(edge) {
  if (!edge) return;
  app.kit.select('edge', edge.id);
  if (RTGraph.edgeType(edge, store.graph) === 'number') {
    openCondPop(edge.id);
  } else {
    toast(t('cv.toastBoolDefault'));
  }
}

function closeOverlays() {
  if (confirmState) { settleConfirm(false); return true; }
  if (app.quick) { closeQuick(); return true; }
  const pop = $('#cvCondPop');
  if (pop && !pop.hidden) { closeCondPop(); return true; }
  const modal = $('#cvDeleteModal');
  if (modal && !modal.hidden) { hideDelete(); return true; }
  const issues = $('#cvIssues');
  if (issues && !issues.hidden) { issues.hidden = true; renderIssues(); return true; }
  return false;
}

/* ── 丢弃草稿确认（切组 / 导入 / 新建共用） ─────────────────────── */
const CONFIRM_TITLES = {
  switch: 'cv.confirmSwitchTitle',
  import: 'cv.confirmImportTitle',
  new: 'cv.confirmNewTitle'
};

function askDiscard(reason, copy) {
  return new Promise(resolve => {
    const modal = $('#cvConfirmModal');
    const body = $('#cvConfirmBody');
    if (!modal || !body) { resolve(true); return; }
    if (confirmState) { confirmState.resolve(false); confirmState = null; }
    const text = copy || {
      title: CONFIRM_TITLES[reason] || 'cv.confirmSwitchTitle',
      body: 'cv.confirmDirtyBody',
      action: 'cv.confirmDiscard'
    };
    const title = t(text.title);
    body.innerHTML = '<h3>' + esc(title) + '</h3>' +
      '<p class="rtc-dialog-lead">' + esc(t(text.body)) + '</p>' +
      '<div class="rtc-dialog-acts">' +
      '<button type="button" class="btn sm" data-act="confirm-cancel">' + esc(t('cv.confirmCancel')) + '</button>' +
      '<button type="button" class="btn sm ' + (copy ? 'pri' : 'danger') +
      '" data-act="confirm-discard">' + esc(t(text.action)) + '</button>' +
      '</div>';
    body.setAttribute('aria-label', title);
    modal.hidden = false;
    confirmState = { resolve: resolve };
    const cancel = body.querySelector('[data-act="confirm-cancel"]');
    if (cancel) cancel.focus();
  });
}

function settleConfirm(ok) {
  const state = confirmState;
  confirmState = null;
  const modal = $('#cvConfirmModal');
  if (modal) modal.hidden = true;
  if (state) state.resolve(!!ok);
}

/* 导入替换整张图后回到「看得见全图」的视口（保持检查器/节点库安全区）。 */
function fitAfterReplace() {
  if (!app.kit || typeof app.kit.fit !== 'function') return;
  app.kit.fit({ padding: 24 });
  renderMinimap();
}

/* 自动整理：DAG 分层只写 position；fit 走画布安全区（避开节点库/检查器/小地图/试算台）。 */
function autoLayout() {
  if (!store.graph || store.tryRunning) return false;
  const positions = RTGraph.layoutPositions('dag', store.graph);
  const moved = (store.graph.nodes || []).some(node => {
    const next = positions[node.id];
    const current = node.position || {};
    return !!next && (next.x !== current.x || next.y !== current.y);
  });
  if (moved) {
    change('layout', tree => {
      tree.root.nodes.forEach(node => {
        if (positions[node.id]) node.position = { x: positions[node.id].x, y: positions[node.id].y };
      });
    });
  }
  app.kit.fit({ padding: 24 });
  toast(t('cv.toastAutoLayout'));
  return moved;
}

/* ── 顶栏动作 ─────────────────────────────────────────────────── */
function bindToolbar() {
  $('#cvUndo').addEventListener('click', () => {
    if (!store.undo()) toast(t('cv.toastNothingUndo'), 'warn');
  });
  $('#cvRedo').addEventListener('click', () => {
    if (!store.redo()) toast(t('cv.toastNothingRedo'), 'warn');
  });
  $('#cvAutoLayout').addEventListener('click', autoLayout);
  /* boot 完成（store.tree 已装配）前，替换整张图的入口一律拒绝：
     newTree/import 都会写草稿，此时 change() 没有可操作对象。 */
  const requireBooted = action => function () {
    if (!app.booted) {
      toast(t('cv.toastNotReady'), 'warn');
      return false;
    }
    return action.apply(null, arguments);
  };
  const actions = {
    /* 新建画布必须等数据库目录：目录未到就点「新建」时排队等待而不是造空 runtime。 */
    cvNewTree: requireBooted(async () => {
      try { await app.catalogReady; } catch (error) { /* newDraftTree 给出明确错误 */ }
      return controller.newTree();
    }),
    cvImport: requireBooted(() => controller.openImport()),
    cvExport: () => controller.exportTree(),
    cvSave: () => controller.save(),
    cvEnable: () => controller.enable(),
    cvDefault: () => controller.setDefault(),
    cvDisable: () => controller.disable()
  };
  Object.keys(actions).forEach(id => {
    const button = document.getElementById(id);
    if (button) button.addEventListener('click', () => { actions[id](); });
  });
  $('#cvIssuesBtn').addEventListener('click', () => {
    const panel = $('#cvIssues');
    panel.hidden = !panel.hidden;
    renderIssues();
    const button = $('#cvIssuesBtn');
    if (button) button.setAttribute('aria-expanded', String(!panel.hidden));
  });
  $('#cvIssues').addEventListener('click', event => {
    const locate = event.target.closest('[data-locate]');
    if (!locate) return;
    const issue = (app.issueRows || [])[Number(locate.dataset.issueIdx)];
    if (issue) locateIssue(issue);
  });
}

function bindViewport() {
  $('#cvZoom').addEventListener('click', event => {
    const act = event.target.closest('[data-act]');
    if (!act) return;
    if (act.dataset.act === 'zoom-out') app.kit.zoomBy(1 / 1.2);
    else if (act.dataset.act === 'zoom-in') app.kit.zoomBy(1.2);
    else if (act.dataset.act === 'zoom-reset') app.kit.setZoom(1);
    else if (act.dataset.act === 'zoom-fit') app.kit.fit();
  });
  $('#cvMinimap').addEventListener('click', onMinimapClick);
}

function bindPalette() {
  $('#cvPalToggle').addEventListener('click', () => {
    app.palCollapsed = !app.palCollapsed;
    $('#cvPalette').classList.toggle('is-collapsed', app.palCollapsed);
    $('#cvPalSearch').disabled = app.palCollapsed;
  });
  $('#cvPalSearch').addEventListener('input', event => {
    app.palSearch = event.target.value || '';
    renderPalette();
  });
  $('#cvPalMini').addEventListener('click', () => {
    app.palCollapsed = false;
    renderPalette();
    $('#cvPalSearch').disabled = false;
  });
  $('#cvPalBody').addEventListener('pointerdown', onLibraryDown);
}

function bindStageCards() {
  nodesLayer.addEventListener('click', event => {
    const chip = event.target.closest('[data-cond-edge]');
    if (!chip) return;
    if (store.tryRunning || isLogSnapshot()) return;
    openCondPop(chip.dataset.condEdge);
  });
}

function bindTreeSelect() {
  $('#cvTreeSelect').addEventListener('change', async event => {
    const id = event.target.value;
    if (!id || id === controller.treeId) return;
    /* 选择框立刻回到当前组；确认/加载成功后再由 loadTree 重建选项。 */
    event.target.value = controller.treeId;
    await controller.selectTree(id);
  });
}

function bindOverlays() {
  $('#cvQuick').addEventListener('click', event => {
    const item = event.target.closest('[data-qkind]');
    if (item) createQuick(item.dataset.qkind);
  });
  document.addEventListener('pointerdown', event => {
    if (app.quick && !event.target.closest('#cvQuick')) closeQuick();
  }, true);
  $('#cvDeleteModal').addEventListener('click', event => {
    if (event.target === $('#cvDeleteModal')) { hideDelete(); return; }
    const act = event.target.closest('[data-act]');
    if (!act) return;
    if (act.dataset.act === 'delete-cancel') hideDelete();
    else if (act.dataset.act === 'delete-confirm') confirmDelete();
  });
  $('#cvDeleteModal').addEventListener('keydown', onDeleteKeydown);
  const confirmModal = $('#cvConfirmModal');
  if (confirmModal) {
    confirmModal.addEventListener('click', event => {
      if (event.target === confirmModal) { settleConfirm(false); return; }
      const act = event.target.closest('[data-act]');
      if (!act) return;
      if (act.dataset.act === 'confirm-cancel') settleConfirm(false);
      else if (act.dataset.act === 'confirm-discard') settleConfirm(true);
    });
  }
  const importFile = $('#cvImportFile');
  if (importFile) importFile.addEventListener('change', event => controller.onImportFile(event));
}

/* 试算台、样例 chips 与画布回放全部由 tryrun.js 实现。 */
function bindTryBar() {
  initTryRun();
}

function renderTreeSelect() {
  const select = $('#cvTreeSelect');
  if (!select) return;
  /* 多图生命周期：默认图加「默认」标记。当前编辑图由 controller.treeId 决定，
     不再从唯一 enabled 图推导（可同时启用多张图）。 */
  const label = tree => esc(tree.name || tree.id) + (tree.default ? esc(t('cv.treeDefault')) : '');
  const options = (controller.trees || []).map(tree =>
    '<option value="' + esc(tree.id) + '">' + label(tree) + '</option>').join('');
  select.innerHTML = options;
  if (controller.treeId && !(controller.trees || []).some(tree => tree.id === controller.treeId)) {
    select.innerHTML += '<option value="' + esc(controller.treeId) + '">' +
      esc(store.tree.name || controller.treeId) + '</option>';
  }
  select.value = controller.treeId;
}

function bindInspectorToggle() {
  const toggle = $('#cvInspToggle');
  if (!toggle) return;
  toggle.addEventListener('click', () => {
    app.inspCollapsed = !app.inspCollapsed;
    applyInspectorCollapsed();
  });
  applyInspectorCollapsed();
}

function bindEvents() {
  bindInspectorEditState();
  bindToolbar();
  bindViewport();
  bindPalette();
  bindInspectorToggle();
  bindStageCards();
  bindTreeSelect();
  bindOverlays();
  bindTryBar();
  bindTryScope();
  /* 只有存在未保存改动（clean 之外）时才拦截关闭/刷新。 */
  window.addEventListener('beforeunload', event => {
    if (!store.tree || store.dirtyKind === 'clean') return;
    event.preventDefault();
    event.returnValue = '';
  });
  onLangChange(() => {
    if (!app.booted) return;
    renderPalette();
    /* 决策组下拉框的「默认」标记是 UI 文案（组名本身是数据），语言切换必须重绘。 */
    renderTreeSelect();
    refreshTryRun();
    renderAll();
    refreshDirty();
  });
}

/* ── 创建画布 + 模块装配 ──────────────────────────────────────── */
function createKit() {
  stage = $('#cvStage');
  world = $('#cvWorld');
  nodesLayer = $('#cvNodes');
  svg = $('#cvEdges');
  app.kit = createCanvas({
    stage: stage,
    world: world,
    nodesLayer: nodesLayer,
    svg: svg,
    store: store,
    callbacks: {
      onSelect: onSelect,
      onInvalid: onInvalid,
      onQuickCreate: openQuickMenu,
      onViewport: onViewport,
      onEdgeCreated: onEdgeCreated,
      onDeleteRequest: requestDelete,
      onEscape: closeOverlays
    }
  });
  app.kit.init();
  configureCondPop({ store: store, getGraph: () => store.graph, nodeName: nodeName });
  configureInspector({
    store: store,
    getGraph: () => store.graph,
    getTree: () => store.tree,
    /* 模型目录来自数据库（选择时补进画布快照）；时段来自画布运行快照。 */
    getModels: () => inspectorModels(),
    getPeriods: () => effectivePeriods(),
    getProviders: () => (app.catalog && app.catalog.providers) || [],
    getJevServices: () => (app.catalog && app.catalog.services) || [],
    getCatalog: () => app.catalog,
    getSelection: () => (app.kit ? app.kit.getSelection() : app.selection),
    requestDelete: selection => requestDelete(selection),
    addEdge: draft => app.kit.addEdge(draft),
    toast: (message, tone) => toast(message, tone),
  });
  configureTryRun({
    store: store,
    getKit: () => app.kit,
    getBooted: () => app.booted,
    change: (kind, mutate) => app.change(kind, mutate),
    toast: (message, tone) => app.toast(message, tone),
    render: () => app.render(),
    collapsePanels: () => app.collapsePanels(),
    updateToolbar: () => updateToolbar(),
    readOnly: isLogSnapshot(),
  });
}

/* ── 日志快照 (只读回放) ────────────────────────────────────────── */
function showSnapshotHeader(result) {
  const badge = $('#cvSnapshotBadge');
  if (badge) badge.hidden = false;
  const select = $('#cvTreeSelect');
  if (select) {
    select.disabled = true;
    select.title = t('cv.snapshotBadge');
  }
}

/* 快照问题放回画布原有的「试算问题」输入位：只读展示本次请求正文。 */
function showSnapshotRequest(result) {
  const text = result && result.request ? String(result.request.text || '') : '';
  if (!text) return '';
  const input = $('#cvTryInput');
  if (input) {
    input.value = text;
    input.readOnly = true;
    input.title = text;
  }
  document.body.classList.add('has-snapshot-request');
  return text;
}

async function bootLogSnapshot() {
  document.body.classList.add('is-log-snapshot');
  bindViewport();
  onLangChange(() => {
    if (!app.booted) return;
    refreshTryRun();
  });
  try {
    const result = await api.get(
      '/admin/requests/' + encodeURIComponent(SNAPSHOT_REQUEST) + '/snapshot');
    const tree = result && result.tree;
    if (!tree || !tree.root) throw new Error('request snapshot has no graph');
    controller.loadTree(tree, true);
    showSnapshotHeader(result);
    const snapshotText = showSnapshotRequest(result);
    const replay = rebuildTryRun(result.trace || [], store.graph);
    replay.trace = Array.isArray(result.trace) ? result.trace : [];
    /* 回放对象带上请求正文：复制/导出等观察入口与普通试算保持同一字段。 */
    replay.text = snapshotText;
    if (snapshotText) replay.request = { text: snapshotText };
    applyTryRun(replay);
    app.kit.setLocked(true);
    app.booted = true;
    requestAnimationFrame(() => {
      if (!app.kit) return;
      app.kit.fit({ padding: 24 });
      renderMinimap();
    });
  } catch (error) {
    app.booted = true;
    toast(tf('cv.snapshotLoadFail', { err: apiErrText(error) }), 'error');
  }
}

/* 数据库目录：providers / models / JEV 服务 / 时段。四个端点相互独立，任一失败
   只让对应目录为空（不阻断画布加载）；新画布创建前必须 await 这个 Promise。 */
async function loadCatalog() {
  const [providers, models, services, periods] = await Promise.all([
    api.get('/admin/providers').then(result => result.providers || []).catch(() => []),
    api.get('/admin/models').then(result => result.models || []).catch(() => []),
    api.get('/admin/jev').then(result => result.services || []).catch(() => []),
    api.get('/admin/time-periods').then(result => result.periods || []).catch(() => [])
  ]);
  app.catalog = {
    providers: Array.isArray(providers) ? providers : [],
    models: Array.isArray(models) ? models : [],
    services: Array.isArray(services) ? services : [],
    periods: Array.isArray(periods) ? periods : []
  };
  app.models = app.catalog.models;
  app.periods = app.catalog.periods
    .map(period => (period && period.name) || '')
    .filter(Boolean);
  return app.catalog;
}

/* boot 兜底 / 新建：创建新画布必须等数据库目录就绪；目录为空或缺少 provider 时
   controller.newDraftTree() 抛明确错误，这里提示并把画布留在「无草稿」状态。 */
async function loadDraftTree() {
  try {
    await app.catalogReady;
  } catch (error) { /* 目录加载失败时由 newDraftTree 给出明确错误 */ }
  try {
    controller.loadTree(controller.newDraftTree(), false);
    return true;
  } catch (error) {
    toast(String(error && error.message ? error.message : error), 'error');
    return false;
  }
}

async function boot() {
  createKit();
  if (isLogSnapshot()) return bootLogSnapshot();
  bindEvents();
  renderPalette();
  /* 目录与决策组解耦：目录抖动不能清空已加载的草稿；只有「新建画布」需要目录。 */
  app.catalogReady = loadCatalog();
  try {
    const treeResult = await api.get('/admin/trees');
    controller.trees = treeResult.trees || [];
    controller.snapshotRevision = treeResult.snapshot_revision || '';
    renderTreeSelect();
    /* 多图生命周期：启动优先打开默认图（公开 /v1 使用的图），没有默认图时退回
       列表第一张。不能用「第一张 enabled 图」推导 —— 有多张启用图时顺序不代表
       默认关系，而且第一张启用的图未必是默认图。 */
    const preferred = controller.trees.find(tree => tree.default) || controller.trees[0];
    if (preferred) {
      const result = await api.get('/admin/trees/' + encodeURIComponent(preferred.id));
      const tree = result && result.tree;
      if (tree && tree.root) controller.loadTree(tree, true);
      else await loadDraftTree();
    } else {
      await loadDraftTree();
    }
  } catch (error) {
    controller.trees = [];
    toast(tf('cv.toastLoadFail', { err: apiErrText(error) }), 'error');
    await loadDraftTree();
  }
  /* 树加载完成即解锁画布与节点库；目录只影响检查器选项与新画布。 */
  app.booted = true;
  await app.catalogReady;
  if (!store.tree) return;
  /* 目录晚到：重绘一次让检查器的下拉框拿到最新选项。
     若用户正在检查器里编辑，跳过这次重绘（下一次选择/保存自然带上新目录），
     否则会清掉还没提交的输入。 */
  if (inspectorEditing()) return;
  renderAll();
  refreshDirty();
}

/* 模块在 </body> 前加载，DOM 已就绪：先同步画一次首屏文案，再进入异步 boot。
   #cvEnableLabel 没有静态种子也没有 data-i18n（唯一 owner 是 paintLifecycle）,
   此时 store.tree 仍是 null，函数会落到 markup 声明的 draft / cv.publish；
   语言已由 nav.js 的 setLang(LANG) 在 import 阶段定好，所以英文会话从第一帧
   起就是英文，而不是先闪一帧中文再被 boot 后的 paintLifecycle 覆盖。 */
paintLifecycle();
boot();

/* 装配层视图对象（无 window 全局）：供模块间回调与测试观察使用。 */
export { app, store, controller, change, renderAll, toast, updateToolbar };
