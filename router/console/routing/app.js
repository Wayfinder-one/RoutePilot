/* RTApp · Policy v4 编辑页装配（Task 8 / 规格 §8）
   ------------------------------------------------------------------
   本文件只做 DOM 装配、事件绑定与渲染：
     · DraftStore 持有 {resource, policy, layout} 草稿、双基线与撤销历史；
     · RoutingController 持有 /admin/policies 的保存/启停/切换/删除编排；
     · editor.js 渲染问题/目标/规则/设置四个编辑区（只编辑数据，不求解路由）；
     · tryrun.js 持有试算面板：一次 /admin/try，只读展示返回的一次 JEV、
       各答案与逐条规则判断（复用 ../trace.js 视图），前端不重新求值任何
       规则；布局（折叠状态）经独立 /layout API 保存。
   所有编辑仍走一次 store.change(kind, mutate) = 一个历史项。
   ------------------------------------------------------------------ */
import '../nav.js';
import { api, apiErrText, esc } from '../api.js';
import { onLangChange, t, tf } from '../i18n.js';
import { DraftStore } from './draft.js';
import { RoutingController } from './controller.js';
import { editorEditing, renderEditor, revealPath } from './editor.js';
import {
  currentTryResult, initTryRun, renderTryResult, resetTryRun,
} from './tryrun.js';

const $ = (sel, scope) => (scope || document).querySelector(sel);

const store = new DraftStore();

let toastSeq = 0;
/* 共享的「丢弃草稿 / 删除策略」确认框；null = 未打开。 */
let confirmState = null;

/* 装配层暴露给模块与测试观察的只读视图；没有 window 全局。 */
const app = {
  models: [],
  periods: [],
  issues: [],
  issueRows: [],
  booted: false,
  lastToast: '',
  controller: null,
  get policy() { return store.policy; },
  get layout() { return store.layout; },
  get resource() { return store.resource; },
  get saved() { return store.saved; },
  get policyId() { return app.controller ? app.controller.policyId : ''; },
  get policies() { return app.controller ? app.controller.policies : []; },
  get saving() { return store.saving; },
  get tryRunning() { return store.tryRunning; },
  get tryResult() { return currentTryResult(); },
  get changeKind() { return store.dirtyKind; },
  kind() { return store.dirtyKind; },
  change: (kind, mutate) => change(kind, mutate),
  render: () => renderAll(),
  toast: (message, tone) => toast(message, tone),
  selectPolicy: id => app.controller.selectPolicy(id),
};

const hooks = {
  toast: (message, tone) => toast(message, tone),
  askDiscard: reason => askDiscard(reason),
  askDelete: policyId => askDelete(policyId),
  render: () => renderAll(),
  renderIssues: () => renderIssues(),
  revealIssues: () => revealIssues(),
  refreshDirty: () => refreshDirty(),
  syncSaving: () => updateToolbar(),
  setIssues: issues => { app.issues = issues || []; },
  isEditorEditing: () => editorEditing(),
  renderPolicySelect: () => renderPolicySelect(),
  getModels: () => app.models,
  change: (kind, mutate) => change(kind, mutate),
};

const controller = new RoutingController({ store: store, hooks: hooks });
app.controller = controller;

/* ── 草稿订阅：Store 是唯一写入路径，装配层只负责重绘 ─────────── */

function handleStoreEvent(event) {
  if (event.type === 'saved' || event.type === 'layoutSaved') {
    refreshDirty();
    updateToolbar();
    return;
  }
  if (event.type === 'load') {
    /* 换策略/新建草稿：在途试算响应随之过期，结果区整体清空。 */
    resetTryRun();
    renderAll();
    refreshDirty();
    return;
  }
  if (event.type === 'undo' || event.type === 'redo') {
    renderAll();
    refreshDirty();
    toast(t(event.type === 'undo' ? 'pv.toastUndo' : 'pv.toastRedo'));
    return;
  }
  renderAll();
  refreshDirty();
}

store.subscribe(handleStoreEvent);

/* ── 渲染：编辑器 + 校验条 + 顶栏 ─────────────────────────────── */

function renderAll() {
  renderEditor(store, app.models, app.periods, {
    change: change,
    toast: toast,
  });
  renderIssues();
  updateToolbar();
}

