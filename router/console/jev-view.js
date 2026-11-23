/* RoutePilot console · 决策服务页视图层（单向 initJevView(actions)）
   ------------------------------------------------------------------
   页面 HTML 只保留一个 `<script type="module" src="jev.js">`（QA 断言每个
   控制台页面恰好一个 script 标签）。jev.js 静态 import 本模块，并在启动前
   调用 `initJevView({ validateWindow, saveConfig, saveWindow, testConnection })`；
   本模块不 import jev.js —— 依赖单向，没有循环。

   职责（只在展示与交互层，不 import 数据层）：
   - 文件内中英词典与 data-x 文案渲染；
   - 只读「服务档案」与编辑态的切换、取消还原快照；
   - 「保存并测试」的真实 Promise 编排：本地窗口校验 → 保存配置 → 保存窗口
     → 连接测试，逐阶段给出精确的部分成功文案；
   - 只读复测、「本次会话」测试徽章、友好测试摘要与按需展开的原始响应。
   ------------------------------------------------------------------ */
import { apiErrText } from './api.js';
import { LANG, onLangChange } from './i18n.js';

/* ── copy that is not already in i18n.js ─────────────────────────────── */
const X = {
  zh: {
    'archive.title': '服务档案',
    'archive.edit': '编辑配置',
    'archive.cancel': '取消编辑',
    'archive.hint': '保存后写入服务配置',
    'key.present': '已配置',
    'key.missing': '未配置',
    'edit.advanced': '高级配置',
    'edit.prompt': '提示词模板',
    'edit.window': '上下文长度',
    'edit.saveHint': '「保存并测试」依次写入配置、上下文，然后发起真实连接测试',
    'save.test': '保存并测试',
    'save.invalid': '未保存：上下文长度校验未通过',
    'save.configFail': '配置未保存：{err}（上下文与连接测试未执行）',
    'save.windowPartial': '连接与提示词已保存，但上下文窗口未保存：{err}（未运行连接测试）',
    'save.testPartial': '配置已保存，上下文也已保存，但连接测试失败：{err}',
    'save.success': '已保存，连接测试通过',
    'test.custom': '自定义测试文本',
    'test.empty': '尚未测试 · 运行一次以验证服务可达',
    'test.untested': '未测试',
    'test.running': '测试中…',
    'test.pass': '测试通过 · {ms}ms',
    'test.passBare': '测试通过',
    'test.fail': '测试失败',
    'test.success': '连接成功 · 选中 {key} · {ms}ms',
    'test.raw': '原始响应',
  },
  en: {
    'archive.title': 'Service profile',
    'archive.edit': 'Edit configuration',
    'archive.cancel': 'Cancel editing',
    'archive.hint': 'Saving writes the service config',
    'key.present': 'Configured',
    'key.missing': 'Not set',
    'edit.advanced': 'Advanced settings',
    'edit.prompt': 'Prompt templates',
    'edit.window': 'Context length',
    'edit.saveHint': '“Save and test” writes the config and context, then runs a real connection test',
    'save.test': 'Save and test',
    'save.invalid': 'Not saved: context length check failed',
    'save.configFail': 'Configuration not saved: {err} (context and connection test skipped)',
    'save.windowPartial': 'Connection and prompts saved, but the context window was not saved: {err} (connection test skipped)',
    'save.testPartial': 'Configuration saved and context saved, but the connection test failed: {err}',
    'save.success': 'Saved; connection test passed',
    'test.custom': 'Custom test text',
    'test.empty': 'Not tested yet · run once to verify the service',
    'test.untested': 'Not tested',
    'test.running': 'Testing…',
    'test.pass': 'Test passed · {ms}ms',
    'test.passBare': 'Test passed',
    'test.fail': 'Test failed',
    'test.success': 'Connection successful · selected {key} · {ms}ms',
    'test.raw': 'Raw response',
  },
};
const dict = () => X[LANG] || X.zh;
const tx = key => (dict()[key] ?? X.zh[key] ?? key);
const fill = (text, vars) => Object.entries(vars).reduce(
  (out, [k, v]) => out.split('{' + k + '}').join(String(v)), text);

const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];

