/* RTCondPop · 输入槽位条件编辑器（规格 §6.6–§6.7）
   ------------------------------------------------------------------
   两个入口共用同一套表单与提交路径：
     · 槽位行条件 chip / 新建数值边 → open(edgeId, anchor)
     · 检查器边分支 → formHTML / readForm / validateAndCommit
   打开时保存 condition 副本；取消、Esc、点击关闭都不写草稿。
   确认时把 draft 交给 validateAndCommit：校验通过才调用 store.change('behavior', …)，
   因此一次确认只产生一个历史项。图里不存在「正在编辑的半成品条件」。

   ES module：app.js 在装配时调用 configureCondPop({ store, getGraph, nodeName })；
   纯函数显式接收 graph，cards.js / inspector.js 直接复用。
   ------------------------------------------------------------------ */
import * as RTGraph from './graph.js';
import { t } from '../i18n.js';

let deps = null;
export function configureCondPop(next) { deps = next || null; }
const graphNow = () => (deps && typeof deps.getGraph === 'function' ? deps.getGraph() : null);
const nodeNameOf = (node, graph) => (deps && typeof deps.nodeName === 'function'
  ? deps.nodeName(node, graph) : (node && node.id));
const esc = value => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const clone = value => (value == null ? value : JSON.parse(JSON.stringify(value)));

/* 任务书给定的校验片段（逐字保留）。UI 文案仍走 i18n：见 messageFor()。 */
function numericCondition(op, raw) {
  if (raw.trim() === '') return { ok: false, message: '请输入阈值。' };
  const value = Number(raw);
  if (!Number.isFinite(value) || Math.abs(value) > 1e9) {
    return { ok: false, message: '阈值必须是有限数值，且绝对值不超过 1e9。' };
  }
  return { ok: true, condition: { op, value } };
}

const MESSAGE_KEYS = {
  '请输入阈值。': 'cv.cond.needValue',
  '阈值必须是有限数值，且绝对值不超过 1e9。': 'cv.cond.badValue'
};

function messageFor(result) {
  if (!result || result.ok) return '';
  const key = MESSAGE_KEYS[result.message];
  return key ? t(key) : (result.message || t('cv.cond.invalid'));
}

function error(message) {
  return { ok: false, message: message };
}

function edgeOf(edgeId, graph) {
  const value = graph || graphNow();
  if (!value || !Array.isArray(value.edges)) return null;
  return value.edges.find(edge => edge && edge.id === edgeId) || null;
}

function edgeType(edge, graph) {
  return RTGraph.edgeType(edge, graph || graphNow());
}

function isStartEdge(edge, graph) {
  const value = graph || graphNow();
  const source = edge && RTGraph.findNode(value, edge.source);
  return !!(source && source.type === 'start');
}

function canEditCondition(edge, graph) {
  if (!edge) return false;
  if (edgeType(edge, graph) === 'number') return true;
  return !isStartEdge(edge, graph) || edge.condition != null;
}

function conditionLabel(condition, { start = false } = {}) {
  if (condition == null) return t(start ? 'cv.cond.startAlways' : 'cv.cond.direct');
  if (condition.op === 'is' && typeof condition.value === 'boolean') {
    if (condition.value === false) return t('cv.cond.negated');
    return t(start ? 'cv.cond.startAlways' : 'cv.cond.direct');
  }
  const symbol = RTGraph.OP[condition.op];
  if (symbol && Number.isFinite(Number(condition.value))) {
    return symbol + ' ' + Number(condition.value);
  }
  return t('cv.cond.invalid');
}

function conditionText(edge, graph) {
  if (!edge) return '';
  if (edgeType(edge, graph) === 'number') {
    if (!edge.condition) return t('cv.cond.unset');
    return conditionLabel(edge.condition, { start: false });
  }
  return conditionLabel(edge.condition, { start: isStartEdge(edge, graph) });
}

function booleanValueOf(condition) {
  if (condition == null) return true;
  if (condition && condition.op === 'is' && typeof condition.value === 'boolean') return condition.value;
  return undefined;
}

function sameCondition(edge, next, graph) {
  if (RTGraph.edgeType(edge, graph || graphNow()) === 'number') {
    return RTGraph.stableStringify(edge.condition == null ? null : edge.condition) ===
      RTGraph.stableStringify(next);
  }
  return booleanValueOf(edge.condition) === booleanValueOf(next);
}