function renderPolicySelect() {
  const select = $('#cvPolicySelect');
  if (!select) return;
  const list = controller.policies || [];
  const options = list.map(resource =>
    '<option value="' + esc(resource.id) + '">' +
    esc((resource.policy && resource.policy.name) || resource.id) +
    (resource.enabled ? ' · ' + esc(t('pv.enabled')) : '') +
    '</option>').join('');
  const known = list.some(resource => resource.id === controller.policyId);
  const current = controller.policyId
    ? '<option value="' + esc(controller.policyId) + '">' +
      esc(store.policy && store.policy.name ? store.policy.name : controller.policyId) +
      (store.saved ? '' : ' · ' + esc(t('pv.unsaved'))) + '</option>'
    : '';
  select.innerHTML = options + (known ? '' : current);
  select.value = controller.policyId || '';
}

function issueCounts() {
  const issues = app.issues || [];
  return {
    hard: issues.filter(issue => issue && issue.level !== 'hint'),
    hints: issues.filter(issue => issue && issue.level === 'hint'),
  };
}

function renderIssues() {
  const counts = issueCounts();
  const text = tf('pv.issuesCount', { errors: counts.hard.length, hints: counts.hints.length });
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
  app.issueRows = rows;
  if (panel.hidden) { panel.innerHTML = ''; return; }
  /* 服务端 422 Issue：{code, zh, en, path, level, params}；文本按语言取，定位靠 path。 */
  panel.innerHTML = rows.map((issue, index) => {
    const lang = document.documentElement.lang === 'en' ? 'en' : 'zh';
    const title = (lang === 'en' && issue.en) ? issue.en : (issue.zh || issue.en || issue.code || '');
    return '<div class="rtc-issue ' + (issue.level === 'hint' ? 'is-hint' : 'is-hard') + '">' +
      '<span class="rtc-idot"></span>' +
      '<span class="rtc-issue-text">' + esc(title) +
      (issue.path ? ' <code class="mono">' + esc(issue.path) + '</code>' : '') + '</span>' +
      (issue.path
        ? '<button type="button" class="rtc-issue-locate" data-locate="1" data-issue-idx="' + index +
          '" data-issue-path="' + esc(issue.path) + '">' + esc(t('pv.issueLocate')) + '</button>'
        : '') +
      '</div>';
  }).join('') || '<div class="rtc-issue is-ok">' + esc(t('pv.issueNone')) + '</div>';
}

function revealIssues() {
  const panel = $('#cvIssues');
  if (!panel) return;
  panel.hidden = false;
  renderIssues();
  const button = $('#cvIssuesBtn');
  if (button) button.setAttribute('aria-expanded', 'true');
}

function updateToolbar() {
  const undo = $('#cvUndo');
  const redo = $('#cvRedo');
  const busy = !!store.tryRunning || !!store.saving;
  if (undo) {
    undo.disabled = busy || !store.canUndo;
    undo.title = t('pv.undo');
  }
  if (redo) {
    redo.disabled = busy || !store.canRedo;
    redo.title = t('pv.redo');
  }
  ['cvNewPolicy', 'cvLoadExample', 'cvSave', 'cvSaveLayout', 'cvDeletePolicy', 'cvTryBtn', 'cvTryRun']
    .forEach(id => {
      const button = document.getElementById(id);
      if (!button) return;
      button.disabled = busy ||
        (id === 'cvSaveLayout' && !store.saved) ||
        /* 未保存草稿也持有本地 UUID 形状的 policyId：只认 store.saved，
           否则首屏空库时删除按钮可点，DELETE 打到不存在的资源上（404）。 */
        (id === 'cvDeletePolicy' && (!controller.policyId || !store.saved));
    });
  const save = $('#cvSave');
  if (save) {
    save.setAttribute('aria-busy', String(!!store.saving));
    save.classList.toggle('is-saving', !!store.saving);
    const label = save.querySelector('span[data-i18n]');
    if (label) label.textContent = store.saving ? t('pv.saving') : t('pv.save');
  }
  const enabled = !!(store.resource && store.resource.enabled);
  const enable = $('#cvEnable');
  if (enable) enable.classList.toggle('is-on', enabled);
  const enableLabel = $('#cvEnableLabel');
  if (enableLabel) enableLabel.textContent = t(enabled ? 'pv.enabled' : 'pv.disabled');
  const enabledPill = $('#cvEnabled');
  const enabledText = $('#cvEnabledLabel');
  if (enabledPill) {
    enabledPill.hidden = !enabled;
    enabledPill.classList.toggle('ok', enabled);
    enabledPill.classList.toggle('warn', !enabled);
  }
  if (enabledText) enabledText.textContent = t(enabled ? 'pv.enabled' : 'pv.disabled');
  const revision = $('#cvRevision');
  if (revision) {
    revision.textContent = store.resource && store.resource.policy_revision
      ? tf('pv.revision', { rev: String(store.resource.policy_revision).slice(0, 10) })
      : t('pv.unpublished');
  }
}