function paintX(lang) {
  const table = X[lang] || X.zh;
  $$('[data-x]').forEach(el => {
    const value = table[el.dataset.x];
    if (value == null) return;
    if (el.dataset.xAttr) el.setAttribute(el.dataset.xAttr, value);
    else el.textContent = value;
  });
}

/* ── read-only profile mirrors the live DOM values ───────────────────── */
const FIELDS = ['baseUrl', 'model', 'apiKey', 'timeout', 'instructions',
  'stateTemplate', 'winContext', 'winInput'];
const KEY_KEEP = ['留空则保留现有密钥', 'Leave empty to keep the current key'];
const KEY_EMPTY = ['可选；也可使用 ROUTER_JEV_API_KEY', 'Optional; ROUTER_JEV_API_KEY also works'];
const TEST_DEFAULT = {
  zh: '请选择一个候选以验证决策服务。',
  en: 'Pick one of the candidates to verify the decision service.',
};

const readPane = $('#readPane');
const editPane = $('#editPane');
const editBtn = $('#editConfig');
const saveMsg = $('#saveMsg');
const saveJevBtn = $('#saveJev');
const testJevBtn = $('#testJev');
const apiKeyField = $('#apiKey');
const testOut = $('#testOut');

let ACTIONS = null;
let editing = false;
let saving = false;
let snapshot = null;

export function initJevView(actions) {
  ACTIONS = actions;
}

function fieldValue(id) {
  const el = $('#' + id);
  return el ? el.value : '';
}

/* which placeholder jev.js wrote tells us whether a key is stored */
function keyState() {
  const ph = apiKeyField ? apiKeyField.placeholder : '';
  if (KEY_KEEP.includes(ph)) return 'present';
  if (KEY_EMPTY.includes(ph)) return 'missing';
  return 'unknown';
}

function refreshProfile() {
  const roUrl = $('#roBaseUrl');
  if (roUrl) roUrl.textContent = fieldValue('baseUrl') || '—';
  const roModel = $('#roModel');
  if (roModel) roModel.textContent = fieldValue('model') || '—';

  const roKey = $('#roKey');
  if (roKey) {
    const state = keyState();
    roKey.textContent = state === 'present' ? tx('key.present')
      : state === 'missing' ? tx('key.missing') : '—';
    roKey.className = 'pill' + (state === 'present' ? ' ok' : '');
  }
}

/* ── save / test feedback ────────────────────────────────────────────── */
function showSaveMessage(text, bad) {
  if (!saveMsg) return;
  saveMsg.textContent = text;
  saveMsg.classList.toggle('bad', !!bad);
  saveMsg.classList.toggle('ok', !bad && !!text);
  const foot = document.querySelector('.card-foot');
  if (foot) foot.classList.toggle('is-empty', !text);
}

function showSaveError(key, error) {
  showSaveMessage(tx(key).replace('{err}', apiErrText(error)), true);
}

/* jev.js may write a boot error straight into #saveMsg; mirror only the
   presence of text/state so a failed boot is visible and coloured. */
function syncFeedbackStyle() {
  if (!saveMsg) return;
  const text = saveMsg.textContent.trim();
  if (text && !saveMsg.classList.contains('ok') && !saveMsg.classList.contains('bad')) {
    saveMsg.classList.add('bad');
  }
  if (!text) saveMsg.classList.remove('ok', 'bad');
  const foot = document.querySelector('.card-foot');
  if (foot) foot.classList.toggle('is-empty', !text);
}

function setActionsBusy(busy) {
  ['#saveJev', '#testJev', '#editConfig'].forEach(selector => {
    const element = $(selector);
    if (element) element.disabled = busy;
  });
  $$('#editPane input, #editPane textarea, #editPane select').forEach(element => {
    element.disabled = busy;
  });
}

/* ── “this session” test badge, read from the real result ───────────── */
function testState() {
  if (!testOut) return { kind: 'untested', ms: null };
  const kind = testOut.dataset.state || 'untested';
  const value = Number(testOut.dataset.durationMs);
  return { kind, ms: Number.isFinite(value) && value > 0 ? value : null };
}

