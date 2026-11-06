import { api, apiErrText, esc, fmtTime } from './api.js';
import { onLangChange, t } from './i18n.js';
import './nav.js';

const $ = selector => document.querySelector(selector);

async function boot() {
  const [overview, jevBody, treeBody] = await Promise.all([
    api.get('/admin/overview'), api.get('/admin/jev'), api.get('/admin/trees'),
  ]);
  const dailyAvg = overview.daily.length ? Math.round(overview.daily.reduce((sum, day) => sum + day.count, 0) / overview.daily.length) : 0;
  const cards = [['ov.total', overview.total], ['ov.sticky', overview.sticky], ['ov.dailyavg', dailyAvg], ['ov.treeCount', treeBody.trees.length]];
  $('#cards').innerHTML = cards.map(([key, value]) => `<div class="card statcard"><div class="num">${value}</div><div class="lbl">${t(key)}</div></div>`).join('');
  const maxRoute = Math.max(1, ...overview.routes.map(row => row.count));
  $('#routes').innerHTML = overview.routes.map(row => `<div class="bar-row"><span class="bl">${esc(row.model)}</span><div class="scorebar"><i style="width:${Math.round(100 * row.count / maxRoute)}%"></i></div><span class="confv">${row.count}</span></div>`).join('') || `<div class="empty">${t('ov.empty')}</div>`;
  const config = jevBody.config || {};
  $('#engineCard').innerHTML = `<div class="col-h">JEV<span class="pill indigo">${esc(config.model || '—')}</span></div><div class="bar-row"><span class="bl">${esc(config.base_url || '—')}</span></div><div class="hr"></div><a class="btn sm" href="jev.html">${t('ov.go.jev')}</a> <a class="btn sm" href="logs.html">${t('ov.go.logs')}</a>`;
  const active = treeBody.trees.find(tree => tree.enabled);
  $('#routingCard').innerHTML = `<div class="col-h">${t('ov.activeTree')}</div><div class="num">${esc(active ? active.name || active.id : '—')}</div><div class="footnote mono">${esc((treeBody.snapshot_revision || '').slice(0, 16))}</div><a class="btn sm" href="routing.html">${t('ov.go.routing')}</a>`;
  const confidence = overview.confidence || [];
  const maxConf = Math.max(1, ...confidence.map(row => row.count));
  $('#conf').innerHTML = confidence.map(row => `<div class="bar-row"><span class="bl">${row.lo.toFixed(1)}–${row.hi.toFixed(1)}</span><div class="scorebar"><i style="width:${Math.round(100 * row.count / maxConf)}%"></i></div><span class="confv">${row.count}</span></div>`).join('');
  $('#confSince').textContent = overview.confidence_since ? ' · ' + fmtTime(overview.confidence_since) : '';
}

onLangChange(() => { boot().catch(() => {}); });
boot().catch(error => { $('#cards').innerHTML = `<div class="empty">${esc(apiErrText(error))}</div>`; });
