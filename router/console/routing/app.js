/* RTApp · 决策组画布页装配（规格 §6.1–§6.3 / §6.10–§6.12）
   ------------------------------------------------------------------
   草稿状态：App.tree 是完整 envelope（name/timezone/sticky/root），App.graph === App.tree.root。
   所有编辑走 App.change(kind, mutate)：一次完整 envelope 快照入历史栈（上限 100），
   然后重算校验、清空回放、重绘卡片/边、刷新 dirty 徽标。
   本任务只装配画布骨架、视口、连线、快速创建、删除与历史栈；检查器 / 条件浮层 / 试算台
   由后续任务填充（inspector.js / condpop.js / tryrun.js 当前是可加载的占位命名空间）。
   ------------------------------------------------------------------ */
(function (root) {
  'use strict';

  const $ = (sel, scope) => (scope || document).querySelector(sel);
  const clone = value => JSON.parse(JSON.stringify(value));
  const esc = value => root.RTCards.escape(value);
  const t = key => root.t(key);
  const tf = (key, vars) => root.tf(key, vars);
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
        { kind: 'logic-or', icon: 'logic', labelKey: 'cv.node.logic-or', descKey: 'cv.desc.logic-or' },
        { kind: 'logic-not', icon: 'logic', labelKey: 'cv.node.logic-not', descKey: 'cv.desc.logic-not' }
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

  function defaultModelId() {
    if (App.models && App.models.length) return App.models[0].id;
    return 'qwen3-8b';
  }

  function reasoningFor(modelId) {
    const model = (App.models || []).find(item => item.id === modelId);
    if (!model) return null;
    const reasoning = model.reasoning ||
      (model.profile && typeof model.profile === 'object' ? model.profile.reasoning : null);
    if (!reasoning || reasoning.declared === false || reasoning.supported === false) return null;
    return reasoning.default || null;
  }

  function nextPriority(graph) {
    const used = Object.create(null);
    (graph.nodes || []).forEach(node => {
      if (node.type === 'target' && Number.isInteger(node.priority)) used[node.priority] = true;
    });
    let priority = 1;
    while (used[priority]) priority += 1;
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
    } else if (kind.indexOf('logic-') === 0) {
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
      node = {
        id: uniqueNodeId('condition-periods', graph, forcedId),
        type: 'condition', source: 'periods',
        periods: ['peak'],
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
        strategy: 'least_inflight',
        position: point
      };
    } else {
      return null;
    }
    return node;
  }

  /* ── App 状态 ─────────────────────────────────────────────────── */
  const App = {
    trees: [],
    models: [],
    periods: [],
    tree: null,
    graph: null,
    treeId: '',
    saved: null,
    issues: [],
    replay: null,
    changeKind: 'clean',
    selection: { kind: null, id: null },
    quick: null,
    viewport: { k: 1, tx: 0, ty: 0 },
    kit: null,
    booted: false,
    lastToast: '',
    palCollapsed: false,
    palSearch: '',
    mini: null,
    kind: function () { return this.changeKind; }
  };
  root.App = App;

  let stage = null;
  let world = null;
  let nodesLayer = null;
  let svg = null;
  let libDrag = null;
  let ghost = null;
  let toastSeq = 0;
  /* 检查器里有未提交输入：焦点仍在表单控件内，或本次渲染后发生过 input/change。 */
  let inspectorDirty = false;

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
    const keyword = App.palSearch.trim().toLowerCase();
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
    palette.classList.toggle('is-collapsed', App.palCollapsed);
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
    if (!root.RTInspector || typeof root.RTInspector.render !== 'function') return;
    const selection = App.kit ? App.kit.getSelection() : App.selection;
    root.RTInspector.render(selection);
    /* 新 DOM 里没有未提交输入；只有下一次 input/change 才会重新置脏。 */
    inspectorDirty = false;
  }

  /* ── 渲染：校验条 ─────────────────────────────────────────────── */
  function issueCounts() {
    const issues = App.issues || [];
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
    if (panel.hidden) { panel.innerHTML = ''; return; }
    const lang = (typeof LANG === 'string' && LANG === 'en') ? 'en' : 'zh';
    const rows = counts.hard.concat(counts.hints);
    panel.innerHTML = rows.map(issue => {
      const title = (lang === 'en' && issue.en) ? issue.en : issue.zh;
      /* 图级问题（时区 / 兜底 / 限额 / 解析）在检查器图设置里修；边问题开条件浮层；节点问题选中居中。 */
      const graphLevel = issue.code === 'invalid_timezone' || issue.code === 'fallback_missing' ||
        issue.code === 'graph_parse_error' || issue.port === 'otherwise_target' ||
        issue.port === 'error_fallback' || (!issue.node_id && !issue.edge_id);
      const target = issue.level === 'hint' ? '' : (graphLevel
        ? ' data-issue-graph="1"'
        : (issue.edge_id
          ? ' data-issue-edge="' + esc(issue.edge_id) + '"'
          : ' data-issue-node="' + esc(issue.node_id) + '"'));
      return '<div class="rtc-issue ' + (issue.level === 'hint' ? 'is-hint' : 'is-hard') + '">' +
        '<span class="rtc-idot"></span><span class="rtc-issue-text">' + esc(title) + '</span>' +
        (target ? '<button type="button" class="rtc-issue-locate" data-locate="1"' + target + '>' +
          esc(t('cv.issueLocate')) + '</button>' : '') +
        '</div>';
    }).join('') || '<div class="rtc-issue is-ok">' + esc(t('cv.issueNone')) + '</div>';
  }

  /* ── 渲染：小地图（只读 graph positions/viewport，不写 dirty） ── */
  const MINI_W = 168;
  const MINI_H = 104;
  const MINI_PAD = 8;

  function nodeBoxSize(node) {
    const el = App.kit ? App.kit.nodeEl(node.id) : null;
    return {
      w: el ? el.offsetWidth : 220,
      h: el ? el.offsetHeight : 100
    };
  }

  function renderMinimap() {
    const svgEl = $('#cvMinimapSvg');
    if (!svgEl) return;
    const graph = App.graph;
    if (!graph || !graph.nodes || !graph.nodes.length) {
      svgEl.innerHTML = '';
      App.mini = null;
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
    App.mini = { scale: scale, ox: ox, oy: oy, minX: minX, minY: minY };
    const mapX = x => ox + (x - minX) * scale;
    const mapY = y => oy + (y - minY) * scale;

    let html = graph.nodes.map(node => {
      const size = nodeBoxSize(node);
      return '<rect class="rtc-mini-node" data-type="' + esc(node.type) + '" x="' + mapX(node.position.x).toFixed(1) +
        '" y="' + mapY(node.position.y).toFixed(1) + '" width="' + Math.max(3, size.w * scale).toFixed(1) +
        '" height="' + Math.max(2, size.h * scale).toFixed(1) + '" rx="1.5"></rect>';
    }).join('');

    if (App.kit && stage) {
      const rect = stage.getBoundingClientRect();
      const a = App.kit.screenToWorld(rect.left, rect.top);
      const b = App.kit.screenToWorld(rect.right, rect.bottom);
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
    if (!App.mini) return;
    const rect = $('#cvMinimapSvg').getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const worldX = App.mini.minX + (px - App.mini.ox) / App.mini.scale;
    const worldY = App.mini.minY + (py - App.mini.oy) / App.mini.scale;
    let best = null;
    let bestDistance = Infinity;
    App.graph.nodes.forEach(node => {
      const size = nodeBoxSize(node);
      const dx = node.position.x + size.w / 2 - worldX;
      const dy = node.position.y + size.h / 2 - worldY;
      const distance = dx * dx + dy * dy;
      if (distance < bestDistance) { bestDistance = distance; best = node; }
    });
    if (best) App.kit.focusNode(best.id, { scale: App.viewport.k });
  }

  /* ── 渲染：全部 ───────────────────────────────────────────────── */
  function renderAll() {
    if (!App.tree || !App.kit) return;
    App.graph = App.tree.root;
    App.kit.attachGraph(App.graph);
    root.RTCards.render(App.graph, nodesLayer);
    App.kit.sync();
    renderIssues();
    renderInspector();
    renderMinimap();
    updateToolbar();
    if (root.RTCondPop && root.RTCondPop.isOpen && root.RTCondPop.isOpen()) root.RTCondPop.refresh();
  }

  function updateToolbar() {
    const undo = $('#cvUndo');
    const redo = $('#cvRedo');
    if (undo) {
      undo.disabled = !(App.kit && App.kit.canUndo());
      undo.title = t('cv.undoTitle');
    }
    if (redo) {
      redo.disabled = !(App.kit && App.kit.canRedo());
      redo.title = t('cv.redoTitle');
    }
    const enableLabel = $('#cvEnableLabel');
    const enabled = !!(App.tree && App.tree.enabled);
    if (enableLabel) enableLabel.textContent = enabled ? t('cv.enabled') : t('cv.disabled');
    const enable = $('#cvEnable');
    if (enable) enable.classList.toggle('is-on', enabled);
    const enabledPill = $('#cvEnabled');
    const enabledText = $('#cvEnabledLabel');
    if (enabledPill) {
      enabledPill.hidden = false;
      enabledPill.classList.toggle('ok', enabled);
      enabledPill.classList.toggle('warn', !enabled);
    }
    if (enabledText) enabledText.textContent = enabled ? t('cv.enabled') : t('cv.disabled');
    const revision = $('#cvRevision');
    if (revision) {
      revision.textContent = App.tree && App.tree.tree_revision
        ? tf('cv.revision', { rev: String(App.tree.tree_revision).slice(0, 10) })
        : t('cv.unpublished');
    }
  }

  function refreshDirty() {
    if (!App.tree) return;
    const kind = App.saved ? RTGraph.changeKind(App.saved, App.tree) : 'behavior';
    App.changeKind = kind;
    const badge = $('#cvDirty');
    if (badge) {
      badge.hidden = kind === 'clean';
      badge.dataset.kind = kind;
      badge.textContent = kind === 'layout' ? t('cv.dirtyLayout') : t('cv.dirty');
    }
    const clean = $('#cvClean');
    if (clean) clean.hidden = kind !== 'clean';
    updateToolbar();
  }

  /* ── 历史 / 编辑入口 ─────────────────────────────────────────── */
  function change(kind, mutate) {
    const before = RTGraph.clone(App.tree);
    mutate(App.tree);
    App.kit.pushHistory(before, RTGraph.clone(App.tree), kind);
    App.issues = RTGraph.validate(App.tree.root);
    App.replay = null;
    renderAll();
    refreshDirty();
  }
  App.change = change;

  function onRestore(envelope, action, kind) {
    App.tree = envelope;
    App.graph = App.tree.root;
    App.issues = RTGraph.validate(App.tree.root);
    App.replay = null;
    renderAll();
    refreshDirty();
    toast(action === 'undo' ? t('cv.toastUndo') : t('cv.toastRedo'));
  }

  function afterCanvasChange() {
    App.issues = RTGraph.validate(App.tree.root);
    App.replay = null;
    renderAll();
    refreshDirty();
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
    App.lastToast = String(message == null ? '' : message);
    host.dataset.last = App.lastToast;
    setTimeout(() => { if (item.parentNode) item.parentNode.removeChild(item); }, 4200);
  }
  App.toast = toast;

  /* ── 删除确认（节点 / 边 / choice 选项共用级联清单） ──────────── */
  function requestDelete(selection) {
    if (!selection || !selection.kind) return;
    const modal = $('#cvDeleteModal');
    if (!modal) return;
    const graph = App.graph;
    let title = '';
    let edges = [];
    let nodeId = '';
    let key = '';
    if (selection.kind === 'node') {
      const node = RTGraph.findNode(graph, selection.id);
      if (!node) return;
      edges = (graph.edges || []).filter(edge => edge.source === selection.id || edge.target === selection.id);
      title = tf('cv.deleteNode', { id: selection.id });
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
        ? ' · ' + RTGraph.conditionLabel(edge, graph)
        : '';
      return '<li>' + esc(edge.source + ' · ' + edge.source_port + ' → ' + edge.target + ' · ' + edge.target_port + condition) + '</li>';
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
    modal.hidden = false;
    modal.dataset.kind = selection.kind;
    modal.dataset.id = selection.id || '';
    modal.dataset.nodeId = nodeId;
    modal.dataset.key = key;
    body.setAttribute('aria-label', t('cv.deleteTitle'));
  }
  App.requestDelete = requestDelete;

  function hideDelete() {
    const modal = $('#cvDeleteModal');
    if (modal) modal.hidden = true;
  }

  function confirmDelete() {
    const modal = $('#cvDeleteModal');
    if (!modal || modal.hidden) return;
    const kind = modal.dataset.kind;
    const id = modal.dataset.id;
    const nodeId = modal.dataset.nodeId;
    const key = modal.dataset.key;
    hideDelete();
    if (!kind) return;
    /* 弹窗打开后目标可能已被撤销/重做移除：先在快照上试删，避免推入空历史。 */
    const probe = App.tree ? RTGraph.clone(App.tree) : null;
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
      const selection = App.kit.getSelection();
      if (selection.kind !== 'node' || selection.id !== nodeId) App.kit.select('node', nodeId, true);
    } else {
      App.kit.clearSelection();
    }
    if (cleared.length) toast(t('cv.toastFallbackCleared'), 'warn');
    else toast(t('cv.toastDeleted'));
  }

  /* ── 快速创建（输出端口拖到空白） ─────────────────────────────── */
  function quickCandidate(kind, context) {
    const probe = makeNode(kind, { x: 0, y: 0 }, App.graph, '__quick_probe');
    if (!probe) return false;
    const tmp = {
      id: App.graph.id || 'root', type: 'graph',
      nodes: (App.graph.nodes || []).concat([probe]),
      edges: App.graph.edges || [],
      otherwise_target: App.graph.otherwise_target,
      error_fallback: App.graph.error_fallback
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
    App.quick = context;
    const sourceNode = RTGraph.findNode(App.graph, context.sourceNodeId);
    const sourcePort = sourceNode ? RTGraph.portOf(sourceNode, context.sourcePort, App.graph) : null;
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
    App.quick = null;
  }

  function createQuick(kind) {
    const context = App.quick;
    closeQuick();
    if (!context) return;
    let createdNode = null;
    let createdEdge = null;
    change('behavior', tree => {
      createdNode = makeNode(kind, { x: context.x + 24, y: context.y - 40 }, tree.root);
      if (!createdNode) return;
      tree.root.nodes.push(createdNode);
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
      App.kit.select('edge', createdEdge.id);
      if (RTGraph.edgeType(createdEdge, App.graph) === 'number' && root.RTCondPop) {
        root.RTCondPop.open(createdEdge.id);
      }
      toast(tf('cv.toastQuickCreated', { label: kindLabel(kind) }));
    } else {
      toast(t('cv.toastConnectFail'), 'warn');
    }
  }

  /* ── 节点库创建（点击 / 拖入） ───────────────────────────────── */
  function createFromLibrary(kind, position) {
    if (!App.booted) return;
    let created = null;
    change('behavior', tree => {
      created = makeNode(kind, position, tree.root);
      if (created) tree.root.nodes.push(created);
    });
    if (!created) return;
    App.kit.select('node', created.id);
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
      const point = App.kit.screenToWorld(event.clientX, event.clientY);
      createFromLibrary(drag.kind, { x: point.x - 110, y: point.y - 50 });
      return;
    }
    const rect = stage.getBoundingClientRect();
    const point = App.kit.screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const offset = ((App.graph && App.graph.nodes.length) || 0) % 5 * 26;
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
    if (!App.booted) {
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

  /* ── 视口 / 历史 / 选中回调 ───────────────────────────────────── */
  function onViewport(viewport) {
    App.viewport = viewport;
    const label = $('#cvZoomLabel');
    if (label) label.textContent = Math.round(viewport.k * 100) + '%';
    renderMinimap();
  }

  function onSelect(selection) {
    App.selection = selection || { kind: null, id: null };
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
    App.kit.select('edge', edge.id);
    if (RTGraph.edgeType(edge, App.graph) === 'number') {
      if (root.RTCondPop) root.RTCondPop.open(edge.id);
    } else {
      toast(t('cv.toastBoolDefault'));
    }
  }

  function onHistory() {
    updateToolbar();
  }

  function closeOverlays() {
    if (App.quick) { closeQuick(); return true; }
    const pop = $('#cvCondPop');
    if (pop && !pop.hidden) { root.RTCondPop.close(); return true; }
    const modal = $('#cvDeleteModal');
    if (modal && !modal.hidden) { hideDelete(); return true; }
    const drawer = $('#cvTraceDrawer');
    if (drawer && !drawer.hidden) { drawer.hidden = true; return true; }
    const issues = $('#cvIssues');
    if (issues && !issues.hidden) { issues.hidden = true; renderIssues(); return true; }
    return false;
  }

  /* ── 顶栏动作 ─────────────────────────────────────────────────── */
  function bindToolbar() {
    $('#cvUndo').addEventListener('click', () => {
      if (!App.kit.undo()) toast(t('cv.toastNothingUndo'), 'warn');
    });
    $('#cvRedo').addEventListener('click', () => {
      if (!App.kit.redo()) toast(t('cv.toastNothingRedo'), 'warn');
    });
    $('#cvAutoLayout').addEventListener('click', () => {
      if (!App.graph) return;
      const positions = RTGraph.layoutPositions('dag', App.graph);
      const moved = (App.graph.nodes || []).some(node => {
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
      App.kit.fit({ padding: 24 });
      toast(t('cv.toastAutoLayout'));
    });
    ['cvImport', 'cvExport', 'cvSave', 'cvEnable', 'cvTryBtn'].forEach(id => {
      const button = document.getElementById(id);
      if (button) button.addEventListener('click', () => toast(t('cv.task12')));
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
      if (locate.dataset.issueGraph) {
        App.kit.clearSelection();
        const inspector = $('#cvInspector');
        if (inspector) {
          inspector.classList.add('is-focus');
          setTimeout(() => inspector.classList.remove('is-focus'), 900);
        }
      } else if (locate.dataset.issueEdge) {
        App.kit.focusEdge(locate.dataset.issueEdge);
        if (root.RTCondPop) root.RTCondPop.open(locate.dataset.issueEdge);
      } else if (locate.dataset.issueNode) {
        App.kit.focusNode(locate.dataset.issueNode);
      }
    });
  }

  function bindViewport() {
    $('#cvZoom').addEventListener('click', event => {
      const act = event.target.closest('[data-act]');
      if (!act) return;
      if (act.dataset.act === 'zoom-out') App.kit.zoomBy(1 / 1.2);
      else if (act.dataset.act === 'zoom-in') App.kit.zoomBy(1.2);
      else if (act.dataset.act === 'zoom-reset') App.kit.setZoom(1);
      else if (act.dataset.act === 'zoom-fit') App.kit.fit();
    });
    $('#cvMinimap').addEventListener('click', onMinimapClick);
  }

  function bindPalette() {
    $('#cvPalToggle').addEventListener('click', () => {
      App.palCollapsed = !App.palCollapsed;
      $('#cvPalette').classList.toggle('is-collapsed', App.palCollapsed);
      $('#cvPalSearch').disabled = App.palCollapsed;
    });
    $('#cvPalSearch').addEventListener('input', event => {
      App.palSearch = event.target.value || '';
      renderPalette();
    });
    $('#cvPalMini').addEventListener('click', () => {
      App.palCollapsed = false;
      renderPalette();
      $('#cvPalSearch').disabled = false;
    });
    $('#cvPalBody').addEventListener('pointerdown', onLibraryDown);
  }

  function bindStageCards() {
    nodesLayer.addEventListener('click', event => {
      const chip = event.target.closest('[data-cond-edge]');
      if (!chip) return;
      if (root.RTCondPop) root.RTCondPop.open(chip.dataset.condEdge);
    });
  }

  function bindTreeSelect() {
    $('#cvTreeSelect').addEventListener('change', async event => {
      const id = event.target.value;
      if (!id || id === App.treeId) return;
      if (App.changeKind !== 'clean') {
        event.target.value = App.treeId;
        toast(t('cv.toastSwitchDirty'), 'warn');
        return;
      }
      try {
        const result = await api.get('/admin/trees/' + encodeURIComponent(id));
        loadTree(result.tree, true);
      } catch (error) {
        event.target.value = App.treeId;
        toast(tf('cv.toastLoadFail', { err: apiErrText(error) }), 'error');
      }
    });
  }

  function bindOverlays() {
    $('#cvQuick').addEventListener('click', event => {
      const item = event.target.closest('[data-qkind]');
      if (item) createQuick(item.dataset.qkind);
    });
    document.addEventListener('pointerdown', event => {
      if (App.quick && !event.target.closest('#cvQuick')) closeQuick();
    }, true);
    $('#cvDeleteModal').addEventListener('click', event => {
      if (event.target === $('#cvDeleteModal')) { hideDelete(); return; }
      const act = event.target.closest('[data-act]');
      if (!act) return;
      if (act.dataset.act === 'delete-cancel') hideDelete();
      else if (act.dataset.act === 'delete-confirm') confirmDelete();
    });
  }

  const TRY_SAMPLES = [
    { label: 'cv.trySample1', text: 'cv.trySample1Text' },
    { label: 'cv.trySample2', text: 'cv.trySample2Text' },
    { label: 'cv.trySample3', text: 'cv.trySample3Text' },
    { label: 'cv.trySample4', text: 'cv.trySample4Text' },
    { label: 'cv.trySample5', text: 'cv.trySample5Text' }
  ];

  function renderTrySamples() {
    const chips = $('#cvTryChips');
    if (!chips) return;
    chips.innerHTML = TRY_SAMPLES.map((sample, index) =>
      '<button type="button" class="rtc-trychip" data-try-sample="' + index + '">' +
      esc(t(sample.label)) + '</button>').join('');
    const input = $('#cvTryInput');
    if (input) input.placeholder = t('cv.tryPlaceholder');
  }

  function bindTryBar() {
    renderTrySamples();
    $('#cvTryChips').addEventListener('click', event => {
      const chip = event.target.closest('[data-try-sample]');
      if (!chip) return;
      const sample = TRY_SAMPLES[Number(chip.dataset.trySample)];
      if (sample) $('#cvTryInput').value = t(sample.text);
    });
    $('#cvTryRun').addEventListener('click', () => toast(t('cv.task12')));
  }

  function bindTreeSelectOptions() {
    const select = $('#cvTreeSelect');
    if (!select) return;
    const options = (App.trees || []).map(tree =>
      '<option value="' + esc(tree.id) + '">' + esc(tree.name || tree.id) + '</option>').join('');
    select.innerHTML = options;
    if (App.treeId && !(App.trees || []).some(tree => tree.id === App.treeId)) {
      select.innerHTML += '<option value="' + esc(App.treeId) + '">' + esc(App.tree.name || App.treeId) + '</option>';
    }
    select.value = App.treeId;
  }

  /* ── 创建画布 + 绑定 ──────────────────────────────────────────── */
  function createKit() {
    stage = $('#cvStage');
    world = $('#cvWorld');
    nodesLayer = $('#cvNodes');
    svg = $('#cvEdges');
    App.kit = RTCanvas.create({
      stage: stage,
      world: world,
      nodesLayer: nodesLayer,
      svg: svg,
      graph: App.graph || { id: 'root', type: 'graph', nodes: [], edges: [] },
      callbacks: {
        onSelect: onSelect,
        onChange: afterCanvasChange,
        onInvalid: onInvalid,
        onQuickCreate: openQuickMenu,
        onViewport: onViewport,
        onRestore: onRestore,
        onEdgeCreated: onEdgeCreated,
        onDeleteRequest: requestDelete,
        onEscape: closeOverlays,
        onHistory: onHistory,
        getTree: () => RTGraph.clone(App.tree || { name: '', timezone: 'Asia/Shanghai', sticky: {}, root: App.graph })
      }
    });
    App.kit.init();
  }

  function normaliseTree(raw) {
    const tree = clone(raw || {});
    tree.root = tree.root || { id: 'root', type: 'graph', nodes: [], edges: [] };
    if (!tree.name) tree.name = t('cv.default.name');
    if (!tree.timezone) tree.timezone = 'Asia/Shanghai';
    if (!tree.sticky) tree.sticky = { enabled: false, ttl_seconds: 1800 };
    if (!Array.isArray(tree.root.nodes)) tree.root.nodes = [];
    if (!Array.isArray(tree.root.edges)) tree.root.edges = [];
    const positions = RTGraph.layoutPositions('dag', tree.root);
    tree.root.nodes.forEach((node, index) => {
      const position = node && node.position;
      const valid = position && typeof position.x === 'number' && Number.isFinite(position.x) &&
        typeof position.y === 'number' && Number.isFinite(position.y);
      if (!valid && node) {
        node.position = positions[node.id] || { x: 30 + index * 30, y: 30 + index * 30 };
      }
    });
    return tree;
  }

  function loadTree(raw, saved) {
    App.tree = normaliseTree(raw);
    App.graph = App.tree.root;
    App.treeId = raw && raw.id ? raw.id : 'local';
    App.saved = saved ? RTGraph.clone(App.tree) : null;
    App.issues = RTGraph.validate(App.tree.root);
    App.replay = null;
    App.quick = null;
    if (App.kit) App.kit.setGraph(App.graph, { resetHistory: true });
    bindTreeSelectOptions();
    renderAll();
    refreshDirty();
    requestAnimationFrame(() => {
      App.kit.fit();
      renderMinimap();
    });
  }

  function renderTreeSelect() {
    bindTreeSelectOptions();
  }

  function bindEvents() {
    bindInspectorEditState();
    bindToolbar();
    bindViewport();
    bindPalette();
    bindStageCards();
    bindTreeSelect();
    bindOverlays();
    bindTryBar();
    if (typeof root.onLangChange === 'function') {
      root.onLangChange(() => {
        if (!App.booted) return;
        renderPalette();
        renderTrySamples();
        renderAll();
        refreshDirty();
      });
    }
  }

  async function boot() {
    createKit();
    bindEvents();
    renderPalette();
    /* 模型目录与决策组解耦：/admin/models 抖动不能回退或清空已加载的草稿。 */
    const modelsReady = api.get('/admin/models')
      .then(result => { App.models = result.models || []; })
      .catch(() => { App.models = []; });
    const periodsReady = api.get('/admin/time-periods')
      .then(result => {
        App.periods = (result.periods || []).map(period => period && period.name).filter(Boolean);
      })
      .catch(() => { App.periods = []; });
    try {
      const treeResult = await api.get('/admin/trees');
      App.trees = treeResult.trees || [];
      renderTreeSelect();
      const enabled = App.trees.find(tree => tree.enabled) || App.trees[0];
      if (enabled) {
        const result = await api.get('/admin/trees/' + encodeURIComponent(enabled.id));
        const tree = result && result.tree;
        if (tree && tree.root) loadTree(tree, true);
        else loadTree(RTGraph.createMinimalTree(defaultModelId()), false);
      } else {
        loadTree(RTGraph.createMinimalTree(defaultModelId()), false);
      }
    } catch (error) {
      App.trees = [];
      App.treeId = 'local';
      toast(tf('cv.toastLoadFail', { err: apiErrText(error) }), 'error');
      loadTree(RTGraph.createMinimalTree(defaultModelId()), false);
    }
    /* 树加载完成即解锁画布与节点库；模型/时段目录只影响检查器选项，不能阻塞建模。 */
    App.booted = true;
    await Promise.all([modelsReady, periodsReady]);
    if (!App.tree) return;
    /* 模型/时段目录晚到：重绘一次让检查器的下拉框拿到最新选项。
       若用户正在检查器里编辑，跳过这次重绘（下一次选择/保存自然带上新目录），
       否则会清掉还没提交的输入。 */
    if (inspectorEditing()) return;
    renderAll();
    refreshDirty();
  }

  boot();
})(window);
