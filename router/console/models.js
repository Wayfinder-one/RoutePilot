/* Models & settings page: read-only model registry + keyword-group editor. */
async function boot() {
  const [m, kw] = await Promise.all([
    api.get('/admin/models'), api.get('/admin/keyword-groups')]);
  document.querySelector('#modelTable').innerHTML =
    `<tr><th>id</th><th>upstream</th><th>provider</th><th data-i18n="models.profile"></th></tr>` +
    m.models.map(x => {
      const p = JSON.parse(x.profile || '{}');
      return `<tr><td class="v">${esc(x.id)}</td><td>${esc(x.upstream)}</td>
        <td>${esc(x.provider)}</td>
        <td class="mut">${(p.domains || []).join(', ') || '—'} · ${p.tier || '—'}</td></tr>`;
    }).join('');
  renderKw(kw.groups);
}

function renderKw(groups) {
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
    el.addEventListener('change', () => { groups = readGroups(); }));
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
  document.querySelectorAll('.kw-group').forEach(g => {
    const name = g.querySelector('.kw-name').value.trim();
    if (name) next[name] = g.querySelector('.kw-words').value
      .split(',').map(w => w.trim()).filter(Boolean);
  });
  return next;
}

window.onLangChange = () => { boot(); };
boot();
