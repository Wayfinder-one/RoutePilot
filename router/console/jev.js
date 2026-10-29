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
    const result = await api.post('/admin/jev/test', { text: $('#testText').value });
    $('#testOut').innerHTML = `<div class="card"><div class="col-h">${esc(result.selected_key)}</div><pre>${esc(JSON.stringify(result, null, 2))}</pre></div>`;
  } catch (error) { $('#testOut').textContent = apiErrText(error); }
});

$('#testText').value = '请选择一个候选以验证决策服务。';
boot();
