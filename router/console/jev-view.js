/* RoutePilot console · 决策服务目录页视图层（单向 initJevView(actions)）
   ------------------------------------------------------------------
   页面 HTML 只保留一个 `<script type="module" src="jev.js">`（QA 断言每个控制台
   页面恰好一个 script 标签）。jev.js 静态 import 本模块，并在启动前调用
   `initJevView({ listServices, createService, updateService, deleteService,
   testService })`；本模块不 import jev.js —— 依赖单向，没有循环。

   职责（只在展示与交互层，不 import 数据层）：
   - 左侧服务目录列表（按 id 排序）：id / model / base_url / 窗口 / 密钥状态，
     永不渲染密钥材料；
   - 右侧当前服务编辑器：服务 ID、连接、提示词模板、State 模板、输入/上下文窗口；
   - 每个服务各自的未保存草稿：切换服务时保存当前草稿、恢复目标草稿；语言切换
     与连接测试只重绘文案/结果，绝不回填表单值（不覆盖草稿）；
   - 409 `impact_confirmation_required` 明确展示“仍被画布引用、需要确认影响”，
     不静默吞掉、不自动重试（共享影响确认 UI 属于下一控制台单元）。
   ------------------------------------------------------------------ */
import { apiErrText } from './api.js';
import { LANG, onLangChange, t, tf } from './i18n.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

/* 与 router/jev/config.py 一致的服务端默认值：新建服务时预填，避免空模板 422。
   这只是默认文案，保存时仍走服务端校验（instructions 非空、模板含 {request}）。 */
const DEFAULT_INSTRUCTIONS = '你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明，选择最合适处理这条请求的模型。';
const DEFAULT_STATE_TEMPLATE = '【待判断的用户请求】\n{request}\n\n【任务】请选择最合适处理这条请求的模型。';
const WINDOW_LIMITS = { minContext: 512, minInput: 256, max: 131072 };

const TEST_DEFAULT = {
  zh: '请选择一个候选以验证决策服务。',
  en: 'Pick one of the candidates to verify the decision service.'
};

let ACTIONS = null;
let services = [];
let drafts = new Map();
let currentId = null;
let creating = false;
let saving = false;
let testing = false;
let deleteArmed = false;
let keyPresent = false;
let lastTest = { state: 'untested', selectedKey: '', ms: null };

/* ── DOM helpers ─────────────────────────────────────────────────────── */
function fieldValue(id) {
  const el = $('#' + id);
  return el ? el.value : '';
}
function setValue(id, value) {
  const el = $('#' + id);
  if (el) el.value = value == null ? '' : String(value);
}
function checked(id) {
  const el = $('#' + id);
  return !!(el && el.checked);
}
function setChecked(id, value) {
  const el = $('#' + id);
  if (el) el.checked = !!value;
}
function focusField(id) {
  const el = $('#' + id);
  if (el && el.focus) el.focus({ preventScroll: false });
}

/* ── draft ⇄ form ────────────────────────────────────────────────────── */
function readDraft() {
  return {
    id: fieldValue('jevServiceId').trim(),
    baseUrl: fieldValue('jevBaseUrl').trim(),
    model: fieldValue('jevModel').trim(),
    keyEnv: fieldValue('jevKeyEnv').trim(),
    timeoutSeconds: fieldValue('jevTimeout'),
    instructions: fieldValue('jevInstructions'),
    stateTemplate: fieldValue('jevStateTemplate'),
    inputTokens: fieldValue('jevInputTokens'),
    contextTokens: fieldValue('jevContextTokens'),
    apiKey: fieldValue('jevApiKey'),
    clearKey: checked('jevClearKey')
  };
}