function refreshDirty() {
  const kind = store.dirtyKind;
  const badge = $('#cvDirty');
  if (badge) {
    badge.hidden = kind === 'clean';
    badge.dataset.kind = kind;
    badge.textContent = kind === 'layout' ? t('pv.dirtyLayout') : t('pv.dirty');
  }
  const clean = $('#cvClean');
  if (clean) clean.hidden = kind !== 'clean';
  updateToolbar();
}

/* ── 历史入口（唯一的 store.change 包装） ─────────────────────── */

function change(kind, mutate) {
  if (!store.draft) return false;
  if (store.tryRunning) {
    toast(t('pv.tryEditBlocked'), 'warn');
    return false;
  }
  return store.change(kind, mutate);
}

/* ── toast ────────────────────────────────────────────────────── */

function toast(message, tone) {
  const host = $('#cvToast');
  if (!host) return;
  toastSeq += 1;
  const item = document.createElement('div');
  item.className = 'rtc-toast' + (tone ? ' is-' + tone : '');
  item.dataset.toast = String(toastSeq);
  const icon = '<svg class="rtc-ic" aria-hidden="true"><use href="#ri-' +
    (tone === 'warn' || tone === 'error' ? 'alert' : 'check') + '"/></svg>';
  item.innerHTML = icon + '<span></span>';
  item.querySelector('span').textContent = String(message == null ? '' : message);
  host.appendChild(item);
  app.lastToast = String(message == null ? '' : message);
  host.dataset.last = app.lastToast;
  setTimeout(() => { if (item.parentNode) item.parentNode.removeChild(item); }, 4200);
}

/* ── 确认框（丢弃草稿 / 删除策略） ────────────────────────────── */

const CONFIRM_TITLES = {
  switch: 'pv.confirmSwitchTitle',
  new: 'pv.confirmNewTitle',
  example: 'pv.confirmExampleTitle',
};

function askDiscard(reason) {
  return new Promise(resolve => {
    const modal = $('#cvConfirmModal');
    const body = $('#cvConfirmBody');
    if (!modal || !body) { resolve(true); return; }
    if (confirmState) { confirmState.resolve(false); confirmState = null; }
    const title = t(CONFIRM_TITLES[reason] || 'pv.confirmSwitchTitle');
    body.innerHTML = '<h3>' + esc(title) + '</h3>' +
      '<p class="rtc-dialog-lead">' + esc(t('pv.confirmDirtyBody')) + '</p>' +
      '<div class="rtc-dialog-acts">' +
      '<button type="button" class="btn sm" data-act="confirm-cancel">' + esc(t('pv.confirmCancel')) + '</button>' +
      '<button type="button" class="btn sm danger" data-act="confirm-discard">' + esc(t('pv.confirmDiscard')) + '</button>' +
      '</div>';
    body.setAttribute('aria-label', title);
    modal.hidden = false;
    confirmState = { resolve: resolve };
    const cancel = body.querySelector('[data-act="confirm-cancel"]');
    if (cancel) cancel.focus();
  });
}

