/* RoutePilot console · 决策服务目录页视图层（单向 initJevView(actions)）
   ------------------------------------------------------------------
   页面 HTML 只保留一个 `<script type="module" src="jev.js">`（QA 断言每个控制台
   页面恰好一个 script 标签）。jev.js 静态 import 本模块，并在启动前调用
   `initJevView({ listServices, createService, updateService, deleteService,
   testService })`；本模块不 import jev.js —— 依赖单向，没有循环。

   职责（只在展示与交互层，不 import 数据层）：
   - 左侧服务目录列表（按 id 排序）：id / model / base_url / 窗口 / 密钥状态，
     永不渲染密钥材料；列表语义是 <ul><li><button>（button 不再被 role=listitem
     覆盖，键盘/读屏仍按按钮语义工作）；
   - 右侧当前服务编辑器：服务 ID、连接、提示词模板、State 模板、输入/上下文窗口；
   - 每个服务各自的未保存草稿：切换服务时保存当前草稿、恢复目标草稿；语言切换
     与连接测试只重绘文案/结果，绝不回填表单值（不覆盖草稿）；
   - 测试结果 / 原始响应属于“当前服务”：切换、新建、保存后复位，避免把上一个
     服务的结果显示在新服务上；
   - 删除的唯一确认机制是共享 impact.js（409 `impact_confirmation_required`）：
     单击直接发 DELETE，被画布引用的服务由影响框确认后带精确哈希与
     `replacement_jev_id` 重试，取消不发任何写请求，陈旧哈希只提示重新加载；
     没有本地的“再次点击确认”二次门。
   ------------------------------------------------------------------ */
import { apiErrText } from './api.js';
import { LANG, onLangChange, t, tf } from './i18n.js';
import { withImpactConfirmation } from './impact.js';

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

/* 非影响类错误（404 / 422 / 网络）直接展示服务端 detail；409 影响确认与
   陈旧哈希由共享 impact.js 组件处理，这里不再重复兜底。 */
function showActionError(error, fallbackKey) {
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
    const empty = document.createElement('li');
    empty.className = 'jev-list-empty mut';
    empty.textContent = t('jev.listEmpty');
    list.appendChild(empty);
    return;
  }
  for (const service of services) {
    /* 列表项是 <li>，可点击主体是 <button>：两者语义分离，不再用
       role="listitem" 覆盖 button 的角色。 */
    const row = document.createElement('li');
    row.className = 'jev-list-row';
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'jev-item' + (service.id === currentId && !creating ? ' is-on' : '');
    item.dataset.jevServiceId = service.id;

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
    row.appendChild(item);
    list.appendChild(row);
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

/* 测试结果属于当前服务：切换 / 新建 / 保存后必须复位，绝不能把上一个服务的
   成功/失败结果或原始响应显示在新服务上。 */
function resetTestResult() {
  lastTest = { state: 'untested', selectedKey: '', ms: null };
  const out = $('#jevTestResult');
  if (out) {
    out.dataset.state = 'untested';
    out.textContent = t('jev.testEmpty');
  }
  const details = $('#jevTestRawDetails');
  if (details) { details.hidden = true; details.open = false; }
  const raw = $('#jevTestRaw');
  if (raw) raw.textContent = '';
}

/* ── selection / new / apply ─────────────────────────────────────────── */
function selectService(id) {
  const service = services.find(item => item.id === id);
  if (!service) return;
  if (!creating && currentId === id) return;
  storeCurrentDraft();
  currentId = id;
  creating = false;
  resetTestResult();
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
  resetTestResult();
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
  /* 保存/应用成功即重置测试结果：配置已变，旧结果不能泄漏到下一次交互。 */
  resetTestResult();
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
      const id = currentId;
      /* 更新被画布引用的服务：第一次 PUT 只拿 409 影响预览；确认后共享组件
         用精确哈希重试，取消不产生写请求。数据层回完整响应
         `{service, synced_trees}`：画布同步数量必须进入成功提示。 */
      const outcome = await withImpactConfirmation(context =>
        ACTIONS.updateService(id, draft, context ? context.impact_confirmation : undefined));
      if (!outcome.ok) {
        if (outcome.conflict) showMessage(t('impact.conflictToast'), true);
        return;
      }
      const payload = outcome.result && typeof outcome.result === 'object' ? outcome.result : {};
      const saved = payload.service || payload;
      upsertService(saved);
      applyService(saved);
      showSaved(saved.id, payload);
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

/* 服务端同步了哪些画布：已打开的画布不会自动重载，明确提示（与保存/删除文案
   合并，避免覆盖掉操作本身的反馈）。 */
function syncedCount(payload) {
  return payload && Array.isArray(payload.synced_trees) ? payload.synced_trees.length : 0;
}
function showSaved(id, payload) {
  const n = syncedCount(payload);
  showMessage(n ? tf('jev.msgSavedSynced', { id, n }) : tf('jev.msgSaved', { id }), false);
}
function showDeleted(id, payload) {
  const n = syncedCount(payload);
  showMessage(n ? tf('jev.msgDeletedSynced', { id, n }) : tf('jev.msgDeleted', { id }), false);
}

async function runTest() {
  if (!ACTIONS || saving || testing) return;
  if (creating || !currentId) {
    showMessage(t('jev.msgUnsavedTest'), true);
    return;
  }
  /* 测试可能跑满 120s：结果只属于发起测试的那个服务。测试期间用户允许切换
     服务（列表按钮不禁用），完成时若当前服务已不是它，就丢弃这次结果并复位
     展示状态，绝不能把结果画到另一个服务上。 */
  const testedId = currentId;
  testing = true;
  setBusy(true);
  showMessage('', false);
  paintTestRunning();
  const stillSelected = () => !creating && currentId === testedId;
  try {
    const result = await ACTIONS.testService(testedId, fieldValue('jevTestText'));
    if (stillSelected()) paintTestPass(result);
    else resetTestResult();
  } catch (error) {
    if (stillSelected()) paintTestFailure(error);
    else resetTestResult();
  } finally {
    testing = false;
    setBusy(false);
  }
}

async function deleteService() {
  if (!ACTIONS || saving) return;
  if (creating || !currentId) {
    showMessage(t('jev.msgPickService'), true);
    return;
  }
  const deletedId = currentId;
  saving = true;
  setBusy(true);
  try {
    /* 单击即发 DELETE：未被画布引用的服务直接删除；被引用的服务第一次 DELETE
       只拿 409 影响预览，共享影响框是唯一确认机制，确认时必须选择替代服务
       （replacement_jev_id），取消则不写任何东西。 */
    const outcome = await withImpactConfirmation(context => ACTIONS.deleteService(
      deletedId,
      context ? {
        replacement_jev_id: context.replacement_id,
        impact_confirmation: context.impact_confirmation,
      } : undefined), { operation: 'delete' });
    if (!outcome.ok) {
      if (outcome.conflict) showMessage(t('impact.conflictToast'), true);
      else showMessage('', false);
      return;
    }
    drafts.delete(deletedId);
    services = services.filter(service => service.id !== deletedId);
    currentId = null;
    creating = false;
    renderList();
    if (services.length) selectService(services[0].id);
    else newService();
    showDeleted(deletedId, outcome.result);
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
  /* #jevMsg 里存的是已翻译好的句子：切换语言后清空，避免中英混排。 */
  showMessage('', false);
});