function paintTestBadge() {
  const badge = $('#testBadge');
  const text = $('#testBadgeText');
  if (!badge || !text) return;
  const state = testState();
  let cls = 'pill';
  let label = tx('test.untested');
  if (state.kind === 'running') { cls = 'pill indigo'; label = tx('test.running'); }
  if (state.kind === 'fail') { cls = 'pill bad'; label = tx('test.fail'); }
  if (state.kind === 'pass') {
    cls = 'pill ok';
    label = state.ms == null ? tx('test.passBare') : fill(tx('test.pass'), { ms: state.ms });
  }
  if (badge.className !== cls) badge.className = cls;
  if (text.textContent !== label) text.textContent = label;
}

/* Repaint the friendly summary from the stored result fields so a language
   switch does not leave the last result in the other language. */
function paintTestText() {
  if (!testOut) return;
  if (testOut.dataset.state === 'pass') {
    testOut.textContent = fill(tx('test.success'), {
      key: testOut.dataset.selectedKey || '—',
      ms: testOut.dataset.durationMs || '—',
    });
  } else if (testOut.dataset.state === 'running') {
    testOut.textContent = tx('test.running');
  }
}

function paintTestPass(result) {
  if (!testOut) return;
  const data = result || {};
  const ms = Number.isFinite(Number(data.duration_ms)) ? Math.round(Number(data.duration_ms)) : null;
  const key = data.selected_key == null ? '—' : String(data.selected_key);
  testOut.dataset.state = 'pass';
  testOut.dataset.durationMs = ms == null ? '' : String(ms);
  testOut.dataset.selectedKey = key;
  paintTestText();
  /* the raw response stays available, but collapsed and closed by default */
  const details = $('#testRawDetails');
  if (details) {
    details.hidden = false;
    details.open = false;
  }
  const raw = $('#testRaw');
  if (raw) raw.textContent = JSON.stringify(data, null, 2);
  paintTestBadge();
}

function paintTestFailure(error) {
  if (!testOut) return;
  testOut.dataset.state = 'fail';
  testOut.dataset.durationMs = '';
  testOut.dataset.selectedKey = '';
  testOut.textContent = apiErrText(error);
  /* a failed run must not leave a stale raw payload behind */
  const details = $('#testRawDetails');
  if (details) {
    details.hidden = true;
    details.open = false;
  }
  const raw = $('#testRaw');
  if (raw) raw.textContent = '';
  paintTestBadge();
}

