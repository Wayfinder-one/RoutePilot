/* Overview page: traffic and routing distribution from /admin/overview. */
async function boot() {
  const o = await api.get('/admin/overview');
  const rate = o.total ? Math.round(100 * o.blocked / o.total) : 0;
  const dailyAvg = o.daily.length
    ? Math.round(o.daily.reduce((a, d) => a + d.count, 0) / o.daily.length) : 0;
  const cards = [
    ['ov.total', o.total], ['ov.blocked', o.blocked],
    ['ov.blockrate', rate + '%'], ['ov.dailyavg', dailyAvg]];
  document.querySelector('#cards').innerHTML = cards.map(([k, v]) =>
    `<div class="card statcard"><div class="num">${v}</div>
     <div class="mut" data-i18n="${k}">${t(k)}</div></div>`).join('');
  const max = Math.max(1, ...o.routes.map(r => r.count));
  document.querySelector('#routes').innerHTML = o.routes.map(r =>
    `<div class="wrow"><span style="width:180px">${esc(r.model)}</span>
     <div class="scorebar" style="flex:1"><i style="width:${Math.round(100 * r.count / max)}%"></i></div>
     <span class="confv">${r.count}</span></div>`).join('') ||
    `<div class="mut">${t('ov.empty')}</div>`;
}

window.onLangChange = () => { boot(); };
boot();
