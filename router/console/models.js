/* Models & settings page: editable model registry + provider list (M3 write
   endpoints) + keyword-group editor + runtime settings + admin token. */
async function boot() {
  const [m, kw, prov, set] = await Promise.all([
    api.get('/admin/models'), api.get('/admin/keyword-groups'),
    api.get('/admin/providers'), api.get('/admin/settings')]);
  renderModels(m.models);
  renderProviders(prov.providers);
  renderKw(kw.groups);
  renderSettings(set.settings || {});
}

/* ── models ──────────────────────────────────────────────────────────── */
function renderModels(models) {
  document.querySelector('#modelTable').innerHTML =
    `<tr><th>id</th><th>upstream</th><th>provider</th><th data-i18n="models.profile"></th></tr>` +
    models.map(x => {
      const p = JSON.parse(x.profile || '{}');
      return `<tr><td class="v">${esc(x.id)}</td>
        <td><input class="m-upstream" data-id="${esc(x.id)}" value="${esc(x.upstream)}"></td>
        <td><input class="m-provider" value="${esc(x.provider)}"></td>
        <td class="mut">${(p.domains || []).join(', ') || '—'} · ${p.tier || '—'}</td></tr>`;
    }).join('');
}

function readModels() {
  return [...document.querySelectorAll('#modelTable tr')].slice(1).map(tr => ({
    id: tr.querySelector('.m-upstream').dataset.id,
    upstream: tr.querySelector('.m-upstream').value.trim(),
    provider: tr.querySelector('.m-provider').value.trim(),
  }));
}

async function saveModels() {
  for (const m of readModels()) {
    await api.put(`/admin/models/${encodeURIComponent(m.id)}`,
      { upstream: m.upstream, provider: m.provider });
  }
  toast(t('common.saved'));
}
document.querySelector('#saveModels').addEventListener('click', saveModels);

/* ── providers ───────────────────────────────────────────────────────── */
function renderProviders(providers) {
  document.querySelector('#providers').innerHTML = providers.map(p =>
    `<div class="prov-group card">
      <input class="p-name" value="${esc(p.name)}">
      <input class="p-base" value="${esc(p.base_url)}" placeholder="base_url">
      <input class="p-key" value="${esc(p.key_env)}" placeholder="key_env">
     </div>`).join('');
}

function readProviders() {
  return [...document.querySelectorAll('#providers .prov-group')].map(g => ({
    name: g.querySelector('.p-name').value.trim(),
    base_url: g.querySelector('.p-base').value.trim(),
    key_env: g.querySelector('.p-key').value.trim(),
  })).filter(p => p.name);
}

async function saveProviders() {
  for (const p of readProviders()) {
    await api.put(`/admin/providers/${encodeURIComponent(p.name)}`,
      { base_url: p.base_url, key_env: p.key_env });
  }
  toast(t('common.saved'));
}
document.querySelector('#saveProviders').addEventListener('click', saveProviders);
document.querySelector('#addProvider').addEventListener('click', () => {
  renderProviders([...readProviders(), { name: 'new', base_url: '', key_env: '' }]);
});

/* ── keyword lexicon ─────────────────────────────────────────────────── */
let KW = {};

function renderKw(groups) {
  KW = groups;
  document.querySelector('#kwGroups').innerHTML = Object.entries(groups).map(
    ([name, words]) => `<div class="kw-group card">
      <input class="kw-name" value="${esc(name)}">
      <input class="kw-words" value="${esc(words.join(', '))}" placeholder="词1, 词2">
      <button class="btn sm" data-del="${esc(name)}">${t('common.delete')}</button>
    </div>`).join('');
  document.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => {
    delete groups[b.dataset.del]; renderKw(groups);
  }));
  document.querySelectorAll('.kw-name, .kw-words').forEach(el =>
    el.addEventListener('change', () => { groups = readGroups(); KW = groups; }));
}

document.querySelector('#addGroup').addEventListener('click', () => {
  const cur = readGroups(); cur['新组'] = []; renderKw(cur);
});
document.querySelector('#saveKw').addEventListener('click', async () => {
  await api.put('/admin/keyword-groups', { groups: readGroups() });
  toast(t('common.saved'));
});
function readGroups() {
  const next = {};
  document.querySelectorAll('#kwGroups .kw-group').forEach(g => {
    const name = g.querySelector('.kw-name').value.trim();
    if (name) next[name] = g.querySelector('.kw-words').value
      .split(',').map(w => w.trim()).filter(Boolean);
  });
  return next;
}

/* ── runtime settings (/admin/settings; pipeline_version is read-only) ── */
function settingsRows() {
  return [...document.querySelectorAll('#settingsRows .set-row')];
}

function renderSettings(settings) {
  const reserved = 'pipeline_version';
  const rows = Object.entries(settings)
    .filter(([k]) => k !== reserved)
    .map(([k, v]) => `<div class="set-row">
        <input class="s-key" value="${esc(k)}">
        <input class="s-val" value="${esc(v)}">
        <button class="btn sm" data-del-setting>${t('common.delete')}</button>
      </div>`).join('');
  const ver = settings[reserved]
    ? `<div class="mut">${t('models.pipelineversion')}：${esc(settings[reserved])}</div>`
    : '';
  document.querySelector('#settingsRows').innerHTML = rows + ver;
  document.querySelectorAll('[data-del-setting]').forEach(b =>
    b.addEventListener('click', e => e.target.closest('.set-row').remove()));
}

function readSettings() {
  const out = {};
  for (const row of settingsRows()) {
    const k = row.querySelector('.s-key').value.trim();
    if (k) out[k] = row.querySelector('.s-val').value;
  }
  return out;
}

document.querySelector('#addSetting').addEventListener('click', () => {
  const wrap = document.createElement('div');
  wrap.className = 'set-row';
  wrap.innerHTML = `<input class="s-key" placeholder="key">
    <input class="s-val" placeholder="value">
    <button class="btn sm" data-del-setting>${t('common.delete')}</button>`;
  wrap.querySelector('[data-del-setting]').addEventListener('click', () => wrap.remove());
  document.querySelector('#settingsRows').prepend(wrap);
});

document.querySelector('#saveSettings').addEventListener('click', async () => {
  try {
    // settings PUT is a merge: untouched keys are left alone
    await api.put('/admin/settings', readSettings());
    renderSettings((await api.get('/admin/settings')).settings || {});
    toast(t('common.saved'));
  } catch (e) {
    toast('保存失败: ' + (Array.isArray(e.detail) ? JSON.stringify(e.detail) : e.detail || e.message));
  }
});

/* ── admin token (browser-local; /admin auth when ROUTER_ADMIN_TOKEN is set) ─ */
const tokenInput = document.querySelector('#adminToken');
tokenInput.value = api.token();

document.querySelector('#saveToken').addEventListener('click', () => {
  api.setToken(tokenInput.value);
  toast(api.token() ? t('common.saved') : t('models.cleartoken'));
  boot();
});
document.querySelector('#clearToken').addEventListener('click', () => {
  api.setToken('');
  tokenInput.value = '';
  toast(t('models.cleartoken'));
});

window.onLangChange = () => { boot(); };
boot();