function draftFromService(service) {
  return {
    id: service.id || '',
    baseUrl: service.base_url || '',
    model: service.model || '',
    keyEnv: service.key_env || '',
    timeoutSeconds: service.timeout_seconds == null ? 10 : service.timeout_seconds,
    instructions: service.instructions || '',
    stateTemplate: service.state_template || '',
    inputTokens: service.input_tokens == null ? 1024 : service.input_tokens,
    contextTokens: service.context_tokens == null ? 4096 : service.context_tokens,
    apiKey: '',
    clearKey: false
  };
}

function fillDraft(draft) {
  const source = draft || {};
  setValue('jevServiceId', source.id);
  setValue('jevBaseUrl', source.baseUrl);
  setValue('jevModel', source.model);
  setValue('jevKeyEnv', source.keyEnv);
  setValue('jevTimeout', source.timeoutSeconds);
  setValue('jevInstructions', source.instructions);
  setValue('jevStateTemplate', source.stateTemplate);
  setValue('jevInputTokens', source.inputTokens);
  setValue('jevContextTokens', source.contextTokens);
  /* 密钥输入永远不回填；草稿也不保留密钥。 */
  setValue('jevApiKey', '');
  setChecked('jevClearKey', false);
}

function blankDraft() {
  return {
    id: '', baseUrl: '', model: '', keyEnv: '', timeoutSeconds: 10,
    instructions: DEFAULT_INSTRUCTIONS, stateTemplate: DEFAULT_STATE_TEMPLATE,
    inputTokens: 1024, contextTokens: 4096, apiKey: '', clearKey: false
  };
}

function storeCurrentDraft() {
  if (!currentId || creating) return;
  /* 草稿只保留非密钥字段：用户输入的 api_key 只在写请求那一刻读取，
     不进入按服务缓存的草稿表，切换/重绘都不可能把它带回来。 */
  const draft = readDraft();
  delete draft.apiKey;
  delete draft.clearKey;
  drafts.set(currentId, draft);
}

/* ── window validation (local, before any write) ─────────────────────── */
function windowProblem(draft) {
  const context = Number(draft.contextTokens);
  const input = Number(draft.inputTokens);
  const { minContext, minInput, max } = WINDOW_LIMITS;
  if (!Number.isInteger(context) || context < minContext || context > max) {
    return tf('models.window.badContext', { min: minContext, max });
  }
  if (!Number.isInteger(input) || input < minInput || input > max) {
    return tf('models.window.badInput', { min: minInput, max });
  }
  if (input > context) return t('models.window.badRelation');
  return '';
}