/* ── 表单渲染（检查器复用） ─────────────────────────────────────── */

function formHTML(edge, graph) {
  if (!edge) return '';
  const type = edgeType(edge, graph);
  const condition = edge.condition;
  if (type === 'number') {
    const validOp = condition && RTGraph.NUM_OPS.indexOf(condition.op) >= 0 ? condition.op : 'gt';
    const value = condition && Number.isFinite(Number(condition.value)) ? String(Number(condition.value)) : '0.5';
    return '<div class="field-row rtc-cond-fields">' +
      '<label class="field"><span>' + esc(t('cv.condOp')) + '</span>' +
      '<select data-cond-field="op" aria-label="' + esc(t('cv.condOp')) + '">' +
      RTGraph.NUM_OPS.map(op =>
        '<option value="' + esc(op) + '"' + (op === validOp ? ' selected' : '') + '>' +
        esc(RTGraph.OP[op]) + '</option>').join('') +
      '</select></label>' +
      '<label class="field"><span>' + esc(t('cv.condValue')) + '</span>' +
      '<input type="text" inputmode="decimal" data-cond-field="value" value="' + esc(value) +
      '" aria-label="' + esc(t('cv.condValue')) + '"></label>' +
      '</div>';
  }
  if (!canEditCondition(edge, graph)) {
    return '<p class="rtc-cond-readonly" data-start-condition>' +
      esc(t('cv.cond.startAlways')) + '</p>';
  }
  const negated = booleanValueOf(condition) === false;
  return '<label class="field rtc-cond-negate">' +
    '<input type="checkbox" data-cond-field="negate" aria-label="' + esc(t('cv.condNegate')) + '"' +
    (negated ? ' checked' : '') + '><span>' + esc(t('cv.condNegate')) + '</span></label>';
}

/* 读取表单 → draft；不写草稿。scope 可以是元素或选择器。 */
function readForm(scope, edge, graph) {
  const container = typeof scope === 'string' ? document.querySelector(scope) : scope;
  if (!container || !edge) return error(t('cv.cond.invalid'));
  if (edgeType(edge, graph) === 'number') {
    const opElement = container.querySelector('[data-cond-field="op"]');
    const valueElement = container.querySelector('[data-cond-field="value"]');
    const op = opElement ? opElement.value : '';
    if (RTGraph.NUM_OPS.indexOf(op) < 0) return error(t('cv.cond.invalid'));
    const result = numericCondition(op, valueElement ? valueElement.value : '');
    if (!result.ok) return error(messageFor(result));
    return { ok: true, condition: result.condition };
  }
  if (!canEditCondition(edge, graph)) return { ok: true, condition: null };
  const negateElement = container.querySelector('[data-cond-field="negate"]');
  const negated = !!(negateElement && negateElement.checked);
  return { ok: true, condition: negated ? { op: 'is', value: false } : null };
}

function normaliseCondition(edge, condition, graph) {
  if (!edge) return error(t('cv.cond.edgeMissing'));
  if (edgeType(edge, graph) === 'number') {
    if (!condition || typeof condition !== 'object') return error(t('cv.cond.invalid'));
    if (RTGraph.NUM_OPS.indexOf(condition.op) < 0) return error(t('cv.cond.invalid'));
    const value = condition.value;
    if (typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > RTGraph.THRESHOLD_LIMIT) {
      return error(t('cv.cond.badValue'));
    }
    return { ok: true, condition: { op: condition.op, value: value } };
  }
  if (condition == null) return { ok: true, condition: null };
  if (condition && condition.op === 'is' && typeof condition.value === 'boolean') {
    return { ok: true, condition: condition.value ? null : { op: 'is', value: false } };
  }
  return error(t('cv.cond.invalid'));
}

/* ── 唯一提交路径 ───────────────────────────────────────────────── */

function validateAndCommit(edgeId, draftCondition) {
  const graph = graphNow();
  const edge = edgeOf(edgeId, graph);
  if (!deps || !deps.store || !edge) return error(t('cv.cond.edgeMissing'));
  const check = normaliseCondition(edge, draftCondition, graph);
  if (!check.ok) return check;
  if (sameCondition(edge, check.condition, graph)) {
    close();
    return { ok: true, changed: false };
  }
  /* store.change() 在运行锁/保存锁下返回 false：绝不能当作「已保存」关掉浮层。 */
  const committed = deps.store.change('behavior', tree => {
    const target = (tree.root.edges || []).find(item => item && item.id === edgeId);
    if (target) target.condition = clone(check.condition);
  });
  if (committed === false) return error(t('cv.saveBlocked'));
  close();
  return { ok: true, changed: true };
}

