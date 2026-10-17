/* Models & settings page: editable model registry + provider list (M3 write
   endpoints) + keyword-group editor. */
async function boot() {
  const [m, kw, prov] = await Promise.all([
    api.get('/admin/models'), api.get('/admin/keyword-groups'),
    api.get('/admin/providers')]);
  renderModels(m.models);
  renderProviders(prov.providers);
  renderKw(kw.groups);
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

window.onLangChange = () => { boot(); };
boot();