/* ── feedback ────────────────────────────────────────────────────────── */
function showMessage(text, bad) {
  const msg = $('#jevMsg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.classList.toggle('bad', !!bad);
  msg.classList.toggle('ok', !bad && !!text);
}

/* 409 impact_confirmation_required：服务被已存画布引用。本单元只明确说明需要
   确认影响，绝不自动重试；共享影响确认 UI 会在下一控制台单元接入。 */
function showActionError(error, fallbackKey) {
  const detail = error && error.detail;
  if (error && error.status === 409 && detail && detail.code === 'impact_confirmation_required') {
    const affected = Array.isArray(detail.affected_trees) ? detail.affected_trees : [];
    const names = affected.map(tree => tree.name || tree.id).filter(Boolean).join('、');
    showMessage(tf('jev.impactRequired', { count: affected.length, trees: names || '—' }), true);
    return;
  }
  showMessage(tf(fallbackKey, { err: apiErrText(error) }), true);
}

function setBusy(busy) {
  ['#jevNew', '#jevSave', '#jevTest', '#jevDelete'].forEach(selector => {
    const el = $(selector);
    if (el) el.disabled = busy;
  });
  $$('#jevEditor input, #jevEditor textarea').forEach(el => { el.disabled = busy; });
}

/* ── key state (never the key material) ──────────────────────────────── */
function paintKeyPlaceholder(state) {
  const input = $('#jevApiKey');
  if (!input) return;
  input.placeholder = state ? t('jev.keyKeep') : t('jev.keyEmpty');
}
function keyPill(service) {
  const pill = document.createElement('span');
  pill.className = 'pill';
  let key = 'jev.keyNone';
  if (service.key_present) {
    key = service.key_source === 'env' ? 'jev.keyFromEnv' : 'jev.keyPresent';
    pill.classList.add(service.key_source === 'env' ? 'indigo' : 'ok');
  }
  pill.textContent = t(key);
  return pill;
}

/* ── list rendering ──────────────────────────────────────────────────── */
function renderList() {
  const list = $('#jevList');
  const count = $('#jevCount');
  if (count) count.textContent = String(services.length);
  if (!list) return;
  list.textContent = '';
  if (!services.length) {
    const empty = document.createElement('p');
    empty.className = 'jev-list-empty mut';
    empty.textContent = t('jev.listEmpty');
    list.appendChild(empty);
    return;
  }
  for (const service of services) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'jev-item' + (service.id === currentId && !creating ? ' is-on' : '');
    item.dataset.jevServiceId = service.id;
    item.setAttribute('role', 'listitem');

    const head = document.createElement('span');
    head.className = 'ji-row';
    const id = document.createElement('span');
    id.className = 'ji-id';
    id.textContent = service.id;
    head.appendChild(id);
    head.appendChild(keyPill(service));
    item.appendChild(head);

    const meta = document.createElement('span');
    meta.className = 'ji-meta';
    meta.textContent = [service.model, service.base_url].filter(Boolean).join(' · ');
    item.appendChild(meta);

    const win = document.createElement('span');
    win.className = 'ji-window';
    win.textContent = (service.input_tokens == null ? '—' : service.input_tokens) +
      ' / ' + (service.context_tokens == null ? '—' : service.context_tokens) + ' token';
    item.appendChild(win);

    item.addEventListener('click', () => selectService(service.id));
    list.appendChild(item);
  }
}

function paintStatus() {
  const status = $('#jevStatus');
  if (status) status.textContent = currentId && !creating ? currentId : t('jev.unconfigured');
  const mode = $('#jevMode');
  if (mode) mode.textContent = creating ? t('jev.modeNew') : currentId ? t('jev.modeEdit') : '—';
}

function markSelected() {
  $$('#jevList [data-jev-service-id]').forEach(item => {
    item.classList.toggle('is-on', !creating && item.dataset.jevServiceId === currentId);
  });
}

/* ── test result (language-sensitive, never touches the draft) ───────── */
function paintTestRunning() {
  lastTest = { state: 'running', selectedKey: '', ms: null };
  const out = $('#jevTestResult');
  if (!out) return;
  out.dataset.state = 'running';
  out.textContent = t('jev.testRunning');
  const details = $('#jevTestRawDetails');
  if (details) { details.hidden = true; details.open = false; }
  const raw = $('#jevTestRaw');
  if (raw) raw.textContent = '';
}

function paintTestPass(result) {
  const data = result || {};
  const ms = Number.isFinite(Number(data.duration_ms)) ? Math.round(Number(data.duration_ms)) : null;
  const key = data.selected_key == null ? '—' : String(data.selected_key);
  lastTest = { state: 'pass', selectedKey: key, ms };
  const out = $('#jevTestResult');
  if (!out) return;
  out.dataset.state = 'pass';
  out.textContent = ms == null
    ? tf('jev.testSuccessBare', { key })
    : tf('jev.testSuccess', { key, ms });
  const details = $('#jevTestRawDetails');
  if (details) { details.hidden = false; details.open = false; }
  const raw = $('#jevTestRaw');
  if (raw) raw.textContent = JSON.stringify(data, null, 2);
}

function paintTestFailure(error) {
  lastTest = { state: 'fail', selectedKey: '', ms: null };
  const out = $('#jevTestResult');
  if (!out) return;
  out.dataset.state = 'fail';
  out.textContent = tf('jev.msgTestFail', { err: apiErrText(error) });
  const details = $('#jevTestRawDetails');
  if (details) { details.hidden = true; details.open = false; }
  const raw = $('#jevTestRaw');
  if (raw) raw.textContent = '';
}

