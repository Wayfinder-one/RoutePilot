/* RoutePilot console · JEV 模型页视图层（C 方案：只读档案 + 按需编辑）
   ------------------------------------------------------------------
   本模块由 jev.js 末尾动态 import 进来，页面 HTML 因此只保留一个
   `<script type="module" src="jev.js">`（scripts/console_qa.mjs 会断言每个
   控制台页面恰好一个 script 标签）。

   职责（只在展示层，不改 jev.js 的取数/保存/测试逻辑）：
   - 文件内中英词典与 data-x 文案渲染；
   - 只读「服务档案」与编辑态的切换、取消还原快照；
   - 档案数值 / 字符数 / 上下文摘要在每次 jev.js 写入 DOM 后的镜像；
   - 「本次会话」测试徽章（未测试 / 测试中 / 通过·Nms / 失败）；
   - 页级「保存全部」的校验前置与配置→上下文的顺序提交，以及 #saveMsg 反馈。
   ------------------------------------------------------------------ */
import { LANG, onLangChange } from './i18n.js';

/* ── copy that is not already in i18n.js ─────────────────────────────── */
const X = {
  zh: {
    'archive.title': '服务档案',
    'archive.edit': '编辑配置',
    'archive.cancel': '取消编辑',
    'archive.hint': '保存后写入服务配置',
    'file.window': '上下文窗口',
    'file.windowNote': '输入 / 预算',
    'file.instructions': '默认指令',
    'file.template': 'State 模板',
    'key.present': '已配置',
    'key.missing': '未配置',
    'unit.chars': '{n} 字符',
    'unit.seconds': '{n} 秒',
    'req.ok': '含 {request}',
    'req.bad': '缺 {request}',
    'edit.conn': '连接与鉴权',
    'edit.prompt': '提示词模板',
    'edit.window': '上下文长度',
    'save.saving': '保存中…',
    'save.all': '保存全部',
    'save.window': '保存上下文',
    'save.invalid': '未保存：上下文长度校验未通过',
    'save.windowFail': '上下文保存失败，请重试',
    'edit.saveHint': '「保存全部」会同时写入连接、提示词与上下文',
    'test.custom': '自定义测试文本',
    'test.label': '测试文本',
    'test.empty': '尚未测试 · 运行一次以验证服务可达',
    'test.untested': '未测试',
    'test.running': '测试中…',
    'test.pass': '测试通过 · {ms}ms',
    'test.passBare': '测试通过',
    'test.fail': '测试失败',
  },
  en: {
    'archive.title': 'Service profile',
    'archive.edit': 'Edit configuration',
    'archive.cancel': 'Cancel editing',
    'archive.hint': 'Saving writes the service config',
    'file.window': 'Context window',
    'file.windowNote': 'input / budget',
    'file.instructions': 'Default instructions',
    'file.template': 'State template',
    'key.present': 'Configured',
    'key.missing': 'Not set',
    'unit.chars': '{n} chars',
    'unit.seconds': '{n} s',
    'req.ok': 'includes {request}',
    'req.bad': 'missing {request}',
    'edit.conn': 'Connection & auth',
    'edit.prompt': 'Prompt templates',
    'edit.window': 'Context length',
    'save.saving': 'Saving…',
    'save.all': 'Save all',
    'save.window': 'Save context',
    'save.invalid': 'Not saved: context length check failed',
    'save.windowFail': 'Context was not saved — please retry',
    'edit.saveHint': '“Save all” writes connection, prompts and context',
    'test.custom': 'Custom test text',
    'test.label': 'Test text',
    'test.empty': 'Not tested yet · run once to verify the service',
    'test.untested': 'Not tested',
    'test.running': 'Testing…',
    'test.pass': 'Test passed · {ms}ms',
    'test.passBare': 'Test passed',
    'test.fail': 'Test failed',
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
const SAVED_TEXTS = ['已保存', 'Saved'];
const LOADING_TEXTS = ['加载中…', 'Loading…'];
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
const windowMsg = $('#windowMsg');
const saveJevBtn = $('#saveJev');
const saveWindowBtn = $('#saveWindow');
const apiKeyField = $('#apiKey');
const testOut = $('#testOut');

let editing = false;
let snapshot = null;

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
  const url = fieldValue('baseUrl');
  const model = fieldValue('model');
  const timeout = parseFloat(fieldValue('timeout'));
  const instructions = fieldValue('instructions');
  const template = fieldValue('stateTemplate');

  const roUrl = $('#roBaseUrl');
  if (roUrl) roUrl.textContent = url || '—';
  const roModel = $('#roModel');
  if (roModel) roModel.textContent = model || '—';
  const roTimeout = $('#roTimeout');
  if (roTimeout) roTimeout.textContent = Number.isFinite(timeout)
    ? fill(tx('unit.seconds'), { n: timeout }) : '—';

  const roKey = $('#roKey');
  if (roKey) {
    const state = keyState();
    roKey.textContent = state === 'present' ? tx('key.present')
      : state === 'missing' ? tx('key.missing') : '—';
    roKey.className = 'pill' + (state === 'present' ? ' ok' : '');
  }

  const roInstr = $('#roInstr');
  if (roInstr) roInstr.textContent = fill(tx('unit.chars'), { n: instructions.length });
  const roTpl = $('#roTpl');
  if (roTpl) roTpl.textContent = fill(tx('unit.chars'), { n: template.length });
  const roReq = $('#roReq');
  if (roReq) {
    const ok = template.includes('{request}');
    roReq.textContent = tx(ok ? 'req.ok' : 'req.bad');
    roReq.className = 'pill' + (ok ? ' ok' : ' bad');
  }
}

/* ── “this session” test badge, read from the real result ───────────── */
function testState() {
  if (!testOut) return { kind: 'untested' };
  if (testOut.querySelector('.out-empty')) return { kind: 'untested' };
  const card = testOut.querySelector('.card');
  if (card) {
    let ms = null;
    try {
      const data = JSON.parse(card.querySelector('pre')?.textContent || '');
      if (typeof data.duration_ms === 'number') ms = Math.round(data.duration_ms);
    } catch (error) { /* result without JSON => still a pass */ }
    return { kind: 'pass', ms };
  }
  const text = testOut.textContent.trim();
  if (!text) return { kind: 'untested' };
  if (LOADING_TEXTS.includes(text)) return { kind: 'running' };
  return { kind: 'fail' };
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
  const grid = document.querySelector('.jev-grid');
  if (grid) grid.classList.toggle('has-result', state.kind !== 'untested');
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
     canceled state cannot leave a stale red message or disabled button. */
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
  const grid = document.querySelector('.jev-grid');
  if (card) card.classList.toggle('is-editing', next);
  if (grid) grid.classList.toggle('is-editing', next);
  if (editBtn) editBtn.setAttribute('aria-expanded', String(next));
  if (next) {
    if (saveMsg) saveMsg.textContent = '';
    paintFeedback();
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
    if (saveMsg) saveMsg.textContent = '';
    paintFeedback();
  });
}

