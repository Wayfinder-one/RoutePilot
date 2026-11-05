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

async function boot() {
  try { showConfig(await api.get('/admin/jev')); }
  catch (error) { $('#saveMsg').textContent = apiErrText(error); }
}

$('#saveJev').addEventListener('click', async () => {
  const body = {
    base_url: $('#baseUrl').value,
    model: $('#model').value,
    timeout_seconds: Number($('#timeout').value),
    instructions: $('#instructions').value,
    state_template: $('#stateTemplate').value,
  };
  if ($('#clearKey').checked) body.api_key = '';
  else if ($('#apiKey').value) body.api_key = $('#apiKey').value;
  try {
    const saved = await api.put('/admin/jev', body);
    showConfig(saved);
    $('#saveMsg').textContent = t('common.saved');
  } catch (error) { $('#saveMsg').textContent = apiErrText(error); }
});

$('#testJev').addEventListener('click', async () => {
  $('#testOut').textContent = t('common.loading');
  try {
    /* 决策服务单次测试可能跑满可配置的 120s 超时；全局 15s 管理面超时不能中途掐断。 */
    const result = await api.post('/admin/jev/test', { text: $('#testText').value }, { timeoutMs: 130000 });
    $('#testOut').innerHTML = `<div class="card"><div class="col-h">${esc(result.selected_key)}</div><pre>${esc(JSON.stringify(result, null, 2))}</pre></div>`;
  } catch (error) { $('#testOut').textContent = apiErrText(error); }
});

$('#testText').value = '请选择一个候选以验证决策服务。';

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
  const save = $('#saveWindow');
  if (save) save.disabled = !!problem;
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
  const pill = $('#windowPill');
  if (pill) pill.innerHTML = `<b>${input}</b> / ${context} token`;
  presetButtons($('#winContextPresets'), WINDOW_CONTEXT_PRESETS, v => { setVal('#winContext', v); paintWindowMsg(); });
  presetButtons($('#winInputPresets'), WINDOW_INPUT_PRESETS, v => { setVal('#winInput', v); paintWindowMsg(); });
  ['#winContext', '#winInput'].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener('input', paintWindowMsg);
  });
  paintWindowMsg();
}

$('#saveWindow').addEventListener('click', async () => {
  const values = windowValues();
  const problem = windowProblem(values, windowLimits);
  if (problem) { toast(problem); return; }
  try {
    const body = await api.put('/admin/jev/window', {
      context_tokens: values.context,
      input_tokens: values.input,
    });
    windowLimits = body;
    renderWindow(body);
    toast(t('models.window.saved'));
  } catch (error) { $('#windowMsg').textContent = apiErrText(error); }
});

window.onLangChange = () => renderWindow(windowLimits);

async function boot() {
  const [jevBody, winBody] = await Promise.all([
    api.get('/admin/jev').catch(error => ({ error })),
    api.get('/admin/jev/window').catch(() => null),
  ]);
  if (winBody) { windowLimits = winBody; renderWindow(winBody); }
  if (jevBody.error) { $('#saveMsg').textContent = apiErrText(jevBody.error); return; }
  showConfig(jevBody);
}

boot();
