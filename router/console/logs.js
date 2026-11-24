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
      summary: '—', facts: [], tone: '', count: 0, events: [] });
    const group = groups.get(id);
    group.count += 1;
    group.events.push(event);
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
  /* 决策耗时：进入路由到选出模型的时间，不含上游生成。 */
  const timed = requests.map(r => r.routing_ms)
    .filter(value => value != null && value !== '')
    .map(Number).filter(Number.isFinite);
  const average = timed.length ? timed.reduce((a, b) => a + b, 0) / timed.length : null;
  const items = [['logs.stat.requests', requests.length, ''],
    ['logs.filter.jev', requests.filter(FILTERS.jev).length, 'hit'],
    ['logs.stat.fallback', requests.filter(FILTERS.fallback).length, 'warn'],
    ['logs.filter.error', requests.filter(FILTERS.error).length, 'bad'],
    ['logs.stat.latency', ms(average), '', t('logs.stat.latencyHint')]];
  $('#kpis').innerHTML = items.map(([key, value, tone, hint]) =>
    '<span class="lg-kpi ' + tone + '"' + (hint ? ' title="' + esc(hint) + '"' : '') +
    '><b>' + safe(value) + '</b><small>' + esc(t(key)) + '</small></span>').join('');
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
  /* 折叠行只画路由决策路径：上游执行结果既不是计算步骤，也会挤掉决策节点；
     执行结果由行首状态点、行状态和详情的「执行结果」阶段表达。 */
  const decision = path.filter(stage => stage.id !== 'outcome');
  if (!decision.length) return '<span class="lg-path-empty">' + esc(t('logs.traceEmpty')) + '</span>';
  const description = decision.map(s => display(s.label) + ': ' + display(s.summary)).join(' → ');
  return '<span class="lg-chain" role="img" aria-label="' + esc(description) + '">' +
    decision.map((stage, index) => (index ? '<span class="lg-sep" aria-hidden="true">›</span>' : '') +
      '<span class="lg-node ' + stage.tone + '" title="' + safe(stage.label) + ' · ' + safe(stage.summary) + '">' +
      '<em>' + safe(stage.label) + '</em><b>' + safe(stage.cell) + '</b></span>').join('') + '</span>';
}
function decisionTitle(record) {
  if (record.routing_ms == null) return '';
  let title = t('logs.stat.latency') + ' ' + ms(record.routing_ms);
  if (record.jev_ms != null) title += ' · ' + t('logs.decisionModel') + ' ' + ms(record.jev_ms);
  return title;
}
function rowHtml(record, path, open) {
  const [status, tone] = OUTCOME[record.outcome_state] || OUTCOME.incomplete;
  const text = String(record.text || '').replace(/\s+/g, ' ').trim();
  const timestamp = fmtTime(record.ts);
  const clock = timestamp.split(' ')[0];
  /* 快照链接放在折叠行上：不必展开就能直接回放当时的决策图。
     它是这一行唯一的导航动作，做成带图示的实心浅色按钮，避免和右侧的
     纯文字耗时混在一起看不出来。 */
  const snapshot = typeof record.snapshot_url === 'string' && record.snapshot_url
    ? '<a class="lg-snap-btn" href="' + esc(record.snapshot_url) +
      '" title="' + esc(t('logs.snapshot')) + '" aria-label="' + esc(t('logs.snapshot')) +
      '"><span class="lg-snap-label">' + esc(t('logs.snapshot')) + '</span>' +
      '<svg class="lg-snap-ic" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
      '<path d="M5.3 10.7 10.7 5.3M6.6 5.3h4.1v4.1"></path></svg></a>'
    : '<span class="lg-snap-empty" aria-hidden="true"></span>';
  return '<div class="lg-row" title="' + esc(text) + '">' +
    '<span class="lg-time" title="' + esc(timestamp) + '" aria-label="' + esc(timestamp) + '"><span class="lg-dot ' + tone + '" aria-hidden="true"></span>' +
    esc(clock) + '</span>' +
    '<span class="lg-text">' + (text ? esc(text) : esc(t('logs.noText'))) + '</span>' +
    chainHtml(path) + '<span class="lg-model">' + safe(record.model) + '</span>' +
    '<span class="lg-lat" title="' + esc(decisionTitle(record)) + '">' +
    esc(ms(record.routing_ms)) + '</span>' + snapshot +
    '<button type="button" class="lg-chev" aria-expanded="' + open +
    '" aria-controls="detail-' + safe(record.id) + '" aria-label="' +
    esc(t(open ? 'logs.collapseRow' : 'logs.expandRow')) +
    '"><span aria-hidden="true">›</span></button>' +
    '<span class="sr-only">' + esc(t(status)) + '</span></div>';
}
function pairsHtml(items, className) {
  return (Array.isArray(items) ? items : []).filter(pair => Array.isArray(pair) && pair.length >= 2)
    .map(([key, value]) => '<span class="' + className + '"><b>' + safe(key) + '</b>' + safe(value) + '</span>').join('');
}
function eventHtml(event) {
  const tone = Object.prototype.hasOwnProperty.call(TONES, event.tone) ? event.tone : '';
  const status = String(event.status ?? '');
  const muted = event.event === 'slot' && (status === 'false' || status === 'unknown');
  const kind = event.kind_label != null ? safe(event.kind_label) : '';
  const facts = Array.isArray(event.facts) ? event.facts : [];
  return '<div class="lg-event ' + tone + (muted ? ' is-muted' : '') + '">' +
    '<div class="lg-rail-cell"></div><div class="lg-event-spacer"></div>' +
    '<div class="lg-event-main">' +
      (kind ? '<span class="lg-event-kind">' + kind + '</span>' : '') +
      safe(event.summary) + '</div>' +
    '<div class="lg-evidence">' + (pairsHtml(facts, 'lg-evidence-pair') || '—') + '</div>' +
    '<div class="lg-event-spacer"></div></div>';
}