/* Page-level save = "save everything". It validates the context window first
   (the one client-side validation we have), then submits the config and the
   window one after another and only reports success when both succeeded.
   Nothing is submitted when the window is invalid — no silent skip. */
function waitFor(check, timeout = 6000) {
  return new Promise(resolve => {
    const started = Date.now();
    const timer = setInterval(() => {
      const result = check();
      if (result) { clearInterval(timer); resolve(result); return; }
      if (Date.now() - started > timeout) { clearInterval(timer); resolve(null); }
    }, 120);
  });
}

function focusInvalidWindow() {
  const context = parseInt(fieldValue('winContext'), 10);
  const input = parseInt(fieldValue('winInput'), 10);
  const target = Number.isFinite(context) && Number.isFinite(input) && input > context
    ? $('#winInput') : $('#winContext');
  const group = $('#windowCard');
  if (group && group.scrollIntoView) group.scrollIntoView({ block: 'center', behavior: 'smooth' });
  if (target) target.focus({ preventScroll: true });
}

function runWindowSave() {
  return new Promise(resolve => {
    const presets = $('#winContextPresets');
    let settled = false;
    let poll = 0;
    let timeout = 0;
    const finish = ok => {
      if (settled) return;
      settled = true;
      observer.disconnect();
      clearInterval(poll);
      clearTimeout(timeout);
      resolve(ok);
    };
    const observer = new MutationObserver(() => finish(true)); // jev.js re-renders presets on success
    if (presets) observer.observe(presets, { childList: true });
    poll = setInterval(() => {
      if (windowMsg && windowMsg.textContent.trim()) finish(false);
    }, 150);
    timeout = setTimeout(() => finish(false), 6000);
    awaitingWindowSave = true;
    if (saveWindowBtn) saveWindowBtn.click();
  });
}

let saving = false;
let bypassSaveClick = false;