function showError(scope, message) {
  const container = typeof scope === 'string' ? document.querySelector(scope) : scope;
  if (!container) return;
  const box = container.querySelector('[data-cond-error]');
  if (!box) return;
  box.textContent = String(message == null ? '' : message);
  box.hidden = false;
}

function clearError(scope) {
  const container = typeof scope === 'string' ? document.querySelector(scope) : scope;
  if (!container) return;
  const box = container.querySelector('[data-cond-error]');
  if (!box) return;
  box.textContent = '';
  box.hidden = true;
}

/* ── 浮层 ───────────────────────────────────────────────────────── */

let state = null;

function element() {
  return document.getElementById('cvCondPop');
}

function position(pop, anchor) {
  pop.style.left = '';
  pop.style.top = '';
  pop.style.transform = '';
  if (!anchor) return;
  let box = null;
  if (anchor.getBoundingClientRect) box = anchor.getBoundingClientRect();
  else if (typeof anchor.clientX === 'number') {
    box = { left: anchor.clientX, top: anchor.clientY, right: anchor.clientX, bottom: anchor.clientY, width: 0, height: 0 };
  } else if (anchor && typeof anchor.left === 'number') box = anchor;
  if (!box) return;
  const width = pop.offsetWidth || 360;
  const height = pop.offsetHeight || 240;
  let x = box.left + (box.width || 0) / 2 - width / 2;
  let y = (box.bottom == null ? box.top : box.bottom) + 8;
  if (y + height > window.innerHeight - 8) y = Math.max(8, box.top - height - 8);
  pop.style.left = Math.max(8, Math.min(window.innerWidth - width - 8, x)) + 'px';
  pop.style.top = Math.max(8, Math.min(window.innerHeight - height - 8, y)) + 'px';
  pop.style.transform = 'none';
}

function paint(edge) {
  const pop = element();
  if (!pop) return;
  const graph = graphNow();
  const type = edgeType(edge, graph);
  const nameOf = id => {
    const node = graph ? RTGraph.findNode(graph, id) : null;
    return node ? nodeNameOf(node, graph) : id;
  };
  const path = nameOf(edge.source) + '.' + edge.source_port + ' → ' +
    nameOf(edge.target) + '.' + edge.target_port;
  pop.dataset.edgeId = edge.id;
  pop.dataset.edgeType = type;
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', t('cv.condTitle'));
  pop.innerHTML =
    '<form class="rtc-cond-form" data-cond-form="pop" novalidate>' +
    '<div class="rtc-condpop-h">' +
    '<span>' + esc(t('cv.condTitle')) + '</span>' +
    '<span class="rtc-condpop-path" title="' + esc(path) + '">' + esc(path) + '</span>' +
    '<button type="button" class="btn ghost sm rtc-condclose" data-act="cond-close" aria-label="' +
    esc(t('cv.condClose')) + '">' +
    '<svg class="rtc-ic" aria-hidden="true"><use href="#ri-close"/></svg></button>' +
    '</div>' +
    '<div class="rtc-condpop-body">' +
    '<p class="rtc-condpop-hint">' + esc(t('cv.condHint')) + '</p>' +
    '<div class="rtc-cond-fields">' + formHTML(edge) + '</div>' +
    '<div class="rtc-cond-error" data-cond-error role="alert" hidden></div>' +
    '<div class="rtc-dialog-acts">' +
    '<button type="button" class="btn sm" data-act="cond-cancel">' + esc(t('cv.condCancel')) + '</button>' +
    '<button type="submit" class="btn sm pri" data-act="cond-confirm">' + esc(t('cv.condConfirm')) + '</button>' +
    '</div></div></form>';
}

function triggerInfo(pop) {
  const active = document.activeElement;
  if (!active || active === document.body || active === pop || typeof active.focus !== 'function') return null;
  if (active.closest && active.closest('#cvCondPop')) return null;
  return {
    element: active,
    condEdge: active.dataset ? (active.dataset.condEdge || '') : ''
  };
}

