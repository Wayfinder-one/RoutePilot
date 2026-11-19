/* Calculation log (Policy v4): one JEV per decision, ordered rules, no waves.
 *
 * The list keeps time · request · JEV calls (0/1) · model · latency; the detail
 * renders the console-owned policy facts (policy_id / policy_revision /
 * jev_calls / matched_rule) followed by the shared trace sections from
 * trace.js (input → one JEV → answers → ordered rules → final → schedule →
 * outcome). Request text, answers and trace come only from the live ring
 * (hydrated SQLite rows render the empty state instead of inventing one).
 */
import { api, apiErrText, esc, fmtTime } from './api.js';
import { LANG, onLangChange, t, tf } from './i18n.js';
import { detailHtml, hasJev } from './trace.js';
import './nav.js';

const $ = selector => document.querySelector(selector);
let requests = [];
let filter = 'all';
const openIds = new Set();
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
/* JEV 调用列：数据面保证一次决策至多一次调用，这里只显示字段原值。 */
function jevCallsText(record) {
  return record.jev_calls == null ? '—' : String(record.jev_calls);
}
/* 筛选只读记录字段与 v4 trace 事件；wave 时代键一律不再参与。 */
const FILTERS = {
  all: () => true,
  jev: record => hasJev(record),
  sticky: record => Number(record.sticky_hit) > 0 ||
    events(record).some(e => e.event === 'sticky_hit'),
  fallback: record => record.fallback_reason != null || record.selection_reason === 'fallback' ||
    events(record).some(e => e.event === 'final' && e.kind === 'fallback'),
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
function rowHtml(record, open) {
  const [status, tone] = OUTCOME[record.outcome_state] || OUTCOME.incomplete;
  const text = String(record.text || '').replace(/\s+/g, ' ').trim();
  const timestamp = fmtTime(record.ts);
  const clock = timestamp.split(' ')[0];
  return '<button type="button" class="lg-row" aria-expanded="' + open +
    '" aria-controls="detail-' + safe(record.id) + '" title="' + esc(text) + '">' +
    '<span class="lg-time" title="' + esc(timestamp) + '" aria-label="' + esc(timestamp) + '"><span class="lg-dot ' + tone + '" aria-hidden="true"></span>' +
    esc(clock) + '</span>' +
    '<span class="lg-text">' + (text ? esc(text) : esc(t('logs.noText'))) + '</span>' +
    '<span class="lg-jev' + (Number(record.jev_calls) > 0 ? ' hit' : '') + '">' +
    esc(jevCallsText(record)) + '</span>' +
    '<span class="lg-model">→ ' + safe(record.model) + '</span>' +
    '<span class="lg-lat">' + esc(ms(record.total_ms ?? record.latency_ms)) + '</span>' +
    '<span class="lg-chev" aria-hidden="true">›</span><span class="sr-only">' +
    esc(t(status)) + '</span></button>';
}
function pairsHtml(items, className) {
  return (Array.isArray(items) ? items : []).filter(pair => Array.isArray(pair) && pair.length >= 2)
    .map(([key, value]) => '<span class="' + className + '"><b>' + safe(key) + '</b>' + safe(value) + '</span>').join('');
}
/* 控制台自有的策略事实行：策略 / 版本 / JEV 调用 / 命中规则（读记录字段）。 */
function policyFactsHtml(record) {
  const revision = record.policy_revision == null || record.policy_revision === ''
    ? '—' : String(record.policy_revision).slice(0, 10);
  const facts = [
    [t('logs.f.policy'), display(record.policy_id)],
    [t('logs.f.policyRevision'), revision],
    [t('logs.f.jevCalls'), jevCallsText(record)],
    [t('logs.f.matchedRule'), display(record.matched_rule)],
  ];
  return '<div class="lg-policyfacts">' + pairsHtml(facts, 'lg-metric') + '</div>';
}
function detailContent(record) {
  const status = OUTCOME[record.outcome_state] || OUTCOME.incomplete;
  /* route_label/metrics/fields are produced by the API presenter for both live
     and SQLite-restored rows; the console renders them verbatim. */
  const route = record.route_label != null ? safe(record.route_label) : esc(t(status[0]));
  const metrics = Array.isArray(record.metrics) ? record.metrics : [];
  const fields = Array.isArray(record.fields) ? record.fields : [];
  return '<div class="lg-detail-head"><span class="lg-route">' + route + '</span>' +
    '<span class="lg-status ' + status[1] + '">' + esc(t(status[0])) + '</span>' +
    '<span class="lg-detail-metrics">' + pairsHtml(metrics, 'lg-metric') + '</span></div>' +
    policyFactsHtml(record) +
    detailHtml(record) +
    '<details class="lg-raw"><summary>' + esc(t('logs.raw')) + '</summary>' +
    '<div class="lg-raw-grid">' + (pairsHtml(fields, 'lg-raw-pair') || '—') + '</div></details>';
}
function paintDetail(item, record) {
  const panel = item.querySelector('.lg-expand');
  if (!panel || panel.dataset.ready === '1') return;
  panel.innerHTML = detailContent(record);
  panel.dataset.ready = '1';
}
function render() {
  const rows = requests.filter(FILTERS[filter] || FILTERS.all);
  $('#countPill').textContent = rows.length + ' / ' + requests.length;
  renderKpis(); renderTabs();
  $('#list').innerHTML = rows.map(record => {
    const open = openIds.has(record.id);
    return '<article class="lg-item' + (open ? ' open' : '') + '" data-id="' + safe(record.id) + '">' +
      rowHtml(record, open) + '<div class="lg-expand" id="detail-' +
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
