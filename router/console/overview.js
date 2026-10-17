/* Overview page: traffic, routing distribution, model-signal confidence
   distribution and the three most recent evaluation reports. */
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

  const maxRoute = Math.max(1, ...o.routes.map(r => r.count));
  document.querySelector('#routes').innerHTML = o.routes.map(r =>
    `<div class="wrow"><span style="width:180px">${esc(r.model)}</span>
     <div class="scorebar" style="flex:1"><i style="width:${Math.round(100 * r.count / maxRoute)}%"></i></div>
     <span class="confv">${r.count}</span></div>`).join('') ||
    `<div class="mut">${t('ov.empty')}</div>`;

  const conf = o.confidence || [];
  const maxConf = Math.max(1, ...conf.map(c => c.count));
  document.querySelector('#conf').innerHTML = conf.map(c =>
    `<div class="wrow bar-row"><span style="width:90px" class="confv">${c.lo.toFixed(1)}–${c.hi.toFixed(1)}</span>
     <div class="scorebar" style="flex:1"><i style="width:${Math.round(100 * c.count / maxConf)}%"></i></div>
     <span class="confv">${c.count}</span></div>`).join('') ||
    `<div class="mut">${t('ov.empty')}</div>`;

  document.querySelector('#evals').innerHTML = (o.evals || []).map(e =>
    `<div class="wrow"><span>${esc(t('ov.evalrow')
      .replace('{mode}', e.mode).replace('{matched}', e.matched)
      .replace('{total}', e.total)
      .replace('{rate}', Math.round(100 * e.rate) + '%'))}</span>
     <span class="confv">${esc(e.ts)}</span></div>`).join('') ||
    `<div class="mut">${t('ov.noeval')}</div>`;
}

window.onLangChange = () => { boot(); };
boot();