/* ── edit ⇄ read only ───────────────────────────────────────────────── */
function snapshotValues() {
  snapshot = {
    values: FIELDS.map(fieldValue),
    clearKey: !!($('#clearKey') || {}).checked,
  };
}
function restoreValues() {
  if (!snapshot) return;
  FIELDS.forEach((id, index) => {
    const el = $('#' + id);
    if (el) el.value = snapshot.values[index];
  });
  const clear = $('#clearKey');
  if (clear) clear.checked = snapshot.clearKey;
  /* re-run jev.js's own window validation (it listens for input) so the
     canceled state cannot leave a stale red message. */
  ['#winContext', '#winInput'].forEach(sel => {
    const el = $(sel);
    if (el) el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function paintEditButton() {
  if (!editBtn) return;
  editBtn.dataset.x = editing ? 'archive.cancel' : 'archive.edit';
  editBtn.textContent = tx(editBtn.dataset.x);
}

function setEditing(next) {
  editing = next;
  if (readPane) readPane.hidden = next;
  if (editPane) editPane.hidden = !next;
  paintEditButton();
  const card = $('#archiveCard');
  if (card) card.classList.toggle('is-editing', next);
  if (editBtn) editBtn.setAttribute('aria-expanded', String(next));
  if (next) {
    showSaveMessage('', false);
    snapshotValues();
    const first = $('#baseUrl');
    if (first) first.focus();
  } else if (editBtn) {
    editBtn.focus();
  }
  if (!next) refreshProfile();
}

if (editBtn) {
  editBtn.addEventListener('click', () => {
    if (!editing) { setEditing(true); return; }
    restoreValues();
    setEditing(false);
    showSaveMessage('', false);
  });
}

/* Focus the local field that failed validation; the advanced section may be
   closed, so open it first (a hidden input cannot take focus). */
function focusInvalidWindow() {
  const advanced = $('#jevAdvanced');
  if (advanced && !advanced.open) advanced.open = true;
  const context = parseInt(fieldValue('winContext'), 10);
  const input = parseInt(fieldValue('winInput'), 10);
  const target = Number.isFinite(context) && Number.isFinite(input) && input > context
    ? $('#winInput') : $('#winContext');
  const group = $('#windowCard');
  if (group && group.scrollIntoView) group.scrollIntoView({ block: 'center', behavior: 'smooth' });
  if (target) target.focus({ preventScroll: true });
}

/* ── one primary action: validate → save config → save window → test ── */
async function saveAllAndTest() {
  if (!editing || saving || !ACTIONS) return;

  /* 1 · local window validation: before any write. It also runs before the
     controls are disabled, because a disabled input cannot take the focus
     that points the user at the offending field. */
  try {
    ACTIONS.validateWindow();
  } catch (error) {
    showSaveMessage(tx('save.invalid') + ' · ' + apiErrText(error), true);
    focusInvalidWindow();
    return;
  }

  saving = true;
  setActionsBusy(true);
  try {
    /* 2 · config: a failure here means nothing else was written */
    try {
      await ACTIONS.saveConfig();
    } catch (error) {
      showSaveError('save.configFail', error);
      return;
    }

    /* 3 · window: the config already landed, so a failure is a partial save */
    try {
      await ACTIONS.saveWindow();
    } catch (error) {
      showSaveError('save.windowPartial', error);
      return;
    }

    /* 4 · connection test: config and window are saved; a test failure is
       still a partial success, never “nothing was saved”. */
    try {
      const result = await ACTIONS.testConnection(fieldValue('testText'));
      paintTestPass(result);
      showSaveMessage(tx('save.success'), false);
      setEditing(false);
    } catch (error) {
      showSaveError('save.testPartial', error);
    }
  } finally {
    saving = false;
    setActionsBusy(false);
  }
}

/* Read-only retest: never writes, only runs POST /admin/jev/test. */
async function runTest() {
  if (!ACTIONS || saving || !testOut) return;
  setActionsBusy(true);
  testOut.dataset.state = 'running';
  testOut.dataset.durationMs = '';
  testOut.dataset.selectedKey = '';
  paintTestText();
  paintTestBadge();
  try {
    paintTestPass(await ACTIONS.testConnection(fieldValue('testText')));
  } catch (error) {
    paintTestFailure(error);
  } finally {
    setActionsBusy(false);
  }
}

if (saveJevBtn) saveJevBtn.addEventListener('click', () => { saveAllAndTest(); });
if (testJevBtn) testJevBtn.addEventListener('click', () => { runTest(); });

/* ── language-sensitive placeholder / seed text ─────────────────────── */
function refreshApiKeyPlaceholder() {
  if (!apiKeyField) return;
  const ph = apiKeyField.placeholder;
  let next = null;
  if (KEY_KEEP.includes(ph)) next = KEY_KEEP;
  else if (KEY_EMPTY.includes(ph)) next = KEY_EMPTY;
  if (!next) return;
  const value = next[LANG === 'en' ? 1 : 0];
  if (value && apiKeyField.placeholder !== value) apiKeyField.placeholder = value;
}

function paintTestDefault() {
  const el = $('#testText');
  if (!el) return;
  if (el.value === TEST_DEFAULT.zh || el.value === TEST_DEFAULT.en) {
    el.value = TEST_DEFAULT[LANG] || TEST_DEFAULT.zh;
  }
}

/* jev.js writes field values and test defaults asynchronously (boot, save);
   a light poll keeps the profile honest without touching its logic. */
setInterval(() => {
  syncFeedbackStyle();
  if (editing) return;
  refreshProfile();
  refreshApiKeyPlaceholder();
}, 700);

paintX(LANG);
refreshProfile();
syncFeedbackStyle();
paintTestBadge();
paintTestDefault();
refreshApiKeyPlaceholder();

onLangChange(lang => {
  paintX(lang);
  paintEditButton();
  refreshProfile();
  paintTestBadge();
  paintTestText();
  paintTestDefault();
  refreshApiKeyPlaceholder();
});
