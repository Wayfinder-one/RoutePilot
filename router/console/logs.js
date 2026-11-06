/* Calculation log: render only phases present in trace. */
import { api, apiErrText, esc, fmtTime } from './api.js';
import { LANG, onLangChange, t, tf } from './i18n.js';
import './nav.js';

const $ = selector => document.querySelector(selector);
let requests = [];
let filter = 'all';
const openIds = new Set();
const TONES = { '': 0, hit: 1, warn: 2, bad: 3 };
const OUTCOME = {
  completed: ['logs.out.completed', 'hit'],
  routing_error: ['logs.out.routing', 'bad'],
  upstream_error: ['logs.out.upstream', 'bad'],
  client_disconnected: ['logs.out.disconnected', 'warn'],
  cancelled: ['logs.out.cancelled', 'warn'],
  incomplete: ['logs.out.incomplete', ''],
};
function display(value) {
  if (value == null || value === '') return '—';
  if (Array.isArray(value)) return value.map(display).join(', ');
  if (typeof value === 'object') {
    if ('zh' in value || 'en' in value) {
      const current = value[LANG];
      return display(current != null && current !== '' ? current : (value.zh || value.en));
    }
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }
  return String(value);
}
function safe(value) { return esc(display(value)); }
function ms(value) {
  if (value == null || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? (n >= 1000 ? (n / 1000).toFixed(2) + 's' : Math.round(n) + 'ms') : '—';
}
function events(record) {
  return Array.isArray(record.trace) ? record.trace.filter(e => e && typeof e === 'object') : [];
}
function stages(record) {
  const groups = new Map();
  events(record).forEach(event => {
    /* The API presenter attaches phase/label/cell/summary/facts to every trace
       event; a raw machine event here means "no displayed stage". */
    if (!event.phase) return;
    const id = String(event.phase);
    if (!groups.has(id)) groups.set(id, { id, label: event.label ?? id, cell: '—',
      summary: '—', facts: [], tone: '', count: 0 });
    const group = groups.get(id);
    group.count += 1;
    if (event.label != null) group.label = event.label;
    if (event.cell != null && event.cell !== '') group.cell = event.cell;
    if (event.summary != null && event.summary !== '') group.summary = event.summary;
    if (Array.isArray(event.facts) && event.facts.length) group.facts = event.facts.slice(0, 3);
    const tone = Object.prototype.hasOwnProperty.call(TONES, event.tone) ? event.tone : '';
    if (TONES[tone] > TONES[group.tone]) group.tone = tone;
  });
  return [...groups.values()];
}
const FILTERS = {
  all: () => true,
  jev: record => events(record).some(e => e.event === 'question' || (e.event === 'wave' && Number(e.wave) > 0) || /^(?:wave[1-9]|jev-wave-[1-9])/.test(String(e.phase || ''))),
  sticky: record => events(record).some(e => e.event === 'sticky_hit' || e.phase === 'sticky'),
  fallback: record => events(record).some(e => e.event === 'fallback' || e.phase === 'fallback' || (e.event === 'final' && e.kind !== 'target')) || ['error_fallback', 'fallback', 'otherwise'].includes(record.selection_reason),
  error: record => record.outcome_state === 'routing_error' || record.outcome_state === 'upstream_error',
};
function renderKpis() {
  const timed = requests.map(r => r.total_ms ?? r.latency_ms ?? r.routing_ms)
    .filter(value => value != null && value !== '')
    .map(Number).filter(Number.isFinite);
  const average = timed.length ? timed.reduce((a, b) => a + b, 0) / timed.length : null;
  const items = [['logs.stat.requests', requests.length, ''],
    ['logs.filter.jev', requests.filter(FILTERS.jev).length, 'hit'],
    ['logs.stat.fallback', requests.filter(FILTERS.fallback).length, 'warn'],
    ['logs.filter.error', requests.filter(FILTERS.error).length, 'bad'],
    ['logs.stat.latency', ms(average), '']];
  $('#kpis').innerHTML = items.map(([key, value, tone]) =>
    '<span class="lg-kpi ' + tone + '"><b>' + safe(value) + '</b><small>' + esc(t(key)) + '</small></span>').join('');
}
function renderTabs() {
  document.querySelectorAll('#filter button').forEach(button => {
    const key = button.dataset.filter;
    button.setAttribute('aria-pressed', String(key === filter));
    button.innerHTML = esc(t('logs.filter.' + key)) + '<span class="c">' + requests.filter(FILTERS[key]).length + '</span>';
  });
}
function renderSpread() {
  const counts = new Map();
  requests.forEach(record => {
    const path = stages(record);
    const tone = path.some(s => s.tone === 'bad') ? 'bad' : path.some(s => s.tone === 'warn') ? 'warn' : '';
    const key = path.length + '|' + tone;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  $('#spread').innerHTML = '<strong>' + esc(t('logs.spread')) + '</strong>' +
    [...counts].sort((a, b) => Number(a[0].split('|')[0]) - Number(b[0].split('|')[0]))
      .map(([key, count]) => {
        const [number, tone] = key.split('|');
        return '<span class="lg-shape ' + tone + '">' + esc(tf('logs.stages', {count: number})) +
          '<b>' + count + '</b></span>';
      }).join('');
}
function chainHtml(path) {
  if (!path.length) return '<span class="lg-path-empty">' + esc(t('logs.traceEmpty')) + '</span>';
  const description = path.map(s => display(s.label) + ': ' + display(s.summary)).join(' → ');
  return '<span class="lg-chain" role="img" aria-label="' + esc(description) + '">' +
    path.map((stage, index) => (index ? '<span class="lg-sep" aria-hidden="true">›</span>' : '') +
      '<span class="lg-node ' + stage.tone + '" title="' + safe(stage.label) + ' · ' + safe(stage.summary) + '">' +
      '<em>' + safe(stage.label) + '</em><b>' + safe(stage.cell) + '</b></span>').join('') + '</span>';
}
function rowHtml(record, path, open) {
  const [status, tone] = OUTCOME[record.outcome_state] || OUTCOME.incomplete;
  const text = String(record.text || '').replace(/\s+/g, ' ').trim();
  const timestamp = fmtTime(record.ts);
  const clock = timestamp.split(' ')[0];
  return '<button type="button" class="lg-row" aria-expanded="' + open +
    '" aria-controls="detail-' + safe(record.id) + '" title="' + esc(text) + '">' +
    '<span class="lg-time" title="' + esc(timestamp) + '" aria-label="' + esc(timestamp) + '"><span class="lg-dot ' + tone + '" aria-hidden="true"></span>' +
    esc(clock) + '</span>' +
    '<span class="lg-text">' + (text ? esc(text) : esc(t('logs.noText'))) + '</span>' +
    chainHtml(path) + '<span class="lg-model">→ ' + safe(record.model) + '</span>' +
    '<span class="lg-lat">' + esc(ms(record.total_ms ?? record.latency_ms)) + '</span>' +
    '<span class="lg-chev" aria-hidden="true">›</span><span class="sr-only">' +
    esc(t(status)) + '</span></button>';
}
function pairsHtml(items, className) {
  return (Array.isArray(items) ? items : []).filter(pair => Array.isArray(pair) && pair.length >= 2)
    .map(([key, value]) => '<span class="' + className + '"><b>' + safe(key) + '</b>' + safe(value) + '</span>').join('');
}
function stageHtml(stage, index) {
  const branch = stage.tone === 'warn' || stage.tone === 'bad' ? ' branch' : '';
  return '<div class="lg-stage ' + stage.tone + branch + '">' +
    '<div class="lg-rail-cell"><span class="lg-pin">' + (index + 1) + '</span></div>' +
    '<div class="lg-stage-name"><b>' + safe(stage.label) + '</b><small>' + safe(stage.id) +
    ' · ' + esc(tf('logs.eventCount', {count: stage.count})) + '</small></div>' +
    '<div class="lg-judge" title="' + safe(stage.summary) + '">' + safe(stage.summary) + '</div>' +
    '<div class="lg-evidence">' + (pairsHtml(stage.facts, 'lg-evidence-pair') || '—') + '</div>' +
    '<div class="lg-stage-count">' + stage.count + '</div></div>';
}
function detailHtml(record, path) {
  const status = OUTCOME[record.outcome_state] || OUTCOME.incomplete;
  /* route_label/metrics/fields are produced by the API presenter for both live
     and SQLite-restored rows; the console renders them verbatim. */
  const route = record.route_label != null ? safe(record.route_label) : esc(t(status[0]));
  const metrics = Array.isArray(record.metrics) ? record.metrics : [];
  const fields = Array.isArray(record.fields) ? record.fields : [];
  return '<div class="lg-detail-head"><span class="lg-route">' + route + '</span>' +
    '<span class="lg-status ' + status[1] + '">' + esc(t(status[0])) + '</span>' +
    '<span class="lg-detail-metrics">' + pairsHtml(metrics, 'lg-metric') + '</span></div>' +
    '<div class="lg-rail" role="group" aria-label="' + esc(t('logs.path')) + '">' +
    '<div class="lg-rail-head" aria-hidden="true"><span></span><span>' + esc(t('logs.stage')) +
    '</span><span>' + esc(t('logs.summary')) + '</span><span>' + esc(t('logs.evidence')) +
    '</span><span>' + esc(t('logs.events')) + '</span></div>' +
    (path.length ? path.map(stageHtml).join('') : '<div class="lg-none">' + esc(t('logs.traceEmpty')) + '</div>') +
    '</div><details class="lg-raw"><summary>' + esc(t('logs.raw')) + '</summary>' +
    '<div class="lg-raw-grid">' + (pairsHtml(fields, 'lg-raw-pair') || '—') + '</div></details>';
}
function paintDetail(item, record) {
  const panel = item.querySelector('.lg-expand');
  if (!panel || panel.dataset.ready === '1') return;
  panel.innerHTML = detailHtml(record, stages(record));
  panel.dataset.ready = '1';
}
function render() {
  const rows = requests.filter(FILTERS[filter] || FILTERS.all);
  $('#countPill').textContent = rows.length + ' / ' + requests.length;
  renderKpis(); renderTabs(); renderSpread();
  $('#list').innerHTML = rows.map(record => {
    const open = openIds.has(record.id);
    return '<article class="lg-item' + (open ? ' open' : '') + '" data-id="' + safe(record.id) + '">' +
      rowHtml(record, stages(record), open) + '<div class="lg-expand" id="detail-' +
      safe(record.id) + '"' + (open ? '' : ' hidden') + '></div></article>';
  }).join('') || '<div class="lg-list-empty">' +
    esc(t(requests.length ? 'logs.emptyFilter' : 'logs.empty')) + '</div>';
  document.querySelectorAll('#list .lg-item.open').forEach(item => {
    const record = requests.find(entry => entry.id === item.dataset.id);
    if (record) paintDetail(item, record);
  });
}
function toggle(item) {
  const id = item.dataset.id;
  const opening = !openIds.has(id);
  if (opening) openIds.add(id); else openIds.delete(id);
  item.classList.toggle('open', opening);
  item.querySelector('.lg-row').setAttribute('aria-expanded', String(opening));
  const panel = item.querySelector('.lg-expand');
  panel.hidden = !opening;
  if (opening) {
    const record = requests.find(entry => entry.id === id);
    if (record) paintDetail(item, record);
  }
}
$('#refresh').addEventListener('click', load);
$('#filter').addEventListener('click', event => {
  const button = event.target.closest('button[data-filter]');
  if (!button || button.dataset.filter === filter) return;
  filter = button.dataset.filter;
  render();
});
$('#list').addEventListener('click', event => {
  const row = event.target.closest('.lg-row');
  if (row) toggle(row.closest('.lg-item'));
});
async function load() {
  try {
    const data = await api.get('/admin/requests/recent');
    requests = Array.isArray(data.requests) ? data.requests : [];
    for (const id of openIds) if (!requests.some(record => record.id === id)) openIds.delete(id);
    render();
  } catch (error) {
    requests = [];
    openIds.clear();
    render();
    $('#list').innerHTML = '<div class="lg-list-empty">' + esc(apiErrText(error)) + '</div>';
  }
}
onLangChange(render);
load();
