/* Overview page: traffic, route distribution, model-signal confidence,
   recent evaluations, and the signal engine card (JEV model metadata). */
const $ = s => document.querySelector(s);

async function boot() {
  const [o, tagBody] = await Promise.all([
    api.get('/admin/overview'),
    api.get('/admin/tagger').catch(() => ({ tagger: {} })),
  ]);
  const tag = tagBody.tagger || {};
  const rate = o.total ? Math.round(100 * o.blocked / o.total) : 0;
  const dailyAvg = o.daily.length
    ? Math.round(o.daily.reduce((a, d) => a + d.count, 0) / o.daily.length) : 0;

  const cards = [
    ['ov.total', o.total], ['ov.blocked', o.blocked],
    ['ov.blockrate', rate + '%'], ['ov.dailyavg', dailyAvg]];
  $('#cards').innerHTML = cards.map(([k, v]) =>
    `<div class="card statcard"><div class="num">${v}</div>
     <div class="lbl">${t(k)}</div></div>`).join('');

  const maxRoute = Math.max(1, ...o.routes.map(r => r.count));
  $('#routes').innerHTML = o.routes.map(r =>
    `<div class="bar-row"><span class="bl">${esc(r.model)}</span>
     <div class="scorebar"><i style="width:${Math.round(100 * r.count / maxRoute)}%"></i></div>
     <span class="confv">${r.count}</span></div>`).join('') ||
    `<div class="empty">${t('ov.empty')}</div>`;

  const temp = tag.temperature != null ? Number(tag.temperature).toFixed(2) : '—';
  $('#engineCard').innerHTML = `<div class="col-h">${t('ov.engine')} <span class="pill indigo" style="font-weight:600">${esc(tag.name || '?')}</span></div>
    <div style="font-size:12px;color:var(--text-3);margin-bottom:10px">${t('ov.engine.desc')}</div>
    <div class="bar-row"><span class="bl">${t('ov.temperature')}</span><b class="mono">${temp}</b></div>
    <div class="bar-row"><span class="bl">${t('ov.maxlen')}</span><b class="mono">${tag.max_length ?? t('ov.nodata')}</b></div>
    <div class="bar-row"><span class="bl">${t('ov.modelpath')}</span><span class="mono" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:260px" title="${esc(tag.model_dir || '')}">${esc((tag.model_dir || '—').split('/').slice(-2).join('/'))}</span></div>
    <div class="hr" style="margin:10px 0"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a class="btn sm" href="signals.html">${t('ov.go.signals')}</a>
      <a class="btn sm" href="logs.html">${t('ov.go.logs')}</a>
    </div>`;

  const conf = o.confidence || [];
  const maxConf = Math.max(1, ...conf.map(c => c.count));
  $('#conf').innerHTML = conf.map(c =>
    `<div class="bar-row"><span class="bl">${c.lo.toFixed(1)}–${c.hi.toFixed(1)}</span>
     <div class="scorebar"><i style="width:${Math.round(100 * c.count / maxConf)}%"></i></div>
     <span class="confv">${c.count}</span></div>`).join('') ||
    `<div class="empty">${t('ov.empty')}</div>`;

  $('#evals').innerHTML = (o.evals || []).map(e =>
    `<div class="bar-row"><span class="bl">${esc(tf('ov.evalrow', {
      mode: e.mode, matched: e.matched, total: e.total,
      rate: Math.round(100 * e.rate) + '%'}))}</span>
     <span class="confv">${esc(e.ts)}</span></div>`).join('') ||
    `<div class="empty">${t('ov.noeval')}</div>`;
}

window.onLangChange = () => boot();
boot();