function stageHtml(stage, index) {
  const branch = stage.tone === 'warn' || stage.tone === 'bad' ? ' branch' : '';
  const rows = (stage.events || []).map(eventHtml).join('');
  return '<div class="lg-stage ' + stage.tone + branch + '">' +
    '<div class="lg-stage-h">' +
      '<div class="lg-rail-cell"><span class="lg-pin">' + (index + 1) + '</span></div>' +
      '<div class="lg-stage-name"><b>' + safe(stage.label) + '</b><small>' + safe(stage.id) +
      ' · ' + esc(tf('logs.eventCount', {count: stage.count})) + '</small></div>' +
      '<div class="lg-stage-spacer"></div><div class="lg-stage-spacer"></div>' +
      '<div class="lg-stage-count">' + stage.count + '</div>' +
    '</div>' + rows + '</div>';
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
    '</span><span>' + esc(t('logs.computation')) + '</span><span>' + esc(t('logs.evidence')) +
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
  markClippedChains();
}
/* 路径列是摘要而不是滚动区：超出时给右侧一道渐隐，读者不会以为最后一个
   阶段被裁坏了；完整阶段仍在展开区里。窗口变化后要重新判定。 */
function markClippedChains() {
  document.querySelectorAll('#list .lg-chain').forEach(chain => {
    chain.classList.toggle('is-clipped', chain.scrollWidth > chain.clientWidth + 1);
  });
}
function toggle(item) {
  const id = item.dataset.id;
  const opening = !openIds.has(id);
  if (opening) openIds.add(id); else openIds.delete(id);
  item.classList.toggle('open', opening);
  const chevron = item.querySelector('.lg-chev');
  chevron.setAttribute('aria-expanded', String(opening));
  chevron.setAttribute('aria-label', t(opening ? 'logs.collapseRow' : 'logs.expandRow'));
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
  /* 快照按钮自己导航，不能顺带折叠/展开这一行。 */
  if (event.target.closest('.lg-snap-btn')) return;
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
window.addEventListener('resize', markClippedChains);
load();