/* Repaint only the parts that are copy, using the stored result metadata. */
function paintTestStored() {
  const out = $('#jevTestResult');
  if (!out) return;
  if (lastTest.state === 'pass') {
    out.textContent = lastTest.ms == null
      ? tf('jev.testSuccessBare', { key: lastTest.selectedKey })
      : tf('jev.testSuccess', { key: lastTest.selectedKey, ms: lastTest.ms });
  } else if (lastTest.state === 'running') {
    out.textContent = t('jev.testRunning');
  } else if (lastTest.state === 'untested') {
    out.textContent = t('jev.testEmpty');
  }
  /* fail 状态保留服务端错误原文（与语言无关），不做二次翻译。 */
}

/* ── selection / new / apply ─────────────────────────────────────────── */
function selectService(id) {
  const service = services.find(item => item.id === id);
  if (!service) return;
  if (!creating && currentId === id) return;
  storeCurrentDraft();
  currentId = id;
  creating = false;
  deleteArmed = false;
  keyPresent = !!service.key_present;
  const draft = drafts.get(id) || draftFromService(service);
  fillDraft(draft);
  const idInput = $('#jevServiceId');
  if (idInput) idInput.readOnly = true;
  paintKeyPlaceholder(keyPresent);
  paintStatus();
  markSelected();
  showMessage('', false);
}

function newService() {
  storeCurrentDraft();
  currentId = null;
  creating = true;
  deleteArmed = false;
  keyPresent = false;
  fillDraft(blankDraft());
  const idInput = $('#jevServiceId');
  if (idInput) idInput.readOnly = false;
  paintKeyPlaceholder(false);
  paintStatus();
  markSelected();
  showMessage('', false);
  focusField('jevServiceId');
}

function applyService(service) {
  currentId = service.id;
  creating = false;
  keyPresent = !!service.key_present;
  const draft = draftFromService(service);
  drafts.set(service.id, draft);
  fillDraft(draft);
  const idInput = $('#jevServiceId');
  if (idInput) idInput.readOnly = true;
  paintKeyPlaceholder(keyPresent);
  renderList();
  paintStatus();
  markSelected();
}

function upsertService(service) {
  const index = services.findIndex(item => item.id === service.id);
  if (index >= 0) services[index] = service;
  else services.push(service);
  services.sort((left, right) => left.id.localeCompare(right.id));
}

/* ── actions ─────────────────────────────────────────────────────────── */
async function saveService() {
  if (!ACTIONS || saving) return;
  const draft = readDraft();
  if (!draft.id) {
    showMessage(t('jev.msgIdRequired'), true);
    focusField('jevServiceId');
    return;
  }
  /* 本地先校验窗口，避免一次注定 422 的写请求；创建与编辑同样适用。 */
  const problem = windowProblem(draft);
  if (problem) {
    showMessage(tf('jev.invalidWindow', { err: problem }), true);
    return;
  }
  saving = true;
  setBusy(true);
  showMessage('', false);
  try {
    if (creating) {
      const saved = await ACTIONS.createService(draft);
      upsertService(saved);
      applyService(saved);
      showMessage(tf('jev.msgCreated', { id: saved.id }), false);
    } else if (currentId) {
      const saved = await ACTIONS.updateService(currentId, draft);
      upsertService(saved);
      applyService(saved);
      showMessage(tf('jev.msgSaved', { id: saved.id }), false);
    } else {
      showMessage(t('jev.msgPickService'), true);
    }
  } catch (error) {
    showActionError(error, 'jev.msgSaveFail');
  } finally {
    saving = false;
    setBusy(false);
  }
}