/* 关闭后把焦点还给触发元素（a11y §6.11）。确认条件会触发 App.change →
   renderAll 重绘卡片；按条件 edge id 重新定位可编辑 chip 或静态开始边。 */
function restoreTrigger(info) {
  if (!info) return;
  let target = info.element && info.element.isConnected ? info.element : null;
  if (!target && info.condEdge) {
    target = Array.from(document.querySelectorAll(
      '[data-cond-edge], [data-start-condition][tabindex="-1"]'
    )).find(element =>
      (element.dataset.condEdge || element.dataset.startCondition || '') === info.condEdge
    ) || null;
  }
  if (target && typeof target.focus === 'function' && target !== document.activeElement) {
    target.focus({ preventScroll: true });
  }
}

function open(edgeId, anchor) {
  const graph = graphNow();
  const edge = edgeOf(edgeId, graph);
  if (!edge) return false;
  if (!canEditCondition(edge, graph)) return false;
  const pop = element();
  if (!pop) return false;
  const trigger = triggerInfo(pop);
  paint(edge);
  pop.hidden = false;
  state = {
    edgeId: edgeId,
    original: clone(edge.condition == null ? null : edge.condition),
    trigger: trigger
  };
  position(pop, anchor);
  const type = edgeType(edge, graph);
  const focus = pop.querySelector(
    type === 'number' ? '[data-cond-field="value"]' : '[data-cond-field="negate"]'
  );
  if (focus) focus.focus();
  return true;
}

function close() {
  const pop = element();
  const restore = state ? state.trigger : null;
  if (pop) {
    pop.hidden = true;
    if (state) delete pop.dataset.edgeId;
  }
  state = null;
  restoreTrigger(restore);
}

function isOpen() {
  const pop = element();
  return !!(pop && !pop.hidden && state);
}

function refresh() {
  if (!state) return;
  const graph = graphNow();
  const edge = edgeOf(state.edgeId, graph);
  if (!edge) { close(); return; }
  const pop = element();
  if (!pop) return;
  const hadFocus = pop.contains(document.activeElement);
  paint(edge);
  if (hadFocus) {
    const type = edgeType(edge, graph);
    const focus = pop.querySelector(
      type === 'number' ? '[data-cond-field="value"]' : '[data-cond-field="negate"]'
    );
    if (focus) focus.focus();
  }
}

/* 打开时保存副本；取消/Esc 不写，关闭时把焦点还给触发元素。 */
function cancel() {
  close();
}

function onSubmit(event) {
  const form = event.target.closest('[data-cond-form="pop"]');
  if (!form) return;
  event.preventDefault();
  const graph = graphNow();
  const edge = edgeOf(state && state.edgeId, graph);
  if (!edge) { close(); return; }
  const draft = readForm(form, edge, graph);
  if (!draft.ok) {
    showError(form, draft.message);
    return;
  }
  const result = validateAndCommit(edge.id, draft.condition);
  if (!result.ok) showError(form, result.message);
}

function onClick(event) {
  const action = event.target.closest('[data-act]');
  if (!action) return;
  if (!action.closest('#cvCondPop')) return;
  const name = action.dataset.act;
  if (name === 'cond-close' || name === 'cond-cancel') cancel();
}

function onKeyDown(event) {
  if (!state) return;
  if (event.key === 'Escape') {
    event.stopPropagation();
    close();
  }
}

function bind() {
  const pop = element();
  if (!pop || pop.dataset.bound === '1') return;
  pop.dataset.bound = '1';
  pop.addEventListener('click', onClick);
  pop.addEventListener('submit', onSubmit);
  pop.addEventListener('keydown', onKeyDown);
}

/* Node 侧导入纯函数（cards/inspector 复用）时没有 DOM：浮层绑定只在浏览器执行。 */
if (typeof document !== 'undefined') bind();

function currentEdgeId() {
  return state ? state.edgeId : '';
}

export {
  open,
  close,
  cancel,
  isOpen,
  refresh,
  isStartEdge,
  canEditCondition,
  conditionLabel,
  conditionText,
  formHTML,
  readForm,
  normaliseCondition,
  validateAndCommit,
  showError,
  clearError,
  numericCondition,
  currentEdgeId
};