async function saveAll() {
  if (!editing || saving) return;
  if (!saveJevBtn) return;
  saving = true;

  /* 1 · context window must be valid before anything is written */
  if (saveWindowBtn && saveWindowBtn.disabled) {
    if (saveMsg) saveMsg.textContent = tx('save.invalid');
    paintFeedback();
    focusInvalidWindow();
    saving = false;
    return;
  }

  /* 2 · connection + prompts (this is jev.js's own #saveJev handler) */
  const previous = saveMsg ? saveMsg.textContent : '';
  if (saveMsg) saveMsg.textContent = tx('save.saving');
  paintFeedback();
  bypassSaveClick = true;
  saveJevBtn.click();
  bypassSaveClick = false;
  const config = await waitFor(() => {
    const text = (saveMsg ? saveMsg.textContent : '').trim();
    if (!text || text === tx('save.saving')) return null;
    return SAVED_TEXTS.includes(text) ? 'ok' : 'error';
  });
  if (config !== 'ok') {
    saving = false;
    paintFeedback();
    return; // jev.js error text stays visible; nothing else is submitted
  }
  if (!editing) { saving = false; return; } // canceled while the request was in flight

  /* 3 · context window (its own handler), only after the config landed */
  const windowOk = await runWindowSave();
  saving = false;
  if (!windowOk) {
    if (saveMsg) saveMsg.textContent = tx('save.windowFail');
    paintFeedback();
    return; // stay in edit mode: never report success for a partial save
  }
  if (editing) setEditing(false); // success: one “已保存”, back to the profile
  paintFeedback();
}

/* Intercept the page-level button in the capture phase so jev.js's own
   listener only runs once our validation passed (otherwise it would start
   writing before we can block an invalid context window). */
const actionsRow = document.querySelector('.edit-actions');
if (actionsRow && saveJevBtn) {
  actionsRow.addEventListener('click', event => {
    const button = event.target.closest && event.target.closest('#saveJev');
    if (!button || bypassSaveClick) return;
    event.preventDefault();
    event.stopPropagation();
    saveAll();
  }, true);
}

/* canceling after a successful window save must keep the saved numbers */
let awaitingWindowSave = false;
if (saveWindowBtn) {
  saveWindowBtn.addEventListener('click', () => { awaitingWindowSave = true; });
  const presets = $('#winContextPresets');
  if (presets) {
    new MutationObserver(() => {
      if (!awaitingWindowSave) return;
      awaitingWindowSave = false;
      if (editing) {
        const values = { winContext: fieldValue('winContext'), winInput: fieldValue('winInput') };
        if (snapshot) FIELDS.forEach((id, index) => {
          if (id in values) snapshot.values[index] = values[id];
        });
      }
      refreshProfile();
    }).observe(presets, { childList: true });
  }
}

/* save feedback: colour comes from shared .modal-msg.ok/.bad; the card footer
   note + divider only exist while editing or while a message is showing. */
const footEl = document.querySelector('.card-foot');
function paintFeedback() {
  const text = saveMsg ? saveMsg.textContent.trim() : '';
  if (saveMsg) {
    saveMsg.classList.toggle('ok', SAVED_TEXTS.includes(text));
    saveMsg.classList.toggle('bad',
      !!text && !SAVED_TEXTS.includes(text) && text !== tx('save.saving'));
  }
  if (footEl) footEl.classList.toggle('is-empty', !text);
}
if (saveMsg) {
  new MutationObserver(paintFeedback)
    .observe(saveMsg, { childList: true, characterData: true, subtree: true });
}

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
if (apiKeyField) {
  new MutationObserver(refreshApiKeyPlaceholder)
    .observe(apiKeyField, { attributes: true, attributeFilter: ['placeholder'] });
}

function paintTestDefault() {
  const el = $('#testText');
  if (!el) return;
  if (el.value === TEST_DEFAULT.zh || el.value === TEST_DEFAULT.en) {
    el.value = TEST_DEFAULT[LANG] || TEST_DEFAULT.zh;
  }
}

/* ── wiring ─────────────────────────────────────────────────────────── */
if (testOut) {
  new MutationObserver(() => {
    paintTestBadge();
    paintTestDefault();
  }).observe(testOut, { childList: true, subtree: true, characterData: true });
}

/* jev.js writes field values asynchronously (boot, save, window save); a light
   poll keeps the profile honest without touching any of its logic. */
setInterval(() => {
  paintFeedback();
  if (editing) return;
  refreshProfile();
  refreshApiKeyPlaceholder();
}, 700);

paintX(LANG);
refreshProfile();
paintFeedback();
paintTestBadge();
paintTestDefault();
refreshApiKeyPlaceholder();

onLangChange(lang => {
  paintX(lang);
  paintEditButton();
  refreshProfile();
  paintTestBadge();
  paintTestDefault();
  refreshApiKeyPlaceholder();
});
