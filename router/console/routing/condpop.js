/* RTCondPop · 输入槽位条件编辑器（规格 §6.6–§6.7）
   ------------------------------------------------------------------
   两个入口共用同一套表单与提交路径：
     · 槽位行条件 chip / 新建数值边 → RTCondPop.open(edgeId, anchor)
     · 检查器边分支 → RTCondPop.formHTML / readForm / validateAndCommit
   打开时保存 condition 副本；取消、Esc、点击关闭都不写草稿。
   确认时把 draft 交给 validateAndCommit：校验通过才调用 App.change('behavior', …)，
   因此一次确认只产生一个历史项。图里不存在「正在编辑的半成品条件」。
   ------------------------------------------------------------------ */
(function (root) {
  'use strict';

  const esc = value => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const t = key => (typeof root.t === 'function' ? root.t(key) : key);
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

  function edgeOf(edgeId) {
    const app = root.App;
    const graph = app && app.graph;
    if (!graph || !Array.isArray(graph.edges)) return null;
    return graph.edges.find(edge => edge && edge.id === edgeId) || null;
  }

  function edgeType(edge) {
    const graph = root.App && root.App.graph;
    return root.RTGraph.edgeType(edge, graph);
  }

  function booleanValueOf(condition) {
    if (condition == null) return true;
    if (condition && condition.op === 'is' && typeof condition.value === 'boolean') return condition.value;
    return undefined;
  }

  function sameCondition(edge, next) {
    const graph = root.App && root.App.graph;
    if (root.RTGraph.edgeType(edge, graph) === 'number') {
      return root.RTGraph.stableStringify(edge.condition == null ? null : edge.condition) ===
        root.RTGraph.stableStringify(next);
    }
    return booleanValueOf(edge.condition) === booleanValueOf(next);
  }

  /* ── 表单渲染（检查器复用） ─────────────────────────────────────── */

  function formHTML(edge) {
    if (!edge) return '';
    const type = edgeType(edge);
    const condition = edge.condition;
    if (type === 'number') {
      const validOp = condition && root.RTGraph.NUM_OPS.indexOf(condition.op) >= 0 ? condition.op : 'gt';
      const value = condition && Number.isFinite(Number(condition.value)) ? String(Number(condition.value)) : '0.5';
      return '<div class="field-row rtc-cond-fields">' +
        '<label class="field"><span>' + esc(t('cv.condOp')) + '</span>' +
        '<select data-cond-field="op" aria-label="' + esc(t('cv.condOp')) + '">' +
        root.RTGraph.NUM_OPS.map(op =>
          '<option value="' + esc(op) + '"' + (op === validOp ? ' selected' : '') + '>' +
          esc(root.RTGraph.OP[op]) + '</option>').join('') +
        '</select></label>' +
        '<label class="field"><span>' + esc(t('cv.condValue')) + '</span>' +
        '<input type="text" inputmode="decimal" data-cond-field="value" value="' + esc(value) +
        '" aria-label="' + esc(t('cv.condValue')) + '"></label>' +
        '</div>';
    }
    const truthy = booleanValueOf(condition);
    return '<label class="field"><span>' + esc(t('cv.condBool')) + '</span>' +
      '<select data-cond-field="bool" aria-label="' + esc(t('cv.condBool')) + '">' +
      '<option value="true"' + (truthy !== false ? ' selected' : '') + '>' + esc(t('cv.condTrue')) + '</option>' +
      '<option value="false"' + (truthy === false ? ' selected' : '') + '>' + esc(t('cv.condFalse')) + '</option>' +
      '</select></label>';
  }

  /* 读取表单 → draft；不写草稿。scope 可以是元素或选择器。 */
  function readForm(scope, edge) {
    const container = typeof scope === 'string' ? document.querySelector(scope) : scope;
    if (!container || !edge) return error(t('cv.cond.invalid'));
    if (edgeType(edge) === 'number') {
      const opElement = container.querySelector('[data-cond-field="op"]');
      const valueElement = container.querySelector('[data-cond-field="value"]');
      const op = opElement ? opElement.value : '';
      if (root.RTGraph.NUM_OPS.indexOf(op) < 0) return error(t('cv.cond.invalid'));
      const result = numericCondition(op, valueElement ? valueElement.value : '');
      if (!result.ok) return error(messageFor(result));
      return { ok: true, condition: result.condition };
    }
    const boolElement = container.querySelector('[data-cond-field="bool"]');
    const wanted = !boolElement || boolElement.value === 'true';
    return { ok: true, condition: wanted ? null : { op: 'is', value: false } };
  }

  function normaliseCondition(edge, condition) {
    if (!edge) return error(t('cv.cond.edgeMissing'));
    if (edgeType(edge) === 'number') {
      if (!condition || typeof condition !== 'object') return error(t('cv.cond.invalid'));
      if (root.RTGraph.NUM_OPS.indexOf(condition.op) < 0) return error(t('cv.cond.invalid'));
      const value = condition.value;
      if (typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > root.RTGraph.THRESHOLD_LIMIT) {
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
    const app = root.App;
    const edge = edgeOf(edgeId);
    if (!app || !edge) return error(t('cv.cond.edgeMissing'));
    const check = normaliseCondition(edge, draftCondition);
    if (!check.ok) return check;
    if (sameCondition(edge, check.condition)) {
      close();
      return { ok: true, changed: false };
    }
    /* App.change() 在运行锁/保存锁下返回 false：绝不能当作「已保存」关掉浮层。 */
    const committed = app.change('behavior', tree => {
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
    const type = edgeType(edge);
    const graph = root.App && root.App.graph;
    const nameOf = id => {
      const node = graph && root.RTGraph ? root.RTGraph.findNode(graph, id) : null;
      return node && root.RTCards ? root.RTCards.nodeName(node, graph) : id;
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
     renderAll 重绘卡片，触发 chip 可能已被替换；按 data-cond-edge 重新定位。 */
  function restoreTrigger(info) {
    if (!info) return;
    let target = info.element && info.element.isConnected ? info.element : null;
    if (!target && info.condEdge) {
      target = Array.from(document.querySelectorAll('[data-cond-edge]'))
        .find(el => el.dataset.condEdge === info.condEdge) || null;
    }
    if (target && typeof target.focus === 'function' && target !== document.activeElement) {
      target.focus({ preventScroll: true });
    }
  }

  function open(edgeId, anchor) {
    const edge = edgeOf(edgeId);
    if (!edge) return false;
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
    const type = edgeType(edge);
    const focus = pop.querySelector(type === 'number' ? '[data-cond-field="value"]' : '[data-cond-field="bool"]');
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
    const edge = edgeOf(state.edgeId);
    if (!edge) { close(); return; }
    const pop = element();
    if (!pop) return;
    const hadFocus = pop.contains(document.activeElement);
    paint(edge);
    if (hadFocus) {
      const type = edgeType(edge);
      const focus = pop.querySelector(type === 'number' ? '[data-cond-field="value"]' : '[data-cond-field="bool"]');
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
    const edge = edgeOf(state && state.edgeId);
    if (!edge) { close(); return; }
    const draft = readForm(form, edge);
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

  bind();

  root.RTCondPop = {
    open: open,
    close: close,
    cancel: cancel,
    isOpen: isOpen,
    refresh: refresh,
    formHTML: formHTML,
    readForm: readForm,
    normaliseCondition: normaliseCondition,
    validateAndCommit: validateAndCommit,
    showError: showError,
    clearError: clearError,
    numericCondition: numericCondition,
    currentEdgeId: () => (state ? state.edgeId : '')
  };
})(window);