function askDelete(policyId) {
  return new Promise(resolve => {
    const modal = $('#cvConfirmModal');
    const body = $('#cvConfirmBody');
    if (!modal || !body) { resolve(false); return; }
    if (confirmState) { confirmState.resolve(false); confirmState = null; }
    const title = t('pv.confirmDeleteTitle');
    body.innerHTML = '<h3>' + esc(title) + '</h3>' +
      '<p class="rtc-dialog-lead mono">' + esc(policyId) + '</p>' +
      '<p class="rtc-dialog-note">' + esc(t('pv.confirmDeleteBody')) + '</p>' +
      '<div class="rtc-dialog-acts">' +
      '<button type="button" class="btn sm" data-act="confirm-cancel">' + esc(t('pv.confirmCancel')) + '</button>' +
      '<button type="button" class="btn sm danger" data-act="confirm-discard">' + esc(t('pv.confirmDeleteOk')) + '</button>' +
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

/* ── 事件绑定 ─────────────────────────────────────────────────── */

function bindEvents() {
  $('#cvUndo').addEventListener('click', () => {
    if (!store.undo()) toast(t('pv.toastNothingUndo'), 'warn');
  });
  $('#cvRedo').addEventListener('click', () => {
    if (!store.redo()) toast(t('pv.toastNothingRedo'), 'warn');
  });
  const requireBooted = action => function () {
    if (!app.booted) {
      toast(t('pv.toastNotReady'), 'warn');
      return false;
    }
    return action.apply(null, arguments);
  };
  const actions = {
    cvNewPolicy: requireBooted(() => controller.newPolicy()),
    cvLoadExample: requireBooted(() => controller.loadExample()),
    cvSave: () => controller.savePolicy(),
    cvSaveLayout: () => controller.saveLayout(),
    cvEnable: () => controller.enable(),
    cvDeletePolicy: requireBooted(() => controller.deletePolicy()),
    cvTryBtn: () => {
      const section = document.getElementById('policyTry');
      if (section) section.scrollIntoView({ block: 'center' });
      const input = $('#cvTryInput');
      if (input) input.focus();
    },
  };
  Object.keys(actions).forEach(id => {
    const button = document.getElementById(id);
    if (button) button.addEventListener('click', () => { actions[id](); });
  });
  /* 试算面板（输入 Enter / 运行按钮）由 tryrun.js 自持绑定。 */
  initTryRun({ store: store, toast: toast, updateToolbar: updateToolbar });
  $('#cvPolicySelect').addEventListener('change', async event => {
    const id = event.target.value;
    if (!id || id === controller.policyId) return;
    /* 选择框立刻回到当前策略；确认/加载成功后再由 renderPolicySelect 重建。 */
    event.target.value = controller.policyId;
    await controller.selectPolicy(id);
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
    if (issue) revealPath(issue.path);
  });
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
  window.addEventListener('beforeunload', event => {
    if (!store.draft || store.dirtyKind === 'clean') return;
    event.preventDefault();
    event.returnValue = '';
  });
  onLangChange(() => {
    if (!app.booted) return;
    renderPolicySelect();
    renderAll();
    refreshDirty();
    renderTryResult();
  });
}

/* ── 启动 ─────────────────────────────────────────────────────── */

async function boot() {
  bindEvents();
  renderIssues();
  /* 模型目录与策略解耦：/admin/models 抖动不能回退或清空已加载的草稿；
     策略装载只依赖 /admin/policies，编辑器不等目录就能用。 */
  const modelsReady = api.get('/admin/models')
    .then(result => { app.models = result.models || []; })
    .catch(() => { app.models = []; });
  const periodsReady = api.get('/admin/time-periods')
    .then(result => {
      app.periods = (result.periods || []).map(period => period && period.name).filter(Boolean);
    })
    .catch(() => { app.periods = []; });
  try {
    const list = await api.get('/admin/policies');
    controller.policies = list.policies || [];
    controller.snapshotRevision = list.snapshot_revision || '';
    const enabled = controller.policies.find(item => item.enabled) || controller.policies[0];
    if (enabled) {
      const result = await api.get('/admin/policies/' + encodeURIComponent(enabled.id));
      const resource = result && result.policy_resource;
      const layout = await api.get(
        '/admin/policies/' + encodeURIComponent(enabled.id) + '/layout').catch(() => ({}));
      if (resource && resource.policy) controller.loadPolicy(resource, layout, true);
      else controller.loadNewDraft();
    } else {
      controller.loadNewDraft();
    }
  } catch (error) {
    controller.policies = [];
    toast(tf('pv.toastLoadFail', { err: apiErrText(error) }), 'error');
    controller.loadNewDraft();
  }
  /* 策略加载完成即解锁编辑器；模型/时段目录只影响下拉选项，不能阻塞建模。 */
  app.booted = true;
  renderPolicySelect();
  await Promise.all([modelsReady, periodsReady]);
  if (!store.draft) return;
  /* 目录晚到重绘一次（下拉框拿到新选项）；用户正在编辑时跳过，
     下一次事件自然带上新目录，不能清掉还没提交的输入。 */
  if (editorEditing()) return;
  renderAll();
  refreshDirty();
}

boot();

/* 装配层视图对象（无 window 全局）：供模块回调与测试观察使用。 */
export { app, store, controller, change, renderAll, toast, updateToolbar };
