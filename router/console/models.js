/* Models & settings page: model registry, providers, runtime settings, admin
   token (browser-local) and the current signal engine card. The keyword
   lexicon moved to the signal configuration page (2026-10-19). */
const $ = s => document.querySelector(s);

async function boot() {
  const [m, prov, set, tagBody] = await Promise.all([
    api.get('/admin/models'),
    api.get('/admin/providers'), api.get('/admin/settings'),
    api.get('/admin/tagger').catch(() => ({ tagger: {} }))]);
  renderEngine(tagBody.tagger || {});
  renderModels(m.models || []);
  renderProviders(prov.providers || []);
  renderSettings(set.settings || {});
}

/* ── current signal engine (read-only) ───────────────────────────────── */
function renderEngine(tag) {
  $('#enginePill').innerHTML = `<span class="dot" style="color:var(--indigo)"></span>` +
    (LANG === 'zh' ? '引擎 <b>' + esc(tag.name || '?') + '</b>'
      : 'engine <b>' + esc(tag.name || '?') + '</b>');
  const temp = tag.temperature != null ? Number(tag.temperature).toFixed(2) : '—';
  $('#engineCard').innerHTML = `<div class="col-h">${t('models.engine')}
      <span class="pill indigo">${esc(tag.name || '?')}</span></div>
    <div class="bar-row"><span class="bl">${t('sig.temperature')}</span><b class="mono">${temp}</b></div>
    <div class="bar-row"><span class="bl">${t('sig.maxlen')}</span><b class="mono">${tag.max_length ?? '—'}</b></div>
    <div class="bar-row"><span class="bl">${t('sig.model')}</span>
      <span class="mono" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:330px" title="${esc(tag.model_dir || '')}">${esc(tag.model_dir || '—')}</span></div>
    <div class="footnote">${t('models.engine.hint')} <a href="signals.html">${t('nav.signals')} →</a></div>`;
}

/* ── models ──────────────────────────────────────────────────────────── */
function renderModels(models) {
  $('#modelTable').innerHTML =
    `<tr><th>id</th><th>upstream</th><th>provider</th><th>${t('models.profile')}</th></tr>` +
    models.map(x => {
      let p = {};
      try { p = JSON.parse(x.profile || '{}'); } catch (e) { p = {}; }
      return `<tr><td class="mono" style="font-size:11px">${esc(x.id)}</td>
        <td><input class="m-upstream" data-id="${esc(x.id)}" value="${esc(x.upstream)}"></td>
        <td><input class="m-provider" value="${esc(x.provider)}"></td>
        <td class="mut" style="font-size:11px">${esc((p.domains || []).join(', ') || '—')} · ${esc(p.tier || '—')}</td></tr>`;
    }).join('');
}
function readModels() {
  return [...document.querySelectorAll('#modelTable tr')].slice(1).map(tr => ({
    id: tr.querySelector('.m-upstream').dataset.id,
    upstream: tr.querySelector('.m-upstream').value.trim(),
    provider: tr.querySelector('.m-provider').value.trim(),
  }));
}
$('#saveModels').addEventListener('click', async () => {
  for (const m of readModels()) {
    await api.put(`/admin/models/${encodeURIComponent(m.id)}`,
      { upstream: m.upstream, provider: m.provider });
  }
  toast(t('common.saved'));
});

/* ── providers ───────────────────────────────────────────────────────── */
function renderProviders(providers) {
  $('#providers').innerHTML = providers.map(p =>
    `<div class="prov-group">
      <input class="p-name" value="${esc(p.name)}" placeholder="name" style="max-width:140px">
      <input class="p-base" value="${esc(p.base_url)}" placeholder="base_url">
      <input class="p-key" value="${esc(p.key_env)}" placeholder="key_env" style="max-width:170px">
    </div>`).join('');
}
function readProviders() {
  return [...document.querySelectorAll('#providers .prov-group')].map(g => ({
    name: g.querySelector('.p-name').value.trim(),
    base_url: g.querySelector('.p-base').value.trim(),
    key_env: g.querySelector('.p-key').value.trim(),
  })).filter(p => p.name);
}
$('#saveProviders').addEventListener('click', async () => {
  for (const p of readProviders()) {
    await api.put(`/admin/providers/${encodeURIComponent(p.name)}`,
      { base_url: p.base_url, key_env: p.key_env });
  }
  toast(t('common.saved'));
});
$('#addProvider').addEventListener('click', () => {
  renderProviders([...readProviders(), { name: 'new', base_url: '', key_env: '' }]);
});

/* ── runtime settings (/admin/settings; pipeline_version is read-only) ─ */
function renderSettings(settings) {
  const rows = Object.entries(settings)
    .filter(([k]) => k !== 'pipeline_version')
    .map(([k, v]) => `<div class="set-row">
        <input class="s-key" value="${esc(k)}" style="max-width:190px">
        <input class="s-val" value="${esc(v)}">
        <button class="del" data-del-setting title="${t('common.delete')}">✕</button>
      </div>`).join('');
  const ver = settings.pipeline_version
    ? `<div class="footnote">${t('models.pipelineversion')}：<b class="mono">${esc(settings.pipeline_version)}</b></div>` : '';
  $('#settingsRows').innerHTML = rows + ver;
  document.querySelectorAll('[data-del-setting]').forEach(b =>
    b.addEventListener('click', () => b.closest('.set-row').remove()));
}
function readSettings() {
  const out = {};
  document.querySelectorAll('#settingsRows .set-row').forEach(row => {
    const k = row.querySelector('.s-key').value.trim();
    if (k) out[k] = row.querySelector('.s-val').value;
  });
  return out;
}
$('#addSetting').addEventListener('click', () => {
  const wrap = document.createElement('div');
  wrap.className = 'set-row';
  wrap.innerHTML = `<input class="s-key" placeholder="key" style="max-width:190px">
    <input class="s-val" placeholder="value">
    <button class="del" data-del-setting>✕</button>`;
  wrap.querySelector('[data-del-setting]').addEventListener('click', () => wrap.remove());
  $('#settingsRows').prepend(wrap);
});
$('#saveSettings').addEventListener('click', async () => {
  try {
    await api.put('/admin/settings', readSettings());
    renderSettings((await api.get('/admin/settings')).settings || {});
    toast(t('common.saved'));
  } catch (e) {
    toast('保存失败: ' + (Array.isArray(e.detail) ? JSON.stringify(e.detail) : (e.detail || e.message)));
  }
});

/* ── admin token ─────────────────────────────────────────────────────── */
$('#adminToken').value = api.token();
$('#saveToken').addEventListener('click', () => {
  api.setToken($('#adminToken').value);
  toast(api.token() ? t('common.saved') : t('models.cleartoken'));
  boot();
});
$('#clearToken').addEventListener('click', () => {
  api.setToken('');
  $('#adminToken').value = '';
  toast(t('models.cleartoken'));
});

window.onLangChange = () => boot();
boot();
