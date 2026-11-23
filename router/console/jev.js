/* RoutePilot console · 决策服务页数据层（连接 / 提示词 / 上下文窗口 / 测试）
   ------------------------------------------------------------------
   本模块是页面唯一入口：静态 import 视图层（jev-view.js），并在启动前用
   `initJevView({ validateWindow, saveConfig, saveWindow, testConnection })`
   把四个真实异步动作交给它。依赖是单向的（jev.js → jev-view.js），视图不
   import 本模块，因此没有循环依赖，也没有合成 click / MutationObserver
   保存编排：视图直接 await 这些动作。

   动作只负责请求与数据回填（showConfig / renderWindow）；异常原样抛给视图，
   由视图按阶段给出“什么已保存、什么没做”的部分成功文案。
   ------------------------------------------------------------------ */
import { api, apiErrText } from './api.js';
import { onLangChange, t, tf } from './i18n.js';
import { initJevView } from './jev-view.js';
import './nav.js';

const $ = selector => document.querySelector(selector);
let defaults = {};

function showConfig(body) {
  const config = body.config || {};
  defaults = body.defaults || defaults;
  $('#baseUrl').value = config.base_url || '';
  $('#model').value = config.model || '';
  $('#timeout').value = config.timeout_seconds ?? 10;
  $('#instructions').value = config.instructions || defaults.instructions || '';
  $('#stateTemplate').value = config.state_template || defaults.state_template || '';
  $('#apiKey').value = '';
  $('#clearKey').checked = false;
  $('#apiKey').placeholder = config.key_present ? t('jev.keyKeep') : t('jev.keyEmpty');
  $('#jevStatus').textContent = config.model || t('jev.unconfigured');
}

/* ── 上下文长度（JEV 决策服务输入窗口；自 2026-11-03 从模型与设置迁入） ── */
const WINDOW_CONTEXT_PRESETS = [2048, 4096, 8192, 16384];
const WINDOW_INPUT_PRESETS = [512, 1024, 2048, 4096];
let windowLimits = null;

function windowValues() {
  return {
    context: parseInt(($('#winContext') || {}).value, 10),
    input: parseInt(($('#winInput') || {}).value, 10),
  };
}

function windowProblem(values, limits) {
  const { context, input } = values;
  const minContext = (limits && limits.min_context_tokens) || 512;
  const minInput = (limits && limits.min_input_tokens) || 256;
  const max = (limits && limits.max_tokens) || 131072;
  if (!Number.isInteger(context) || context < minContext || context > max)
    return tf('models.window.badContext', { min: minContext, max });
  if (!Number.isInteger(input) || input < minInput || input > max)
    return tf('models.window.badInput', { min: minInput, max });
  if (input > context) return t('models.window.badRelation');
  return '';
}

function paintWindowMsg() {
  const problem = windowProblem(windowValues(), windowLimits);
  const msg = $('#windowMsg');
  if (msg) msg.textContent = problem || '';
}

function presetButtons(container, values, onPick) {
  if (!container) return;
  container.innerHTML = values.map(v => `<button class="btn sm" data-story="${v}">${v}</button>`).join(' ');
  container.querySelectorAll('[data-story]').forEach(btn => {
    btn.addEventListener('click', () => onPick(parseInt(btn.dataset.story, 10)));
  });
}

function renderWindow(windowConfig) {
  const cfg = windowConfig || {};
  const context = cfg.context_tokens || 4096;
  const input = cfg.input_tokens || 1024;
  const setVal = (sel, val) => { const el = $(sel); if (el) el.value = val; };
  setVal('#winContext', context);
  setVal('#winInput', input);
  presetButtons($('#winContextPresets'), WINDOW_CONTEXT_PRESETS, v => { setVal('#winContext', v); paintWindowMsg(); });
  presetButtons($('#winInputPresets'), WINDOW_INPUT_PRESETS, v => { setVal('#winInput', v); paintWindowMsg(); });
  paintWindowMsg();
}

/* 窗口字段的输入监听只挂一次；renderWindow 会在 boot / 保存 / 切语言时重绘预设。 */
['#winContext', '#winInput'].forEach(sel => {
  const el = $(sel);
  if (el) el.addEventListener('input', paintWindowMsg);
});

/* ── actions handed to the view (never import the view's caller) ────── */

export async function saveConfig() {
  const body = {
    base_url: $('#baseUrl').value,
    model: $('#model').value,
    timeout_seconds: Number($('#timeout').value),
    instructions: $('#instructions').value,
    state_template: $('#stateTemplate').value,
  };
  if ($('#clearKey').checked) body.api_key = '';
  else if ($('#apiKey').value) body.api_key = $('#apiKey').value;
  const saved = await api.put('/admin/jev', body);
  showConfig(saved);
  return saved;
}

export function validateWindow() {
  const problem = windowProblem(windowValues(), windowLimits);
  if (!problem) return;
  const error = new Error(problem);
  error.validation = true;
  throw error;
}

export async function saveWindow() {
  validateWindow();
  const values = windowValues();
  const body = await api.put('/admin/jev/window', {
    context_tokens: values.context,
    input_tokens: values.input,
  });
  windowLimits = body;
  renderWindow(body);
  return body;
}

export async function testConnection(text) {
  /* 决策服务单次测试可能跑满可配置的 120s 超时；全局 15s 管理面超时不能中途掐断。 */
  return api.post('/admin/jev/test', { text }, { timeoutMs: 130000 });
}

onLangChange(() => renderWindow(windowLimits));

async function boot() {
  const [jevBody, winBody] = await Promise.all([
    api.get('/admin/jev').catch(error => ({ error })),
    api.get('/admin/jev/window').catch(() => null),
  ]);
  if (winBody) { windowLimits = winBody; renderWindow(winBody); }
  if (jevBody.error) { $('#saveMsg').textContent = apiErrText(jevBody.error); return; }
  showConfig(jevBody);
}

initJevView({ validateWindow, saveConfig, saveWindow, testConnection });
boot();