async function runTest() {
  if (!ACTIONS || saving || testing) return;
  if (creating || !currentId) {
    showMessage(t('jev.msgUnsavedTest'), true);
    return;
  }
  testing = true;
  setBusy(true);
  showMessage('', false);
  paintTestRunning();
  try {
    paintTestPass(await ACTIONS.testService(currentId, fieldValue('jevTestText')));
  } catch (error) {
    paintTestFailure(error);
  } finally {
    testing = false;
    setBusy(false);
  }
}

function resetDeleteArm() {
  deleteArmed = false;
  const btn = $('#jevDelete');
  if (!btn) return;
  btn.dataset.armed = '0';
  btn.classList.remove('armed');
  btn.textContent = t('jev.delete');
}

async function deleteService() {
  if (!ACTIONS || saving) return;
  if (creating || !currentId) {
    showMessage(t('jev.msgPickService'), true);
    return;
  }
  if (!deleteArmed) {
    deleteArmed = true;
    const btn = $('#jevDelete');
    if (btn) {
      btn.dataset.armed = '1';
      btn.classList.add('armed');
      btn.textContent = t('jev.confirmDelete');
    }
    showMessage(tf('jev.msgDeleteArmed', { id: currentId }), false);
    setTimeout(() => { if (deleteArmed) resetDeleteArm(); }, 3500);
    return;
  }
  const deletedId = currentId;
  resetDeleteArm();
  saving = true;
  setBusy(true);
  try {
    await ACTIONS.deleteService(deletedId);
    drafts.delete(deletedId);
    services = services.filter(service => service.id !== deletedId);
    currentId = null;
    creating = false;
    renderList();
    if (services.length) selectService(services[0].id);
    else newService();
    showMessage(tf('jev.msgDeleted', { id: deletedId }), false);
  } catch (error) {
    showActionError(error, 'jev.msgDeleteFail');
  } finally {
    saving = false;
    setBusy(false);
  }
}

/* ── language-sensitive copy ─────────────────────────────────────────── */
function paintTitle() {
  const title = 'RoutePilot · ' + t('jev.title');
  if (document.title !== title) document.title = title;
}

function paintTestDefault() {
  const el = $('#jevTestText');
  if (!el) return;
  const value = el.value.trim();
  if (value === '' || value === TEST_DEFAULT.zh || value === TEST_DEFAULT.en) {
    el.value = TEST_DEFAULT[LANG] || TEST_DEFAULT.zh;
  }
}

function scrollToEditor() {
  const card = $('#jevEditor');
  if (card && card.scrollIntoView) card.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

/* ── wiring ──────────────────────────────────────────────────────────── */
export function initJevView(actions) {
  ACTIONS = actions;
  const save = $('#jevSave');
  if (save) save.addEventListener('click', () => { saveService(); });
  const test = $('#jevTest');
  if (test) test.addEventListener('click', () => { runTest(); });
  const remove = $('#jevDelete');
  if (remove) remove.addEventListener('click', () => { deleteService(); });
  const create = $('#jevNew');
  if (create) create.addEventListener('click', () => { newService(); scrollToEditor(); });
  boot();
}

async function boot() {
  paintTitle();
  paintTestDefault();
  paintTestStored();
  try {
    services = (await ACTIONS.listServices()).slice().sort((left, right) => left.id.localeCompare(right.id));
  } catch (error) {
    renderList();
    paintStatus();
    showMessage(tf('jev.msgLoadFail', { err: apiErrText(error) }), true);
    return;
  }
  renderList();
  if (services.length) selectService(services[0].id);
  else newService();
}

onLangChange(() => {
  paintTitle();
  paintTestDefault();
  paintKeyPlaceholder(keyPresent);
  renderList();
  paintStatus();
  paintTestStored();
  resetDeleteArm();
  /* #jevMsg 里存的是已翻译好的句子：切换语言后清空，避免中英混排。 */
  showMessage('', false);
});
